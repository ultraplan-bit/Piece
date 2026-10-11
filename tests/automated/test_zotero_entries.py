"""Zotero HTTP/CLI 适配：角色隔离、参数约束、请求体与确认；不连接真实服务。"""

import io
import json
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import cli
from app.client import make_envelope

# 写入要求 Zotero 10.0.5 及以上；测试统一用 10.0.6 作为具体版本。
ZOTERO_VERSION = "10.0.6"


def _stub(calls, name, errors=None, results=None):
    def fn(**kwargs):
        calls.append((name, kwargs))
        if errors is not None and name in errors:
            raise errors[name]
        return results.get(name, {"handler": name}) if results is not None else {"handler": name}
    return fn


@pytest.fixture
def zotero_calls():
    return {"read": [], "write": [], "errors": {}, "results": {}}


@pytest.fixture
def api(knowledge_base, monkeypatch, zotero_calls):
    """复用真实 ASGI/认证栈，但把 Zotero 服务函数换成记录调用的替身。"""
    from indexing.services import zotero_service as zs
    reads = ["status", "search", "list_collections", "list_tags", "get_item",
             "get_children", "get_fulltext", "export_citations", "get_links",
             "preview_import", "import_library"]
    for name in reads:
        monkeypatch.setattr(zs, name, _stub(zotero_calls["read"], name, results=zotero_calls["results"]))
    from indexing.services import zotero_write_service as write
    for name in ("authorize", "preview_changes", "apply_changes"):
        monkeypatch.setattr(write, name, _stub(zotero_calls["write"], name, zotero_calls["errors"], zotero_calls["results"]))

    from app.api import create_api, identity
    from app.runtime import BootstrapTokens
    runtime = SimpleNamespace(port=8689, ready=True, with_gui=False, with_mcp=False,
                              database_path=knowledge_base.settings.get_db_path().resolve(),
                              status=lambda: {"components": {"worker": "ready"}},
                              bootstrap_tokens=BootstrapTokens(ttl=60),
                              open_window=lambda url: None)
    client = TestClient(create_api(runtime), base_url="http://127.0.0.1:8689")
    client.headers.update({"Authorization": f"Bearer {knowledge_base.settings.api.admin_key}",
                           "X-Piece-Target": identity(runtime)["target_id"]})
    with client:
        yield client


READ_ROUTES = [
    ("zotero/status", {}),
    ("zotero/search", {"query": "x"}),
    ("zotero/collections", {}),
    ("zotero/tags", {}),
    ("zotero/item", {"item_key": "ITEM1"}),
    ("zotero/children", {"item_key": "ITEM1"}),
    ("zotero/fulltext", {"attachment_key": "ATT1"}),
    ("zotero/citations", {"item_keys": ["ITEM1"]}),
    ("zotero/links", {}),
]

WRITE_ROUTES = [
    ("zotero/authorize", {"server_id": "S1"}),
    ("zotero/changes/preview", {"changes": [{"action": "update_item", "item_key": "ITEM1"}], "server_id": "S1"}),
    ("zotero/changes/apply", {"plan_id": "PLAN1"}),
    ("zotero/preview", {}),
    ("zotero/import", {}),
]


@pytest.mark.parametrize("path,payload", READ_ROUTES)
def test_zotero_read_routes_allow_read_role(api, path, payload):
    read = {"Authorization": "Bearer read-test-key"}
    response = api.post(f"/api/v1/{path}", json=payload, headers=read)
    assert response.status_code == 200 and response.json()["success"]
    assert api.post(f"/api/v1/{path}", json=payload, headers={"Authorization": "Bearer nope"}).status_code == 401


@pytest.mark.parametrize("path,payload", WRITE_ROUTES)
def test_zotero_write_routes_reject_read_and_write(api, path, payload):
    for key in ("read-test-key", "write-test-key"):
        response = api.post(f"/api/v1/{path}", json=payload, headers={"Authorization": f"Bearer {key}"})
        assert response.status_code == 403 and response.json()["error"]["code"] == "FORBIDDEN"


