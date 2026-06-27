# -*- mode: python ; coding: utf-8 -*-
# 客户端版（完全静默 / 无窗口）：
#   - 无任何控制台窗口（双击直开、cmd 启动、开机自启都不弹窗）
#   - 开机自启（--autostart），每次启动检测自启状态并自动纠正路径
#   - 连接服务端实时上报上线，日志写入 C:\KuGouFilterLogs
#   - 服务端地址可通过 kg_server.txt / --server / KG_CLOUD_SERVER 自定义
# 仅依赖 mitmproxy / psutil / pywin32 / requests（不依赖 frida）

block_cipher = None

a = Analysis(
    ['src/kugou_launcher_v2.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('src/kugou_config.py', '.'),
        ('src/kugou_filter.py', '.'),
        ('src/cloud_updater.py', '.'),
        ('src/kugou_ssl_bypass.js', '.'),
        ('config/singer_whitelist.txt', 'config'),
        ('config/song_whitelist.txt', 'config'),
    ],
    hiddenimports=[
        'mitmproxy',
        'mitmproxy.tools.dump',
        'mitmproxy.tools.main',
        'psutil',
        'requests',
        'win32gui',
        'win32con',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'frida',
        'frida_tools',
        'tkinter',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ku_client',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
