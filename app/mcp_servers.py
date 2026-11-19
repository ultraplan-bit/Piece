"""在应用事件循环中独立管理 MCP 监听与配置应用。"""

import asyncio
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import uvicorn
from starlette.responses import JSONResponse
from app.asgi_server import DrainingServer

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from indexing.settings import McpSettings

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


@dataclass
class _Listener:
    mcp: "FastMCP"
    host: str
    port: int
    server: _EmbeddedServer
    task: asyncio.Task
    stopping: bool = False

    @property
    def ready(self):
        return self.server.started and not self.server.should_exit and not self.task.done()


class MCPServerManager:
    """每个监听独立启停；共享核心资源，但不负责数据库或 Worker 的生命周期。"""

    def __init__(self):
        self._listeners: dict[str, _Listener] = {}
        self._lock = asyncio.Lock()
        self._stopping = False
        self._stop_errors: list[Exception] = []
        self._runtime = None
        self._host = "127.0.0.1"
        self._loop = None
        self._configuration_task: asyncio.Task | None = None
        self._desired_config: "McpSettings | None" = None
        self._config: "McpSettings | None" = None
        self._configuration_error: str | None = None

    @property
    def servers(self):
        return tuple(listener.server for listener in self._listeners.values())

    @property
    def tasks(self):
        return tuple(listener.task for listener in self._listeners.values())

    @property
    def stopping(self):
        return self._stopping

    async def start(self, services: list[tuple["FastMCP", int]], host: str | dict[str, str], *, runtime=None) -> None:
        async with self._lock:
            if self._stopping:
                if self._listeners or self._stop_errors or self._configuration_task is not None:
                    raise RuntimeError("MCP 服务正在停止")
                self._stopping = False
            if runtime is not None:
                self._runtime = runtime
            for mcp, port in services:
                address = host[mcp.name] if isinstance(host, dict) else host
                old = self._listeners.get(mcp.name)
                if old is not None:
                    if old.mcp is mcp and old.host == address and old.port == port and old.ready:
                        continue
                    await self._stop_listeners([mcp.name])
                self._start_listener(mcp, port, address)
            await asyncio.gather(*(self._wait_started(listener) for listener in self._listeners.values()))
            self._publish_status()

    def _start_listener(self, mcp, port, host):
        # 每次创建新的 HTTP app/session manager；不能重新进入已经结束的传输 lifespan。
        application = mcp.http_app(path="/mcp", transport="streamable-http")
        if self._runtime is not None:
            application = _RuntimeGate(application, self._runtime)
        server = _EmbeddedServer(uvicorn.Config(
            application, host=host, port=port, lifespan="on", log_config=None, ws="none",
            timeout_graceful_shutdown=5,
        ))
        task = asyncio.create_task(self._serve(mcp.name, server), name=mcp.name)
        listener = _Listener(mcp, host, port, server, task)
        self._listeners[mcp.name] = listener
        task.add_done_callback(lambda _: self._publish_status())
        return listener

    async def _serve(self, name, server):
        try:
            await server.serve()
        except (Exception, SystemExit):
            logger.exception("[MCP %s] 服务启动或运行失败", name)
            server.should_exit = True
            if server.started:
                try:
                    await server.shutdown()
                except Exception as exc:
                    self._stop_errors.append(exc)
        else:
            listener = self._listeners.get(name)
            if not self._stopping and listener is not None and not listener.stopping:
                logger.error("[MCP %s] 服务意外退出", name)
        finally:
            lifespan = getattr(server, "lifespan", None)
            if lifespan is not None and getattr(lifespan, "shutdown_failed", False):
                self._stop_errors.append(RuntimeError(f"MCP {name} lifespan 停止失败"))

    async def _wait_started(self, listener):
        while not listener.server.started and not listener.task.done():
            await asyncio.sleep(0.05)
        if listener.ready and not self._stopping:
            logger.info("[MCP %s] 服务已就绪 - http://%s:%s/mcp",
                        listener.mcp.name, listener.host, listener.port)

    async def _stop_listeners(self, names):
        listeners = [self._listeners[name] for name in names if name in self._listeners]
        for listener in listeners:
            listener.stopping = True
            listener.server.should_exit = True
        results = await asyncio.gather(*(listener.task for listener in listeners), return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise BaseExceptionGroup("MCP 停止失败", errors)
        if self._stop_errors:
            raise ExceptionGroup("MCP 停止失败", self._stop_errors)
        for listener in listeners:
            self._listeners.pop(listener.mcp.name, None)

    async def stop(self, name: str | None = None) -> None:
        """停止一个监听或整个管理器；必须排空请求和 lifespan 才能报告关闭。"""
        if name is None:
            self._stopping = True
            from indexing.services.config_service import unbind_mcp_reconfigure
            unbind_mcp_reconfigure(self.request_reconfigure)
            if self._configuration_task is not None:
                await self._configuration_task
        async with self._lock:
            await self._stop_listeners(list(self._listeners) if name is None else [name])
            if name is None:
                self._configuration_task = None
                self._desired_config = None
            else:
                self._publish_status()

    def _services(self, config):
        from indexing.settings import get_mcp_service_port
        from indexing.mcp.server import mcp as index_mcp
        from indexing.mcp.graph_server import mcp as graph_mcp
        from indexing.mcp.wiki_server import mcp as wiki_mcp
        from retrieval.server import mcp as retrieval_mcp

        services = [(retrieval_mcp, get_mcp_service_port("retrieval", config=config), self._host),
                    (index_mcp, get_mcp_service_port("index", config=config), self._host),
                    (graph_mcp, get_mcp_service_port("graph", config=config), self._host),
                    (wiki_mcp, get_mcp_service_port("wiki", config=config), self._host)]
        if config.zotero_enabled:
            from indexing.mcp.zotero_server import mcp as zotero_mcp
            services.append((zotero_mcp, config.zotero_port, "127.0.0.1"))
        return {mcp.name: (mcp, port, host) for mcp, port, host in services}

    async def configure(self, config: "McpSettings", *, host="127.0.0.1", runtime=None) -> None:
        """装配项目服务；配置提交通过 request_reconfigure 调度，不占用保存线程。"""
        self._host = host
        self._loop = asyncio.get_running_loop()
        self._config = config.model_copy(deep=True)
        services = self._services(config)
        await self.start([(mcp, port) for mcp, port, _ in services.values()],
                         host={name: address for name, (_, _, address) in services.items()}, runtime=runtime)
        if runtime is not None:
            from indexing.services.config_service import bind_mcp_reconfigure
            bind_mcp_reconfigure(self.request_reconfigure, self.service_status)
        self._publish_status()

    def request_reconfigure(self, config: "McpSettings") -> bool:
        """保存入口可能在 GUI 循环或 API 线程；只把应用工作交回服务事件循环。"""
        if self._stopping or self._loop is None or self._loop.is_closed():
            return False
        try:
            self._loop.call_soon_threadsafe(self._schedule_reconfigure, config.model_copy(deep=True))
        except RuntimeError:
            return False
        return True

    def _schedule_reconfigure(self, config):
        if self._stopping:
            return
        self._desired_config = config
        if self._configuration_task is None or self._configuration_task.done():
            self._configuration_task = asyncio.create_task(self._apply_pending(), name="MCPConfiguration")

    async def wait_configured(self):
        """等待已提交的配置；供管理 API 与隔离验收使用，不阻塞 GUI 保存回调。"""
        await asyncio.sleep(0)
        if self._configuration_task is not None:
            await self._configuration_task

    async def _apply_pending(self):
        from indexing.services.config_service import mark_mcp_applied
        from indexing.utils import run_sync
        while not self._stopping:
            config = self._desired_config
            self._configuration_error = None
            self._publish_status(applying=True)
            try:
                async with self._lock:
                    await self._reconfigure(config)
                self._config = config
                await run_sync(mark_mcp_applied, config)
            except Exception:
                self._configuration_error = "MCP 配置应用失败，已尝试恢复原监听；请查看日志"
                await run_sync(mark_mcp_applied, config, error=self._configuration_error)
                logger.exception("[MCP] 配置应用失败")
            self._publish_status()
            if config is self._desired_config:
                break

    async def _reconfigure(self, config):
        desired = self._services(config)
        changed = {name for name, old in self._listeners.items()
                   if name not in desired or desired[name] != (old.mcp, old.port, old.host) or not old.ready}
        added = set(desired) - self._listeners.keys()
        previous = [(old.mcp, old.port, old.host) for name, old in self._listeners.items() if name in changed]
        # 先排空全部受影响监听，允许端口互换；不触碰未变化的服务。
        await self._stop_listeners(changed)
        started = []
        try:
            for name, (mcp, port, host) in desired.items():
                if name in changed or name in added:
                    started.append(self._start_listener(mcp, port, host))
            await asyncio.gather(*(self._wait_started(listener) for listener in started))
            if not all(listener.ready for listener in started):
                raise RuntimeError("新的 MCP 监听未能就绪")
        except Exception:
            await self._stop_listeners([listener.mcp.name for listener in started])
            restored = [self._start_listener(mcp, port, host) for mcp, port, host in previous]
            await asyncio.gather(*(self._wait_started(listener) for listener in restored))
            if not all(listener.ready for listener in restored):
                logger.error("[MCP] 原监听未能全部恢复，服务降级运行")
            raise

    def service_status(self):
        """实际监听快照；配置页面与 Runtime 使用同一份状态来源。"""
        from indexing.mcp.config import SERVICE_NAMES
        names = {name: service for service, name in SERVICE_NAMES.items()}
        statuses = {}
        for name, listener in self._listeners.copy().items():
            service = names.get(name, name)
            state = "stopping" if listener.stopping else "ready" if listener.ready else "failed" if listener.task.done() else "starting"
            statuses[service] = {"status": state, "host": listener.host, "port": listener.port,
                                 "url": f"http://{listener.host}:{listener.port}/mcp"}
        if self._config is not None and "zotero" not in statuses:
            statuses["zotero"] = {"status": "disabled", "host": "127.0.0.1", "port": self._config.zotero_port}
        return statuses

    def _publish_status(self, *, applying=False):
        if self._runtime is None or self._stopping:
            return
        statuses = self.service_status()
        failures = [service for service, status in statuses.items() if status["status"] == "failed"]
        if self._configuration_error:
            failures.append("configuration")
        self._runtime.mark_mcp_services(statuses, failures)
        if hasattr(self._runtime, "mark_component"):
            self._runtime.mark_component("mcp", "degraded" if failures else "ready",
                                         configuration={"status": "applying" if applying else "failed" if self._configuration_error else "ready",
                                                        "error": self._configuration_error})
