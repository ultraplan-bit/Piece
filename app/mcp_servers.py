"""在应用事件循环中托管独立端口的 MCP 服务。"""

import asyncio
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING

import uvicorn
from starlette.responses import JSONResponse
from app.asgi_server import DrainingServer

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


class _RuntimeGate:
    def __init__(self, app, runtime):
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not self.runtime.ready:
            await JSONResponse({"error": "Piece 尚未就绪或正在停止"}, status_code=503)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _EmbeddedServer(DrainingServer):
    @contextmanager
    def capture_signals(self):
        # 进程信号只交给主服务；多个 Server 注册信号会互相覆盖。
        yield


class MCPServerManager:
    """保留两个 ASGI 应用及其 lifespan，只共享事件循环和进程资源。"""

    def __init__(self):
        self._servers: list[_EmbeddedServer] = []
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self._stop_errors: list[Exception] = []

    @property
    def servers(self):
        return tuple(self._servers)

    @property
    def tasks(self):
        return tuple(self._tasks)

    @property
    def stopping(self):
        return self._stopping

    async def start(self, services: list[tuple["FastMCP", int]], host: str, *, runtime=None) -> None:
        if self._tasks:
            return

        self._stopping = False
        self._stop_errors.clear()
        for mcp, port in services:
            application = mcp.http_app(path="/mcp", transport="streamable-http")
            if runtime is not None:
                application = _RuntimeGate(application, runtime)
            server = _EmbeddedServer(uvicorn.Config(
                application,
                host=host,
                port=port,
                lifespan="on",
                log_config=None,  # 复用应用日志，不重配全局 handler
                ws="none",
                # 超时后安全取消长连接，但必须排空同步工作才退出 lifespan。
                timeout_graceful_shutdown=5,
            ))
            self._servers.append(server)
            self._tasks.append(asyncio.create_task(
                self._serve(mcp.name, server), name=mcp.name,
            ))

        await asyncio.gather(*(
            self._wait_started(server, task)
            for server, task in zip(self._servers, self._tasks)
        ))

    async def _serve(self, name: str, server: _EmbeddedServer) -> None:
        try:
            await server.serve()
        except (Exception, SystemExit):
            # 绑定端口或启动失败只使对应 MCP 降级，不能结束整个事件循环。
            logger.exception("[MCP %s] 服务启动或运行失败", name)
            server.should_exit = True
            if server.started:
                try:
                    await server.shutdown()
                except Exception as exc:
                    self._stop_errors.append(exc)
        else:
            if not self._stopping:
                logger.error("[MCP %s] 服务意外退出", name)
        finally:
            lifespan = getattr(server, "lifespan", None)
            if lifespan is not None and getattr(lifespan, "shutdown_failed", False):
                self._stop_errors.append(RuntimeError(f"MCP {name} lifespan 停止失败"))

    async def _wait_started(self, server: _EmbeddedServer, task: asyncio.Task) -> None:
        while not server.started and not task.done():
            await asyncio.sleep(0.05)
        if server.started and not server.should_exit and not task.done() and not self._stopping:
            logger.info(
                "[MCP %s] 服务已就绪 - http://%s:%s/mcp",
                task.get_name(), server.config.host, server.config.port,
            )

    async def stop(self) -> None:
        self._stopping = True
        for server in self._servers:
            server.should_exit = True
        # 等待请求及 FastMCP lifespan；异常必须交回运行时，不能假报已安全关闭。
        results = await asyncio.gather(*self._tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        if self._stop_errors:
            raise ExceptionGroup("MCP 停止失败", self._stop_errors)
        self._tasks.clear()
        self._servers.clear()
