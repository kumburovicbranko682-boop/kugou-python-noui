from mitmproxy import http, ctx
import json
import os
import datetime
import time
import re
import threading
import logging
from collections import deque

# ==============================================
# 从配置文件导入所有配置
# ==============================================
from kugou_config import (
    SINGER_WHITELIST_FILE,
    SONG_WHITELIST_FILE,
    KUGOU_DOMAINS,
    KUGOU_IP_PATTERNS,
    COMPILED_SEARCH_PATTERNS,
    STATIC_EXTENSIONS,
    COMPILED_BLOCK_PATTERNS,
    COMPILED_NAV_BLOCK_PATTERNS,
    EMPTY_RESPONSE,
    ALLOW_AUDIO_EXTENSIONS,
    EXCLUDE_HOSTS
)

# ==============================================
# 搜索记录（供服务端查看，由 cloud_updater 周期性上报后清空）
# ==============================================
SEARCH_LOG = deque(maxlen=1000)
SEARCH_LOG_LOCK = threading.Lock()


def record_search(keyword, action):
    """记录一次搜索关键词及处理结果

    action: "allowed"（白名单放行） / "blocked"（被拦截）
    """
    if not keyword:
        return
    try:
        with SEARCH_LOG_LOCK:
            SEARCH_LOG.append({
                "keyword": str(keyword).strip(),
                "action": action,
                "time": datetime.datetime.now().isoformat()
            })
    except Exception:
        pass


def drain_search_log(max_items=200):
    """取出并清空待上报的搜索记录（cloud_updater 调用）"""
    items = []
    try:
        with SEARCH_LOG_LOCK:
            while SEARCH_LOG and len(items) < max_items:
                items.append(SEARCH_LOG.popleft())
    except Exception:
        pass
    return items


# ==============================================
# 运行日志环形缓冲（供服务端实时查看客户端日志）
# 捕获 mitmproxy/过滤器输出，周期性由 cloud_updater 上报到服务端
# ==============================================
LOG_BUFFER = deque(maxlen=1500)
LOG_BUFFER_LOCK = threading.Lock()
_LOG_HANDLER_INSTALLED = False


class _BufferLogHandler(logging.Handler):
    """把日志记录写入内存环形缓冲，绝不抛异常影响代理主流程"""

    def emit(self, record):
        try:
            msg = record.getMessage()
            with LOG_BUFFER_LOCK:
                LOG_BUFFER.append({
                    "time": datetime.datetime.now().isoformat(),
                    "level": record.levelname,
                    "msg": msg,
                })
        except Exception:
            pass


def install_log_capture():
    """在根 logger 上安装缓冲处理器，捕获 mitmproxy 与过滤器日志（幂等）"""
    global _LOG_HANDLER_INSTALLED
    if _LOG_HANDLER_INSTALLED:
        return
    try:
        root = logging.getLogger()
        if not any(isinstance(h, _BufferLogHandler) for h in root.handlers):
            handler = _BufferLogHandler()
            handler.setLevel(logging.INFO)
            root.addHandler(handler)
        # 确保 INFO 级别日志不会被根 logger 级别过滤掉
        if root.level > logging.INFO or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)
        _LOG_HANDLER_INSTALLED = True
    except Exception:
        pass


def drain_log_buffer(max_items=300):
    """取出并清空待上报的运行日志（cloud_updater 调用）"""
    items = []
    try:
        with LOG_BUFFER_LOCK:
            while LOG_BUFFER and len(items) < max_items:
                items.append(LOG_BUFFER.popleft())
    except Exception:
        pass
    return items

# ==============================================
# 云更新支持
# ==============================================
CLOUD_UPDATE_ENABLED = os.environ.get("KG_CLOUD_UPDATE", "false").lower() == "true"
CLOUD_SERVER_URL = os.environ.get("KG_CLOUD_SERVER", "http://127.0.0.1:5000")

