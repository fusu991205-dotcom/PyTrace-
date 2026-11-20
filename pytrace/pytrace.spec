# -*- mode: python ; coding: utf-8 -*-
# PyTrace 打包配置（PyInstaller）
# 用法：python -m PyInstaller --clean --noconfirm pytrace.spec
# 产物：dist/PyTrace（Linux/Mac）或 dist/PyTrace.exe（Windows），单文件、双击即用
#
# 设计要点：
# 1) app 自身用 PyInstaller 的 Python 跑（fastapi/uvicorn/explain 打成 pyc）；
# 2) runner.py / guard.py / instrument.py / envcheck.py / interp.py 必须以
#    【源码】形式进包（datas，不是 hiddenimports）——它们是用【用户自己的 Python】
#    执行的子进程脚本，exe 里捆绑的 pyc 对用户解释器毫无意义；
# 3) web/ 与 samples/ 是静态资源，原样带进 _MEIPASS；
# 4) 运行历史（runtime/）落在 exe 旁边而不是 _MEIPASS——见 app.py data_root()。

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('web', 'web'),
        ('samples', 'samples'),
        ('runner.py', '.'),
        ('guard.py', '.'),
        ('instrument.py', '.'),
        ('envcheck.py', '.'),
        ('interp.py', '.'),
    ],
    hiddenimports=[
        # uvicorn 内部用字符串动态 import，PyInstaller 静态分析看不见，必须点名
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.protocols.websockets.websockets_impl',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.middleware',
        'uvicorn.middleware.proxy_headers',
        # WebSocket 实现（uvicorn 按装没装动态选择）
        'websockets',
        'websockets.legacy',
        'websockets.legacy.server',
        # 本地模块（app.py 里 try-import，PyInstaller 能看到，但点名更稳）
        'explain',
        'instrument',
        'guard',
        'interp',
        'envcheck',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 纯瘦身：GUI 工具包我们用不上（pywebview 可选装，不打包）
        'tkinter',
        'matplotlib',
        'numpy',
        'pandas',
        'PIL',
        # Qt 绑定必须全部排除：PyInstaller 明确禁止一包多 Qt（Anaconda 环境常同时装
        # PyQt5 + PySide6，直接报 "attempt to collect multiple Qt bindings" 中断打包）。
        # PyTrace 界面是浏览器网页，任何 GUI 库都用不上；排除不存在的模块是无害的。
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        'shiboken2', 'shiboken6', 'shiboken',
        'qtpy',
        'PyQtWebEngine', 'PyQt5.QtWebEngineCore',
        'pywebview',
        'gi', 'gtk',
        'IPython',
        'jedi',
        'test',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PyTrace',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # 保留控制台：新手能看见 BANNER 与报错，出问题不发懵
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
