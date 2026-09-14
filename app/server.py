"""Piece 常驻服务入口。

核心资源由 :class:`app.runtime.Runtime` 持有，FastAPI（``app.api``）是主应用；
GUI、MCP、托盘和窗口都是可选入口。直接执行本文件仍先委派给 CLI，避免
``--help``、helper 和窗口子进程加载日志、数据库或桌面依赖。
"""

from __future__ import annotations

import importlib.util
import logging
import multiprocessing
import os
import sys
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

if not __package__ and not getattr(sys, "frozen", False):
    sys.path[0] = str(Path(__file__).resolve().parent.parent)

multiprocessing.freeze_support()

if __name__ == "__main__":
    from app.cli import main as cli_main

    sys.exit(cli_main())

from app.logging_config import setup_logging

from app.platform import database_lock
from app.runtime import Runtime

logger = logging.getLogger(__name__)
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
UI_PORT = 8689
UI_URL = f"http://127.0.0.1:{UI_PORT}"
_is_main_process = multiprocessing.current_process().name == "MainProcess"
# 关闭失败时保持锁句柄存活，直至进程退出；不能让仍存活的线程遇到第二个写者。
_retained_locks: list[ExitStack] = []

MCP_INSTALL_HINT = (
    "MCP 依赖未安装。请运行 `uv sync --extra mcp`（完整桌面模式运行 "
    "`uv sync --extra desktop`），或使用 `--no-mcp`。"
)


def check_optional_dependencies(*, with_gui: bool, with_mcp: bool) -> None:
    """显式请求的可选入口缺依赖时直接失败，不静默降级。"""
    from app.gui import GUI_INSTALL_HINT

    if with_gui and importlib.util.find_spec("nicegui") is None:
        raise RuntimeError(GUI_INSTALL_HINT)
    if with_mcp and importlib.util.find_spec("fastmcp") is None:
        raise RuntimeError(MCP_INSTALL_HINT)


async def start_mcp_servers(runtime: Runtime, manager: Any) -> None:
    """在主事件循环中启动两个独立端口的 MCP 服务，逐个记录就绪或失败。"""
    from indexing.mcp.config import get_mcp_port
    from indexing.mcp.server import mcp as index_mcp
    from retrieval.server import mcp as retrieval_mcp

    services = [("retrieval", retrieval_mcp, runtime.settings.mcp.port), ("index", index_mcp, get_mcp_port())]
    await manager.start([(mcp, port) for _, mcp, port in services], host=MCP_HOST, runtime=runtime)

    def refresh_status(_task=None):
        if manager.stopping:
            return
        statuses, failures = {}, []
        for (name, _mcp, port), server, task in zip(services, manager.servers, manager.tasks):
            ready = bool(server.started and not server.should_exit and not task.done())
            statuses[name] = {"status": "ready" if ready else "failed", "host": MCP_HOST, "port": port,
                              "url": f"http://{MCP_HOST}:{port}/mcp"}
            if not ready:
                failures.append(name)
        runtime.mark_mcp_services(statuses, failures)

    refresh_status()
    for task in manager.tasks:
        task.add_done_callback(refresh_status)


def build_application(runtime: Runtime, *, manager: Any | None = None, on_ready: Any | None = None):
    """创建主应用并按 Runtime → (GUI) → MCP → on_ready 的顺序组合生命周期。

    ``ui.run_with`` 已把 NiceGUI ``_startup``/``_shutdown`` 包在当前 lifespan
    外层，这里捕获它后再放进 Runtime 内层：核心先启动、最后关库。MCP 启动
    失败记为 degraded；托盘/开窗等外壳失败只记录日志，不带走核心服务。
    """
    from app.api import create_api

    application = create_api(runtime)
    runtime.mark_component("api", "mounted")
    if runtime.with_gui:
        from app import gui

        gui.register(application, runtime)
    mounted_lifespan = application.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: Any):
        async with runtime.lifespan(app):
            try:
                async with mounted_lifespan(app):
                    if runtime.with_gui:
                        runtime.mark_component("gui", "ready")
                    if runtime.with_mcp:
                        try:
                            await start_mcp_servers(runtime, manager)
                        except Exception as exc:
                            runtime.mark_mcp_services({}, [f"{type(exc).__name__}: MCP 启动失败"])
                            logger.exception("[MCP] 可选 MCP 入口启动失败，服务降级运行")
                    runtime.mark_ready()
                    if on_ready is not None:
                        try:
                            on_ready()
                        except Exception:
                            logger.exception("[Shell] 可选桌面外壳启动失败，服务继续运行")
                    try:
                        yield
                    finally:
                        runtime.begin_shutdown()
                        if runtime.with_mcp and manager is not None:
                            await manager.stop()
                            runtime.mark_mcp_services({}, [])
                            runtime.mark_component("mcp", "stopped")
                if runtime.with_gui:
                    await gui.drain_threads()
                    runtime.mark_component("gui", "stopped")
            except BaseException as exc:
                # 入口尚未排空时 Runtime.lifespan 不得继续关库。
                runtime.shutdown_failed(exc)
                raise

    application.router.lifespan_context = lifespan
    return application


