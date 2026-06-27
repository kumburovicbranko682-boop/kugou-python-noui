"""NapCat(OneBot v11) 桥接 —— 通过 QQ 机器人用 `#` 前缀指令远程管控酷狗拦截系统。

设计要点（零侵入接入现有 napcat 容器）：
- 作为「第二个 WebSocket 客户端」主动连接 NapCat 的 websocketServers（默认 ws://127.0.0.1:3001）。
  NapCat 的 WS 服务端支持多客户端同时连接，因此不需要改动 napcat 任何配置，
  也不会影响已经在用同一端口的 MaiBot。
- 接收群聊/私聊消息事件，仅处理以 `#` 开头的指令（避免与 MaiBot 等其它机器人冲突）。
- 用同一条 WS 连接回发 OneBot action（send_msg）作为指令回复。
- 全部能力（白名单/黑名单/课堂管控/客户端查询/通知/检查酷狗）都通过指令暴露。

通过环境变量配置：
- KG_NAPCAT_WS_URL    NapCat WS 地址，默认 ws://127.0.0.1:3001
- KG_NAPCAT_TOKEN     连接令牌，默认 123（对应 onebot11 配置里的 websocketServers.token）
- KG_NAPCAT_ADMINS    允许下达指令的 QQ 号，逗号分隔；为空表示放开所有人（仅建议测试）
- KG_NAPCAT_GROUPS    允许响应的群号，逗号分隔；为空表示不限制群
- KG_NAPCAT_ENABLE    设为 0/false 可整体关闭桥接
"""
import os
import json
import time
import threading
import traceback
from datetime import datetime, timedelta

try:
    import websocket  # websocket-client
except Exception:  # pragma: no cover - 缺依赖时优雅降级
    websocket = None


CMD_PREFIX = "#"


def _env_bool(name, default=True):
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


