from flask import Flask, jsonify, request, Response
import json
import os
import re
import sys
import platform
import subprocess
import socket
from datetime import datetime, timedelta
import queue
import threading
import time

from collections import deque

app = Flask(__name__)

# 配置文件固定放在 app.py 同级目录，避免从不同工作目录启动时
# 解析出 server/server/kugou_config.json 这类嵌套路径，导致读写到不同文件。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "kugou_config.json")
CLIENTS = {}
CLIENTS_LOCK = threading.Lock()

# 客户端搜索记录（每个客户端一个环形队列）
SEARCH_HISTORY = {}
SEARCH_HISTORY_LOCK = threading.Lock()
SEARCH_HISTORY_MAX = 500

# 客户端运行日志（每个客户端一个环形队列，供实时查看）
CLIENT_LOGS = {}
CLIENT_LOGS_LOCK = threading.Lock()
CLIENT_LOGS_MAX = 3000

# SSE 推送队列（每个客户端一个队列）
SSE_QUEUES = {}
SSE_QUEUES_LOCK = threading.Lock()

# 待处理的 check_kugou 请求（需要客户端响应）
PENDING_CHECKS = {}
PENDING_CHECKS_LOCK = threading.Lock()
CHECK_TIMEOUT = 10  # 检查超时时间（秒）


# ============================================================
# 配置内存缓存 —— 支撑 200w+ 条白名单的高性能读写
# 磁盘只在变更时写一次；读取/分页/搜索全部走内存，避免每次请求都解析大 JSON。
# ============================================================
_CONFIG_CACHE = None
_CONFIG_LOCK = threading.RLock()


def _default_class_control():
    """课堂管控默认配置。
    - enabled: 总开关
    - processes: 管控时段内需要查杀的进程名列表（如 ["kugou.exe", "game.exe"]，不区分大小写）
    - periods: 管控时段列表，每项 {start, end, days}
        start/end 为 "HH:MM" 24 小时制；days 为星期列表（1=周一..7=周日），空列表表示每天生效。
    """
    return {
        "enabled": False,
        "processes": [],
        "periods": [],
    }


def _default_config():
    return {
        "singer_whitelist": [],
        "song_whitelist": [],
        "blacklist_keywords": ["9178", "7891"],
        "class_control": _default_class_control(),
        "version": "1.0.0",
        "updated_at": datetime.now().isoformat()
    }


def _load_config_from_disk():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg.setdefault("singer_whitelist", [])
            cfg.setdefault("song_whitelist", [])
            cfg.setdefault("blacklist_keywords", ["9178", "7891"])
            cc = cfg.get("class_control")
            if not isinstance(cc, dict):
                cc = _default_class_control()
            cc.setdefault("enabled", False)
            cc.setdefault("processes", [])
            cc.setdefault("periods", [])
            cfg["class_control"] = cc
            cfg.setdefault("version", "1.0.0")
            cfg.setdefault("updated_at", datetime.now().isoformat())
            return cfg
        except Exception as e:
            print(f"读取配置失败，使用默认配置: {e}")
    return _default_config()


def _ensure_cache():
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        with _CONFIG_LOCK:
            if _CONFIG_CACHE is None:
                _CONFIG_CACHE = _load_config_from_disk()
    return _CONFIG_CACHE


def load_config():
    """返回配置的浅拷贝（列表为引用，仅供只读/序列化使用）"""
    with _CONFIG_LOCK:
        cfg = _ensure_cache()
        return dict(cfg)


def _write_config_to_disk(cfg):
    os.makedirs(os.path.dirname(CONFIG_FILE) if os.path.dirname(CONFIG_FILE) else ".", exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)


def save_config(config):
    """更新内存缓存 + 落盘 + 通知客户端"""
    global _CONFIG_CACHE
    with _CONFIG_LOCK:
        _CONFIG_CACHE = config
        try:
            _write_config_to_disk(config)
        except Exception as e:
            print(f"保存配置失败: {e}")
    notify_config_update(config)


def notify_config_update(config):
    """通知所有SSE客户端配置已更新"""
    message = json.dumps({
        "type": "config_update",
        "config": config,
        "timestamp": datetime.now().isoformat()
    })

    # 推送到所有连接的SSE客户端
    dead_clients = []
    with SSE_QUEUES_LOCK:
        for client_id, q in list(SSE_QUEUES.items()):
            try:
                q.put_nowait(message)
                print(f"[SSE] 推送配置更新到客户端: {client_id}")
            except:
                dead_clients.append(client_id)

        # 清理断线的客户端
        for client_id in dead_clients:
            SSE_QUEUES.pop(client_id, None)


def send_immediate_message(client_id, message):
    """立即向指定客户端发送消息（不走队列）"""
    with SSE_QUEUES_LOCK:
        if client_id in SSE_QUEUES:
            try:
                SSE_QUEUES[client_id].put_nowait(message)
                return True
            except:
                pass
    return False


