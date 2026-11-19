"""核心资源收尾合同；故障注入不接触默认配置或外部服务。"""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.runtime import Runtime


@pytest.fixture
def resources(tmp_path, monkeypatch):
    monkeypatch.setenv("PIECE_DATA_DIR", str(tmp_path))
    from indexing import database
    from indexing.services import parser_helper, sync_service
    from indexing import worker_manager

    events = []
    sync = SimpleNamespace(start=lambda: events.append("sync-start"), stop=lambda: events.append("sync-stop"))
    worker = SimpleNamespace(start=AsyncMock(side_effect=lambda: events.append("worker-start")),
                             stop=AsyncMock(side_effect=lambda: events.append("worker-stop")),
                             request_stop=lambda: None)
    parser = SimpleNamespace(start=lambda count: events.append("parser-start"),
                             stop=lambda: events.append("parser-stop"))
    monkeypatch.setattr(parser_helper, "start_parser_helpers", parser.start)
    monkeypatch.setattr(parser_helper, "begin_parser_shutdown", lambda: None)
    monkeypatch.setattr(parser_helper, "stop_parser_helpers", lambda: parser.stop())
    monkeypatch.setattr(database, "init_database", lambda path: events.append("database-init"))
    monkeypatch.setattr(database, "init_connection_pool", lambda path: events.append("pool-init"))
    monkeypatch.setattr(database, "close_connection_pool", lambda: events.append("pool-close"))
    monkeypatch.setattr(sync_service, "get_sync_service", lambda: sync)
    monkeypatch.setattr(worker_manager, "worker_manager", worker)
    runtime = Runtime(SimpleNamespace(get_db_path=lambda: tmp_path / "kb.db",
                                      performance=SimpleNamespace(worker_concurrency=2)), with_gui=False, with_mcp=False)
    return SimpleNamespace(runtime=runtime, events=events, sync=sync, worker=worker, parser=parser)


def test_readiness_and_shutdown_order(resources):
    async def scenario():
        runtime = resources.runtime
        await runtime.start()
        assert not runtime.ready and not runtime.safe_to_unlock
        runtime.mark_ready()
        assert runtime.ready
        await runtime.stop()
        assert runtime.safe_to_unlock and not runtime.ready
        assert runtime.status()["components"]["core"]["status"] == "stopped"
        await runtime.stop()
    asyncio.run(scenario())
    assert resources.events == ["database-init", "pool-init", "sync-start", "parser-start", "worker-start",
                                 "sync-stop", "worker-stop", "parser-stop", "pool-close"]


@pytest.mark.parametrize("failed", ["sync", "worker", "parser"])
def test_stop_failure_never_closes_pool_or_claims_success(resources, failed):
    def fail():
        raise RuntimeError("injected stop failure")
    if failed == "sync":
        resources.sync.stop = fail
    elif failed == "worker":
        resources.worker.stop = AsyncMock(side_effect=fail)
    else:
        resources.parser.stop = fail

    async def scenario():
        runtime = resources.runtime
        await runtime.start()
        with pytest.raises(ExceptionGroup):
            await runtime.stop()
        assert runtime.shutdown_error and not runtime.safe_to_unlock
        assert runtime.components["core"]["status"] == "failed"
        assert "pool-close" not in resources.events
    asyncio.run(scenario())


def test_mcp_stop_failure_prevents_core_teardown(resources, monkeypatch):
    from fastapi import FastAPI
    from app import api, server
    monkeypatch.setattr(api, "create_api", lambda runtime: FastAPI())
    monkeypatch.setattr(server, "start_mcp_servers", AsyncMock())
    runtime = resources.runtime
    runtime.with_mcp = True
    manager = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("MCP shutdown failed")))
    application = server.build_application(runtime, manager=manager)

    async def scenario():
        with pytest.raises(RuntimeError):
            async with application.router.lifespan_context(application):
                assert runtime.ready
        assert not runtime.ready and not runtime.safe_to_unlock
        assert runtime.shutdown_error
        assert "pool-close" not in resources.events
    asyncio.run(scenario())


def test_failed_main_retains_both_locks(knowledge_base, monkeypatch):
    from app import server
    from app.platform import database_lock
    from indexing.settings import _get_config_file_path
    monkeypatch.setattr(server, "setup_logging", lambda: None)
    monkeypatch.setattr(server, "build_application", lambda *a, **kw: None)
    def fail(application, runtime):
        runtime.safe_to_unlock = False
        raise RuntimeError("injected shutdown failure")
    monkeypatch.setattr(server, "run_application", fail)
    before = len(server._retained_locks)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            server.main(with_gui=False, with_mcp=False, with_tray=False)
        assert len(server._retained_locks) == before + 1
        for path in (_get_config_file_path(), knowledge_base.settings.get_db_path()):
            with pytest.raises(RuntimeError, match="独占"):
                with database_lock(path):
                    pytest.fail("failed shutdown released the lock")
    finally:
        while len(server._retained_locks) > before:
            server._retained_locks.pop().close()


