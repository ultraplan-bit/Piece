"""临时知识库与真实本机 HTTP：配置保存、局部监听重配和动态鉴权。"""

import asyncio
import socket
from contextlib import ExitStack, asynccontextmanager

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from mcp.shared.exceptions import McpError

from app.mcp_servers import MCPServerManager
from app.runtime import Runtime
from indexing import settings
from indexing.mcp.auth import apply_bearer_auth
from indexing.services import config_service


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_mcp(knowledge_base, monkeypatch):
    events = []

    def service(name, kind):
        @asynccontextmanager
        async def lifespan(_):
            events.append((name, "start"))
            try:
                yield {}
            finally:
                events.append((name, "stop"))

        mcp = FastMCP(name, lifespan=lifespan)
        apply_bearer_auth(mcp, kind)

        @mcp.tool()
        async def identity() -> dict:
            return {"name": name}

        return mcp

    core = service("piece-kb", "retrieval")
    zotero = service("piece-zotero", "zotero")
    config = settings.get_settings()
    config.mcp.port, config.mcp.zotero_port = _port(), _port()
    assert settings.save_settings(config)
    runtime = Runtime(config, with_gui=False, with_mcp=True)
    runtime.mark_ready()
    manager = MCPServerManager()

    def services(config):
        result = {core.name: (core, config.port, "127.0.0.1")}
        if config.zotero_enabled:
            result[zotero.name] = (zotero, config.zotero_port, "127.0.0.1")
        return result

    monkeypatch.setattr(manager, "_services", services)
    return manager, runtime, events


async def _identity(port, key):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    transport = StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", headers=headers)
    async with Client(transport) as client:
        await client.list_tools()
        return (await client.call_tool("identity")).data["name"]