# ==============================================
# 全局变量（支持热更新）
# ==============================================
SINGER_WHITELIST = set()
SONG_WHITELIST = set()
# 歌名级索引：白名单条目多为「歌手 - 歌名」格式，但酷狗搜索常只传裸歌名（如「小城夏天」），
# 直接全量匹配会漏放行。这里额外抽取每条的歌名部分建立索引，搜索裸歌名也能命中放行。
SONG_TITLE_WHITELIST = set()

# 专门的黑名单关键词（必须拦截）+ 额外拦截规则
BLACKLIST_KEYWORDS = {"9178", "7891"}

# 最后加载时间
LAST_LOAD_TIME = 0
LOAD_INTERVAL = 60  # 60秒重新加载一次

# 仅匹配“开头的数字序号 + 点 + 空白”，例如 "1. " / "10. " / "1012. "
# 不会误伤名字内部的点号（如 "G.E.M. 邓紫棋" / "Mr."），因为序号必须是行首纯数字
_SEQ_PREFIX_RE = re.compile(r"^\s*\d+\.\s+")


def normalize_name(name):
    """统一白名单条目格式：剥离行首“数字序号. ”前缀并去掉首尾空白。

    本地文件与云端下发两条加载路径都必须经过此函数，保证写入集合的 key
    与酷狗实际搜索关键词的口径完全一致，避免“配置已生效但仍被拦截”。
    """
    if not name:
        return ""
    s = str(name).strip()
    s = _SEQ_PREFIX_RE.sub("", s, count=1)
    return s.strip()


# 歌名级索引的最短长度阈值：太短（1 字）的歌名过于宽泛，不进索引，避免误放行
_MIN_TITLE_LEN = 2


def extract_song_title(entry):
    """从白名单条目中抽取「歌名」部分。

    条目通常是「歌手 - 歌名」，也可能是「组合 - 成员 - 歌名」，取最后一段为歌名。
    分隔符固定为带空格的 ' - '，避免误切「小城夏天-幸福据点」这类歌名内部的连字符。
    """
    if not entry:
        return ""
    parts = str(entry).split(" - ")
    title = parts[-1].strip() if len(parts) > 1 else ""
    return title


def rebuild_song_title_index():
    """根据当前 SONG_WHITELIST 重建歌名级索引。

    本地加载与云端热更新两条路径都应调用本函数，保证索引与歌曲白名单同步。
    """
    global SONG_TITLE_WHITELIST
    titles = set()
    for entry in SONG_WHITELIST:
        title = extract_song_title(entry)
        if title and len(title) >= _MIN_TITLE_LEN:
            titles.add(title)
    SONG_TITLE_WHITELIST = titles
    return titles

