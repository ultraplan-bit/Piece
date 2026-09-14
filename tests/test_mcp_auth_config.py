"""MCP 密钥分权、旧配置兼容和全部客户端配置模板回归。"""

import asyncio
import copy
import json
import shlex
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from indexing import settings
from indexing.mcp import auth
from indexing.mcp.config import get_mcp_port
from app.ui.views import mcp_config_view


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DEFAULT_DATA_PATH", tmp_path)
    monkeypatch.setattr(settings, "_settings", settings.AppSettings(data_path=str(tmp_path)))
    monkeypatch.delenv("PIECE_INDEX_MCP_PORT", raising=False)


@pytest.mark.parametrize("payload, expected", [
    ({"api_key": "legacy"}, ("legacy", "legacy")),
    ({"api_key": "legacy", "index_api_key": None}, ("legacy", "legacy")),
    ({"api_key": "read", "index_api_key": "write"}, ("read", "write")),
    ({"api_key": "read", "index_api_key": ""}, ("read", "")),
    ({"api_key": "", "index_api_key": "write"}, ("", "write")),
    ({}, ("", "")),
])
def test_key_selection_and_empty_key_semantics(payload, expected):
    settings.get_settings().mcp = settings.McpSettings(**payload)
    for service, key in zip(("retrieval", "index"), expected):
        assert settings.get_mcp_api_key(service) == key
        assert settings.is_mcp_auth_enabled(service) == bool(key)
    assert settings.get_mcp_api_key() == expected[0]


def test_new_install_generates_independent_keys(tmp_path):
    config = settings.load_settings()
    assert len(config.mcp.api_key) == 32
    assert isinstance(config.mcp.index_api_key, str)
    assert len(config.mcp.index_api_key) == 32
    assert config.mcp.api_key != config.mcp.index_api_key
    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert stored["mcp"]["api_key"] == config.mcp.api_key
    assert stored["mcp"]["index_api_key"] == config.mcp.index_api_key


def test_unknown_service_is_rejected():
    with pytest.raises(ValueError, match="未知 MCP 服务"):
        settings.McpSettings(api_key="secret").get_api_key("typo")


@pytest.mark.parametrize("service, correct, wrong", [
    ("retrieval", "read", "write"),
    ("index", "write", "read"),
])
@pytest.mark.parametrize("method", ["on_call_tool", "on_list_tools"])
def test_service_auth_rejects_cross_service_and_missing_keys(monkeypatch, service, correct, wrong, method):
    settings.get_settings().mcp = settings.McpSettings(api_key="read", index_api_key="write")
    middleware = []
    auth.apply_bearer_auth(SimpleNamespace(add_middleware=middleware.append), service)
    assert len(middleware) == 1
    handler = getattr(middleware[0], method)
    call_next = AsyncMock(return_value="allowed")

    for header in ("", f"Bearer {wrong}", "Basic abc", f"Bearer {correct}extra"):
        monkeypatch.setattr(auth, "get_http_headers", lambda h=header: {"authorization": h})
        with pytest.raises(auth.ToolError, match="Unauthorized"):
            asyncio.run(handler(None, call_next))
    call_next.assert_not_awaited()

    monkeypatch.setattr(auth, "get_http_headers", lambda: {"authorization": f"Bearer {correct}"})
    assert asyncio.run(handler(None, call_next)) == "allowed"
    call_next.assert_awaited_once()


def test_legacy_key_is_accepted_by_both_services(monkeypatch):
    settings.get_settings().mcp = settings.McpSettings(api_key="legacy")
    monkeypatch.setattr(auth, "get_http_headers", lambda: {"authorization": "Bearer legacy"})
    for service in ("retrieval", "index"):
        middleware = []
        auth.apply_bearer_auth(SimpleNamespace(add_middleware=middleware.append), service)
        assert middleware[0]._verify_token()


@pytest.mark.parametrize("service", ["retrieval", "index"])
def test_auth_switch_disables_both_services(service):
    settings.get_settings().mcp = settings.McpSettings(
        api_key="read", index_api_key="write", auth_enabled=False,
    )
    middleware = []
    auth.apply_bearer_auth(SimpleNamespace(add_middleware=middleware.append), service)
    assert not middleware
    assert not settings.is_mcp_auth_enabled(service)