def test_zotero_enable_disable_keeps_core_and_applies_image_options(live_mcp):
    async def scenario():
        manager, runtime, _ = live_mcp
        await manager.configure(settings.get_settings().mcp, runtime=runtime)
        core = manager.tasks[0]
        try:
            result = config_service.update_config({"mcp": {"zotero_enabled": True, "include_images_default": True}})
            assert result["pending_apply"] == ["mcp"] and not result["requires_restart"]
            assert not settings.get_settings().mcp.zotero_enabled
            assert settings.get_settings().mcp.include_images_default
            await manager.wait_configured()
            assert settings.get_settings().mcp.zotero_enabled
            config = settings.get_settings().mcp
            assert await _identity(config.zotero_port, config.zotero_api_key) == "piece-zotero"
            assert manager.tasks[0] is core
            assert runtime.components["mcp"]["services"]["zotero"]["status"] == "ready"
            config_service.update_config({"mcp": {"zotero_enabled": False}})
            await manager.wait_configured()
            assert not settings.get_settings().mcp.zotero_enabled
            assert manager.tasks == (core,)
            assert runtime.components["mcp"]["services"]["zotero"]["status"] == "disabled"
            with socket.socket() as sock:
                assert sock.connect_ex(("127.0.0.1", config.zotero_port)) != 0
        finally:
            await manager.stop()

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_rapid_save_reversion_does_not_apply_stale_enable(live_mcp):
    async def scenario():
        manager, runtime, events = live_mcp
        await manager.configure(settings.get_settings().mcp, runtime=runtime)
        try:
            config_service.update_config({"mcp": {"zotero_enabled": True}})
            config_service.update_config({"mcp": {"zotero_enabled": False}})
            await manager.wait_configured()
            assert not settings.get_settings().mcp.zotero_enabled
            assert events == [("piece-kb", "start")]
            assert not config_service.show_config()["pending_apply"]
        finally:
            await manager.stop()

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_port_conflict_restores_old_listener_and_save_can_retry(live_mcp):
    from app.ui.handlers.settings_handlers import SettingsHandlers

    async def scenario():
        manager, runtime, _ = live_mcp
        config_service.update_config({"mcp": {"zotero_enabled": True}})
        settings._settings = config_service.get_saved_settings()
        await manager.configure(settings.get_settings().mcp, runtime=runtime)
        core = manager.tasks[0]
        original = settings.get_settings().mcp.zotero_port
        try:
            with socket.socket() as occupied:
                occupied.bind(("127.0.0.1", 0))
                occupied.listen(1)
                port = occupied.getsockname()[1]
                config_service.update_config({"mcp": {"zotero_port": port}})
                await manager.wait_configured()
                assert settings.get_settings().mcp.zotero_port == original
                assert runtime.components["mcp"]["configuration"]["status"] == "failed"
                assert manager.tasks[0] is core
                assert await _identity(original, settings.get_settings().mcp.zotero_api_key) == "piece-zotero"
                handler = SettingsHandlers({})
                handler.init_settings_form()
                handler.active_group = "zotero"
                assert handler.can_save and not handler.dirty
                assert handler.effect_message
            assert handler.save_settings_form("zotero", notify=False)
            await manager.wait_configured()
            assert settings.get_settings().mcp.zotero_port == port
            assert not handler.pending_apply and not handler.can_save
            assert await _identity(port, settings.get_settings().mcp.zotero_api_key) == "piece-zotero"
            assert runtime.components["mcp"]["configuration"]["status"] == "ready"
            assert manager.tasks[0] is core
        finally:
            await manager.stop()

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_key_rotation_and_auth_switch_do_not_restart_listener(live_mcp):
    async def scenario():
        manager, runtime, _ = live_mcp
        await manager.configure(settings.get_settings().mcp, runtime=runtime)
        task = manager.tasks[0]
        config = settings.get_settings().mcp
        old = config.api_key
        try:
            assert await _identity(config.port, old) == "piece-kb"
            transport = StreamableHttpTransport(f"http://127.0.0.1:{config.port}/mcp", headers={"Authorization": f"Bearer {old}"})
            async with Client(transport) as stale_session:
                await stale_session.list_tools()
                result = config_service.update_config({"mcp": {"api_key": "rotated-read-key"}})
                assert not result["requires_restart"] and not result["pending_apply"]
                with pytest.raises(McpError, match="Unauthorized"):
                    await stale_session.list_tools()
                assert (await stale_session.call_tool("identity", raise_on_error=False)).is_error
            with pytest.raises(McpError, match="Unauthorized"):
                await _identity(config.port, old)
            assert await _identity(config.port, "rotated-read-key") == "piece-kb"
            config_service.update_config({"mcp": {"auth_enabled": False}})
            assert await _identity(config.port, "") == "piece-kb"
            config_service.update_config({"mcp": {"auth_enabled": True}})
            with pytest.raises(McpError, match="Unauthorized"):
                await _identity(config.port, "")
            assert manager.tasks == (task,)
        finally:
            await manager.stop()

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_credentials_do_not_require_reindex_and_other_fields_still_apply(knowledge_base):
    config_service.update_config({"mcp": {"zotero_enabled": True}, "ocr": {"api_key": "existing-ocr-key"}})
    result = config_service.update_config({"mcp": {"include_images_default": True},
                                          "embedding": {"api_key": "updated-secret"},
                                          "ocr": {"api_key": "updated-ocr-secret"}})
    assert not result["requires_reindex"]
    assert result["requires_restart"] == ["mcp"]
    assert settings.get_settings().mcp.include_images_default
    assert settings.get_settings().embedding.api_key == "updated-secret"


def test_hot_mcp_key_cannot_reuse_pending_management_key(knowledge_base):
    from indexing.services.errors import BusinessError

    current = settings.get_settings().model_copy(deep=True)
    saved = config_service.get_saved_settings()
    with pytest.raises(BusinessError, match="当前管理凭据冲突"):
        config_service.update_config({"api": {"admin_key": "new-management-key" * 2},
                                      "mcp": {"api_key": current.api.admin_key}})
    assert config_service.get_saved_settings() == saved
    assert settings.get_settings() == current