def test_zotero_schema_rejects_unknown_fields_and_empty_scope(api):
    assert api.post("/api/v1/zotero/status", json={"bogus": 1}).json()["error"]["code"] == "INVALID_INPUT"
    # 显式空列表表示空范围，必须在 schema 层就报错，不能静默当整库
    assert api.post("/api/v1/zotero/preview", json={"item_keys": []}).json()["error"]["code"] == "INVALID_INPUT"
    assert api.post("/api/v1/zotero/preview", json={"collection_keys": []}).json()["error"]["code"] == "INVALID_INPUT"
    assert api.post("/api/v1/zotero/preview", json={"attachment_keys": []}).json()["error"]["code"] == "INVALID_INPUT"
    assert api.post("/api/v1/zotero/item", json={}).json()["error"]["code"] == "INVALID_INPUT"
    # changes 批次不能为空，server_id 必填
    assert api.post("/api/v1/zotero/changes/preview",
                    json={"changes": [], "server_id": "S1"}).json()["error"]["code"] == "INVALID_INPUT"
    assert api.post("/api/v1/zotero/changes/preview",
                    json={"changes": [{"action": "update_item"}]}).json()["error"]["code"] == "INVALID_INPUT"
    # 全文 limit 是字符预算，允许超过列表分页的 100 上限
    assert api.post("/api/v1/zotero/fulltext",
                    json={"attachment_key": "A1", "limit": 12000}).status_code == 200
    # 与服务契约一致的上限：键列表 50、changes 批次 50、全文 50000 字符
    assert api.post("/api/v1/zotero/preview",
                    json={"item_keys": ["K"] * 51}).json()["error"]["code"] == "INVALID_INPUT"
    assert api.post("/api/v1/zotero/changes/preview",
                    json={"changes": [{"action": "update_item"}] * 51, "server_id": "S1"}
                    ).json()["error"]["code"] == "INVALID_INPUT"
    assert api.post("/api/v1/zotero/fulltext",
                    json={"attachment_key": "A1", "limit": 50001}).json()["error"]["code"] == "INVALID_INPUT"
    assert api.post("/api/v1/zotero/fulltext",
                    json={"attachment_key": "A1", "limit": 50000}).status_code == 200


def test_zotero_scope_forwarded_to_service(api, zotero_calls):
    api.post("/api/v1/zotero/preview", json={"item_keys": ["I1", "I2"], "server_id": "S1"})
    name, kwargs = zotero_calls["read"][-1]
    assert name == "preview_import"
    assert kwargs["item_keys"] == ["I1", "I2"] and kwargs["server_id"] == "S1"
    assert kwargs["collection_keys"] is None and kwargs["attachment_keys"] is None
    api.post("/api/v1/zotero/import", json={"attachment_keys": ["A1"]})
    name, kwargs = zotero_calls["read"][-1]
    assert name == "import_library" and kwargs["attachment_keys"] == ["A1"]
    # 未传任何范围保留旧版整库导入
    api.post("/api/v1/zotero/import", json={})
    assert zotero_calls["read"][-1][1]["collection_keys"] is None


def test_zotero_read_route_forwards_arguments(api, zotero_calls):
    payload = {"query": "q", "collection_key": "C1", "tag": "t", "item_type": "journalArticle",
               "limit": 5, "offset": 2, "server_id": "S1"}
    api.post("/api/v1/zotero/search", json=payload)
    # scope 未传时由模型默认补成 title_creator_year，再原样转发给服务。
    assert zotero_calls["read"][-1] == ("search", {**payload, "scope": "title_creator_year"})
    api.post("/api/v1/zotero/search", json={**payload, "scope": "everything"})
    assert zotero_calls["read"][-1] == ("search", {**payload, "scope": "everything"})


def test_zotero_write_routes_forward_plan_only(api, zotero_calls):
    api.post("/api/v1/zotero/authorize", json={"server_id": "S1"})
    assert zotero_calls["write"][-1] == ("authorize", {"server_id": "S1"})
    changes = [{"action": "create_note", "content": "总结", "parent_item_key": "ITEM1"}]
    api.post("/api/v1/zotero/changes/preview", json={"changes": changes, "server_id": "S1"})
    assert zotero_calls["write"][-1] == ("preview_changes", {"changes": changes, "server_id": "S1"})
    # apply 只接 plan_id 与确认，不直接接可篡改的变更对象
    api.post("/api/v1/zotero/changes/apply", json={"plan_id": "P2"})
    assert zotero_calls["write"][-1] == ("apply_changes", {"plan_id": "P2", "confirmed": False})
    api.post("/api/v1/zotero/changes/apply", json={"plan_id": "P3", "confirmed": True})
    assert zotero_calls["write"][-1] == ("apply_changes", {"plan_id": "P3", "confirmed": True})


