"""独立 Zotero MCP 的注册、边界、部分失败与同步 I/O 卸载；不访问真实 Zotero。"""

import asyncio
import json
import threading

import pytest
from fastmcp.exceptions import ToolError

from indexing import settings
from indexing.services.errors import BusinessError

READ_TOOLS = {
    "zotero_status", "zotero_search", "zotero_list_collections", "zotero_list_tags",
    "zotero_get_item", "zotero_get_children", "zotero_get_fulltext", "zotero_export_citations",
    "zotero_get_links", "zotero_preview_import", "zotero_check_tasks", "zotero_list_piece_collections",
}
WRITE_TOOLS = {"zotero_import", "zotero_authorize", "zotero_preview_changes", "zotero_apply_changes"}


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "_settings", settings.AppSettings(data_path=str(tmp_path)))


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=20))


def test_tool_ownership_annotations_and_schema():
    from indexing.mcp import server as index, zotero_server
    from retrieval import server as retrieval
    tools = zotero_server.mcp._tool_manager._tools
    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    assert not (READ_TOOLS | WRITE_TOOLS) & index.mcp._tool_manager._tools.keys()
    assert not (READ_TOOLS | WRITE_TOOLS) & retrieval.mcp._tool_manager._tools.keys()
    for name in READ_TOOLS:
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.openWorldHint is True
    assert tools["zotero_authorize"].annotations.idempotentHint is False
    assert tools["zotero_apply_changes"].annotations.destructiveHint is True
    assert tools["zotero_apply_changes"].annotations.idempotentHint is True
    assert tools["zotero_preview_changes"].annotations.readOnlyHint is True
    assert tools["zotero_preview_changes"].parameters["properties"]["changes"]["maxItems"] == 50
    assert tools["zotero_authorize"].parameters["required"] == ["server_id"]
    assert set(tools["zotero_apply_changes"].parameters["properties"]) == {"plan_id", "confirmed"}
    assert tools["zotero_get_fulltext"].parameters["properties"]["limit"]["maximum"] == 50000
    for tool in tools.values():
        assert not {"base_url", "url", "max_items", "client"} & tool.parameters["properties"].keys()


def test_read_is_offloaded_and_passes_scope(monkeypatch):
    from indexing.mcp.tools import zotero_tools
    from indexing.mcp import zotero_server as server
    calls = []

    def search(**kwargs):
        calls.append((kwargs, threading.get_ident()))
        return {"items": [], "next_offset": None}

    monkeypatch.setattr(zotero_tools, "search", search)
    result = run(server.zotero_search.fn(query="超声", collection_key="COLLROOT", limit=5, server_id="srv"))
    assert result["items"] == []
    args, thread = calls[0]
    assert args["query"] == "超声" and args["collection_key"] == "COLLROOT" and args["server_id"] == "srv"
    assert thread != threading.get_ident()


@pytest.mark.parametrize("scope", [
    {}, {"item_keys": []}, {"item_keys": ""}, {"item_keys": [None]},
    {"collection_keys": [], "item_keys": ["ITEMAAAA"]},
    {"collection_keys": ["COLLROOT"], "item_keys": ["ITEMAAAA"]},
])
def test_import_requires_one_explicit_nonempty_scope(scope):
    from indexing.mcp import zotero_server as server
    with pytest.raises(ToolError, match="INVALID_INPUT"):
        run(server.zotero_preview_import.fn(**scope))
    result = run(server.zotero_import.fn(**scope))
    assert result["success"] is False and result["error"]["code"] == "INVALID_INPUT"


def test_import_normalizes_keys_and_limits_batch(monkeypatch):
    from indexing.mcp.tools import zotero_tools
    from indexing.mcp import zotero_server as server
    calls = []

    def accepted(**kwargs):
        calls.append(kwargs)
        return {"accepted_count": 1, "task_ids": [7]}

    monkeypatch.setattr(zotero_tools.zotero, "preview_import", accepted)
    monkeypatch.setattr(zotero_tools.zotero, "import_library", accepted)
    run(server.zotero_preview_import.fn(item_keys='["ITEMAAAA", "ITEMAAAA"]'))
    result = run(server.zotero_import.fn(attachment_keys="23456789", collection_mode="none"))
    assert result["success"] is True and result["data"]["task_ids"] == [7]
    assert calls[0]["item_keys"] == ["ITEMAAAA"]
    assert calls[1]["attachment_keys"] == ["23456789"]
    assert all(call["max_items"] == 50 for call in calls)


def test_errors_keep_code_and_partial_data(monkeypatch):
    from indexing.mcp.tools import zotero_tools
    from indexing.mcp import zotero_server as server

    def failed(**kwargs):
        raise BusinessError("ZOTERO_AUTH_REQUIRED", "请明确授权", data={"plan_id": "p", "successful_count": 1})

    monkeypatch.setattr(zotero_tools, "status", failed)
    monkeypatch.setattr(zotero_tools, "authorize", failed)
    with pytest.raises(ToolError) as caught:
        run(server.zotero_status.fn())
    assert json.loads(str(caught.value))["code"] == "ZOTERO_AUTH_REQUIRED"
    result = run(server.zotero_authorize.fn(server_id="srv"))
    assert result["success"] is False and result["data"]["successful_count"] == 1


@pytest.mark.parametrize("state", [
    {"successful_count": 1, "failed_count": 1, "partial": True},
    {"successful_count": 1, "pending_count": 1, "needs_authorization": True},
])
def test_partial_write_is_not_reported_as_success(monkeypatch, state):
    from indexing.mcp.tools import zotero_tools
    from indexing.mcp import zotero_server as server
    calls = []

    def apply(**kwargs):
        calls.append(kwargs)
        return state

    monkeypatch.setattr(zotero_tools, "apply_changes", apply)
    result = run(server.zotero_apply_changes.fn(plan_id="plan", confirmed=True))
    assert result["success"] is False and result["error"]["code"] == "PARTIAL_FAILURE"
    assert result["data"] == state
    assert calls == [{"plan_id": "plan", "confirmed": True}]


def test_import_followup_stays_in_zotero_service(monkeypatch):
    from indexing.mcp import zotero_server as server
    monkeypatch.setattr(server, "get_tasks_status", lambda ids: {"task_ids": ids, "all_succeeded": False})
    monkeypatch.setattr(server, "list_collections", lambda: {"collections": [{"id": 1, "name": "论文"}]})
    assert run(server.zotero_check_tasks.fn(task_ids=[7])) == {"task_ids": [7], "all_succeeded": False}
    assert run(server.zotero_list_piece_collections.fn())["collections"][0]["name"] == "论文"
