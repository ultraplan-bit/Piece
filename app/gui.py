"""NiceGUI 可选入口：只负责挂载页面；数据库、Worker、MCP 由 Runtime 管理。

导入本模块不加载 NiceGUI；``register`` 才导入，缺依赖时给出安装提示。
"""

from __future__ import annotations

import importlib.util
from typing import Any

GUI_INSTALL_HINT = (
    "GUI 依赖未安装。请运行 `uv sync --extra gui`（完整桌面模式运行 "
    "`uv sync --extra desktop`），或使用 `--no-gui`。"
)


def is_available() -> bool:
    return importlib.util.find_spec("nicegui") is not None


def register(application: Any, runtime: Any) -> None:
    """在 API 路由注册完成后挂载静态资源和 NiceGUI 根应用。

    ``ui.run_with`` 会把 NiceGUI 的 ``_startup``/``_shutdown`` 包在现有
    lifespan 外层；:mod:`app.server` 随后再把 Runtime 包到最外层，保证
    GUI 关闭后才关闭数据库。认证与 Host/Origin 防护由主应用中间件覆盖。
    """
    if not is_available():
        raise RuntimeError(GUI_INSTALL_HINT)
    import os
    # NiceGUI 默认在当前工作目录创建 .nicegui；GUI 状态应留在数据目录。
    os.environ.setdefault("NICEGUI_STORAGE_PATH", str(runtime.settings.get_data_path() / ".nicegui"))
    from nicegui import ui
    from fastapi.staticfiles import StaticFiles
    from indexing.services.page_render import get_page_cache_dir
    from app.platform import get_icon_path
    from app.ui import register_pages

    working_dir = runtime.settings.get_files_path() / "working"
    working_dir.mkdir(parents=True, exist_ok=True)
    application.mount("/working", StaticFiles(directory=working_dir), name="working")
    application.mount("/pages", StaticFiles(directory=get_page_cache_dir()), name="pages")

    register_pages(port=runtime.port)
    icon = get_icon_path()
    ui.run_with(
        application,
        title="Piece - 个人知识库",
        favicon=str(icon) if icon.is_file() else None,
        show_welcome_message=False,
        mount_path="/",
    )
    runtime.mark_component("gui", "mounted")


async def drain_threads() -> None:
    """NiceGUI 3.3.1 的 tear_down 不等待 I/O 线程，这里在关库前补上排空。"""
    from nicegui import run
    from indexing.utils import run_sync

    await run_sync(run.thread_pool.shutdown, wait=True, cancel_futures=True)


__all__ = ["GUI_INSTALL_HINT", "is_available", "register", "drain_threads"]