def test_zotero_status_reports_supported_version(api, zotero_calls):
    zotero_calls["results"]["status"] = {"zotero_version": ZOTERO_VERSION, "write_ready": True}
    data = api.post("/api/v1/zotero/status", json={}).json()["data"]
    assert data["zotero_version"] == "10.0.6" and data["write_ready"] is True


def test_zotero_write_error_surfaces_without_key(api, zotero_calls, knowledge_base):
    from indexing.services.errors import BusinessError
    zotero_calls["errors"]["authorize"] = BusinessError(
        "ZOTERO_WRITE_DISABLED", "Zotero 写入未开启，或版本低于 10.0.5")
    body = api.post("/api/v1/zotero/authorize", json={"server_id": "S1"}).json()
    assert body["error"]["code"] == "ZOTERO_WRITE_DISABLED" and "10.0.5" in body["message"]
    assert knowledge_base.settings.api.admin_key not in str(body)


# --- 配置开关 ---------------------------------------------------------------

def test_zotero_write_flag_defaults_off_and_old_config_compatible(tmp_path):
    from indexing import settings
    config = settings.AppSettings(data_path=str(tmp_path))
    assert config.zotero_write_enabled is False
    legacy = config.model_dump()
    legacy.pop("zotero_write_enabled")
    assert settings.AppSettings.model_validate(legacy).zotero_write_enabled is False


def test_zotero_write_flag_can_be_enabled_via_config_update(knowledge_base):
    from indexing import settings
    from indexing.services import config_service
    result = config_service.update_config({"zotero_write_enabled": True}, offline=True)
    assert "zotero_write_enabled" in result["changed"]
    assert settings.get_settings().zotero_write_enabled is True


# --- CLI -------------------------------------------------------------------

class _FakeClient:
    def __init__(self):
        self.calls = []

    def post(self, path, payload):
        self.calls.append((path, payload))
        return make_envelope(True, "ok", {"path": path})


@pytest.fixture
def cli_client(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(cli, "_client_for", lambda args: fake)
    return fake


def _run_cli(capsys, *args):
    code = cli.main([*args, "--json"])
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out.strip() else None)


def test_cli_read_commands_map_to_http(cli_client, capsys):
    code, _ = _run_cli(capsys, "zotero", "status", "--server-id", "S1")
    assert code == 0 and cli_client.calls[-1] == ("/api/v1/zotero/status", {"server_id": "S1"})
    _run_cli(capsys, "zotero", "collections")
    assert cli_client.calls[-1] == ("/api/v1/zotero/collections", {"limit": 50, "offset": 0})
    _run_cli(capsys, "zotero", "search", "deep learning", "--collection-key", "C1", "--tag", "ai", "--limit", "5")
    assert cli_client.calls[-1] == ("/api/v1/zotero/search",
                                    {"query": "deep learning", "collection_key": "C1", "tag": "ai", "limit": 5,
                                     "offset": 0, "scope": "title_creator_year"})
    _run_cli(capsys, "zotero", "search", "ultrasound", "--scope", "everything")
    assert cli_client.calls[-1] == ("/api/v1/zotero/search",
                                    {"query": "ultrasound", "limit": 20, "offset": 0, "scope": "everything"})
    _run_cli(capsys, "zotero", "item", "ITEM1")
    assert cli_client.calls[-1] == ("/api/v1/zotero/item", {"item_key": "ITEM1"})
    _run_cli(capsys, "zotero", "children", "ITEM1", "--item-type", "attachment")
    assert cli_client.calls[-1] == ("/api/v1/zotero/children",
                                    {"item_key": "ITEM1", "item_type": "attachment", "limit": 20, "offset": 0})
    _run_cli(capsys, "zotero", "fulltext", "ATT1")
    assert cli_client.calls[-1] == ("/api/v1/zotero/fulltext",
                                    {"attachment_key": "ATT1", "offset": 0, "limit": 12000})
    _run_cli(capsys, "zotero", "citations", "A", "B", "--format", "ris")
    assert cli_client.calls[-1] == ("/api/v1/zotero/citations",
                                    {"item_keys": ["A", "B"], "format": "ris", "style": "apa", "locale": "en-US"})
    _run_cli(capsys, "zotero", "links", "--item-key", "I1", "--attachment-key", "A1")
    assert cli_client.calls[-1] == ("/api/v1/zotero/links",
                                    {"item_keys": ["I1"], "attachment_keys": ["A1"], "limit": 100, "offset": 0})