def run_application(application: Any, runtime: Runtime) -> None:
    import uvicorn

    from app.asgi_server import DrainingServer

    class CoreServer(DrainingServer):
        def handle_exit(self, sig, frame):
            runtime.begin_shutdown()
            super().handle_exit(sig, frame)
            # 第二次 Ctrl+C 也不能跳过资源收尾；强制终止只能由操作系统完成。
            self.force_exit = False

        async def shutdown(self, sockets=None):
            runtime.begin_shutdown()
            await super().shutdown(sockets=sockets)

    server = CoreServer(uvicorn.Config(
        application, host="127.0.0.1", port=runtime.port, lifespan="on",
        log_config=None, timeout_graceful_shutdown=5,
    ))
    if runtime.with_gui:
        # NiceGUI 3.3.1 的 run_with 不会绑定外部 Uvicorn，app.shutdown 需要它。
        from nicegui.server import Server
        Server.instance = server
    runtime.set_shutdown_callback(lambda: (runtime.begin_shutdown(), setattr(server, "should_exit", True)))
    server.run()
    if not server.started:
        raise RuntimeError("Piece 服务未能完成启动，请查看日志")
    if runtime.shutdown_error or not runtime.safe_to_unlock:
        raise RuntimeError("Piece 未能安全停止，资源和独占锁保留至进程退出，请查看日志")


def main(*, port: int | None = None, open_ui: bool = True, with_tray: bool = True,
         with_gui: bool = True, with_mcp: bool = True) -> None:
    """启动核心服务；配置锁先于 get_settings，数据库锁覆盖整个服务生命周期。"""
    global UI_PORT, UI_URL
    if port is not None:
        UI_PORT = port
    UI_URL = f"http://127.0.0.1:{UI_PORT}"
    if not _is_main_process:
        raise RuntimeError("Piece 核心服务只能在主进程启动")
    check_optional_dependencies(with_gui=with_gui, with_mcp=with_mcp)

    from indexing.settings import _get_config_file_path, get_settings

    with ExitStack() as locks:
        locks.enter_context(database_lock(_get_config_file_path()))
        setup_logging()
        first_run = not _get_config_file_path().exists()
        settings = get_settings()
        if first_run:
            logger.info("[App] 首次运行已生成管理凭据；管理界面密码位于 %s 的 api.admin_key", _get_config_file_path())
        locks.enter_context(database_lock(settings.get_db_path()))
        runtime = Runtime(settings, port=UI_PORT, with_gui=with_gui, with_mcp=with_mcp)
        manager = None
        if with_mcp:
            from app.mcp_servers import MCPServerManager

            manager = MCPServerManager()
        if with_gui:
            from app.window import open_window as open_browser_window

            runtime.set_open_window_callback(open_browser_window)

        def start_shell() -> None:
            logger.info("[App] 管理地址 %s；可用 piece open 打开，Ctrl+C 安全退出", UI_URL)
            if with_tray:
                from app import tray

                tray.start(UI_URL, on_shutdown=runtime.request_shutdown, runtime=runtime)
            if open_ui and with_gui:
                # 开窗走一次性引导令牌，用户无需查找密码登录。
                runtime.open_window(f"{UI_URL}/bootstrap?token={runtime.bootstrap_tokens.issue()}")

        application = build_application(runtime, manager=manager, on_ready=start_shell)
        try:
            run_application(application, runtime)
        finally:
            if not runtime.safe_to_unlock:
                _retained_locks.append(locks.pop_all())
                logger.critical("[Shutdown] 资源尚未停止；独占锁将保持至进程退出")
            try:
                if with_tray:
                    from app import tray
                    tray.stop()
            finally:
                if with_gui:
                    from app.window import close_window
                    close_window()


__all__ = ["Runtime", "build_application", "check_optional_dependencies", "main", "run_application", "start_mcp_servers"]
