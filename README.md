# KuGou Music Interceptor - Python No UI

酷狗音乐搜索拦截与集中配置上报工具的 **Python 无 UI 版本**。仓库仅保留命令行/后台运行所需代码，不包含桌面 UI、客户端 UI、C++ 版本、打包产物和历史调试文件。

## 功能

- 酷狗请求拦截：基于 `mitmproxy` 过滤酷狗搜索、推荐、广告、导航栏目等请求。
- 白名单放行：支持歌手白名单、歌曲白名单，以及“歌手 - 歌名”条目的裸歌名匹配。
- 黑名单/关键词拦截：默认拦截 `9178`、`7891`，以及包含 `91` 或 `78` 的搜索词。
- 无 UI 后台客户端：`src/kugou_launcher_v2.py` 可后台启动代理、安装证书、注册开机自启、监控酷狗进程。
- 实时连接与上报：客户端通过 SSE/HTTP 向服务端实时上报在线状态、酷狗是否运行、CPU/内存、本地配置数量、搜索记录和运行日志。
- 配置热更新：服务端配置变更后通过 SSE 推送给客户端，客户端断线时也会轮询兜底。
- API 服务端：`server/app.py` 提供配置、客户端、日志、搜索历史、远程检测酷狗等接口。
- 可选 NapCat/OneBot 桥接：`server/napcat_bridge.py` 可通过 QQ 指令远程管理（需要额外安装 `websocket-client` 并配置环境变量）。

## 目录结构

```text
.
├── src/
│   ├── kugou_launcher_v2.py   # 无 UI 客户端启动器
│   ├── kugou_filter.py        # mitmproxy 过滤脚本
│   ├── kugou_config.py        # 拦截规则与白名单路径配置
│   ├── cloud_updater.py       # SSE/HTTP 云端配置同步与状态上报
│   └── kugou_ssl_bypass.js    # 预留 SSL bypass 脚本资源
├── server/
│   ├── app.py                 # 无 UI Flask API 服务端
│   └── napcat_bridge.py       # 可选 QQ/NapCat 远程管控桥接
├── config/
│   ├── singer_whitelist.txt   # 歌手白名单
│   └── song_whitelist.txt     # 歌曲白名单
├── build_noui.spec            # PyInstaller 无窗口客户端打包配置
├── requirements.txt
└── .gitignore
```

## 环境要求

- Windows 10/11（客户端功能主要面向 Windows）
- Python 3.10+
- 酷狗音乐客户端
- 管理员权限（首次安装 mitmproxy CA 证书、配置开机自启时可能需要）

## 安装依赖

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

如果需要 QQ/NapCat 远程指令桥接：

```bash
pip install websocket-client
```

## 启动服务端

```bash
python server/app.py
```

默认监听：`http://0.0.0.0:5000`

常用接口：

- `GET /api/status`：服务端状态与在线客户端数量
- `GET /api/config`：获取白名单/黑名单/课堂管控配置
- `POST /api/config`：更新配置并推送给客户端
- `GET /api/clients`：在线客户端列表
- `POST /api/check_kugou`：通过 SSE 触发客户端实时检查酷狗是否运行
- `GET /api/clients/<client_id>/logs`：客户端运行日志
- `GET /api/clients/<client_id>/search_history`：客户端搜索记录

服务端配置会保存在 `server/kugou_config.json`，首次运行若不存在则自动使用默认配置。

## 启动客户端（源码方式）

1. 先启动服务端。
2. 配置服务端地址（任选一种方式）：
   - 命令行：`python src/kugou_launcher_v2.py --server http://服务端IP:5000`
   - 环境变量：`set KG_CLOUD_SERVER=http://服务端IP:5000`
   - 客户端同目录 `kg_server.txt` 首行写入服务端地址
3. 以管理员权限启动客户端：

```bash
python src/kugou_launcher_v2.py --server http://127.0.0.1:5000
```

客户端会：

- 启动本地 mitmproxy 代理；
- 启用云端配置同步与实时状态上报；
- 检测并上报酷狗进程状态；
- 生成/读取 `kg_server.txt`；
- 尝试注册开机自启。

## 配置酷狗代理

在酷狗音乐中配置 HTTP/HTTPS 代理为：

```text
127.0.0.1:8080
```

然后重启酷狗音乐。

## 打包无 UI 客户端

```bash
pyinstaller --clean build_noui.spec
```

输出文件默认在 `dist/ku_client.exe`。

> 注意：本仓库不提交 `.exe`、`dist/`、`build/` 等打包产物。

## 白名单格式

`config/singer_whitelist.txt`：每行一个歌手。

```text
周杰伦
林俊杰
陈奕迅
```

`config/song_whitelist.txt`：每行一首歌，推荐格式为 `歌手 - 歌名`。

```text
周杰伦 - 晴天
林俊杰 - 江南
陈奕迅 - 十年
```

程序会自动剥离行首序号，例如 `1. 周杰伦 - 晴天`。

## 日志

客户端日志默认写入：

```text
C:\KuGouFilterLogs
```

客户端运行日志和搜索记录也会周期性上报到服务端，可通过 API 查询。

## 可选：NapCat/OneBot 远程管控

`server/napcat_bridge.py` 默认会尝试启动桥接。可用环境变量控制：

- `KG_NAPCAT_ENABLE=0`：关闭桥接
- `KG_NAPCAT_WS_URL`：NapCat WebSocket 地址，默认 `ws://127.0.0.1:3001`
- `KG_NAPCAT_TOKEN`：连接令牌
- `KG_NAPCAT_ADMINS`：允许下指令的 QQ 号，逗号分隔
- `KG_NAPCAT_GROUPS`：允许响应的群号，逗号分隔

不需要 QQ 远程管控时建议设置：

```bash
set KG_NAPCAT_ENABLE=0
python server/app.py
```

## 免责声明

本项目仅供学习、研究和自有环境管理使用。使用者应自行确认对网络代理、证书安装、进程管控和客户端配置具有授权，并遵守相关软件服务条款与当地法律法规。
