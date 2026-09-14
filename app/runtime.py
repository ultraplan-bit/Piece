"""Piece 核心运行时：持有数据库、任务编排、helper 和同步服务的生命周期。

本模块不导入 GUI、托盘或 MCP；可选入口由 :mod:`app.server` 装配，并通过
``components`` 记录各自状态。配置锁与数据库锁由 ``server.main`` 在外层持有，
Runtime 只负责在锁内启动和关闭资源。
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
BOOTSTRAP_TOKEN_TTL = 60.0


class BootstrapTokens:
    """服务发起开窗用的一次性引导令牌：60 秒内单次消费，替代用户手输密码。

    令牌不是凭据的替代品——它只在进程内存中存活、验证后立即作废，
    且与 admin_key 无派生关系；手动访问浏览器界面仍走 Basic 登录。
    """

    def __init__(self, ttl: float = BOOTSTRAP_TOKEN_TTL) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._issued: dict[str, float] = {}

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = time.monotonic()
            self._issued = {key: expiry for key, expiry in self._issued.items() if expiry > now}
            self._issued[token] = now + self._ttl
        return token

    def consume(self, token: str) -> bool:
        """验证并作废令牌；过期、未知或已消费的令牌一律拒绝。"""
        if not token:
            return False
        with self._lock:
            expiry = self._issued.pop(token, None)
        return expiry is not None and expiry > time.monotonic()


class Runtime:
    def __init__(self, settings: Any, *, port: int = 8689, with_gui: bool = True, with_mcp: bool = True) -> None:
        self.settings = settings
        self.port = port
        self.with_gui = with_gui
        self.with_mcp = with_mcp
        self.ready = False
        self.database_path: Path = settings.get_db_path().resolve()
        self.components: dict[str, dict[str, Any]] = {
            "core": {"status": "stopped", "database": str(self.database_path)},
            "api": {"status": "stopped"},
            "gui": {"status": "pending" if with_gui else "disabled"},
            "mcp": {"status": "pending" if with_mcp else "disabled", "services": {}, "failures": []},
        }
        self._started = False
        self.safe_to_unlock = True
        self.shutdown_error: str | None = None
        self._shutdown_requested = threading.Event()
        self._shutdown_callback: Callable[[], Any] | None = None
        self._open_window_callback: Callable[[str], Any] | None = None
        self.bootstrap_tokens = BootstrapTokens()

    # ---- 入口回调 -----------------------------------------------------
    def set_shutdown_callback(self, callback: Callable[[], Any] | None) -> None:
        self._shutdown_callback = callback

    def request_shutdown(self) -> None:
        """托盘、信号等入口请求停止主服务；可从任意线程调用，幂等。"""
        if self._shutdown_requested.is_set():
            return
        self._shutdown_requested.set()
        self.begin_shutdown()
        callback = self._shutdown_callback
        if callback is None:
            logger.warning("[Runtime] 收到退出请求，但尚未绑定服务器")
            return
        try:
            callback()
        except Exception:
            logger.exception("[Runtime] 调度退出请求失败")

    def set_open_window_callback(self, callback: Callable[[str], Any] | None) -> None:
        self._open_window_callback = callback

    def open_window(self, url: str) -> None:
        """打开管理界面；API 层传入带一次性引导令牌的 URL 实现免登录。"""
        if self._open_window_callback is None:
            raise RuntimeError("GUI 窗口入口未启用")
        self._open_window_callback(url)

    # ---- 状态 -----------------------------------------------------------
    def mark_component(self, name: str, status: str, *, error: str | None = None, **extra: Any) -> None:
        component = self.components.setdefault(name, {})
        component["status"] = status
        if error:
            component["error"] = error
        else:
            component.pop("error", None)
        component.update(extra)

    def mark_mcp_services(self, services: dict[str, dict[str, Any]], failures: list[str]) -> None:
        mcp = self.components["mcp"]
        mcp["services"] = services
        mcp["failures"] = failures
        mcp["status"] = "degraded" if failures else "ready"

    def status(self) -> dict[str, Any]:
        """可序列化状态；MCP 服务失败表示为 degraded，而不是整体成功。"""
        components = {}
        for name, value in self.components.items():
            copied = dict(value)
            if "services" in copied:
                copied["services"] = {key: dict(item) for key, item in copied["services"].items()}
                copied["failures"] = list(copied["failures"])
            components[name] = copied
        return {"ready": self.ready, "port": self.port, "with_gui": self.with_gui,
                "with_mcp": self.with_mcp, "components": components}

    # ---- 生命周期 -------------------------------------------------------
    def mark_ready(self) -> None:
        """所有入口完成启动（或明确降级）后才对客户端宣布就绪。"""
        self.ready = True
        self.mark_component("core", "ready")
        self.mark_component("api", "ready")

    def begin_shutdown(self) -> None:
        self.ready = False
        self.mark_component("core", "stopping")
        self.mark_component("api", "stopping")

    def shutdown_failed(self, exc: BaseException) -> None:
        self.ready = False
        self.shutdown_error = f"{type(exc).__name__}: 资源未能安全停止"
        self.mark_component("core", "failed", error=self.shutdown_error)
        logger.error("[Shutdown] %s；不关闭数据库或释放独占锁", self.shutdown_error)

    async def start(self) -> None:
        if self._started:
            return
        from indexing.database import init_database, init_connection_pool
        from indexing.worker_manager import worker_manager

        self.components["core"]["status"] = "starting"
        self.safe_to_unlock = False
        self.shutdown_error = None
        try:
            init_database(self.database_path)
            init_connection_pool(self.database_path)
            from indexing.services.sync_service import get_sync_service
            get_sync_service().start()
            await worker_manager.start()
        except BaseException:
            await self._stop_resources()
            raise
        self._started = True
        logger.info("[Runtime] 核心资源已启动：%s", self.database_path)

    async def _stop_resources(self) -> None:
        """排空同步、Worker/helper 后关库；任一停止失败都保留数据库和锁。"""
        from indexing.database import close_connection_pool
        from indexing.services.sync_service import get_sync_service
        from indexing.worker_manager import worker_manager
        from indexing.utils import run_sync

        self.begin_shutdown()
        try:
            errors = []
            try:
                await run_sync(get_sync_service().stop)
            except Exception as exc:
                errors.append(exc)
            try:
                await worker_manager.stop()
            except Exception as exc:
                errors.append(exc)
            if errors:
                raise ExceptionGroup("后台资源停止失败", errors)
            close_connection_pool()
        except BaseException as exc:
            self.shutdown_failed(exc)
            raise
        self._started = False
        self.safe_to_unlock = True
        self.components["core"]["status"] = "stopped"
        self.components["api"]["status"] = "stopped"

    async def stop(self) -> None:
        if self.shutdown_error:
            raise RuntimeError(self.shutdown_error)
        if self._started:
            await self._stop_resources()

    @asynccontextmanager
    async def lifespan(self, _app: Any) -> AsyncGenerator[None]:
        await self.start()
        try:
            yield
        finally:
            await self.stop()


__all__ = ["Runtime", "BootstrapTokens"]