def _json_servers(config):
    """从任意 JSON 层级找到两项服务，不依赖模板的包装格式。"""
    servers = {}
    if isinstance(config, dict):
        for name in ("piece-kb", "piece-index"):
            if name in config and isinstance(config[name], dict):
                servers[name] = config[name]
        if config.get("name") in ("piece-kb", "piece-index"):
            servers[config["name"]] = config
        for value in config.values():
            servers.update(_json_servers(value))
    elif isinstance(config, list):
        for value in config:
            servers.update(_json_servers(value))
    return servers


def _parse_config(client, text):
    if client.get("format") == "bash":
        servers = {}
        for command in text.split("\n\n"):
            args = shlex.split(command.replace("\\\n", ""))
            assert args[:3] == ["claude", "mcp", "add"]
            server = {"url": args[-1], "headers": {}}
            if "--header" in args:
                key, value = args[args.index("--header") + 1].split(": ", 1)
                server["headers"][key] = value
            servers[args[-2]] = server
        return servers
    if client.get("format") == "toml":
        servers = tomllib.loads(text)["mcp_servers"]
        for server in servers.values():
            assert "headers" not in server, "Codex 需要 http_headers 而非 headers"
            server["headers"] = server.pop("http_headers", {})
        return servers
    return _json_servers(json.loads(text))


@pytest.mark.parametrize("client", mcp_config_view.MCP_CLIENTS, ids=lambda c: c["id"])
@pytest.mark.parametrize("auth_enabled", [True, False])
def test_all_client_templates_use_service_specific_credentials(client, auth_enabled):
    settings.get_settings().mcp = settings.McpSettings(
        port=9230, api_key="read-key", index_api_key="write-key", auth_enabled=auth_enabled,
    )
    original = copy.deepcopy(client)
    text = mcp_config_view._get_config_json(client)
    servers = _parse_config(client, text)
    assert set(servers) == {"piece-kb", "piece-index"}
    for name, port, key in (("piece-kb", 9230, "read-key"), ("piece-index", 9231, "write-key")):
        server = servers[name]
        url = server.get("url") or server.get("serverUrl") or server.get("httpUrl")
        assert url == f"http://localhost:{port}/mcp"
        assert server.get("headers", {}).get("Authorization") == (f"Bearer {key}" if auth_enabled else None)
    if not auth_enabled:
        assert "read-key" not in text and "write-key" not in text
        assert "Authorization" not in text
    assert client == original, "生成配置不能污染全局模板"


@pytest.mark.parametrize("client_id", ["cursor", "claude_code", "openai_codex"])
@pytest.mark.parametrize("keys", [("read", ""), ("", "write"), ("legacy", None)])
def test_config_handles_empty_and_inherited_keys(client_id, keys):
    settings.get_settings().mcp = settings.McpSettings(api_key=keys[0], index_api_key=keys[1])
    client = next(c for c in mcp_config_view.MCP_CLIENTS if c["id"] == client_id)
    servers = _parse_config(client, mcp_config_view._get_config_json(client))
    for name, key in (("piece-kb", keys[0]), ("piece-index", keys[0] if keys[1] is None else keys[1])):
        assert servers[name].get("headers", {}).get("Authorization") == (f"Bearer {key}" if key else None)


@pytest.mark.parametrize("client_id", ["cursor", "claude_code", "openai_codex"])
def test_custom_keys_are_escaped_and_not_treated_as_templates(client_id):
    key = "key-'\"\\-{port}-{index_port}-密钥"
    settings.get_settings().mcp = settings.McpSettings(api_key=key, index_api_key="index")
    client = next(c for c in mcp_config_view.MCP_CLIENTS if c["id"] == client_id)
    servers = _parse_config(client, mcp_config_view._get_config_json(client))
    assert servers["piece-kb"]["headers"]["Authorization"] == f"Bearer {key}"


def test_export_and_settings_port_preview_follow_environment_override(monkeypatch):
    settings.get_settings().mcp.port = 9120
    monkeypatch.setenv("PIECE_INDEX_MCP_PORT", "9457")
    assert get_mcp_port() == get_mcp_port(9220) == 9457
    client = mcp_config_view.MCP_CLIENTS[0]
    servers = _parse_config(client, mcp_config_view._get_config_json(client))
    assert servers["piece-index"]["url"] == "http://localhost:9457/mcp"
    monkeypatch.setenv("PIECE_INDEX_MCP_PORT", "invalid")
    assert get_mcp_port() == 9121
    assert get_mcp_port(9220) == 9221
