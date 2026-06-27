# kugou_config.py - 统一配置文件
import os
import re
import sys

# ==============================================
# 基础路径配置（支持PyInstaller打包）
# ==============================================
def get_base_path():
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

BASE_PATH = get_base_path()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def get_whitelist_path(filename, env_name=None):
    env_path = os.environ.get(env_name) if env_name else None
    if env_path:
        return env_path

    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        exe_config_path = os.path.join(exe_dir, "config", filename)
        if os.path.exists(exe_config_path):
            return exe_config_path

    # 优先查找项目根目录的 config 文件夹
    project_root = os.path.dirname(SCRIPT_DIR)
    root_config_path = os.path.join(project_root, "config", filename)
    if os.path.exists(root_config_path):
        return root_config_path

    frozen_path = os.path.join(BASE_PATH, filename)
    if os.path.exists(frozen_path):
        return frozen_path

    # 查找 src/config 目录
    src_config_path = os.path.join(SCRIPT_DIR, "config", filename)
    if os.path.exists(src_config_path):
        return src_config_path

    # 最后查找 src 目录本身
    script_path = os.path.join(SCRIPT_DIR, filename)
    if os.path.exists(script_path):
        return script_path

    return root_config_path  # 返回项目根目录的路径作为默认值

SINGER_WHITELIST_FILE = get_whitelist_path("singer_whitelist.txt", "KG_SINGER_WHITELIST")
SONG_WHITELIST_FILE = get_whitelist_path("song_whitelist.txt", "KG_SONG_WHITELIST")

# ==============================================
# 酷狗域名列表（合并两个文件的域名）
# ==============================================
KUGOU_DOMAINS = [
    # 核心域名
    "kugou.com", "kugou.net",
    "gateway.kugou.com", "gatewayretry.kugou.com",
    "songsearch.kugou.com", "complexsearch.kugou.com",
    "fxsong.kugou.com", "fxsong1.kugou.com", "fxsong2.kugou.com",
    "fxsong3.kugou.com", "fxsong4.kugou.com", "fxsong5.kugou.com",
    "msearchcdn.kugou.com", "mobilecdn.kugou.com",
    "hotsearch.kugou.com", "recommend.kugou.com", "newrecommend.kugou.com",
    "www.kugou.com", "t.kugou.com", "rt-p.kugou.com",
    "pc.service.kugou.com", "pigeon.service.kugou.com",
    # 静态资源域名
    "c1.kgimg.com", "imge.kugou.com", "singerimg.kugou.com",
    "openapicdn.kugou.com", "imgessl.kugou.com",
    "m1kgshow.kugou.com", "musicmall.kugou.com",
    "longaudio.kugou.com", "fanxing.kugou.com",
    "fx1.service.kugou.com", "p3fx.kgimg.com",
    "fxbssdl.kgimg.com", "staticssl.kugou.com",
    # 统计和配置域名
    "serveraddrweb.kugou.com", "logwebs.kugou.com",
    "rtwebcollects.kugou.com", "tj.kugou.com",
    "videorecordbssdlbig.kugou.com",
    "collect.kugou.com", "pcstat.kugou.com", "softstat.kugou.com",
    "config.mobile.kugou.com", "serveraddr.service.kugou.com",
    # 新增域名
    "mobilecdnbj.kugou.com", "service.mobile.kugou.com", "m.kugou.com"
]

# ==============================================
# 搜索接口匹配规则（合并两个文件的规则）
# ==============================================
SEARCH_PATTERNS = [
    r"/search/", r"/mixed", r"/query", r"/mobsearchV3",
    r"/complex", r"/songsearch", r"/v1/search", r"/v2/search", r"/v3/search",
    r"/mvsearch", r"/searchmv", r"/mv.*search", r"/search.*mv",
    r"/v1/mv", r"/v2/mv", r"/v3/mv",
    r"/getmv", r"/mvlist", r"/mvinfo",
]

COMPILED_SEARCH_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in SEARCH_PATTERNS)

