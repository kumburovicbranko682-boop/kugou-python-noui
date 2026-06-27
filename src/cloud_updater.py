from mitmproxy import ctx
import json
import time
import threading
import socket
import os
import re
import platform
import subprocess
import requests
import psutil

# 无控制台窗口标志：宿主进程以 console=False 打包后，powershell/wmic 等子进程
# 若不带此标志会各自弹出黑窗口，这里统一抑制。
NO_WINDOW_FLAGS = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

# 兼容非 mitmproxy 环境
try:
    _log = _log
except NameError:
    class MockLog:
        def info(self, msg): print(f"[INFO] {msg}")
        def warn(self, msg): print(f"[WARN] {msg}")
        def error(self, msg): print(f"[ERROR] {msg}")
    _log = MockLog()


class CloudUpdater:
    def __init__(self, server_url, report_interval=3):
        self.server_url = server_url.rstrip('/')
        self.report_interval = report_interval  # 上报间隔（秒）- 缩短到3秒
        self.config_poll_interval = 5  # 配置轮询兜底间隔（秒）- 即使SSE断开也能生效
        self.config_hash = ""
        self.config_version = 0  # 配置版本号
        self.config_lock = threading.Lock()  # 保护配置应用，避免SSE与轮询并发修改
        self.running = False
        self.stop_event = threading.Event()
        # 配置应用后立即触发一次上报，避免本地计数最多滞后一个上报周期才回传服务端
        self.report_now_event = threading.Event()
        self.client_id = socket.gethostname()
        self.start_time = time.time()
        self.last_report = 0
        self.last_config_update = 0  # 最后配置更新时间
        self.config_synced = True   # 配置是否已同步
        self.last_sse_connect_time = 0  # 最后SSE连接成功时间

        # 实时状态
        self.kugou_running = False
        self.sse_connected = False
        self.is_reconnecting = False  # 正在重连中
        self.mitmproxy_running = True  # mitmproxy运行状态
        self.filter_stats = {"allowed": 0, "blocked": 0, "total": 0}
        self.memory_usage = 0
        self.cpu_usage = 0

        # 客户端本地配置信息（实时上报给服务端）
        self.local_singer_count = 0
        self.local_song_count = 0
        self.local_blacklist_count = 0

        # 保存 kugou_filter 模块的直接引用（通过 init_updater 传入）
        self._kugou_filter_module = None

        # ===== 课堂管控 =====
        # 配置随主配置由服务端下发（SSE / 轮询），结构见服务端 _default_class_control。
        self.class_control = {"enabled": False, "processes": [], "periods": []}
        self.class_control_lock = threading.Lock()
        self.class_control_poll_interval = 5  # 管控查杀检测间隔（秒）
        self._class_control_active = False  # 当前是否处于管控时段（用于状态切换日志）

        # 硬件信息缓存（硬件不变，避免每次上报都跑子进程）
        self._hardware_cache = None

    def get_config_hash(self, config_data):
        import hashlib
        return hashlib.md5(json.dumps(config_data, sort_keys=True).encode()).hexdigest()

    def fetch_config(self):
        """获取配置"""
        try:
            resp = requests.get(f"{self.server_url}/api/config", timeout=5)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            _log.warn(f"[CLOUD] 获取配置失败: {e}")
        return None

    def check_kugou_running(self):
        """检查酷狗音乐是否在运行"""
        try:
            # 尝试多种方式检测酷狗进程
            for proc in psutil.process_iter(['name', 'exe']):
                try:
                    name = (proc.info.get('name') or '').lower()
                    exe = (proc.info.get('exe') or '').lower()
                    # 检查进程名或可执行文件路径中是否包含kugou（不区分大小写）
                    if 'kugou' in name or 'kugou' in exe:
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            return False
        except Exception as e:
            _log.warn(f"[CLOUD] 酷狗检测异常: {e}")
            return False

    def get_system_stats(self):
        """获取系统统计信息"""
        try:
            self.memory_usage = psutil.virtual_memory().percent
            self.cpu_usage = psutil.cpu_percent(interval=0.1)
        except:
            pass

    @staticmethod
    def _clean_hw_text(s):
        """清洗硬件名称：去除空字符/控制符，合并空白，避免显示乱码"""
        if not s:
            return ""
        # 去掉 UTF-16 解码残留的空字节和不可见控制字符
        s = s.replace("\x00", "")
        s = "".join(ch for ch in s if ch == " " or ch.isprintable())
        s = re.sub(r"\s+", " ", s).strip()
        return s

    @classmethod
    def _split_hw_lines(cls, raw):
        """把多张硬件名称（用 ' || ' / 换行分隔）拆成去重保序列表"""
        if not raw:
            return []
        # 兼容 " || " 分隔符与换行
        parts = re.split(r"\s*\|\|\s*|[\r\n]+", str(raw))
        seen = set()
        result = []
        for p in parts:
            name = cls._clean_hw_text(p)
            if not name or name.lower() == "name":
                continue
            if name not in seen:
                seen.add(name)
                result.append(name)
        return result

    def _query_powershell(self, ps_cmd, timeout=8):
        """用 PowerShell 查询并强制 UTF-8 输出，彻底解决中文/特殊字符乱码"""
        try:
            full_cmd = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + ps_cmd
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", full_cmd],
                capture_output=True, timeout=timeout,
                creationflags=NO_WINDOW_FLAGS
            )
            out = result.stdout.decode("utf-8", errors="ignore")
            return self._clean_hw_text(out)
        except Exception:
            return ""

    def _query_wmic(self, wmic_args):
        """wmic 兜底查询，尝试多种编码解码，避免乱码"""
        try:
            result = subprocess.run(wmic_args, capture_output=True, shell=True, timeout=8,
                                     creationflags=NO_WINDOW_FLAGS)
            raw = result.stdout or b""
            decoded = None
            # wmic 在中文系统下通常是 UTF-16-LE；逐一尝试并选取无替换符的结果
            for enc in ("utf-16-le", "gbk", "utf-8"):
                try:
                    cand = raw.decode(enc)
                    if "\ufffd" not in cand:
                        decoded = cand
                        break
                except Exception:
                    continue
            if decoded is None:
                decoded = raw.decode("utf-8", errors="ignore")
            lines = [self._clean_hw_text(l) for l in decoded.splitlines()]
            lines = [l for l in lines if l and l.lower() != "name"]
            return lines[0] if lines else ""
        except Exception:
            return ""

    def get_hardware_info(self):
        """获取硬件信息（带缓存，硬件不变只采集一次）"""
        if self._hardware_cache is not None:
            return self._hardware_cache

        hardware = {}
        is_windows = os.name == "nt"

        # CPU 型号
        cpu_model = ""
        if is_windows:
            cpu_model = self._query_powershell(
                "(Get-CimInstance Win32_Processor | Select-Object -ExpandProperty Name) -join ', '"
            )
            if not cpu_model:
                cpu_model = self._query_wmic(["wmic", "cpu", "get", "name"])
        if not cpu_model:
            cpu_model = self._clean_hw_text(platform.processor())
        hardware["cpu_model"] = cpu_model or "Unknown"

        try:
            hardware["cpu_cores"] = psutil.cpu_count(logical=False) or os.cpu_count() or 0
        except Exception:
            hardware["cpu_cores"] = os.cpu_count() or 0

        # 内存
        try:
            mem = psutil.virtual_memory()
            hardware["total_memory_gb"] = round(mem.total / (1024**3), 1)
        except Exception:
            hardware["total_memory_gb"] = 0

        # 显卡（多显卡分割输出：每张显卡单独成项，去重保序）
        gpu_list = []
        if is_windows:
            # 用 " || " 作为分隔符逐张采集（换行会被 _clean_hw_text 折叠成空格，
            # 而 " || " 能在清洗后保留，从而可靠区分多张显卡）
            raw = self._query_powershell(
                "(Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name) -join ' || '"
            )
            if not raw:
                raw = self._query_wmic(["wmic", "path", "win32_VideoController", "get", "name"])
            gpu_list = self._split_hw_lines(raw)
            hardware["gpu_list"] = gpu_list
            # 兼容旧前端字段：用 " | " 连接多张显卡
            hardware["gpu_model"] = " | ".join(gpu_list) if gpu_list else "Unknown"
        else:
            hardware["gpu_list"] = []
            hardware["gpu_model"] = "N/A"

        # IP 地址
        try:
            hostname = socket.gethostname()
            hardware["ip_address"] = socket.gethostbyname(hostname)
        except Exception:
            hardware["ip_address"] = "Unknown"

        # 操作系统
        try:
            hardware["os_info"] = f"{platform.system()} {platform.release()}"
        except Exception:
            hardware["os_info"] = "Unknown"

        # 仅当关键信息成功采集时才缓存，否则下次重试
        if hardware.get("cpu_model") and hardware["cpu_model"] != "Unknown":
            self._hardware_cache = hardware
        return hardware

    def send_real_time_report(self):
        """发送实时状态报告 - 核心功能"""
        try:
            self.get_system_stats()
            kugou_running = self.check_kugou_running()
            self.kugou_running = kugou_running

            # 获取本地配置信息
            self.local_singer_count = self.get_singer_count()
            self.local_song_count = self.get_song_count()
            self.local_blacklist_count = self.get_blacklist_count()

            hardware_info = self.get_hardware_info()

            report = {
                "client_id": self.client_id,
                "hostname": self.client_id,
                "timestamp": time.time(),
                "uptime": time.time() - self.start_time,
                "status": {
                    "kugou_running": kugou_running,
                    "sse_connected": self.sse_connected,
                    "mitmproxy_running": self.mitmproxy_running,
                    "memory_usage": self.memory_usage,
                    "cpu_usage": self.cpu_usage,
                    "filter_stats": self.filter_stats.copy(),
                    "config_version": self.config_version,
                    "config_synced": self.config_synced,
                    "last_config_update": self.last_config_update,
                    # 客户端本地配置信息（服务端看不到的）
                    "local_singer_count": self.local_singer_count,
                    "local_song_count": self.local_song_count,
                    "local_blacklist_count": self.local_blacklist_count,
                    "config_hash": self.config_hash,
                    # 与服务端配置的对比信息
                    "server_config_version": self.server_config_version if hasattr(self, 'server_config_version') else 0,
                    "config_match": self.config_hash == self.server_config_hash if hasattr(self, 'server_config_hash') else True,
                },
                "hardware": hardware_info,
                "searches": self.get_recent_searches(),
                "logs": self.get_recent_logs()
            }

            resp = requests.post(
                f"{self.server_url}/api/report",
                json=report,
                timeout=3
            )

            if resp.status_code == 200:
                self.last_report = time.time()
                return True
        except Exception as e:
            _log.warn(f"[CLOUD] 状态上报失败: {e}")
        return False

    def _get_namespace(self):
        """返回 kugou_filter 模块的命名空间字典（读写白名单的唯一可靠入口）。

        优先使用 init_updater 传入的 globals() 字典——它就是过滤逻辑实际读取
        SINGER_WHITELIST/SONG_WHITELIST 的同一份命名空间，原地修改即时生效，
        不受 mitmdump 模块命名 / importlib 副本影响。
        """
        g = self._kugou_globals
        if isinstance(g, dict) and 'SINGER_WHITELIST' in g:
            return g
        mod = self._get_module()
        if mod is not None:
            return mod.__dict__
        return None

    def _get_module(self):
        """获取 kugou_filter 模块引用（优先使用保存的引用，避免重复扫描）"""
        if self._kugou_filter_module is not None and hasattr(self._kugou_filter_module, 'SINGER_WHITELIST'):
            return self._kugou_filter_module
        import sys
        module_names = [
            '__mitmproxy_script__.kugou_filter',
            'kugou_filter',
            'src.kugou_filter'
        ]
        for name in module_names:
            mod = sys.modules.get(name)
            if mod is not None and hasattr(mod, 'SINGER_WHITELIST'):
                self._kugou_filter_module = mod
                return mod
        for name, mod in list(sys.modules.items()):
            if 'kugou_filter' in name and hasattr(mod, 'SINGER_WHITELIST'):
                self._kugou_filter_module = mod
                return mod
        return None

    def get_recent_searches(self, max_items=200):
        """从 kugou_filter 取出待上报的搜索记录（取出即清空，避免重复上报）"""
        try:
            ns = self._get_namespace()
            if ns is not None and callable(ns.get('drain_search_log')):
                return ns['drain_search_log'](max_items)
        except Exception as e:
            _log.warn(f"[CLOUD] get_recent_searches error: {e}")
        return []

    def get_recent_logs(self, max_items=300):
        """从 kugou_filter 取出待上报的运行日志（取出即清空），供服务端实时查看"""
        try:
            ns = self._get_namespace()
            if ns is not None and callable(ns.get('drain_log_buffer')):
                return ns['drain_log_buffer'](max_items)
        except Exception as e:
            _log.warn(f"[CLOUD] get_recent_logs error: {e}")
        return []

    def get_singer_count(self):
        """获取当前歌手白名单数量"""
        try:
            ns = self._get_namespace()
            if ns is not None:
                return len(ns['SINGER_WHITELIST'])
            import kugou_filter
            return len(kugou_filter.SINGER_WHITELIST)
        except Exception as e:
            _log.warn(f"[CLOUD] get_singer_count error: {e}")
            return 0

    def get_song_count(self):
        """获取当前歌曲白名单数量"""
        try:
            ns = self._get_namespace()
            if ns is not None:
                return len(ns['SONG_WHITELIST'])
            import kugou_filter
            return len(kugou_filter.SONG_WHITELIST)
        except:
            return 0

    def get_blacklist_count(self):
        """获取当前黑名单关键词数量"""
        try:
            ns = self._get_namespace()
            if ns is not None:
                return len(ns['BLACKLIST_KEYWORDS'])
            import kugou_filter
            return len(kugou_filter.BLACKLIST_KEYWORDS)
        except:
            return 0

    def apply_config(self, config):
        """应用配置更新 - 增强版，带完整验证（线程安全）"""
        with self.config_lock:
            return self._apply_config_locked(config)

    def _apply_config_locked(self, config):
        # 通过命名空间字典操作，确保改的就是过滤逻辑实际读取 SINGER_WHITELIST 的同一份。
        # 这彻底解决“importlib 创建第二份模块副本，配置写进副本但过滤用另一份”导致的
        # “配置已应用但搜索仍被拦截”。
        ns = self._get_namespace()

        if ns is None:
            _log.error("[CLOUD] 无法获取 kugou_filter 命名空间，配置无法应用")
            return

        updated = []
        current_time = time.time()

        # 课堂管控配置：独立于白名单字段解析，配置哈希变化即会进入这里。
        # 即使本次没有白名单字段变更，也要让最新管控时段/进程列表即时生效。
        if "class_control" in config:
            self._apply_class_control(config.get("class_control"))

        # 记录更新前的状态
        old_singer_count = len(ns.get('SINGER_WHITELIST') or ())
        old_song_count = len(ns.get('SONG_WHITELIST') or ())
        old_blacklist_count = len(ns.get('BLACKLIST_KEYWORDS') or ())

        _log.info(f"[CLOUD] 配置更新前 | 歌手:{old_singer_count}, 歌曲:{old_song_count}, 黑名单:{old_blacklist_count}")

        # 与本地文件加载保持一致：剥离序号前缀 / 去首尾空白，避免云端格式
        # 与酷狗搜索关键词无法精确匹配，导致“配置已生效但仍被拦截”
        normalize = ns.get("normalize_name")

        def _clean_list(items):
            if not callable(normalize):
                return [i for i in items if i]
            cleaned = []
            for i in items:
                n = normalize(i)
                if n:
                    cleaned.append(n)
            return cleaned

        # 关键：用「先构建完整新集合 -> 原子重绑定全局名」替代 clear()+update()。
        # mitmproxy 的 request()/response() 钩子运行在独立线程，原子重绑定保证请求线程
        # 任何时刻看到的要么是旧集合、要么是完整新集合，不会出现中间空窗（错拦截）。
        # 由于 ns 就是过滤模块的 globals()，这里赋值等价于重绑定模块全局变量。
        if "singer_whitelist" in config:
            singers = config["singer_whitelist"]
            if isinstance(singers, list):
                cleaned_singers = _clean_list(singers)
                # 防护：云端下发空歌手白名单时，绝不覆盖已有的非空本地集合。
                # 典型场景是服务端配置尚未播种（空），否则会把本地白名单瞬间清成 0，
                # 导致所有搜索被拦（“配了也放不过”）。
                if not cleaned_singers and old_singer_count > 0:
                    _log.warn(f"[CLOUD] 忽略空歌手白名单下发，保留本地 {old_singer_count} 位（疑似服务端未播种）")
                else:
                    ns['SINGER_WHITELIST'] = set(cleaned_singers)
                    updated.append(f"歌手白名单({len(cleaned_singers)}位)")

        if "song_whitelist" in config:
            songs = config["song_whitelist"]
            if isinstance(songs, list):
                cleaned_songs = _clean_list(songs)
                # 防护：同上，云端空歌曲白名单不覆盖非空本地集合
                if not cleaned_songs and old_song_count > 0:
                    _log.warn(f"[CLOUD] 忽略空歌曲白名单下发，保留本地 {old_song_count} 首（疑似服务端未播种）")
                else:
                    ns['SONG_WHITELIST'] = set(cleaned_songs)
                    # 云端热更新后同步重建歌名级索引，保证裸歌名搜索放行与本地加载行为一致
                    rebuild_index = ns.get("rebuild_song_title_index")
                    if callable(rebuild_index):
                        try:
                            rebuild_index()
                        except Exception as e:
                            _log.warn(f"[CLOUD] 歌名索引重建失败: {e}")
                    updated.append(f"歌曲白名单({len(cleaned_songs)}首)")

        if "blacklist_keywords" in config:
            keywords = config["blacklist_keywords"]
            if isinstance(keywords, (list, set)):
                ns['BLACKLIST_KEYWORDS'] = set(keywords)
                updated.append(f"黑名单({len(keywords)}个)")

        if updated:
            # 计算新的配置哈希
            new_hash = self.get_config_hash(config)

            _log.info(f"[CLOUD] 配置已更新: {', '.join(updated)}")
            self.last_config_update = current_time
            self.config_synced = True

            # 同步更新配置哈希
            self.config_hash = new_hash

            # 更新配置版本号
            config_version_raw = config.get("version")
            if config_version_raw is not None:
                # 兼容字符串版本号（如 "1.0.0"）和数字版本号
                try:
                    self.config_version = int(config_version_raw)
                except (ValueError, TypeError):
                    self.config_version = self.config_version + 1
            else:
                self.config_version = self.config_version + 1

            # 关键修复：更新 kugou_filter 的最后加载时间，防止被本地轮询覆盖
            ns['LAST_LOAD_TIME'] = current_time

            # 配置已变更，唤醒上报线程立即上报最新本地计数（秒级反馈到服务端/面板）
            self.report_now_event.set()

            # 完整验证配置是否真正生效
            new_singer_count = len(ns['SINGER_WHITELIST'])
            new_song_count = len(ns['SONG_WHITELIST'])
            new_blacklist_count = len(ns['BLACKLIST_KEYWORDS'])

            _log.info(
                f"[CLOUD] 配置验证 | 歌手:{old_singer_count}→{new_singer_count} "
                f"歌曲:{old_song_count}→{new_song_count} "
                f"黑名单:{old_blacklist_count}→{new_blacklist_count}"
            )

            # 如果验证失败，记录警告
            if (config.get("singer_whitelist") and
                len(config["singer_whitelist"]) != new_singer_count):
                _log.warn(f"[CLOUD] 歌手白名单数量不匹配: 期望{len(config['singer_whitelist'])}, 实际{new_singer_count}")

            if (config.get("song_whitelist") and
                len(config["song_whitelist"]) != new_song_count):
                _log.warn(f"[CLOUD] 歌曲白名单数量不匹配: 期望{len(config['song_whitelist'])}, 实际{new_song_count}")
        else:
            _log.warn(f"[CLOUD] 配置更新但无有效字段: {list(config.keys())}")

    # ============================================================
    # 课堂管控：管控时段内每 5 秒查杀服务端指定进程，并写入可上报日志
    # ============================================================
    def _emit_class_log(self, msg, level="INFO"):
        """写入运行日志缓冲（复用 kugou_filter.LOG_BUFFER），供服务端「实时日志」查看。"""
        try:
            _log.info(f"[CLASS] {msg}")
        except Exception:
            pass
        try:
            ns = self._get_namespace()
            if ns is None:
                return
            buf = ns.get("LOG_BUFFER")
            lock = ns.get("LOG_BUFFER_LOCK")
            if buf is None:
                return
            import datetime as _dt
            entry = {
                "time": _dt.datetime.now().isoformat(),
                "level": level,
                "msg": f"[课堂管控] {msg}",
            }
            if lock is not None:
                with lock:
                    buf.append(entry)
            else:
                buf.append(entry)
        except Exception as e:
            _log.warn(f"[CLASS] 写日志失败: {e}")

    def _apply_class_control(self, cc):
        """应用服务端下发的课堂管控配置（线程安全）。"""
        if not isinstance(cc, dict):
            return
        enabled = bool(cc.get("enabled", False))
        processes = [str(p).strip() for p in (cc.get("processes") or []) if str(p).strip()]
        periods = []
        for item in (cc.get("periods") or []):
            if not isinstance(item, dict):
                continue
            start = item.get("start")
            end = item.get("end")
            if not start or not end:
                continue
            days = []
            for d in (item.get("days") or []):
                try:
                    di = int(d)
                except (ValueError, TypeError):
                    continue
                if 1 <= di <= 7:
                    days.append(di)
            periods.append({"start": str(start), "end": str(end), "days": days})
        with self.class_control_lock:
            self.class_control = {"enabled": enabled, "processes": processes, "periods": periods}
        _log.info(f"[CLASS] 课堂管控配置已更新: 启用={enabled}, 进程={processes}, 时段数={len(periods)}")

    @staticmethod
    def _parse_hhmm(value):
        """把 "HH:MM" 解析为当天分钟数；非法返回 None。"""
        try:
            h, m = str(value).split(":")
            h, m = int(h), int(m)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h * 60 + m
        except Exception:
            pass
        return None

    def _now_in_control_window(self, now=None):
        """判断当前是否处于任一启用的管控时段内。"""
        with self.class_control_lock:
            cc = dict(self.class_control)
            periods = list(cc.get("periods") or [])
        if not cc.get("enabled"):
            return False
        if not periods:
            return False
        if now is None:
            now = time.localtime()
        cur_min = now.tm_hour * 60 + now.tm_min
        cur_day = now.tm_wday + 1  # tm_wday: 0=周一..6=周日 -> 1..7
        for p in periods:
            start = self._parse_hhmm(p.get("start"))
            end = self._parse_hhmm(p.get("end"))
            if start is None or end is None:
                continue
            days = p.get("days") or []
            if days and cur_day not in days:
                continue
            if start <= end:
                in_window = start <= cur_min < end
            else:
                # 跨午夜时段，如 22:00-06:00
                in_window = cur_min >= start or cur_min < end
            if in_window:
                return True
        return False

    def _kill_class_processes(self):
        """查杀管控进程列表中正在运行的进程，返回本轮被杀进程描述列表。"""
        with self.class_control_lock:
            targets = [t.lower() for t in (self.class_control.get("processes") or [])]
        if not targets:
            return []
        # 同时兼容带/不带 .exe 的匹配
        target_set = set()
        for t in targets:
            target_set.add(t)
            if t.endswith(".exe"):
                target_set.add(t[:-4])
            else:
                target_set.add(t + ".exe")

        my_pid = os.getpid()
        killed = []
        try:
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    pid = proc.info.get('pid')
                    name = (proc.info.get('name') or '').strip()
                    if not name or pid == my_pid:
                        continue
                    if name.lower() in target_set:
                        proc.kill()
                        killed.append(f"{name}(PID={pid})")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                except Exception:
                    continue
        except Exception as e:
            self._emit_class_log(f"扫描进程异常: {e}", "WARN")
        return killed

    def class_control_loop(self):
        """课堂管控循环：进入管控时段后每 5 秒查杀指定进程。"""
        _log.info(f"[CLASS] 课堂管控线程启动 (检测间隔: {self.class_control_poll_interval}秒)")
        while not self.stop_event.is_set():
            try:
                in_window = self._now_in_control_window()
                # 管控时段进入/退出切换时各记录一次日志，便于服务端审计
                if in_window and not self._class_control_active:
                    self._class_control_active = True
                    with self.class_control_lock:
                        procs = list(self.class_control.get("processes") or [])
                    self._emit_class_log(f"进入管控时段，开始监控进程: {procs}")
                elif not in_window and self._class_control_active:
                    self._class_control_active = False
                    self._emit_class_log("退出管控时段，停止进程查杀")

                if in_window:
                    killed = self._kill_class_processes()
                    if killed:
                        self._emit_class_log(f"已查杀违规进程: {', '.join(killed)}", "WARN")
            except Exception as e:
                _log.warn(f"[CLASS] 管控循环异常: {e}")
            self.stop_event.wait(self.class_control_poll_interval)

    def sse_listener(self):
        """SSE 长连接监听"""
        _log.info(f"[SSE] 启动SSE监听: {self.server_url}")
        retry_delay = 1

        while not self.stop_event.is_set():
            try:
                self.is_reconnecting = True
                url = f"{self.server_url}/api/sse/stream?client_id={self.client_id}&hostname={self.client_id}"
                resp = requests.get(url, stream=True, timeout=30)

                if resp.status_code == 200:
                    _log.info(f"[SSE] 连接成功")
                    retry_delay = 1
                    self.sse_connected = True
                    self.last_sse_connect_time = time.time()
                    self.is_reconnecting = False

                    for line in resp.iter_lines(decode_unicode=True):
                        if self.stop_event.is_set():
                            break

                        if line and line.startswith('data: '):
                            try:
                                data = json.loads(line[6:])
                                msg_type = data.get("type")

                                if msg_type == "connected":
                                    _log.info(f"[SSE] 连接确认")
                                    # 连接成功后立即发送一次上报
                                    self.send_real_time_report()

                                elif msg_type == "init":
                                    config = data.get("config")
                                    if config:
                                        new_hash = self.get_config_hash(config)
                                        # 记录服务端配置版本和哈希
                                        self.server_config_version = config.get("version", 0)
                                        self.server_config_hash = new_hash
                                        _log.info(f"[SSE] 服务端配置版本: {self.server_config_version}, Hash: {new_hash[:8]}...")
                                        if new_hash != self.config_hash:
                                            self.apply_config(config)
                                            self.config_hash = new_hash
                                            # 标记配置未同步，稍后检查
                                            self.config_synced = False
                                        else:
                                            self.config_synced = True
                                            _log.info(f"[SSE] 配置已是最新")

                                elif msg_type == "config_update":
                                    _log.info(f"[SSE] 收到配置更新推送")
                                    config = data.get("config")
                                    if config:
                                        self.server_config_version = config.get("version", 0)
                                        self.server_config_hash = self.get_config_hash(config)
                                        self.apply_config(config)
                                        self.config_hash = self.server_config_hash
                                        self.config_synced = True

                                elif msg_type == "check_kugou":
                                    request_id = data.get("request_id", "")
                                    _log.info(f"[SSE] 收到检查请求: {request_id}")
                                    # 关键：每次收到检查请求都做「实时」进程探测并第一时间回传，
                                    # 不再先跑较重的 send_real_time_report()（含 cpu_percent 采样等），
                                    # 否则会拖慢服务端「检测酷狗」的响应，造成状态更新滞后。
                                    try:
                                        kugou_running = self.check_kugou_running()
                                        self.kugou_running = kugou_running
                                        requests.post(
                                            f"{self.server_url}/api/heartbeat",
                                            json={
                                                "client_id": self.client_id,
                                                "hostname": self.client_id,
                                                "kugou_running": kugou_running,
                                                "request_id": request_id
                                            },
                                            timeout=3
                                        )
                                        _log.info(f"[SSE] 已回传实时检查响应: 酷狗运行={kugou_running} (request_id: {request_id})")
                                    except Exception as e:
                                        _log.warn(f"[SSE] 检查响应回传失败: {e}")
                                    # 回传后再补一次完整状态上报，刷新面板的 CPU/内存/配置等信息（不阻塞检测结果）
                                    try:
                                        self.send_real_time_report()
                                    except Exception as e:
                                        _log.warn(f"[SSE] 检查后状态上报失败: {e}")

                                elif msg_type == "ping":
                                    # 收到服务端ping，保持连接活跃
                                    pass

                            except Exception as e:
                                _log.warn(f"[SSE] 消息处理异常: {e}")

                else:
                    self.sse_connected = False
                    self.is_reconnecting = False
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 30)

            except requests.exceptions.Timeout:
                _log.warn(f"[SSE] 连接超时，正在重连...")
                self.sse_connected = False
                self.is_reconnecting = False
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)
            except requests.exceptions.ConnectionError as e:
                _log.warn(f"[SSE] 连接错误: {e}")
                self.sse_connected = False
                self.is_reconnecting = False
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)
            except Exception as e:
                _log.warn(f"[SSE] 未知错误: {e}")
                self.sse_connected = False
                self.is_reconnecting = False
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)

        self.sse_connected = False
        self.is_reconnecting = False

    def report_loop(self):
        """实时上报循环"""
        _log.info(f"[CLOUD] 实时上报服务启动 (间隔: {self.report_interval}秒)")

        while not self.stop_event.is_set():
            elapsed = time.time() - self.last_report
            if elapsed >= self.report_interval or self.report_now_event.is_set():
                self.report_now_event.clear()
                self.send_real_time_report()
            # 正常按秒轮询；配置应用后 report_now_event 被置位会立即唤醒，实现秒级上报
            if self.report_now_event.wait(timeout=1):
                continue

    def poll_config(self):
        """主动拉取服务端配置并在变化时应用（SSE 的兜底机制）

        即使 SSE 连接断开或推送丢失，也能保证配置最终一致。
        通过哈希比对，配置未变化时不做任何修改，开销极小。
        """
        config = self.fetch_config()
        if not config:
            return False
        new_hash = self.get_config_hash(config)
        # 记录服务端版本/哈希，供状态对比使用
        self.server_config_version = config.get("version", 0)
        self.server_config_hash = new_hash
        if new_hash != self.config_hash:
            _log.info(f"[POLL] 检测到配置变化(SSE兜底)，应用新配置 | Hash: {new_hash[:8]}...")
            self.apply_config(config)
            self.config_hash = new_hash
            self.config_synced = True
            return True
        return False

    def config_poll_loop(self):
        """配置轮询兜底循环：无论 SSE 是否连接都定期校验配置"""
        _log.info(f"[CLOUD] 配置轮询兜底启动 (间隔: {self.config_poll_interval}秒)")
        # 启动时先立即拉取一次，确保初始配置正确
        try:
            self.poll_config()
        except Exception as e:
            _log.warn(f"[POLL] 初次拉取配置失败: {e}")

        while not self.stop_event.is_set():
            self.stop_event.wait(self.config_poll_interval)
            if self.stop_event.is_set():
                break
            try:
                self.poll_config()
            except Exception as e:
                _log.warn(f"[POLL] 配置轮询异常: {e}")

    def run(self):
        """运行云更新服务"""
        self.running = True
        _log.info(f"[CLOUD] 云更新服务启动: {self.server_url}")

        # 启动 SSE 监听线程
        self.sse_thread = threading.Thread(target=self.sse_listener, daemon=True)
        self.sse_thread.start()

        # 启动实时上报线程
        self.report_thread = threading.Thread(target=self.report_loop, daemon=True)
        self.report_thread.start()

        # 启动配置轮询兜底线程（即使SSE断开也能保证热重载生效）
        self.config_poll_thread = threading.Thread(target=self.config_poll_loop, daemon=True)
        self.config_poll_thread.start()

        # 启动课堂管控线程（管控时段内每5秒查杀服务端指定进程）
        self.class_control_thread = threading.Thread(target=self.class_control_loop, daemon=True)
        self.class_control_thread.start()

    def start(self):
        """启动云更新服务"""
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        _log.info(f"[CLOUD] 云更新线程已启动")

    def stop(self):
        """停止云更新服务"""
        self.running = False
        self.stop_event.set()
        self.sse_connected = False
        _log.info(f"[CLOUD] 云更新服务已停止")

    def update_filter_stats(self, allowed=0, blocked=0):
        """更新过滤器统计（供外部调用）"""
        self.filter_stats["allowed"] += allowed
        self.filter_stats["blocked"] += blocked
        self.filter_stats["total"] += (allowed + blocked)

    def _dynamic_import(self):
        """动态导入 kugou_filter 模块"""
        try:
            import importlib
            for module_name in ['kugou_filter', 'src.kugou_filter']:
                try:
                    module = importlib.import_module(module_name)
                    if hasattr(module, 'SINGER_WHITELIST'):
                        _log.info(f"[CLOUD] 通过 importlib 成功导入 {module_name}")
                        return module
                except:
                    continue
            return None
        except Exception as e:
            _log.warn(f"[CLOUD] 动态导入失败: {e}")
            return None

    def _find_kugou_filter_module(self):
        """扫描 sys.modules 查找 kugou_filter 相关模块"""
        import sys  # 确保 sys 可用
        try:
            for name, mod in list(sys.modules.items()):
                if 'kugou_filter' in name and hasattr(mod, 'SINGER_WHITELIST'):
                    _log.info(f"[CLOUD] 通过扫描找到模块: {name}")
                    return mod
            return None
        except Exception as e:
            _log.warn(f"[CLOUD] 扫描模块失败: {e}")
            return None


updater = None


def init_updater(server_url, report_interval=5, kugou_filter_module=None, kugou_globals=None):
    """初始化并启动云更新服务

    Args:
        server_url: 服务端地址
        report_interval: 上报间隔（秒）
        kugou_filter_module: 可选，kugou_filter 模块引用
        kugou_globals: 可选，kugou_filter 模块的 globals() 命名空间字典。
            这是最可靠的方式——直接操作过滤逻辑实际读取的同一份白名单集合，
            彻底避免“模块副本”导致的“配置已应用但搜索仍被拦截”。
    """
    global updater
    updater = CloudUpdater(server_url, report_interval)
    updater._kugou_filter_module = kugou_filter_module
    updater._kugou_globals = kugou_globals
    updater.start()
    return updater


def get_updater():
    """获取全局 updater 实例"""
    return updater


def update_filter_stats(allowed=0, blocked=0):
    """更新过滤器统计（供 kugou_filter.py 调用）"""
    global updater
    if updater:
        updater.update_filter_stats(allowed, blocked)