def test_cli_import_forwards_selectors_and_keeps_wait(cli_client, capsys):
    code, _ = _run_cli(capsys, "zotero", "import", "--item-key", "I1", "--item-key", "I2",
                       "--collection", "组", "--server-id", "S1", "--wait")
    path, payload = cli_client.calls[-1]
    assert path == "/api/v1/zotero/import"
    assert payload["item_keys"] == ["I1", "I2"] and payload["attachment_keys"] is None
    assert payload["collection_keys"] is None and payload["collections"] == ["组"] and payload["server_id"] == "S1"
    assert code == 0  # --wait 无 task_ids 时返回初始结果，不报错


def test_cli_authorize_requires_server_id(cli_client, capsys):
    _run_cli(capsys, "zotero", "authorize", "--server-id", "S1")
    assert cli_client.calls[-1] == ("/api/v1/zotero/authorize", {"server_id": "S1"})
    with pytest.raises(SystemExit) as exc:
        cli.main(["zotero", "authorize", "--json"])
    assert exc.value.code == 2


def test_cli_write_help_mentions_version_requirement(capsys):
    with pytest.raises(SystemExit):
        cli.main(["zotero", "authorize", "--help"])
    assert "10.0.5" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["zotero", "apply-changes", "--help"])
    assert "10.0.5" in capsys.readouterr().out


def test_cli_preview_and_apply_changes(cli_client, capsys, tmp_path):
    body = tmp_path / "changes.json"
    body.write_text(json.dumps({"changes": [{"action": "update_item", "item_key": "I1", "add_tags": ["x"]}]}),
                    encoding="utf-8")
    _run_cli(capsys, "zotero", "preview-changes", "--input", str(body), "--server-id", "S1")
    assert cli_client.calls[-1] == ("/api/v1/zotero/changes/preview",
                                    {"changes": [{"action": "update_item", "item_key": "I1", "add_tags": ["x"]}],
                                     "server_id": "S1"})
    # 直接 JSON 数组也接受
    array = tmp_path / "array.json"
    array.write_text(json.dumps([{"action": "create_note", "content": "n"}]), encoding="utf-8")
    _run_cli(capsys, "zotero", "preview-changes", "--input", str(array), "--server-id", "S1")
    assert cli_client.calls[-1][1]["changes"] == [{"action": "create_note", "content": "n"}]
    # apply 使用 plan_id + --yes 确认
    _run_cli(capsys, "zotero", "apply-changes", "PLAN1", "--yes")
    assert cli_client.calls[-1] == ("/api/v1/zotero/changes/apply", {"plan_id": "PLAN1", "confirmed": True})


def test_cli_preview_changes_rejects_invalid_input(cli_client, capsys, tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    _, result = _run_cli(capsys, "zotero", "preview-changes", "--input", str(empty), "--server-id", "S1")
    assert result["error"]["code"] == "INVALID_JSON" and cli_client.calls == []


def test_cli_apply_changes_requires_confirmation(cli_client, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO())  # 非交互
    _, result = _run_cli(capsys, "zotero", "apply-changes", "PLAN1")
    assert result["error"]["code"] == "CONFIRMATION_REQUIRED" and cli_client.calls == []


def test_cli_apply_changes_interactive_yes(cli_client, capsys, monkeypatch):
    stdin = io.StringIO("yes\n")
    monkeypatch.setattr(stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdin", stdin)
    _run_cli(capsys, "zotero", "apply-changes", "PLAN1")
    assert cli_client.calls[-1] == ("/api/v1/zotero/changes/apply", {"plan_id": "PLAN1", "confirmed": True})