# ==============================================
# 静态资源扩展名
# ==============================================
STATIC_EXTENSIONS = [
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".ico",
    ".js", ".css",
    ".woff", ".woff2", ".ttf", ".eot", ".svg",
    ".avif"
]

# ==============================================
# 拦截规则（保留 kugou_filter.py 的完整规则）
# ==============================================
BLOCK_PATTERNS = [
    # 首页相关 - 强化拦截
    r"/api/\d+/radio",
    r"/api/\d+/recommend",
    r"/api/\d+/home",
    r"/api/\d+/index",
    r"/recommend",
    r"/newrecommend",
    r"/homepage",
    r"/indexpage",
    r"/mainpage",
    r"/firstpage",
    r"/diversion/homepage",
    r"/special_recommend",
    r"/mfanxing-home",
    r"/find/index",
    r"/yueku/v9",
    r"/mv/v9",
    r"/fm2/app",
    r"/gtools",
    r"/els\.abt",
    r"/ocean",

    # 首页缓存/数据接口
    r"home/getcache/getData",
    r"getrecomendmv",
    r"gethomepage",
    r"getindex",
    r"getmain",
    r"getfirst",
    r"getrecommend",
    r"getradio",
    r"data\.json",
    r"getSpecial",
    r"internet_cafe_check",
    r"tmeab",
    r"pc_list",
    r"getcommentsnum",
    r"check_pc",
    r"userstat",
    r"/v2/gen",
    r"/v2/appconfig/index",
    r"/statistics/statistics\.html",

    # 每日推荐/视频推荐/特别推荐
    r"everyday_song_recommend",
    r"video_recommend",
    r"special_recommend",
    r"daily_recommend",
    r"today_recommend",
    r"single_card_recommend",
    r"everydayrecretry",
    r"videorecretry",
    r"specialrecretry",

    # 广告相关
    r"/ads/",
    r"/ad/",
    r"/banner/",
    r"/advertisement/",
    r"musicadservice",
    r"singlecardrec",
    r"ads\.gateway",
    r"/adp/ad",
    r"adslot",
    r"adposition",
    r"adspace",
    r"/pc_home_focus",
    r"/pc_diantai",

    # 排行榜
    r"/api/\d+/rank",
    r"/api/\d+/bill",
    r"/rank/audio",
    r"/rank/",
    r"/bill",
    r"/toplist",
    r"/charts",

    # 个性化推荐
    r"/api/\d+/personal",
    r"/api/\d+/guess",
    r"/api/\d+/discovery",
    r"/guess",
    r"/discovery",
    r"/personalized",
    r"/recommendation",

    # 活动/弹窗
    r"/api/\d+/activity",
    r"/api/\d+/popup",
    r"/activity",
    r"/popup",
    r"/pop",
    r"/notice",
    r"/alert",

    # 新歌/首发
    r"/api/\d+/new",
    r"/api/\d+/first",
    r"/newsong_publish",
    r"/newmusic",
    r"/newrelease",
    r"/premiere",

    # 歌手/歌单/专辑
    r"/singer",
    r"/artist",
    r"/album",
    r"/fxmusic",
    r"/songlist",
    r"/songlistcount",
    r"/playlist",
    r"/collection",
    r"/author/lang",
    r"/video/audio",
    r"/kmrserviceretry",
    r"/sum\.comment",
    r"/count/v1/audio",

    # 热搜/猜你想搜
    r"/hotsearch",
    r"/hotword",
    r"/guess_you_search",
    r"/getSearchTip",
    r"/search_no_focus_word",
    r"/hot",
    r"/trending",
    r"/searchtip",

    # 统计/追踪/日志
    r"analytics",
    r"track",
    r"monitor",
    r"/tracker",
    r"/statistics",
    r"/collect",
    r"/pcstat",
    r"/softstat",
    r"/log\.stat",
    r"/stat",
    r"/log",
    r"/report",

    # 其他业务接口 - 强化拦截
    r"/kmr/v1/",
    r"/kmr/v2/",
    r"/kmr/v3/",
    r"/pc.service",
    r"/pigeon.service",
    r"/serveraddr",
    r"/rt-p.kugou.com",
    r"/post",
    r"/m1kgshow",
    r"/musicmall",
    r"/longaudio",
    r"/service",
    r"/openapicdn",
    r"/list_audiobook_listen",
    r"/config\.mobile",
    r"/openapi",

    # 首页模块接口
    r"/module",
    r"/block",
    r"/section",
    r"/widget",
    r"/component",
]