@app.route("/")
def index():
    """无 UI 版本根路径：返回服务状态与常用 API 提示。"""
    payload = {
        "status": "online",
        "name": "KuGou Interceptor No-UI Server",
        "api": {
            "status": "/api/status",
            "config": "/api/config",
            "clients": "/api/clients",
            "summary": "/api/config/summary",
        },
    }
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/config", methods=["GET"])
def get_config():
    config = load_config()
    print(f"[CONFIG] 配置被请求 - 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return jsonify(config)


@app.route("/api/config", methods=["POST"])
def update_config():
    config = load_config()
    data = request.json

    if data.get("singer_whitelist") is not None:
        config["singer_whitelist"] = data["singer_whitelist"]
    if data.get("song_whitelist") is not None:
        config["song_whitelist"] = data["song_whitelist"]
    if data.get("blacklist_keywords") is not None:
        config["blacklist_keywords"] = data["blacklist_keywords"]
    if data.get("class_control") is not None:
        config["class_control"] = _sanitize_class_control(data["class_control"])

    config["updated_at"] = datetime.now().isoformat()
    save_config(config)

    return jsonify({"status": "ok", "message": "配置已更新"})


def _sanitize_class_control(raw):
    """规范化课堂管控配置，过滤非法字段，保证下发给客户端的数据干净可靠。"""
    cc = _default_class_control()
    if not isinstance(raw, dict):
        return cc
    cc["enabled"] = bool(raw.get("enabled", False))

    procs = []
    seen = set()
    for p in (raw.get("processes") or []):
        name = str(p).strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            procs.append(name)
    cc["processes"] = procs

    periods = []
    for item in (raw.get("periods") or []):
        if not isinstance(item, dict):
            continue
        start = _norm_hhmm(item.get("start"))
        end = _norm_hhmm(item.get("end"))
        if start is None or end is None:
            continue
        days = []
        for d in (item.get("days") or []):
            try:
                di = int(d)
            except (ValueError, TypeError):
                continue
            if 1 <= di <= 7 and di not in days:
                days.append(di)
        periods.append({"start": start, "end": end, "days": sorted(days)})
    cc["periods"] = periods
    return cc


def _norm_hhmm(value):
    """校验并规范化 "HH:MM" 24 小时制时间字符串，非法返回 None。"""
    if not value:
        return None
    s = str(value).strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mm <= 59:
        return f"{h:02d}:{mm:02d}"
    return None


@app.route("/api/class_control", methods=["GET"])
def get_class_control():
    """获取当前课堂管控配置"""
    with _CONFIG_LOCK:
        cfg = _ensure_cache()
        cc = cfg.get("class_control") or _default_class_control()
    return jsonify({"status": "ok", "class_control": cc})


@app.route("/api/class_control", methods=["POST"])
def set_class_control():
    """更新课堂管控配置并通过现有 SSE/轮询机制下发到所有客户端"""
    data = request.json or {}
    cc = _sanitize_class_control(data.get("class_control", data))
    with _CONFIG_LOCK:
        cfg = _ensure_cache()
        cfg["class_control"] = cc
        cfg["updated_at"] = datetime.now().isoformat()
        save_config(cfg)
    print(f"[CLASS] 课堂管控已更新: 启用={cc['enabled']}, 进程={cc['processes']}, 时段数={len(cc['periods'])}")
    return jsonify({"status": "ok", "message": "课堂管控配置已更新", "class_control": cc})


@app.route("/api/push", methods=["POST"])
def push_update():
    try:
        data = request.json
        config = load_config()

        if "add_singers" in data:
            config["singer_whitelist"].extend(data["add_singers"])
            config["singer_whitelist"] = list(set(config["singer_whitelist"]))

        if "add_songs" in data:
            config["song_whitelist"].extend(data["add_songs"])
            config["song_whitelist"] = list(set(config["song_whitelist"]))

        if "remove_singers" in data:
            for s in data["remove_singers"]:
                if s in config["singer_whitelist"]:
                    config["singer_whitelist"].remove(s)

        if "remove_songs" in data:
            for s in data["remove_songs"]:
                if s in config["song_whitelist"]:
                    config["song_whitelist"].remove(s)

        if "add_keywords" in data:
            config["blacklist_keywords"].extend(data["add_keywords"])
            config["blacklist_keywords"] = list(set(config["blacklist_keywords"]))

        if "remove_keywords" in data:
            for k in data["remove_keywords"]:
                if k in config["blacklist_keywords"]:
                    config["blacklist_keywords"].remove(k)

        config["updated_at"] = datetime.now().isoformat()
        save_config(config)

        return jsonify({"status": "ok", "message": "推送成功", "config": config})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
# 白/黑名单管理（分页查看 / 大文件导入 / 清空）—— 支撑 200w+ 条
# ============================================================
_LIST_KEYS = {
    "singer": "singer_whitelist",
    "song": "song_whitelist",
    "keyword": "blacklist_keywords",
}
_SEQ_PREFIX_RE = re.compile(r"^\s*\d+[\.\、]\s*")


def _parse_lines(text):
    """解析导入文本：按行拆分，去序号前缀/首尾空白/空行/#注释，并去重"""
    out = []
    seen = set()
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        s = _SEQ_PREFIX_RE.sub("", s, count=1).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


@app.route("/api/config/summary", methods=["GET"])
def config_summary():
    """仅返回配置计数与元信息（不返回庞大的列表，供仪表盘轮询）"""
    with _CONFIG_LOCK:
        cfg = _ensure_cache()
        return jsonify({
            "status": "ok",
            "singer_count": len(cfg.get("singer_whitelist", [])),
            "song_count": len(cfg.get("song_whitelist", [])),
            "keyword_count": len(cfg.get("blacklist_keywords", [])),
            "version": cfg.get("version", "1.0.0"),
            "updated_at": cfg.get("updated_at"),
        })


@app.route("/api/list/<ltype>", methods=["GET"])
def get_list(ltype):
    """分页 + 关键字搜索查看某类名单"""
    key = _LIST_KEYS.get(ltype)
    if not key:
        return jsonify({"status": "error", "message": "未知类型"}), 400
    q = (request.args.get("q") or "").strip().lower()
    page = max(1, request.args.get("page", default=1, type=int))
    size = min(1000, max(1, request.args.get("size", default=50, type=int)))
    with _CONFIG_LOCK:
        items = _ensure_cache().get(key, [])
        if q:
            items = [x for x in items if q in str(x).lower()]
        total = len(items)
        start = (page - 1) * size
        page_items = list(items[start:start + size])
    return jsonify({
        "status": "ok", "type": ltype, "total": total,
        "page": page, "size": size, "items": page_items
    })


@app.route("/api/import/<ltype>", methods=["POST"])
def import_list(ltype):
    """大文件批量导入。支持 multipart 文件上传或 raw 文本 body。
    query: mode=append(默认追加去重) | replace(整表替换)
    """
    key = _LIST_KEYS.get(ltype)
    if not key:
        return jsonify({"status": "error", "message": "未知类型"}), 400
    mode = request.args.get("mode", "append")

    content = ""
    if request.files:
        f = next(iter(request.files.values()))
        content = f.read().decode("utf-8", errors="ignore")
    else:
        content = request.get_data(as_text=True) or ""

    new_items = _parse_lines(content)

    with _CONFIG_LOCK:
        cfg = _ensure_cache()
        cur = cfg.get(key, [])
        before = len(cur)
        if mode == "replace":
            merged = new_items
        else:
            seen = set(cur)
            merged = list(cur)
            for x in new_items:
                if x not in seen:
                    seen.add(x)
                    merged.append(x)
        cfg[key] = merged
        cfg["updated_at"] = datetime.now().isoformat()
        save_config(cfg)
        total = len(merged)

    added = total - before if mode != "replace" else total
    print(f"[IMPORT] {ltype} 导入: 收到{len(new_items)}条, 模式={mode}, 现共{total}条")
    return jsonify({
        "status": "ok", "type": ltype, "mode": mode,
        "received": len(new_items), "added": added, "total": total
    })


@app.route("/api/list/<ltype>/clear", methods=["POST"])
def clear_list(ltype):
    """清空某类名单"""
    key = _LIST_KEYS.get(ltype)
    if not key:
        return jsonify({"status": "error", "message": "未知类型"}), 400
    with _CONFIG_LOCK:
        cfg = _ensure_cache()
        cfg[key] = []
        cfg["updated_at"] = datetime.now().isoformat()
        save_config(cfg)
    return jsonify({"status": "ok", "type": ltype, "total": 0})


@app.route("/api/status", methods=["GET"])
def status():
    config = load_config()
    now = datetime.now()
    online_clients = [c for c in CLIENTS.values() if now - c["last_seen"] < timedelta(seconds=30)]
    return jsonify({
        "status": "online",
        "version": config.get("version", "1.0.0"),
        "singer_count": len(config.get("singer_whitelist", [])),
        "song_count": len(config.get("song_whitelist", [])),
        "updated_at": config.get("updated_at"),
        "clients_online": len(online_clients)
    })


@app.route("/api/report", methods=["POST"])
def receive_report():
    """接收客户端实时状态报告"""
    data = request.json
    client_id = data.get("client_id")
    hostname = data.get("hostname", client_id)
    status_data = data.get("status", {})
    hardware_data = data.get("hardware", {})

    with CLIENTS_LOCK:
        # 获取现有的客户端信息
        existing = CLIENTS.get(client_id, {})
        CLIENTS[client_id] = {
            "client_id": client_id,
            "hostname": hostname,
            "last_seen": datetime.now(),
            "last_report": datetime.now(),
            "uptime": data.get("uptime", 0),
            "kugou_running": status_data.get("kugou_running", False),
            "sse_connected": status_data.get("sse_connected", False),
            "mitmproxy_running": status_data.get("mitmproxy_running", True),
            "memory_usage": status_data.get("memory_usage", 0),
            "cpu_usage": status_data.get("cpu_usage", 0),
            "filter_stats": status_data.get("filter_stats", {}),
            "config_version": status_data.get("config_version", 0),
            "config_synced": status_data.get("config_synced", True),
            "last_config_update": status_data.get("last_config_update", 0),
            # 客户端本地配置信息（服务端看不到的）
            "local_singer_count": status_data.get("local_singer_count", 0),
            "local_song_count": status_data.get("local_song_count", 0),
            "local_blacklist_count": status_data.get("local_blacklist_count", 0),
            "config_hash": status_data.get("config_hash", ""),
            "config_match": status_data.get("config_match", True),
            "has_sse": client_id in SSE_QUEUES,
            # 硬件信息
            "cpu_model": hardware_data.get("cpu_model", ""),
            "cpu_cores": hardware_data.get("cpu_cores", 0),
            "total_memory_gb": hardware_data.get("total_memory_gb", 0),
            "gpu_model": hardware_data.get("gpu_model", ""),
            "gpu_list": hardware_data.get("gpu_list", []),
            "ip_address": hardware_data.get("ip_address", ""),
            "os_info": hardware_data.get("os_info", ""),
        }

    # 接收客户端上报的运行日志并归档（供实时查看）
    logs = data.get("logs") or []
    if logs:
        with CLIENT_LOGS_LOCK:
            dq = CLIENT_LOGS.setdefault(client_id, deque(maxlen=CLIENT_LOGS_MAX))
            for entry in logs:
                if isinstance(entry, dict) and entry.get("msg"):
                    dq.append({
                        "time": entry.get("time") or datetime.now().isoformat(),
                        "level": entry.get("level", "INFO"),
                        "msg": str(entry.get("msg"))
                    })

    # 接收客户端上报的搜索记录并归档
    searches = data.get("searches") or []
    if searches:
        with SEARCH_HISTORY_LOCK:
            dq = SEARCH_HISTORY.setdefault(client_id, deque(maxlen=SEARCH_HISTORY_MAX))
            for s in searches:
                if not isinstance(s, dict):
                    continue
                kw = (s.get("keyword") or "").strip()
                if not kw:
                    continue
                dq.append({
                    "keyword": kw,
                    "action": s.get("action", "blocked"),
                    "time": s.get("time") or datetime.now().isoformat()
                })

    # 获取客户端IP地址
    client_ip = request.remote_addr
    kugou_status = "运行中" if status_data.get("kugou_running") else "未运行"

    print(f"[REPORT] {hostname} ({client_ip}): "
          f"酷狗={kugou_status}, "
          f"内存={status_data.get('memory_usage', 0):.1f}%, "
          f"CPU={status_data.get('cpu_usage', 0):.1f}%, "
          f"歌手={status_data.get('local_singer_count', 0)}, "
          f"歌曲={status_data.get('local_song_count', 0)}, "
          f"黑词={status_data.get('local_blacklist_count', 0)}, "
          f"配置同步={'是' if status_data.get('config_match', True) else '否'}")

    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


@app.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    data = request.json
    client_id = data.get("client_id")
    hostname = data.get("hostname", client_id)
    kugou_running = data.get("kugou_running", False)
    client_status = data.get("status", {})  # 新增：客户端详细状态

    with CLIENTS_LOCK:
        # 合并而非覆盖，保留 /api/report 上报的本地配置字段
        existing = CLIENTS.get(client_id, {})
        existing.update({
            "client_id": client_id,
            "hostname": hostname,
            "last_seen": datetime.now(),
            "kugou_running": kugou_running,
            "status": client_status
        })
        CLIENTS[client_id] = existing

    print(f"[HEARTBEAT] 收到心跳 - 客户端ID: {client_id}, 主机名: {hostname}, 酷狗运行: {kugou_running}, 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 检查是否有待处理的 check_kugou 请求需要该客户端响应
    request_id = data.get("request_id")
    if request_id:
        with PENDING_CHECKS_LOCK:
            if request_id in PENDING_CHECKS:
                check_info = PENDING_CHECKS[request_id]
                if not check_info.get("responded"):
                    check_info["responded"] = True
                    check_info["response_time"] = datetime.now().isoformat()
                    check_info["kugou_running"] = kugou_running
                    print(f"[CHECK] 收到客户端 {client_id} 的检查响应 (request_id: {request_id}): 酷狗运行={kugou_running}")

    return jsonify({"status": "ok"})


@app.route("/api/clients", methods=["GET"])
def get_clients():
    now = datetime.now()
    online_clients = []
    with CLIENTS_LOCK:
        for client in CLIENTS.values():
            if now - client["last_seen"] < timedelta(seconds=30):
                client_id = client["client_id"]
                with SSE_QUEUES_LOCK:
                    has_sse_queue = client_id in SSE_QUEUES
                # 综合判定：服务端实时队列存在，或客户端自报 SSE 已连接。
                # 解决重连瞬间/旧连接 finally 误删队列导致的「明明连着却显示仅心跳」。
                client_reported_sse = bool(client.get("sse_connected", False))
                has_sse = has_sse_queue or client_reported_sse
                online_clients.append({
                    "client_id": client_id,
                    "hostname": client["hostname"],
                    "last_seen": client["last_seen"].isoformat(),
                    "kugou_running": client.get("kugou_running", False),
                    "sse_connected": has_sse,
                    "has_sse": has_sse,
                    "memory_usage": client.get("memory_usage", 0),
                    "cpu_usage": client.get("cpu_usage", 0),
                    "filter_stats": client.get("filter_stats", {}),
                    "local_singer_count": client.get("local_singer_count", 0),
                    "local_song_count": client.get("local_song_count", 0),
                    "local_blacklist_count": client.get("local_blacklist_count", 0),
                    "config_hash": client.get("config_hash", ""),
                    "config_match": client.get("config_match", True),
                    "uptime": client.get("uptime", 0),
                    "status": client.get("status", {}),
                    # 硬件信息
                    "cpu_model": client.get("cpu_model", ""),
                    "cpu_cores": client.get("cpu_cores", 0),
                    "total_memory_gb": client.get("total_memory_gb", 0),
                    "gpu_model": client.get("gpu_model", ""),
                    "gpu_list": client.get("gpu_list", []),
                    "ip_address": client.get("ip_address", ""),
                    "os_info": client.get("os_info", ""),
                    "config_version": client.get("config_version", 0),
                    "last_config_update": client.get("last_config_update", 0),
                })
    return jsonify({"clients": online_clients})


@app.route("/api/heartbeat/trigger", methods=["POST"])
def trigger_heartbeat():
    """手动触发心跳，用于测试"""
    data = request.json
    client_id = data.get("client_id", "manual-trigger")
    hostname = data.get("hostname", client_id)
    kugou_running = data.get("kugou_running", False)

    with CLIENTS_LOCK:
        CLIENTS[client_id] = {
            "client_id": client_id,
            "hostname": hostname,
            "last_seen": datetime.now(),
            "kugou_running": kugou_running
        }

    return jsonify({
        "status": "ok",
        "message": "手动心跳已记录",
        "client": {
            "client_id": client_id,
            "hostname": hostname,
            "last_seen": datetime.now().isoformat(),
            "kugou_running": kugou_running
        }
    })


@app.route("/api/sse/stream")
def sse_stream():
    """SSE流端点，用于实时推送配置更新"""
    client_id = request.args.get("client_id", "unknown")
    hostname = request.args.get("hostname", client_id)

    def event_stream():
        # 创建客户端专属队列（使用 PriorityQueue 支持优先级）
        q = queue.Queue()
        with SSE_QUEUES_LOCK:
            SSE_QUEUES[client_id] = q

        # 更新客户端状态（表示客户端在线）
        # 注意：必须合并而非覆盖，否则会抹掉 /api/report 上报的
        # local_singer_count / local_song_count 等热重载校验字段，
        # 导致 SSE 连接/重连后这些字段丢失，热重载验证失败。
        with CLIENTS_LOCK:
            existing = CLIENTS.get(client_id, {})
            existing.update({
                "client_id": client_id,
                "hostname": hostname,
                "last_seen": datetime.now(),
                "has_sse": True
            })
            existing.setdefault("kugou_running", False)
            CLIENTS[client_id] = existing

        print(f"[SSE] 客户端连接: {client_id} ({hostname})")

        try:
            # 首次连接时发送当前配置和确认消息
            config = load_config()
            yield f"data: {json.dumps({'type': 'init', 'config': config, 'timestamp': datetime.now().isoformat()})}\n\n"
            yield f"data: {json.dumps({'type': 'connected', 'message': 'SSE连接成功', 'client_id': client_id, 'timestamp': datetime.now().isoformat()})}\n\n"

            last_heartbeat = time.time()
            heartbeat_interval = 3  # 每3秒发送一次心跳（缩短延迟）

            # 持续监听队列
            while True:
                try:
                    # 等待新消息，最多等待 heartbeat_interval 秒
                    elapsed = time.time() - last_heartbeat
                    timeout = max(0.5, heartbeat_interval - elapsed)
                    message = q.get(timeout=min(timeout, 3))

                    # 更新客户端最后活跃时间
                    with CLIENTS_LOCK:
                        if client_id in CLIENTS:
                            CLIENTS[client_id]["last_seen"] = datetime.now()

                    yield f"data: {message}\n\n"
                    last_heartbeat = time.time()

                except queue.Empty:
                    # 超时后发送心跳保持连接（缩短到3秒）
                    yield f"data: {json.dumps({'type': 'ping', 'timestamp': datetime.now().isoformat()})}\n\n"
                    last_heartbeat = time.time()

        except GeneratorExit:
            print(f"[SSE] 客户端断开: {client_id}")
        finally:
            # 清理队列 —— 仅当当前队列仍是自己注册的那个时才移除，
            # 否则重连时会把新连接刚注册的队列误删，导致 has_sse 抖动。
            removed = False
            with SSE_QUEUES_LOCK:
                if SSE_QUEUES.get(client_id) is q:
                    SSE_QUEUES.pop(client_id, None)
                    removed = True
            # 只有真正没有活跃队列了才标记 SSE 断开
            if removed:
                with CLIENTS_LOCK:
                    if client_id in CLIENTS:
                        CLIENTS[client_id]["has_sse"] = False

    return Response(event_stream(), mimetype='text/event-stream')


@app.route("/api/check_kugou", methods=["POST"])
def check_kugou():
    """服务端主动触发检查客户端酷狗进程 - 快速响应版本"""
    data = request.json
    target_client = data.get("client_id")
    request_id = data.get("request_id", f"req_{int(time.time() * 1000)}")

    if not target_client:
        return jsonify({"status": "error", "message": "缺少client_id"}), 400

    # 检查客户端是否有SSE连接
    with SSE_QUEUES_LOCK:
        has_sse = target_client in SSE_QUEUES

    with CLIENTS_LOCK:
        client_info = CLIENTS.get(target_client)

    if not has_sse:
        if not client_info:
            return jsonify({"status": "error", "message": "客户端不在线", "client_id": target_client}), 404
        last_seen = client_info.get("last_seen", datetime.now() - timedelta(seconds=60))
        if datetime.now() - last_seen > timedelta(seconds=30):
            return jsonify({"status": "error", "message": "客户端已离线", "client_id": target_client}), 404

    # 创建待处理的检查请求
    check_info = {
        "request_id": request_id,
        "target_client": target_client,
        "created_at": datetime.now().isoformat(),
        "responded": False,
        "kugou_running": None
    }

    with PENDING_CHECKS_LOCK:
        PENDING_CHECKS[request_id] = check_info

    # 通过SSE立即推送检查请求（高优先级）
    message = json.dumps({
        "type": "check_kugou",
        "request_id": request_id,  # 添加请求ID用于追踪
        "timestamp": datetime.now().isoformat()
    })

    # 立即发送，不等待队列
    sent = send_immediate_message(target_client, message)

    if sent:
        print(f"[SSE] 立即发送酷狗进程检查请求: {target_client} (request_id: {request_id})")

        # 等待响应（最多等待 CHECK_TIMEOUT 秒）
        start_time = time.time()
        while time.time() - start_time < CHECK_TIMEOUT:
            time.sleep(0.1)  # 短暂休眠后检查
            with PENDING_CHECKS_LOCK:
                if request_id in PENDING_CHECKS and PENDING_CHECKS[request_id].get("responded"):
                    check_result = PENDING_CHECKS[request_id]
                    # 清理
                    PENDING_CHECKS.pop(request_id, None)
                    return jsonify({
                        "status": "ok",
                        "message": f"客户端 {target_client} 的检查结果",
                        "request_id": request_id,
                        "kugou_running": check_result.get("kugou_running", False),
                        "response_time": check_result.get("response_time"),
                        "latency_ms": int((datetime.fromisoformat(check_result["response_time"]) - datetime.fromisoformat(check_info["created_at"])).total_seconds() * 1000) if check_result.get("response_time") else None
                    })

        # 超时
        PENDING_CHECKS.pop(request_id, None)
        return jsonify({
            "status": "timeout",
            "message": f"等待客户端 {target_client} 响应超时",
            "request_id": request_id,
            "timeout_seconds": CHECK_TIMEOUT
        }), 408
    else:
        PENDING_CHECKS.pop(request_id, None)
        return jsonify({"status": "error", "message": "无法发送检查请求，客户端可能已断开"}), 500


@app.route("/api/check_kugou/async", methods=["POST"])
def check_kugou_async():
    """异步版本：服务端主动触发检查客户端酷狗进程（不等待响应）"""
    data = request.json
    target_client = data.get("client_id")
    request_id = f"req_{int(time.time() * 1000)}"

    if not target_client:
        return jsonify({"status": "error", "message": "缺少client_id"}), 400

    with SSE_QUEUES_LOCK:
        has_sse = target_client in SSE_QUEUES

    if not has_sse:
        with CLIENTS_LOCK:
            client_info = CLIENTS.get(target_client)
        if not client_info:
            return jsonify({"status": "error", "message": "客户端不在线", "client_id": target_client}), 404

    # 立即发送检查请求
    message = json.dumps({
        "type": "check_kugou",
        "request_id": request_id,
        "timestamp": datetime.now().isoformat()
    })

    sent = send_immediate_message(target_client, message)

    if sent:
        return jsonify({
            "status": "ok",
            "message": f"已向客户端 {target_client} 发送检查请求",
            "request_id": request_id
        })
    else:
        return jsonify({"status": "error", "message": "发送失败，客户端可能已断开"}), 500


@app.route("/api/clients/clear", methods=["POST"])
def clear_offline_clients():
    """清理离线客户端"""
    now = datetime.now()
    removed = []
    with CLIENTS_LOCK:
        for client_id in list(CLIENTS.keys()):
            if now - CLIENTS[client_id]["last_seen"] >= timedelta(seconds=60):
                removed.append(client_id)
                del CLIENTS[client_id]

    print(f"[CLEANUP] 清理离线客户端: {len(removed)} 个")
    return jsonify({"status": "ok", "message": f"已清理 {len(removed)} 个离线客户端", "removed": removed})


@app.route("/api/clients/<client_id>", methods=["GET"])
def get_client_detail(client_id):
    """获取单个客户端详细信息"""
    with CLIENTS_LOCK:
        client = CLIENTS.get(client_id)
        if not client:
            return jsonify({"status": "error", "message": "客户端不存在"}), 404

        # 获取SSE连接状态
        with SSE_QUEUES_LOCK:
            has_sse = client_id in SSE_QUEUES

        return jsonify({
            "status": "ok",
            "client": {
                **client,
                "last_seen": client["last_seen"].isoformat() if isinstance(client.get("last_seen"), datetime) else client.get("last_seen"),
                "last_report": client["last_report"].isoformat() if isinstance(client.get("last_report"), datetime) else client.get("last_report"),
                "has_sse": has_sse,
                "online_seconds": (datetime.now() - client.get("last_seen", datetime.now())).total_seconds()
            }
        })


@app.route("/api/clients/<client_id>/config", methods=["GET"])
def get_client_config_info(client_id):
    """获取客户端配置信息（服务端存储的）"""
    with CLIENTS_LOCK:
        client = CLIENTS.get(client_id)
        if not client:
            return jsonify({"status": "error", "message": "客户端不存在"}), 404

        config = load_config()
        return jsonify({
            "status": "ok",
            "client": {
                "client_id": client_id,
                "hostname": client.get("hostname", ""),
                "local_singer_count": client.get("local_singer_count", 0),
                "local_song_count": client.get("local_song_count", 0),
                "local_blacklist_count": client.get("local_blacklist_count", 0),
                "config_hash": client.get("config_hash", ""),
                "config_match": client.get("config_match", True),
                "last_seen": client["last_seen"].isoformat() if isinstance(client.get("last_seen"), datetime) else client.get("last_seen"),
            },
            "server_config": {
                "singer_whitelist_count": len(config.get("singer_whitelist", [])),
                "song_whitelist_count": len(config.get("song_whitelist", [])),
                "blacklist_keywords_count": len(config.get("blacklist_keywords", [])),
                "updated_at": config.get("updated_at"),
            }
        })


@app.route("/api/clients/<client_id>/search_history", methods=["GET"])
def get_client_search_history(client_id):
    """获取指定客户端的搜索记录（最新在前）"""
    limit = request.args.get("limit", type=int)
    with SEARCH_HISTORY_LOCK:
        dq = SEARCH_HISTORY.get(client_id)
        items = list(dq) if dq else []
    items = items[::-1]
    if limit and limit > 0:
        items = items[:limit]
    blocked = sum(1 for s in items if s.get("action") == "blocked")
    allowed = sum(1 for s in items if s.get("action") == "allowed")
    return jsonify({
        "status": "ok",
        "client_id": client_id,
        "count": len(items),
        "blocked": blocked,
        "allowed": allowed,
        "searches": items
    })


@app.route("/api/clients/<client_id>/search_history", methods=["DELETE"])
def clear_client_search_history(client_id):
    """清空指定客户端的搜索记录"""
    with SEARCH_HISTORY_LOCK:
        SEARCH_HISTORY.pop(client_id, None)
    return jsonify({"status": "ok", "message": "搜索记录已清空"})


@app.route("/api/clients/<client_id>/logs", methods=["GET"])
def get_client_logs(client_id):
    """获取指定客户端的运行日志（最新在前），供实时查看

    支持 ?limit=N 限制条数，?since=<ISO时间> 仅取该时间之后的新日志（用于增量轮询）。
    """
    limit = request.args.get("limit", type=int)
    since = request.args.get("since", type=str)
    with CLIENT_LOGS_LOCK:
        dq = CLIENT_LOGS.get(client_id)
        items = list(dq) if dq else []
    if since:
        items = [x for x in items if (x.get("time") or "") > since]
    total = len(items)
    latest_time = items[-1]["time"] if items else since
    items = items[::-1]
    if limit and limit > 0:
        items = items[:limit]
    return jsonify({
        "status": "ok",
        "client_id": client_id,
        "count": total,
        "latest_time": latest_time,
        "logs": items
    })


@app.route("/api/clients/<client_id>/logs", methods=["DELETE"])
def clear_client_logs(client_id):
    """清空指定客户端的运行日志"""
    with CLIENT_LOGS_LOCK:
        CLIENT_LOGS.pop(client_id, None)
    return jsonify({"status": "ok", "message": "客户端日志已清空"})


@app.route("/api/clients/<client_id>/notify", methods=["POST"])
def notify_client(client_id):
    """向指定客户端发送通知"""
    data = request.json
    message = data.get("message", "")
    msg_type = data.get("type", "notification")

    with SSE_QUEUES_LOCK:
        if client_id not in SSE_QUEUES:
            return jsonify({"status": "error", "message": "客户端SSE未连接"}), 404

        notification = json.dumps({
            "type": msg_type,
            "message": message,
            "timestamp": datetime.now().isoformat()
        })
        try:
            SSE_QUEUES[client_id].put_nowait(notification)
            return jsonify({"status": "ok", "message": "通知已发送"})
        except:
            return jsonify({"status": "error", "message": "发送失败"}), 500


@app.route("/api/dashboard/stats", methods=["GET"])
def get_dashboard_stats():
    """获取仪表盘统计数据（SSE推送用）"""
    config = load_config()
    now = datetime.now()

    stats = {
        "config": {
            "singer_count": len(config.get("singer_whitelist", [])),
            "song_count": len(config.get("song_whitelist", [])),
            "keyword_count": len(config.get("blacklist_keywords", [])),
            "updated_at": config.get("updated_at")
        },
        "clients": {
            "total": 0,
            "online": 0,
            "kugou_running": 0,
            "sse_connected": 0
        },
        "server": {
            "status": "online",
            "uptime": (now - datetime(2024, 1, 1)).total_seconds(),  # 简化计算
            "timestamp": now.isoformat()
        }
    }

    with CLIENTS_LOCK:
        stats["clients"]["total"] = len(CLIENTS)
        for client in CLIENTS.values():
            if now - client.get("last_seen", datetime.now()) < timedelta(seconds=30):
                stats["clients"]["online"] += 1
                if client.get("kugou_running"):
                    stats["clients"]["kugou_running"] += 1

    with SSE_QUEUES_LOCK:
        stats["clients"]["sse_connected"] = len(SSE_QUEUES)

    return jsonify(stats)


# 定期清理过期检查请求的线程
def cleanup_expired_checks():
    """定期清理过期的检查请求"""
    while True:
        time.sleep(5)
        with PENDING_CHECKS_LOCK:
            now = datetime.now()
            expired = []
            for request_id, check_info in PENDING_CHECKS.items():
                created = datetime.fromisoformat(check_info["created_at"])
                if (now - created).total_seconds() > CHECK_TIMEOUT * 2:
                    expired.append(request_id)
            for request_id in expired:
                PENDING_CHECKS.pop(request_id, None)
            if expired:
                print(f"[CLEANUP] 清理过期检查请求: {len(expired)} 个")


# 启动清理线程
cleanup_thread = threading.Thread(target=cleanup_expired_checks, daemon=True)
cleanup_thread.start()


# ============================================================
# NapCat(OneBot v11) QQ 远程管控桥接
# 作为第二个 WS 客户端接入现有 napcat 容器（默认 ws://127.0.0.1:3001），
# 用 `#` 前缀指令远程管控白/黑名单、课堂管控与客户端，不影响 MaiBot。
# ============================================================
try:
    import napcat_bridge
    _NAPCAT_BRIDGE = napcat_bridge.start_bridge(sys.modules[__name__])
except Exception as _e:
    _NAPCAT_BRIDGE = None
    print(f"[NAPCAT] 桥接启动失败（不影响主服务）: {_e}")


@app.route("/api/napcat/status", methods=["GET"])
def napcat_status():
    """查看 QQ(NapCat) 桥接连接状态。"""
    if _NAPCAT_BRIDGE is None:
        return jsonify({"status": "ok", "napcat": {"enabled": False, "connected": False,
                                                   "reason": "bridge not started"}})
    return jsonify({"status": "ok", "napcat": _NAPCAT_BRIDGE.status})


# ============================================================
# 端口占用自检（与客户端 start.py 一致的机制）
# Werkzeug 开发服务器默认 allow_reuse_address，在 Windows 下会让多个
# `python app.py` 同时“绑定成功”，浏览器随机命中旧进程，导致更新后仍看到旧界面。
# 启动前主动检测并清理占用本端口的残留进程，确保只有当前实例在跑、只发最新页面。
# ============================================================
def _get_pids_on_port(port):
    """通过 netstat 找出所有 LISTENING 在指定端口的进程 PID（排除自身）"""
    pids = set()
    try:
        result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, shell=True)
        for line in (result.stdout or "").splitlines():
            if f":{port} " in line and "LISTENING" in line.upper():
                parts = line.split()
                if parts and parts[-1].isdigit():
                    pids.add(int(parts[-1]))
    except Exception as e:
        print(f"[PORT] 查询端口占用失败: {e}")
    pids.discard(os.getpid())
    pids.discard(0)
    return pids


def _kill_pid(pid):
    """强制终止指定 PID"""
    try:
        r = subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, text=True, shell=True)
        return r.returncode == 0
    except Exception as e:
        print(f"[PORT] 终止进程 {pid} 失败: {e}")
        return False