def _env_id_set(name):
    raw = os.environ.get(name, "") or ""
    out = set()
    for part in raw.replace("，", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part:
            out.add(str(part))
    return out


class NapCatBridge:
    """维护与 NapCat 的 WS 长连接，分发 `#` 指令并回复。"""

    def __init__(self, ctx):
        # ctx 为 app 模块（提供 load_config/save_config 等），避免循环导入
        self.ctx = ctx
        self.ws_url = os.environ.get("KG_NAPCAT_WS_URL", "ws://127.0.0.1:3001")
        self.token = os.environ.get("KG_NAPCAT_TOKEN", "123")
        self.admins = _env_id_set("KG_NAPCAT_ADMINS")
        self.groups = _env_id_set("KG_NAPCAT_GROUPS")
        self.enabled = _env_bool("KG_NAPCAT_ENABLE", True)
        self._ws = None
        self._echo_seq = 0
        self._stop = False
        self._connected = False
        self._last_event_ts = 0

    # ---------------------------------------------------------------- 连接管理
    def start(self):
        if not self.enabled:
            print("[NAPCAT] 桥接已被 KG_NAPCAT_ENABLE 关闭，不启动")
            return
        if websocket is None:
            print("[NAPCAT] 未安装 websocket-client，桥接不可用（pip install websocket-client）")
            return
        t = threading.Thread(target=self._run_forever, name="napcat-bridge", daemon=True)
        t.start()
        print(f"[NAPCAT] 桥接线程已启动 -> {self.ws_url} "
              f"(admins={sorted(self.admins) or '不限'}, groups={sorted(self.groups) or '不限'})")

    def _run_forever(self):
        backoff = 2
        while not self._stop:
            try:
                self._connect_once()
                backoff = 2  # 正常退出循环后重置退避
            except Exception as e:
                self._connected = False
                print(f"[NAPCAT] 连接异常: {e}，{backoff}s 后重连")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _connect_once(self):
        headers = [f"Authorization: Bearer {self.token}"] if self.token else None
        url = self.ws_url
        # 同时通过 query 传 token，兼容部分 NapCat 版本只认 access_token
        if self.token and "access_token=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={self.token}"

        ws = websocket.WebSocket()
        ws.connect(url, header=headers, timeout=15)
        self._ws = ws
        self._connected = True
        print(f"[NAPCAT] 已连接 NapCat WS: {self.ws_url}")
        ws.settimeout(60)
        while not self._stop:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            if not raw:
                continue
            self._last_event_ts = time.time()
            try:
                self._on_message(raw)
            except Exception:
                print("[NAPCAT] 处理消息出错:\n" + traceback.format_exc())
        try:
            ws.close()
        except Exception:
            pass
        self._connected = False

    @property
    def status(self):
        return {
            "enabled": self.enabled,
            "connected": self._connected,
            "ws_url": self.ws_url,
            "admins": sorted(self.admins),
            "groups": sorted(self.groups),
            "last_event_ago": (round(time.time() - self._last_event_ts, 1)
                               if self._last_event_ts else None),
        }

    # ---------------------------------------------------------------- 收发消息
    def _on_message(self, raw):
        try:
            data = json.loads(raw)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        # 只关心消息事件
        if data.get("post_type") != "message":
            return
        text = self._extract_text(data).strip()
        if not text.startswith(CMD_PREFIX):
            return

        msg_type = data.get("message_type")  # group / private
        user_id = str(data.get("user_id", ""))
        group_id = str(data.get("group_id", "")) if data.get("group_id") else ""

        # 群限制
        if msg_type == "group" and self.groups and group_id not in self.groups:
            return
        # 权限校验
        if self.admins and user_id not in self.admins:
            self._reply(msg_type, group_id, user_id,
                        "⛔ 无权限：你的 QQ 不在管控管理员名单中。")
            return

        sender_name = (data.get("sender") or {}).get("nickname") or user_id
        print(f"[NAPCAT] 指令来自 {sender_name}({user_id}) "
              f"{'群' + group_id if group_id else '私聊'}: {text}")

        reply = self._dispatch(text)
        if reply:
            self._reply(msg_type, group_id, user_id, reply)

    def _extract_text(self, data):
        """从 OneBot 消息事件提取纯文本（兼容 array / string 两种 messagePostFormat）。"""
        msg = data.get("message")
        if isinstance(msg, list):
            parts = []
            for seg in msg:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    parts.append(str((seg.get("data") or {}).get("text", "")))
            if parts:
                return "".join(parts)
        if isinstance(msg, str) and msg:
            return msg
        return str(data.get("raw_message", "") or "")

    def _reply(self, msg_type, group_id, user_id, text):
        if not self._ws:
            return
        self._echo_seq += 1
        params = {"message": text}
        if msg_type == "group" and group_id:
            params.update({"message_type": "group", "group_id": int(group_id)})
        else:
            params.update({"message_type": "private", "user_id": int(user_id)})
        payload = {"action": "send_msg", "params": params, "echo": f"kg-{self._echo_seq}"}
        try:
            self._ws.send(json.dumps(payload))
        except Exception as e:
            print(f"[NAPCAT] 回复发送失败: {e}")

    # ---------------------------------------------------------------- 指令分发
    def _dispatch(self, text):
        body = text[len(CMD_PREFIX):].strip()
        if not body:
            return self._help()
        # 第一段为指令名，其余为参数
        parts = body.split(None, 1)
        cmd = parts[0].strip().lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        handler = _COMMANDS.get(cmd)
        if not handler:
            # 未知 # 指令一律静默：避免与同群其它以 # 为前缀的机器人/插件
            # （如鹿管 #已起飞 / #本月起飞 / #机长排名 / #航班表）抢命名空间、刷屏。
            # 需要指令列表时主动发 #帮助。
            return None
        try:
            return handler(self, arg)
        except Exception as e:
            print("[NAPCAT] 指令执行出错:\n" + traceback.format_exc())
            return f"⚠️ 执行出错：{e}"

    # ---------------------------------------------------------------- 配置操作
    def _split_items(self, arg):
        """把 '周杰伦,林俊杰 / 周杰伦、林俊杰 / 多行' 解析为去重后的列表。"""
        if not arg:
            return []
        norm = arg.replace("，", ",").replace("、", ",").replace("\n", ",")
        out, seen = [], set()
        for x in norm.split(","):
            x = x.strip()
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    def _list_add(self, key, label, arg):
        items = self._split_items(arg)
        if not items:
            return f"用法：{CMD_PREFIX}加{label} 名称1,名称2"
        cfg = self.ctx.load_config()
        cur = list(cfg.get(key, []))
        seen = set(cur)
        added = []
        for x in items:
            if x not in seen:
                seen.add(x)
                cur.append(x)
                added.append(x)
        cfg[key] = cur
        cfg["updated_at"] = datetime.now().isoformat()
        self.ctx.save_config(cfg)
        return (f"✅ 已添加{label} {len(added)} 项，当前共 {len(cur)} 项。\n"
                f"新增：{'、'.join(added) if added else '（均已存在）'}")

    def _list_remove(self, key, label, arg):
        items = self._split_items(arg)
        if not items:
            return f"用法：{CMD_PREFIX}删{label} 名称1,名称2"
        cfg = self.ctx.load_config()
        cur = list(cfg.get(key, []))
        removed = []
        for x in items:
            if x in cur:
                cur.remove(x)
                removed.append(x)
        cfg[key] = cur
        cfg["updated_at"] = datetime.now().isoformat()
        self.ctx.save_config(cfg)
        return (f"🗑️ 已删除{label} {len(removed)} 项，当前共 {len(cur)} 项。\n"
                f"删除：{'、'.join(removed) if removed else '（未找到匹配项）'}")

    def _list_search(self, key, label, arg):
        q = arg.strip().lower()
        cfg = self.ctx.load_config()
        cur = cfg.get(key, [])
        if not q:
            sample = cur[:20]
            more = f"\n…共 {len(cur)} 项，仅显示前 20 项。" if len(cur) > 20 else ""
            return f"{label}（共 {len(cur)} 项）：\n" + ("、".join(map(str, sample)) or "（空）") + more
        hits = [x for x in cur if q in str(x).lower()]
        show = hits[:30]
        more = f"\n…共匹配 {len(hits)} 项，仅显示前 30 项。" if len(hits) > 30 else ""
        return (f"🔎 {label} 搜索“{arg}”命中 {len(hits)} 项：\n"
                + ("、".join(map(str, show)) or "（无匹配）") + more)

    def _list_clear(self, key, label, arg):
        cfg = self.ctx.load_config()
        n = len(cfg.get(key, []))
        cfg[key] = []
        cfg["updated_at"] = datetime.now().isoformat()
        self.ctx.save_config(cfg)
        return f"🧹 已清空{label}（原 {n} 项）。"

    # ---------------------------------------------------------------- 帮助文本
    def _help(self):
        return (
            "🎵 酷狗拦截 · QQ 远程管控\n"
            "所有指令以 # 开头：\n"
            "— 概览 —\n"
            f"{CMD_PREFIX}帮助  指令列表\n"
            f"{CMD_PREFIX}状态  服务端+客户端概览\n"
            f"{CMD_PREFIX}配置  当前名单计数与管控状态\n"
            f"{CMD_PREFIX}客户端  在线客户端列表\n"
            "— 歌手白名单 —\n"
            f"{CMD_PREFIX}加歌手 周杰伦,林俊杰\n"
            f"{CMD_PREFIX}删歌手 周杰伦   {CMD_PREFIX}查歌手 周   {CMD_PREFIX}清空歌手\n"
            "— 歌曲白名单 —\n"
            f"{CMD_PREFIX}加歌曲 晴天   {CMD_PREFIX}删歌曲 晴天   {CMD_PREFIX}查歌曲 晴   {CMD_PREFIX}清空歌曲\n"
            "— 黑名单关键词 —\n"
            f"{CMD_PREFIX}加黑词 9178   {CMD_PREFIX}删黑词 9178   {CMD_PREFIX}查黑词 91   {CMD_PREFIX}清空黑词\n"
            "— 课堂管控 —\n"
            f"{CMD_PREFIX}管控状态   {CMD_PREFIX}管控开   {CMD_PREFIX}管控关\n"
            f"{CMD_PREFIX}加管控进程 kugou.exe   {CMD_PREFIX}删管控进程 kugou.exe\n"
            f"{CMD_PREFIX}加管控时段 08:00-09:00 1,2,3,4,5\n"
            f"{CMD_PREFIX}清空管控时段\n"
            "— 客户端 —\n"
            f"{CMD_PREFIX}检查酷狗 <客户端ID>   {CMD_PREFIX}通知 <客户端ID> 文本\n"
            f"{CMD_PREFIX}日志 <客户端ID>   {CMD_PREFIX}搜索记录 <客户端ID>"
        )


# ============================================================
# 指令表（函数签名: handler(bridge, arg) -> str）
# ============================================================
def _cmd_help(b, arg):
    return b._help()


def _cmd_status(b, arg):
    cfg = b.ctx.load_config()
    now = datetime.now()
    online = total = kg = 0
    with b.ctx.CLIENTS_LOCK:
        total = len(b.ctx.CLIENTS)
        for c in b.ctx.CLIENTS.values():
            if now - c.get("last_seen", now) < timedelta(seconds=30):
                online += 1
                if c.get("kugou_running"):
                    kg += 1
    cc = cfg.get("class_control", {}) or {}
    return (
        "📊 服务端状态\n"
        f"版本：{cfg.get('version', '1.0.0')}\n"
        f"歌手白名单：{len(cfg.get('singer_whitelist', []))}　"
        f"歌曲白名单：{len(cfg.get('song_whitelist', []))}\n"
        f"黑名单关键词：{len(cfg.get('blacklist_keywords', []))}\n"
        f"课堂管控：{'开启' if cc.get('enabled') else '关闭'}"
        f"（进程 {len(cc.get('processes', []))} · 时段 {len(cc.get('periods', []))}）\n"
        f"客户端：在线 {online}/{total}，酷狗运行中 {kg}\n"
        f"更新时间：{cfg.get('updated_at', '')}"
    )


def _cmd_config(b, arg):
    cfg = b.ctx.load_config()
    cc = cfg.get("class_control", {}) or {}
    lines = [
        "⚙️ 当前配置",
        f"歌手白名单：{len(cfg.get('singer_whitelist', []))} 项",
        f"歌曲白名单：{len(cfg.get('song_whitelist', []))} 项",
        f"黑名单关键词：{len(cfg.get('blacklist_keywords', []))} 项",
        f"课堂管控：{'开启' if cc.get('enabled') else '关闭'}",
        f"  管控进程：{'、'.join(cc.get('processes', [])) or '（无）'}",
    ]
    periods = cc.get("periods", [])
    if periods:
        lines.append("  管控时段：")
        for p in periods:
            days = p.get("days") or []
            dtxt = "每天" if not days else "周" + "".join("一二三四五六日"[d - 1] for d in days)
            lines.append(f"    {p.get('start')}-{p.get('end')} {dtxt}")
    else:
        lines.append("  管控时段：（无）")
    return "\n".join(lines)


def _cmd_clients(b, arg):
    now = datetime.now()
    rows = []
    with b.ctx.CLIENTS_LOCK:
        for c in b.ctx.CLIENTS.values():
            if now - c.get("last_seen", now) < timedelta(seconds=60):
                rows.append(c)
    if not rows:
        return "📭 当前没有在线客户端。"
    out = [f"💻 在线客户端 {len(rows)} 台："]
    for c in rows[:30]:
        out.append(
            f"· {c.get('hostname') or c.get('client_id')} "
            f"[{c.get('client_id')}] 酷狗:{'运行' if c.get('kugou_running') else '停止'}"
        )
    if len(rows) > 30:
        out.append(f"…仅显示前 30 台，共 {len(rows)} 台。")
    return "\n".join(out)


# —— 名单类（歌手 / 歌曲 / 黑词）——
def _cmd_add_singer(b, arg):
    return b._list_add("singer_whitelist", "歌手", arg)


def _cmd_del_singer(b, arg):
    return b._list_remove("singer_whitelist", "歌手", arg)


def _cmd_find_singer(b, arg):
    return b._list_search("singer_whitelist", "歌手白名单", arg)


def _cmd_clear_singer(b, arg):
    return b._list_clear("singer_whitelist", "歌手白名单", arg)


def _cmd_add_song(b, arg):
    return b._list_add("song_whitelist", "歌曲", arg)


def _cmd_del_song(b, arg):
    return b._list_remove("song_whitelist", "歌曲", arg)


def _cmd_find_song(b, arg):
    return b._list_search("song_whitelist", "歌曲白名单", arg)


def _cmd_clear_song(b, arg):
    return b._list_clear("song_whitelist", "歌曲白名单", arg)


def _cmd_add_keyword(b, arg):
    return b._list_add("blacklist_keywords", "黑词", arg)


def _cmd_del_keyword(b, arg):
    return b._list_remove("blacklist_keywords", "黑词", arg)


def _cmd_find_keyword(b, arg):
    return b._list_search("blacklist_keywords", "黑名单关键词", arg)


def _cmd_clear_keyword(b, arg):
    return b._list_clear("blacklist_keywords", "黑名单关键词", arg)


# —— 课堂管控 ——
def _cmd_class_status(b, arg):
    return _cmd_config(b, arg)


def _set_class_enabled(b, enabled):
    cfg = b.ctx.load_config()
    cc = cfg.get("class_control") or b.ctx._default_class_control()
    cc["enabled"] = enabled
    cfg["class_control"] = b.ctx._sanitize_class_control(cc)
    cfg["updated_at"] = datetime.now().isoformat()
    b.ctx.save_config(cfg)
    return f"{'🟢 课堂管控已开启' if enabled else '⚪ 课堂管控已关闭'}"


def _cmd_class_on(b, arg):
    return _set_class_enabled(b, True)


def _cmd_class_off(b, arg):
    return _set_class_enabled(b, False)


def _cmd_add_proc(b, arg):
    procs = b._split_items(arg)
    if not procs:
        return f"用法：{CMD_PREFIX}加管控进程 kugou.exe,game.exe"
    cfg = b.ctx.load_config()
    cc = cfg.get("class_control") or b.ctx._default_class_control()
    cur = list(cc.get("processes", []))
    low = {p.lower() for p in cur}
    added = []
    for p in procs:
        if p.lower() not in low:
            low.add(p.lower())
            cur.append(p)
            added.append(p)
    cc["processes"] = cur
    cfg["class_control"] = b.ctx._sanitize_class_control(cc)
    cfg["updated_at"] = datetime.now().isoformat()
    b.ctx.save_config(cfg)
    return f"✅ 已添加管控进程：{'、'.join(added) or '（均已存在）'}\n当前：{'、'.join(cur)}"


def _cmd_del_proc(b, arg):
    procs = b._split_items(arg)
    if not procs:
        return f"用法：{CMD_PREFIX}删管控进程 kugou.exe"
    cfg = b.ctx.load_config()
    cc = cfg.get("class_control") or b.ctx._default_class_control()
    cur = list(cc.get("processes", []))
    rm = {p.lower() for p in procs}
    removed = [p for p in cur if p.lower() in rm]
    cur = [p for p in cur if p.lower() not in rm]
    cc["processes"] = cur
    cfg["class_control"] = b.ctx._sanitize_class_control(cc)
    cfg["updated_at"] = datetime.now().isoformat()
    b.ctx.save_config(cfg)
    return f"🗑️ 已删除管控进程：{'、'.join(removed) or '（未找到）'}\n当前：{'、'.join(cur) or '（无）'}"


def _cmd_add_period(b, arg):
    """#加管控时段 08:00-09:00 [1,2,3,4,5]"""
    if not arg:
        return f"用法：{CMD_PREFIX}加管控时段 08:00-09:00 1,2,3,4,5（星期可省略=每天）"
    seg = arg.split(None, 1)
    rng = seg[0].replace("～", "-").replace("—", "-")
    if "-" not in rng:
        return "时间段格式应为 起-止，例如 08:00-09:00"
    start_raw, end_raw = rng.split("-", 1)
    start = b.ctx._norm_hhmm(start_raw)
    end = b.ctx._norm_hhmm(end_raw)
    if not start or not end:
        return "时间格式非法，应为 HH:MM，例如 08:00-09:00"
    days = []
    if len(seg) > 1:
        for d in seg[1].replace("，", ",").replace(" ", ",").split(","):
            d = d.strip()
            if d.isdigit() and 1 <= int(d) <= 7:
                days.append(int(d))
    cfg = b.ctx.load_config()
    cc = cfg.get("class_control") or b.ctx._default_class_control()
    periods = list(cc.get("periods", []))
    periods.append({"start": start, "end": end, "days": sorted(set(days))})
    cc["periods"] = periods
    cfg["class_control"] = b.ctx._sanitize_class_control(cc)
    cfg["updated_at"] = datetime.now().isoformat()
    b.ctx.save_config(cfg)
    dtxt = "每天" if not days else "周" + "".join("一二三四五六日"[d - 1] for d in sorted(set(days)))
    return f"✅ 已添加管控时段 {start}-{end} {dtxt}，当前共 {len(periods)} 段。"


def _cmd_clear_periods(b, arg):
    cfg = b.ctx.load_config()
    cc = cfg.get("class_control") or b.ctx._default_class_control()
    n = len(cc.get("periods", []))
    cc["periods"] = []
    cfg["class_control"] = b.ctx._sanitize_class_control(cc)
    cfg["updated_at"] = datetime.now().isoformat()
    b.ctx.save_config(cfg)
    return f"🧹 已清空管控时段（原 {n} 段）。"


# —— 客户端操作 ——
def _cmd_check_kugou(b, arg):
    client_id = arg.strip()
    if not client_id:
        return f"用法：{CMD_PREFIX}检查酷狗 <客户端ID>（用 {CMD_PREFIX}客户端 查看ID）"
    msg = json.dumps({
        "type": "check_kugou",
        "request_id": f"qq_{int(time.time() * 1000)}",
        "timestamp": datetime.now().isoformat(),
    })
    ok = b.ctx.send_immediate_message(client_id, msg)
    if ok:
        return f"📡 已向客户端 {client_id} 下发酷狗进程检查请求。"
    return f"❌ 发送失败：客户端 {client_id} 当前没有 SSE 实时连接。"


def _cmd_notify(b, arg):
    seg = arg.split(None, 1)
    if len(seg) < 2:
        return f"用法：{CMD_PREFIX}通知 <客户端ID> 文本内容"
    client_id, text = seg[0].strip(), seg[1].strip()
    msg = json.dumps({
        "type": "notification",
        "message": text,
        "timestamp": datetime.now().isoformat(),
    })
    ok = b.ctx.send_immediate_message(client_id, msg)
    if ok:
        return f"📨 已向客户端 {client_id} 发送通知。"
    return f"❌ 发送失败：客户端 {client_id} 当前没有 SSE 实时连接。"


def _cmd_logs(b, arg):
    client_id = arg.strip()
    if not client_id:
        return f"用法：{CMD_PREFIX}日志 <客户端ID>"
    with b.ctx.CLIENT_LOGS_LOCK:
        dq = b.ctx.CLIENT_LOGS.get(client_id)
        items = list(dq)[-15:] if dq else []
    if not items:
        return f"📄 客户端 {client_id} 暂无日志记录。"
    out = [f"📄 {client_id} 最近 {len(items)} 条日志："]
    for x in items:
        out.append(f"[{x.get('level', 'INFO')}] {x.get('msg', '')}")
    return "\n".join(out)


def _cmd_searches(b, arg):
    client_id = arg.strip()
    if not client_id:
        return f"用法：{CMD_PREFIX}搜索记录 <客户端ID>"
    with b.ctx.SEARCH_HISTORY_LOCK:
        dq = b.ctx.SEARCH_HISTORY.get(client_id)
        items = list(dq)[-15:] if dq else []
    if not items:
        return f"🔍 客户端 {client_id} 暂无搜索记录。"
    items = items[::-1]
    out = [f"🔍 {client_id} 最近 {len(items)} 条搜索："]
    for s in items:
        act = "拦截" if s.get("action") == "blocked" else "放行"
        out.append(f"[{act}] {s.get('keyword', '')}")
    return "\n".join(out)


_COMMANDS = {
    # 概览
    "帮助": _cmd_help, "help": _cmd_help, "?": _cmd_help, "菜单": _cmd_help,
    "状态": _cmd_status, "status": _cmd_status,
    "配置": _cmd_config, "config": _cmd_config,
    "客户端": _cmd_clients, "clients": _cmd_clients,
    # 歌手
    "加歌手": _cmd_add_singer, "删歌手": _cmd_del_singer,
    "查歌手": _cmd_find_singer, "清空歌手": _cmd_clear_singer,
    # 歌曲
    "加歌曲": _cmd_add_song, "删歌曲": _cmd_del_song,
    "查歌曲": _cmd_find_song, "清空歌曲": _cmd_clear_song,
    # 黑词
    "加黑词": _cmd_add_keyword, "删黑词": _cmd_del_keyword,
    "查黑词": _cmd_find_keyword, "清空黑词": _cmd_clear_keyword,
    # 课堂管控
    "管控状态": _cmd_class_status, "管控开": _cmd_class_on, "管控关": _cmd_class_off,
    "加管控进程": _cmd_add_proc, "删管控进程": _cmd_del_proc,
    "加管控时段": _cmd_add_period, "清空管控时段": _cmd_clear_periods,
    # 客户端
    "检查酷狗": _cmd_check_kugou, "通知": _cmd_notify,
    "日志": _cmd_logs, "搜索记录": _cmd_searches,
}


_bridge_singleton = None


def start_bridge(ctx):
    """由 app.py 在启动时调用，传入 app 模块作为上下文。"""
    global _bridge_singleton
    if _bridge_singleton is not None:
        return _bridge_singleton
    _bridge_singleton = NapCatBridge(ctx)
    _bridge_singleton.start()
    return _bridge_singleton


def get_bridge():
    return _bridge_singleton