COMPILED_BLOCK_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in BLOCK_PATTERNS)

# ==============================================
# 顶部导航栏 / 发现页栏目拦截规则（高优先级）
# 对应酷狗顶栏：推荐 / 乐库 / 歌单 / 频道 / 分类 / 视频 / AI帮唱 / 金币中心
# 这些规则在「播放/音频放行」之前判定，确保栏目内容一律拦下，
# 但只匹配“发现/列表/推荐”类接口，不会误伤真实播放接口（playurl/.mp3 等）。
# ==============================================
NAV_BLOCK_PATTERNS = [
    # 推荐
    r"recommend",
    r"/diversion",
    r"/home_focus",
    r"/pc_home_focus",

    # 乐库
    r"/yueku",
    r"musiclib",
    r"/musiclibrary",
    r"/library/",

    # 歌单（含歌单内歌曲列表接口，避免点开歌单仍能加载/播放整张歌单）
    r"/songlist",
    r"/playlist",
    r"/gedan",
    r"/special/",
    r"/specialrec",
    r"/collection",
    r"get_other_list_file",      # 歌单内歌曲清单
    r"pubsongs",                 # 歌单/专辑歌曲列表
    r"getspecialsong",           # 歌单歌曲信息
    r"special.*songlist",
    r"specialsonglist",
    r"/playlist/.*song",
    r"/get_playlist",

    # 频道 / 电台
    r"/channel",
    r"/diantai",
    r"/radio",
    r"/fm2?/",
    r"/scene",

    # 分类
    r"/category",
    r"/classify",
    r"/tag/",
    r"/sort/",
    r"/style/",
    r"/theme/",

    # 视频（仅拦截发现/列表，不拦真实播放）
    r"/mvlist",
    r"mvsearch",
    r"recomendmv",
    r"getrecomendmv",
    r"video_recommend",
    r"/short_?video",
    r"shortvideo",
    r"/mv/v\d",
    r"/video/list",
    r"/getmvdata",
    r"/mv/recommend",

    # AI帮唱
    r"/ai[_/-]?sing",
    r"ai_?chorus",
    r"aichorus",
    r"ai_?bangchang",
    r"/banchang",
    r"/accompan",
    r"/ai/",
    r"/aigc",

    # 金币中心 / 商城 / 福利 / 任务
    r"/coin",
    r"/gold",
    r"jinbi",
    r"/welfare",
    r"/musicmall",
    r"/mall",
    r"/shop",
    r"/task[_/-]?center",
    r"/reward",
    r"/credit",
    r"/integral",
    r"/sign[_/-]?in",
]

COMPILED_NAV_BLOCK_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in NAV_BLOCK_PATTERNS)

# ==============================================
# 空响应模板
# ==============================================
EMPTY_RESPONSE = {
    "status": 0,
    "error_code": 152,
    "error_msg": "Parameter Error",
    "data": {
        "lists": [], "correctiontip": "", "total": 0,
        "info": [], "songs": [], "artists": [], "albums": [],
        "search_tip": []
    }
}

# ==============================================
# 排除地址（不拦截这些地址）
# ==============================================
EXCLUDE_HOSTS = [
    "localhost", "127.0.0.1",
    "192.168.3.168",  # 本地服务端地址
]

# ==============================================
# 酷狗 IP 网段正则（匹配IP直连场景）
# ==============================================
KUGOU_IP_PATTERNS = [
    # 已知酷狗IP网段
    re.compile(r'^120\.241\.'),
    re.compile(r'^120\.232\.'),
    re.compile(r'^113\.96\.'),
    re.compile(r'^39\.156\.'),
    re.compile(r'^119\.188\.'),
    re.compile(r'^61\.160\.'),
    re.compile(r'^112\.90\.'),
    re.compile(r'^183\.232\.'),
    # 新增酷狗相关IP网段
    re.compile(r'^49\.'),
    re.compile(r'^183\.'),
    re.compile(r'^123\.'),
    re.compile(r'^59\.'),
    re.compile(r'^218\.'),
    re.compile(r'^220\.'),
    re.compile(r'^221\.'),
    # 注意：移除了匹配所有IP的规则，避免拦截本地服务端
]