# ==============================================
# 加载白名单
# ==============================================
def load_song_whitelist():
    global SINGER_WHITELIST, SONG_WHITELIST, LAST_LOAD_TIME
    singer_set = set()
    song_set = set()
    
    # 加载歌手白名单
    if os.path.exists(SINGER_WHITELIST_FILE):
        with open(SINGER_WHITELIST_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    name = normalize_name(line)
                    if name:
                        singer_set.add(name)
    
    # 加载歌曲白名单
    if os.path.exists(SONG_WHITELIST_FILE):
        with open(SONG_WHITELIST_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    name = normalize_name(line)
                    if name:
                        song_set.add(name)
    
    # 更新全局变量
    SINGER_WHITELIST = singer_set
    SONG_WHITELIST = song_set
    # 同步重建歌名级索引（支持裸歌名搜索放行）
    rebuild_song_title_index()
    LAST_LOAD_TIME = time.time()
    
    return singer_set, song_set

# 初始加载
load_song_whitelist()

# ==============================================
# 检查是否需要重新加载配置（云端优先 + 本地备份策略）
# ==============================================
def check_reload_config():
    """检查是否需要重新加载配置

    策略：云端优先 + 本地备份
    - 如果云端连接正常，由 cloud_updater 通过 apply_config() 实时生效
    - 如果云端连接断开超过5分钟，才重新加载本地文件作为备份

    这样可以避免云端配置被本地文件覆盖的问题
    """
    global LAST_LOAD_TIME
    current_time = time.time()

    # 如果云端连接正常，跳过本地文件加载
    try:
        from cloud_updater import get_updater
        updater = get_updater()
        if updater and (updater.sse_connected or updater.is_reconnecting):
            # 云端连接正常或在重连中，更新最后加载时间避免误触发
            LAST_LOAD_TIME = current_time
            return

        # 如果云端连接断开超过5分钟，才加载本地文件作为备份
        if updater and (current_time - updater.last_config_update > 300):
            # 检查是否需要重新加载（避免重复加载）
            if current_time - LAST_LOAD_TIME > LOAD_INTERVAL:
                load_song_whitelist()
                ctx.log.info(f"[CONFIG] 云端连接断开超过5分钟，使用本地文件备份")
            return
    except ImportError:
        # cloud_updater 未导入，说明未启用云更新，使用本地文件
        pass

    # 启用云更新时，绝不让本地文件覆盖云端下发的白名单（即使 SSE 短暂断开），
    # 避免“云端配置已应用又被本地轮询冲掉”的反复横跳。
    if CLOUD_UPDATE_ENABLED:
        LAST_LOAD_TIME = current_time
        return

    # 如果没有云更新功能，保留原有的60秒轮询逻辑
    # 但要先检查是否真的需要加载（避免重复操作）
    if current_time - LAST_LOAD_TIME > LOAD_INTERVAL:
        load_song_whitelist()
        ctx.log.info(f"[CONFIG] 配置已重新加载（本地模式），共 {len(SINGER_WHITELIST)} 位歌手，{len(SONG_WHITELIST)} 首歌曲")


# ==============================================
# 配置状态验证（用于诊断热重载是否生效）
# ==============================================
def validate_config_state():
    """验证当前配置状态，返回诊断信息"""
    try:
        from cloud_updater import get_updater
        updater = get_updater()

        info = {
            "local_singer_count": len(SINGER_WHITELIST),
            "local_song_count": len(SONG_WHITELIST),
            "blacklist_count": len(BLACKLIST_KEYWORDS),
            "last_load_time": LAST_LOAD_TIME,
            "cloud_connected": updater.sse_connected if updater else False,
            "config_synced": updater.config_synced if updater else True,
            "config_version": updater.config_version if updater else 0,
            "server_config_version": getattr(updater, 'server_config_version', 0) if updater else 0,
        }
        return info
    except ImportError:
        return {
            "local_singer_count": len(SINGER_WHITELIST),
            "local_song_count": len(SONG_WHITELIST),
            "blacklist_count": len(BLACKLIST_KEYWORDS),
            "last_load_time": LAST_LOAD_TIME,
            "cloud_connected": False,
            "config_synced": True,
            "config_version": 0,
            "server_config_version": 0,
        }


PLAY_KEYWORDS = (
    "getsonginfo", "get_song_info", "playinfo", "play_info",
    "trackercdn", "tracker", "get_res_privilege", "get_res",
    "get_url", "geturl", "playurl", "play_url",
    "getsongplayinfo", "getplayinfo",
    "mv", "getmv", "mvplay", "getmvinfo", "video", "getvideo", "playvideo",
    "mvstartflv", "union_all_mv_play", "/v1/video"
)

SEARCH_QUERY_KEYS = (
    "keyword", "songName", "singerName", "query", "key",
    "keyword_original", "songname", "singername"
)

FORM_QUERY_KEYS = (
    "keyword", "songName", "singerName", "query", "key", "songname", "singername"
)

def is_in_special_time():
    """检查当前时间是否在解除限制的特殊时间段内
    
    解除限制的时间段：
    - 每周一下午 5:12~5:48
    - 每周一下午 6:02~6:36
    - 每周四下午 4:26~4:56
    """
    now = datetime.datetime.now()
    weekday = now.weekday()  # 0-6，0表示周一
    hour = now.hour
    minute = now.minute
    
    # 每周一下午 5:12~5:48
    if weekday == 0 and hour == 17 and 12 <= minute <= 48:
        return True
    # 每周一下午 6:02~6:36
    if weekday == 0 and hour == 18 and 2 <= minute <= 36:
        return True
    # 每周四下午 4:26~4:56
    if weekday == 3 and hour == 16 and 26 <= minute <= 56:
        return True
    
    return False


def contains_blocked_numbers(text):
    """检查文本是否包含需要拦截的数字（91、78、9178、7891等）"""
    if not text:
        return False
    text = str(text).lower()
    # 检查是否包含91或78
    if "91" in text or "78" in text:
        return True
    return False


def is_in_blacklist(text):
    """检查是否在黑名单中"""
    if not text:
        return False
    text = text.strip()
    return text in BLACKLIST_KEYWORDS


def load(loader):
    """在 addon 注册后打日志；勿在模块 import 时调用 ctx.log，否则 mitmdump 可能加载失败立刻退出"""
    # 安装运行日志捕获，供服务端实时查看客户端日志
    install_log_capture()
    ctx.log.info(
        f"[√] 白名单已加载，共 {len(SINGER_WHITELIST)} 位歌手，{len(SONG_WHITELIST)} 首歌曲"
    )

    # 启动云更新服务
    if CLOUD_UPDATE_ENABLED:
        from cloud_updater import init_updater
        import sys
        # 关键：必须拿到 mitmproxy 实际执行 request()/is_in_whitelist 的“同一份”模块对象，
        # 否则云端热重载会写进另一份副本，导致“配置验证已更新但搜索仍被拦截”。
        current_module = None

        # 方式一（最可靠）：按 globals 身份在 sys.modules 中反查本模块对象。
        # 不依赖模块名，无论 mitmdump 用什么名字加载脚本都能命中同一份。
        this_globals = globals()
        for name, mod in list(sys.modules.items()):
            try:
                if getattr(mod, '__dict__', None) is this_globals:
                    current_module = mod
                    break
            except Exception:
                continue

        # 方式二：按常见模块名兜底（含运行时实际的 __name__）
        if current_module is None or not hasattr(current_module, 'SINGER_WHITELIST'):
            for name in (this_globals.get('__name__'), __name__, 'kugou_filter',
                         '__mitmproxy_script__.kugou_filter', 'src.kugou_filter'):
                if not name:
                    continue
                mod = sys.modules.get(name)
                if mod is not None and hasattr(mod, 'SINGER_WHITELIST'):
                    current_module = mod
                    break

        # 方式三：用 inspect 反查 load() 所属模块（不依赖 sys.modules 命名约定，
        # 在 PyInstaller 打包 / mitmdump 特殊加载下也能稳定命中本模块）。
        if current_module is None or not hasattr(current_module, 'SINGER_WHITELIST'):
            try:
                import inspect
                m = inspect.getmodule(load)
                if m is not None and hasattr(m, 'SINGER_WHITELIST'):
                    current_module = m
            except Exception:
                pass

        # 强制把“正在运行的本模块”注册为 sys.modules['kugou_filter']，
        # 使 cloud_updater 的 _get_module / _dynamic_import(importlib) 永远返回同一份，
        # 彻底避免 importlib.import_module('kugou_filter') 创建出第二份副本。
        if current_module is not None:
            sys.modules['kugou_filter'] = current_module

        # 同时传入本模块的 globals() 命名空间：这是云端热重载写白名单的最可靠入口，
        # 保证改的就是 request()/is_in_whitelist 实际读取的同一份集合。
        init_updater(CLOUD_SERVER_URL, kugou_filter_module=current_module, kugou_globals=this_globals)

        # 绑定成功判定以“真正生效的命名空间”为准：cloud_updater 写白名单优先走
        # kugou_globals（即本 globals），只要它含 SINGER_WHITELIST 即可保证热重载落到
        # request()/is_in_whitelist 实际读取的同一份集合——这才是热重载是否可用的真凭据。
        ns_ok = isinstance(this_globals, dict) and 'SINGER_WHITELIST' in this_globals
        mod_ok = current_module is not None and hasattr(current_module, 'SINGER_WHITELIST')
        bind_state = '成功' if (ns_ok or mod_ok) else '失败'
        ctx.log.info(
            f"[√] 云更新服务已启动: {CLOUD_SERVER_URL} | "
            f"模块绑定: {bind_state} (命名空间={'√' if ns_ok else '×'}, 模块={'√' if mod_ok else '×'})"
        )


# ==============================================
# 白名单匹配
# ==============================================
def is_in_whitelist(text, log=True):
    # log=False 用于 response() 对返回 JSON 的逐字符串递归扫描，避免热路径上对成百上千个
    # 字符串逐条打日志造成日志洪泛、拖慢代理（间接引发超时丢流）。request() 路径仍默认打日志。
    if not text:
        return False
    text = text.strip()
    
    # 过滤掉明显不是搜索词的内容（避免Parameter Error等提示）
    if len(text) < 2:
        return False
    if text.lower() in ["parameter error", "error", "null", "none", "undefined"]:
        return False
    # 过滤纯数字（搜索建议接口返回的数字ID）
    if text.isdigit():
        return False
    
    # 包含love的歌曲单独开白名单，一律放行
    if "love" in text.lower():
        if log:
            ctx.log.info(f"[√] Love歌曲白名单 | {text}")
        return True
    
    # 必须完全匹配白名单才放行
    if text in SINGER_WHITELIST or text in SONG_WHITELIST:
        if log:
            ctx.log.info(f"[√] 白名单完全匹配 | {text}")
        return True

    # 归一化兜底：剥离可能的序号前缀后再比对，避免格式差异导致误拦
    normalized = normalize_name(text)
    if normalized != text and (normalized in SINGER_WHITELIST or normalized in SONG_WHITELIST):
        if log:
            ctx.log.info(f"[√] 白名单归一化匹配 | {text} -> {normalized}")
        return True

    # 歌名级兜底：白名单里是「歌手 - 歌名」，而搜索词常只是裸歌名（如「小城夏天」）。
    # 命中歌名索引即放行，解决「明明配了却放不过」。
    if text in SONG_TITLE_WHITELIST or (normalized != text and normalized in SONG_TITLE_WHITELIST):
        if log:
            ctx.log.info(f"[√] 白名单歌名匹配 | {text}")
        return True

    if log:
        ctx.log.info(f"[X] 未完全匹配白名单 | {text}")
    return False


def host_matches_domain(host, domain):
    return host == domain or host.endswith(f".{domain}")


def url_has_extension(url, extensions):
    return any(
        url.endswith(ext) or f"{ext}?" in url or f"{ext}&" in url
        for ext in extensions
    )


def extract_keyword_from_json(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in ["keyword", "songname", "singername", "query", "key"]:
                return str(v)
            result = extract_keyword_from_json(v)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = extract_keyword_from_json(item)
            if result:
                return result
    return ""

# ==============================================
# 第一步：判断是否是酷狗的请求
# ==============================================
def is_kugou_url(host):
    host = host.lower()
    # 先检查是否是排除地址
    if host in EXCLUDE_HOSTS:
        return False
    if any(host_matches_domain(host, domain) for domain in KUGOU_DOMAINS):
        return True
    for pattern in KUGOU_IP_PATTERNS:
        if pattern.match(host):
            return True
    return False

# ==============================================
# 第二步：判断是否是音频文件（优先放行）
# ==============================================
def is_audio_file(url):
    url = url.lower()
    if url_has_extension(url, ALLOW_AUDIO_EXTENSIONS):
        return True
    media_keywords = ["playurl", "play_url", "get_res_privilege", "get_url", "media", "audio", "song", "video", "mv", "playvideo"]
    for keyword in media_keywords:
        if keyword in url:
            for ext in ALLOW_AUDIO_EXTENSIONS:
                # 必须带点匹配（如 ".mov"），避免裸子串误判：
                # "mov" 会命中 remove/move、"ape" 命中 shape/escape，导致非音频接口被误放行（漏拦截）。
                if ext in url:
                    return True
    return False

# ==============================================
# 第三步：判断是否是静态资源
# ==============================================
def is_static_resource(url):
    url = url.lower()
    return url_has_extension(url, STATIC_EXTENSIONS)

# ==============================================
# 第三步：拦截规则（匹配完整 URL 和路径）
# ==============================================
def should_block(url, path=None):
    url = url.lower()
    for pattern in COMPILED_BLOCK_PATTERNS:
        if pattern.search(url):
            return True
    if path:
        path = path.lower()
        for pattern in COMPILED_BLOCK_PATTERNS:
            if pattern.search(path):
                return True
    return False

def should_block_nav(url, path=None):
    """判断是否属于顶部导航栏/发现页栏目接口（推荐/乐库/歌单/频道/分类/视频/AI帮唱/金币中心）

    在「播放/音频放行」之前调用，确保这些栏目内容始终被拦截，
    同时只匹配发现/列表/推荐类接口，不影响真实歌曲播放。
    """
    url = url.lower()
    for pattern in COMPILED_NAV_BLOCK_PATTERNS:
        if pattern.search(url):
            return True
    if path:
        path = path.lower()
        for pattern in COMPILED_NAV_BLOCK_PATTERNS:
            if pattern.search(path):
                return True
    return False


def is_search_path(url, path=None):
    url = url.lower()
    for pattern in COMPILED_SEARCH_PATTERNS:
        if pattern.search(url):
            return True
    if path:
        path = path.lower()
        for pattern in COMPILED_SEARCH_PATTERNS:
            if pattern.search(path):
                return True
    return False


def is_lyrics_request(host, url):
    """判断是否为歌词接口请求。

    歌词接口（如 lyrics2.kugou.com/v1/search?...&lrctxt=1&album_audio_id=...）会命中
    搜索路径规则，但它是「正在播放歌曲」自动拉取歌词，关键词是 “歌手 - 歌名” 形式，
    并非用户主动搜索。这类请求不应写入搜索历史，否则历史里会混入当前播放曲目。
    """
    host = (host or "").lower()
    url = (url or "").lower()
    if "lyric" in host:
        return True
    # 兜底：带歌词专用参数的请求
    if ("lrctxt=" in url or "album_audio_id=" in url) and "/search" in url:
        return True
    return False


def is_play_request(url, method=None, path=None):
    # 仅凭可识别的播放/取流关键字放行。
    # 移除原先「method == POST and path == / 一律放行」的兜底：酷狗 gateway 的搜索/推荐等
    # 协议接口大量走根路径 POST，原兜底会把它们整体放行，造成漏拦截。真实取流/播放接口都带
    # 明确关键字（get_res_privilege / playinfo / playurl 等）或音频扩展名，会被关键字分支或
    # is_audio_file 正常放行，因此移除兜底不影响正常播放。
    return any(keyword in url for keyword in PLAY_KEYWORDS)

# ==============================================
# 空响应
# ==============================================
EMPTY_JSON = json.dumps(EMPTY_RESPONSE).encode("utf-8")

# ==============================================
# 核心拦截逻辑
# ==============================================
def request(flow: http.HTTPFlow):
    try:
        # 检查是否需要重新加载配置
        check_reload_config()
        
        host = flow.request.host.lower()
        url = flow.request.pretty_url.lower()
        path = flow.request.path.lower()
        method = flow.request.method
        
        # 检查是否在特殊时间段，解除所有限制
        if is_in_special_time():
            ctx.log.info(f"[SPECIAL TIME] 特殊时间段，解除所有限制 | {flow.request.pretty_url}")
            return
        
        # 第一步：只处理酷狗的请求
        if not is_kugou_url(host):
            return

        # 第一步半：顶部导航栏/发现页栏目内容拦截（高优先级）
        # 必须在播放放行之前判定，否则含 "mv"/"video" 的发现接口会被误当作播放放行。
        # 仅匹配发现/列表/推荐类接口，不会拦截真实歌曲播放。
        if should_block_nav(url, path):
            ctx.log.warn(f"[KUGOU BLOCK] 导航栏栏目拦截 | {flow.request.pretty_url}")
            flow.response = http.Response.make(200, EMPTY_JSON, {"Content-Type": "application/json;charset=utf-8"})
            return

        # 第二步：播放相关接口优先放行（核心播放接口）
        if is_play_request(url, method, path):
            ctx.log.info(f"[√] 放行播放接口 | {flow.request.pretty_url}")
            return

        # 第三步：搜索建议接口直接拦截（优先处理，避免Parameter Error重复提示）
        if "getsearchtip" in url or "search_no_focus_word" in url:
            ctx.log.warn(f"[KUGOU BLOCK] 搜索建议接口拦截 | {flow.request.pretty_url}")
            flow.response = http.Response.make(200, EMPTY_JSON, {"Content-Type": "application/json;charset=utf-8"})
            return

        # 第四步：音频文件优先放行（确保可以播放）
        if is_audio_file(url):
            ctx.log.info(f"[√] 放行音频文件 | {flow.request.pretty_url}")
            return

        # 第五步：检查是否拦截 - 先排除本地地址
        # 检查host是否是排除地址（本地服务端）
        host_lower = flow.request.host.lower()
        if host_lower in EXCLUDE_HOSTS:
            ctx.log.info(f"[√] 排除地址放行 | {flow.request.pretty_url}")
            return

        # 检查是否拦截 - 同时检查完整URL和路径（优先拦截）
        # 对于IP地址请求，先检查是否是播放接口，否则直接拦截
        is_ip_request = any(pattern.match(host_lower) for pattern in KUGOU_IP_PATTERNS)
        if is_ip_request:
            if not is_play_request(url, method, path) and not is_audio_file(url):
                ctx.log.warn(f"[KUGOU BLOCK] IP地址请求拦截 | {flow.request.pretty_url}")
                flow.response = http.Response.make(200, EMPTY_JSON, {"Content-Type": "application/json;charset=utf-8"})
                return
        
        if should_block(url, path):
            ctx.log.warn(f"[KUGOU BLOCK] {flow.request.pretty_url}")
            flow.response = http.Response.make(200, EMPTY_JSON, {"Content-Type": "application/json;charset=utf-8"})
            return

        # 第六步：检查是否是搜索路径
        if is_search_path(url, path):
            
            ctx.log.info(f"[DEBUG] 检测到搜索路径 | {flow.request.pretty_url}")
            keyword = ""

            if flow.request.query:
                for key in SEARCH_QUERY_KEYS:
                    keyword = flow.request.query.get(key, "")
                    if keyword:
                        break

            if not keyword and method == "POST":
                try:
                    if flow.request.urlencoded_form:
                        for key in FORM_QUERY_KEYS:
                            keyword = flow.request.urlencoded_form.get(key, "")
                            if keyword:
                                break
                    elif flow.request.text:
                        body = json.loads(flow.request.text)
                        keyword = extract_keyword_from_json(body)
                except:
                    pass

            if keyword:
                ctx.log.info(f"[+] 捕获搜索 | {keyword}")
                # 歌词接口（正在播放歌曲自动拉取歌词）不写入搜索历史，避免污染用户搜索记录
                record_ok = not is_lyrics_request(host, url)
                # 优先检查额外拦截规则：任何包含91或78的都拦截
                if contains_blocked_numbers(keyword):
                    ctx.log.info(f"[X] 额外拦截（包含91/78） | {keyword}")
                    if record_ok:
                        record_search(keyword, "blocked")
                    flow.metadata["whitelist_allowed"] = False
                    flow.response = http.Response.make(200, EMPTY_JSON, {"Content-Type": "application/json;charset=utf-8"})
                    return
                # 然后检查黑名单，黑名单中的关键词必须拦截
                elif is_in_blacklist(keyword):
                    ctx.log.info(f"[X] 黑名单拦截 | {keyword}")
                    if record_ok:
                        record_search(keyword, "blocked")
                    flow.metadata["whitelist_allowed"] = False
                    flow.response = http.Response.make(200, EMPTY_JSON, {"Content-Type": "application/json;charset=utf-8"})
                    return
                elif is_in_whitelist(keyword):
                    ctx.log.info(f"[√] 白名单放行 | {keyword}")
                    if record_ok:
                        record_search(keyword, "allowed")
                    flow.metadata["whitelist_allowed"] = True
                    return
                else:
                    ctx.log.info(f"[X] 拦截非白名单 | {keyword}")
                    if record_ok:
                        record_search(keyword, "blocked")
                    flow.metadata["whitelist_allowed"] = False
                    flow.response = http.Response.make(200, EMPTY_JSON, {"Content-Type": "application/json;charset=utf-8"})
                    return
            else:
                ctx.log.info(f"[!] 拦截无关键词搜索")
                flow.response = http.Response.make(200, EMPTY_JSON, {"Content-Type": "application/json;charset=utf-8"})
                return

        # 第六步：静态资源放行（仅在没有拦截规则匹配时）
        if is_static_resource(url):
            ctx.log.info(f"[√] 放行静态资源 | {flow.request.pretty_url}")
            return

        # 第七步：其他所有酷狗请求都拦截！（激进策略）
        ctx.log.warn(f"[KUGOU BLOCK] 激进拦截 | {flow.request.pretty_url}")
        flow.response = http.Response.make(200, EMPTY_JSON, {"Content-Type": "application/json;charset=utf-8"})
        return
        
    except Exception as e:
        ctx.log.error(f"request() 异常: {e}")

# ==============================================
# 响应拦截
# ==============================================
def response(flow: http.HTTPFlow):
    try:
        # 检查是否在特殊时间段，解除所有限制
        if is_in_special_time():
            return
        
        host = flow.request.host.lower()
        url = flow.request.pretty_url.lower()
        path = flow.request.path.lower()
        
        if not is_kugou_url(host):
            return

        if not is_search_path(url, path):
            return

        # 检查是否是白名单放行的请求
        if flow.metadata.get("whitelist_allowed"):
            ctx.log.info(f"[√] 白名单响应保留 | {flow.request.pretty_url}")
            return

        # 禁用缓存
        flow.response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        flow.response.headers["Pragma"] = "no-cache"
        flow.response.headers["Expires"] = "0"

        content_type = flow.response.headers.get("Content-Type", "")
        if "application/json" not in content_type and "text/json" not in content_type:
            flow.response.text = ""
            flow.response.status_code = 404
            return

        try:
            data = json.loads(flow.response.text)
            has_allow = False
            def check_content(obj):
                nonlocal has_allow
                if has_allow:
                    return
                if isinstance(obj, str):
                    if is_in_whitelist(obj, log=False):
                        has_allow = True
                elif isinstance(obj, dict):
                    for v in obj.values():
                        check_content(v)
                elif isinstance(obj, list):
                    for item in obj:
                        check_content(item)

            check_content(data)
            if not has_allow:
                ctx.log.info(f"[!] 响应清空 | {flow.request.pretty_url}")
                flow.response.text = json.dumps(EMPTY_RESPONSE)
                flow.response.status_code = 200
        except:
            flow.response.text = json.dumps(EMPTY_RESPONSE)
            flow.response.status_code = 200
            
    except Exception as e:
        ctx.log.error(f"response() 异常: {e}")

# ==============================================
# 错误处理
# ==============================================
def error(flow: http.HTTPFlow):
    if flow.error:
        error_msg = str(flow.error)
        if any(key in error_msg for key in ["Connection killed", "ConnectionResetError", "WinError 10054", "client disconnect", "server disconnect", "stream reset"]):
            return
        ctx.log.error(f"非致命错误: {flow.error}")