def test_failed_start_can_retry_without_unrelated_save_restarting_mcp(live_mcp):
    async def scenario():
        manager, runtime, events = live_mcp
        try:
            with socket.socket() as occupied:
                occupied.bind(("127.0.0.1", 0))
                occupied.listen(1)
                config = settings.get_settings()
                config.mcp.port = occupied.getsockname()[1]
                assert settings.save_settings(config)
                await manager.configure(config.mcp, runtime=runtime)
                effects = config_service.show_config()
                assert effects["pending_apply"] == ["mcp"] and effects["apply_error"]
            before = list(events)
            config_service.update_config({"webdav": {"last_sync_time": "test-marker"}})
            await manager.wait_configured()
            assert events == before and not manager.servers[0].started
            config_service.update_config({"mcp": {}})
            await manager.wait_configured()
            assert await _identity(config.mcp.port, config.mcp.api_key) == "piece-kb"
            assert not config_service.show_config()["pending_apply"]
        finally:
            await manager.stop()

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_management_api_applies_real_zotero_service_without_core_restart(knowledge_base, monkeypatch):
    import httpx
    from app import api, server
    from indexing import worker_manager
    from indexing.services.zotero_client import ZoteroClient

    def reject_zotero_connection(*args, **kwargs):
        raise AssertionError("隔离验收禁止创建真实 Zotero 客户端")

    monkeypatch.setattr(ZoteroClient, "__init__", reject_zotero_connection)
    monkeypatch.setattr(server, "MCP_HOST", "127.0.0.1")
    # 所有监听分别申请端口，避免派生端口被后续临时连接占用。
    with ExitStack() as reserved:
        ports = {}
        for name in ("admin", "retrieval", "zotero", *settings.MCP_PORT_ENV):
            sock = reserved.enter_context(socket.socket())
            sock.bind(("127.0.0.1", 0))
            ports[name] = sock.getsockname()[1]
        for name, variable in settings.MCP_PORT_ENV.items():
            monkeypatch.setenv(variable, str(ports[name]))
        config = settings.get_settings()
        config.mcp.port = ports["retrieval"]
        config.mcp.zotero_port = ports["zotero"]
        assert settings.save_settings(config)
    runtime = Runtime(config, port=ports["admin"], with_gui=False, with_mcp=True)
    manager = MCPServerManager()
    application = server.build_application(runtime, manager=manager)

    async def scenario():
        async with application.router.lifespan_context(application):
            core_tasks = manager.tasks
            worker_task = worker_manager.worker_manager._task
            assert worker_task is not None
            headers = {"Authorization": f"Bearer {config.api.admin_key}",
                       "X-Piece-Target": api.identity(runtime)["target_id"]}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                         base_url=f"http://127.0.0.1:{runtime.port}", headers=headers) as client:
                response = await client.post("/api/v1/config/update", json={"patch": {"mcp": {"zotero_enabled": True}}})
                assert response.json()["success"], response.text
                await manager.wait_configured()
                assert settings.get_settings().mcp.zotero_enabled
                transport = StreamableHttpTransport(f"http://127.0.0.1:{config.mcp.zotero_port}/mcp",
                                                    headers={"Authorization": f"Bearer {config.mcp.zotero_api_key}"})
                async with Client(transport) as zotero:
                    tools = {tool.name for tool in await zotero.list_tools()}
                    assert "zotero_status" in tools and "create_file" not in tools
                    assert len(tools) == 16
                response = await client.post("/api/v1/config/update", json={"patch": {"mcp": {"zotero_enabled": False}}})
                assert response.json()["success"]
                await manager.wait_configured()
                assert manager.tasks == core_tasks
                assert worker_manager.worker_manager._task is worker_task and not worker_task.done()
                status = (await client.get("/api/v1/status")).json()["data"]
                assert status["components"]["mcp"]["services"]["zotero"]["status"] == "disabled"
        assert runtime.safe_to_unlock and not manager.tasks

    asyncio.run(asyncio.wait_for(scenario(), 60))