def free_port_if_occupied(port, auto_kill=True):
    """检测端口占用，自动清理残留进程。返回端口是否最终空闲。"""
    if os.name != "nt":
        # 仅在 Windows 上做自动清理；其余平台交由操作系统报错，避免误杀
        return True
    pids = _get_pids_on_port(port)
    if not pids:
        print(f"[PORT] 端口 {port} 空闲，正常启动")
        return True
    print(f"[PORT] 检测到端口 {port} 已被占用，残留进程 PID: {sorted(pids)}，准备清理")
    if not auto_kill:
        return False
    for pid in pids:
        if _kill_pid(pid):
            print(f"[PORT] 已终止残留进程 {pid}")
        else:
            print(f"[PORT] 无法终止进程 {pid}（可能需要管理员权限）")
    time.sleep(1)
    left = _get_pids_on_port(port)
    if left:
        print(f"[PORT] 警告：端口 {port} 仍被占用 {sorted(left)}，新实例可能与旧实例冲突")
        return False
    print(f"[PORT] 端口 {port} 已释放，准备启动新实例")
    return True


if __name__ == "__main__":
    PORT = int(os.environ.get("KG_SERVER_PORT", "5000"))
    free_port_if_occupied(PORT)
    print(f"[START] 控制台服务启动中，端口 {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)