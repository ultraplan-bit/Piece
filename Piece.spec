# -*- mode: python ; coding: utf-8 -*-
"""
Piece PyInstaller 打包配置

使用方式:
    pyinstaller Piece.spec

输出:
    dist/Piece/
    ├── Piece.exe
    ├── piece-cli.exe
    ├── _internal/
    └── ...
"""

import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import copy_metadata, collect_data_files

# 项目根目录
PROJECT_ROOT = Path(SPECPATH)

# 查找 sqlite_vec 的 DLL 路径
def find_sqlite_vec_dll():
    """查找 sqlite_vec 的 vec0.dll 路径"""
    try:
        import sqlite_vec
        sqlite_vec_dir = Path(sqlite_vec.__file__).parent
        dll_path = sqlite_vec_dir / "vec0.dll"
        if dll_path.exists():
            return str(dll_path)
    except ImportError:
        pass
    return None

# 收集二进制文件
binaries = []
sqlite_vec_dll = find_sqlite_vec_dll()
if sqlite_vec_dll:
    # 将 vec0.dll 放到 sqlite_vec 包目录下
    binaries.append((sqlite_vec_dll, 'sqlite_vec'))

# 收集数据文件
datas = [
    # 图标
    (str(PROJECT_ROOT / 'assets' / 'icon.ico'), 'assets'),
    # 国际化文件
    (str(PROJECT_ROOT / 'app' / 'i18n' / 'locales'), 'app/i18n/locales'),
    # 内置 Skill（GUI 的 Skill 导出视图读取）
    (str(PROJECT_ROOT / 'skills'), 'skills'),
]

# 添加包元数据（完整桌面制品包含可选 GUI/MCP 依赖）
# piece 自身的元数据供 importlib.metadata.version("piece") 使用（版本号单一来源）
datas += copy_metadata('piece')
datas += copy_metadata('fastmcp')
datas += copy_metadata('nicegui')
datas += copy_metadata('uvicorn')
datas += copy_metadata('starlette')
datas += copy_metadata('httpx')
datas += copy_metadata('openai')
datas += copy_metadata('pydantic')
datas += copy_metadata('langchain-core')
datas += copy_metadata('langchain-openai')
datas += copy_metadata('langgraph')
datas += copy_metadata('webdav4')
datas += copy_metadata('markitdown')

# 收集 jieba 词典文件
datas += collect_data_files('jieba')

# 公式转换器导入时读取符号表，hiddenimports 不会收集此类数据文件
datas += collect_data_files('latex2mathml')

# 收集 magika 模型文件（markitdown 依赖）
datas += collect_data_files('magika')

# fastmcp 2.14 lifespan → docket → fakeredis 隐式依赖链的包数据
# （fakeredis/commands.json；lupa.lua51 见 hiddenimports）
datas += collect_data_files('fakeredis')

# 隐式导入（PyInstaller 无法自动检测的模块）
hiddenimports = [
    # 常驻服务主入口的延迟导入（核心、API、可选 GUI/MCP/托盘）
    'app.api',
    'app.client',
    'app.asgi_server',
    'app.runtime',
    'app.gui',
    'app.mcp_servers',
    'app.tray',
    'app.ui',
    'app.ui.pages',
    'app.window',
    'retrieval.server',
    'indexing.mcp.server',
    'indexing.mcp.config',
    # 轻量 CLI 与平台边界（含独立窗口 / helper 的早期分派）
    'app.cli',
    'app.platform',
    # NiceGUI 相关
    'nicegui',
    # 托盘（pystray 按平台动态导入后端）
    'pystray',
    'pystray._win32',
    # 独立窗口进程（Piece.exe --window 分派进去）
    'app.window_host',
    'webview',
    'webview.platforms.edgechromium',
    # 数据库、服务内任务编排和短命解析 helper
    'sqlite3',
    'sqlite_vec',
    'indexing.worker_process',
    'indexing.worker_manager',
    'indexing.services.parser_helper',
    'pymupdf',
    'pythoncom',
    'pywintypes',
    'win32com.client',
    'win32com.shell',
    # MCP/FastAPI 相关
    'fastmcp',
    # fastmcp 2.14 lifespan → docket → fakeredis/lupa 隐式依赖
    # （lupa.lua51 由 fakeredis 动态导入，静态分析不可见）
    'docket',
    'fakeredis',
    'lupa',
    'lupa.lua51',
    'uvicorn',
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'starlette',
    'starlette.routing',
    'starlette.responses',
    'starlette.middleware',
    'starlette.middleware.cors',
    'anyio',
    'anyio._backends',
    'anyio._backends._asyncio',
    # LangChain 相关
    'langchain_core',
    'langchain_openai',
    'langgraph',
    # 其他
    'pydantic',
    'httpx',
    'openai',
    'jieba',
    'aiofiles',
    'webdav4',
    # PDF 处理
    'markitdown',
    'magika',
    # Markdown 公式渲染（markdown2 latex extra 动态导入）
    'latex2mathml',
    'latex2mathml.converter',
    # 插图尺寸过滤（chunk_images 内延迟导入）
    'PIL',
    'PIL.Image',
    # frontmatter 解析（metadata_service 内延迟导入）
    'yaml',
]

# 排除不需要的模块（减小体积）
excludes = [
    'tkinter',
    'matplotlib',
    'scipy',
    'numpy.testing',
]

block_cipher = None

a = Analysis(
    [str(PROJECT_ROOT / 'app' / 'server.py')],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Piece',
    debug=False,
    contents_directory='lib',
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / 'assets' / 'icon.ico'),
)

# 控制台版入口：终端与 agent 管道需要等待输出与退出码，windowed exe 在
# cmd/PowerShell 直接执行时拿不到结果。两个 exe 共用同一 lib/ 目录
# （contents_directory 必须与第一个 EXE 一致，COLLECT 取第一个 EXE 的值），
# 不能命名为 piece.exe：Windows 不区分大小写，与 Piece.exe 冲突。
exe_cli = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='piece-cli',
    debug=False,
    contents_directory='lib',
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / 'assets' / 'icon.ico'),
)

coll = COLLECT(
    exe,
    exe_cli,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Piece',
)