def test_repeated_cancellation_waits_for_sync_write():
    from indexing.utils import run_sync
    started, release = threading.Event(), threading.Event()
    events = []
    def write():
        started.set()
        assert release.wait(5)
        events.append("write-finished")

    async def scenario():
        task = asyncio.create_task(run_sync(write))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        events.append("caller-finished")
    asyncio.run(scenario())
    assert events == ["write-finished", "caller-finished"]


def test_repeated_signals_do_not_skip_lifespan(resources, monkeypatch):
    import signal
    import uvicorn
    from app.server import run_application
    from sse_starlette.sse import AppStatus
    def run(server):
        from app.asgi_server import DrainingServer
        assert isinstance(server, DrainingServer)
        server.handle_exit(signal.SIGINT, None)
        server.handle_exit(signal.SIGINT, None)
        assert server.should_exit and not server.force_exit
        server.started = True
    monkeypatch.setattr(uvicorn.Server, "run", run)
    try:
        run_application(None, resources.runtime)
    finally:
        # sse_starlette 在 import 时把 uvicorn.Server.handle_exit 换成 AppStatus.handle_exit，
        # 该包装会置位进程级类属性 AppStatus.should_exit（信号只应由真实服务进程消费）。
        # 不复位会污染同进程内后续所有 SSE 流（MCP streamable-http 的 POST 通道立即告别排空），
        # 曾导致两目录合并跑 pytest 时 test_mcp_batches 在 asyncio.run 收尾处挂死。
        AppStatus.should_exit = False


@pytest.mark.parametrize("platform, loop", [("win32", "asyncio:SelectorEventLoop"), ("linux", "auto")])
def test_service_event_loop_configuration(resources, monkeypatch, platform, loop):
    import uvicorn
    from app import server

    def run(instance):
        assert instance.config.loop == loop
        if platform == "win32":
            assert instance.config.get_loop_factory() is asyncio.SelectorEventLoop
        instance.started = True

    monkeypatch.setattr(server, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(uvicorn.Server, "run", run)
    server.run_application(None, resources.runtime)


def test_shutdown_closes_window_before_draining(resources, monkeypatch):
    import uvicorn
    from app import window
    from app.server import run_application
    events = []
    monkeypatch.setattr(window, "close_window", lambda: events.append("window-closed"))
    def run(server):
        server.servers = []
        server.lifespan = SimpleNamespace(shutdown=AsyncMock(side_effect=lambda: events.append("lifespan-stop")))
        asyncio.run(server.shutdown())
        server.started = True
    monkeypatch.setattr(uvicorn.Server, "run", run)
    run_application(None, resources.runtime)
    assert events == ["window-closed", "lifespan-stop"]


def test_runtime_closes_shared_parser_sessions(knowledge_base, monkeypatch):
    import pymupdf
    from indexing import worker_manager
    from indexing.services import parser_helper
    from indexing.utils import run_sync

    worker = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), request_stop=lambda: None)
    monkeypatch.setattr(worker_manager, "worker_manager", worker)
    pdf = knowledge_base.path / "runtime-preview.pdf"
    document = pymupdf.open()
    document.new_page()
    document.save(pdf)
    document.close()
    runtime = Runtime(knowledge_base.settings, with_gui=False, with_mcp=False)

    async def scenario():
        await runtime.start()
        session = parser_helper.ParserSession()
        try:
            assert await run_sync(session.run, "page_count", pdf) == 1
            assert await run_sync(parser_helper.run_preview_page_count, pdf) == 1
            preview = parser_helper._preview_session
            assert preview is not None
            sessions = (session, preview)
            processes = [item.process for item in sessions]
            from pathlib import Path
            directories = [Path(item.directory.name) for item in sessions if item.directory is not None]
            assert len(directories) == 2
            runtime.mark_ready()
        finally:
            await runtime.stop()
            session.close()
        assert runtime.safe_to_unlock
        assert all(process is not None and process.poll() is not None for process in processes)
        assert all(directory is not None and not directory.exists() for directory in directories)
        assert not parser_helper._sessions

    asyncio.run(scenario())


def test_mcp_manager_reports_shutdown_failure(monkeypatch):
    from app.mcp_servers import MCPServerManager
    manager = MCPServerManager()
    async def scenario():
        manager._stop_errors = [RuntimeError("lifespan failure")]
        with pytest.raises(ExceptionGroup, match="MCP"):
            await manager.stop()
    asyncio.run(scenario())