# ==============================================
# 首页接口拦截规则（保留 kugou_config.py 的规则）
# ==============================================
HOME_PAGE_INTERFACE_PATTERNS = [
    r'/recommend', r'/everyday_song_recommend', r'/video_recommend',
    r'/special_recommend', r'/single_card_recommend', r'/homepage/recommend',
    r'/newrecommend', r'/hotsearch',
    r'/rank/', r'/rank_list', r'/toplist', r'/rank/list',
    r'/ads.gateway', r'/adp/ad/', r'/musicadservice', r'/diantai',
    r'/newsong_publish', r'/albums', r'/yueku/', r'/mtv/getrecomendmv',
    r'/diversion/homepage', r'/home_focus',
    r'/collect.kugou.com', r'/userstat', r'/softstat', r'/pcstat',
    r'/appconfig/index', r'/get_ack_info', r'/privacy/info',
    r'/get_version', r'/songlistcount/', r'/author/lang',
    r'/serveraddr.service.kugou.com', r'/config.mobile.kugou.com',
    r'/tag/list', r'/tag/recommend', r'/category/special', r'/yueku/recommend',
    r'/api/v3/rank', r'/api/v3/tag', r'/api/v3/category', r'/v1/yueku',
    r'/api/v3/rank/newsong',
    r'/api/v3/tag/recommend',
    r'/api/v5/special/recommend',
    r'/api/v3/search/hot',
]

COMPILED_HOME_PAGE_INTERFACE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in HOME_PAGE_INTERFACE_PATTERNS
)

# ==============================================
# 首页静态资源域名黑名单
# ==============================================
HOME_STATIC_DOMAINS = [
    "c1.kgimg.com", "imge.kugou.com", "singerimg.kugou.com",
    "imgessl.kugou.com", "m1kgshow.kugou.com", "staticssl.kugou.com",
    "openapicdn.kugou.com", "p3fx.kgimg.com", "fxbssdl.kgimg.com"
]

# ==============================================
# 允许放行的音频文件扩展名
# ==============================================
ALLOW_AUDIO_EXTENSIONS = [".mp3", ".flac", ".wav", ".aac", ".ape", ".ogg", ".m4a",
                         ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"]

# ==============================================
# 配置验证
# ==============================================
def get_validation_messages():
    messages = []

    if os.path.exists(SINGER_WHITELIST_FILE):
        messages.append(f"[√] 歌手白名单文件存在: {SINGER_WHITELIST_FILE}")
    else:
        messages.append(f"[!] 歌手白名单文件不存在: {SINGER_WHITELIST_FILE}")

    if os.path.exists(SONG_WHITELIST_FILE):
        messages.append(f"[√] 歌曲白名单文件存在: {SONG_WHITELIST_FILE}")
    else:
        messages.append(f"[!] 歌曲白名单文件不存在: {SONG_WHITELIST_FILE}")

    messages.append(
        f"[√] 配置验证通过，共 {len(KUGOU_DOMAINS)} 个域名，{len(BLOCK_PATTERNS)} 条拦截规则"
    )
    return messages


def validate_config(verbose=True):
    assert len(KUGOU_DOMAINS) > 0, "KUGOU_DOMAINS 不能为空"
    assert len(SEARCH_PATTERNS) > 0, "SEARCH_PATTERNS 不能为空"
    assert len(BLOCK_PATTERNS) > 0, "BLOCK_PATTERNS 不能为空"
    assert EMPTY_RESPONSE is not None, "EMPTY_RESPONSE 不能为空"
    messages = get_validation_messages()
    if verbose:
        for message in messages:
            print(message)
    return messages


if __name__ == "__main__":
    validate_config()
