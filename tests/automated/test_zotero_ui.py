"""Zotero 工作区离线交互：隔离知识库、模拟服务，不接触真实 Zotero。"""

import argparse
import asyncio
import json
import tomllib
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.i18n import t
from indexing import settings
from test_import_dialogs import _buttons, _click, _find, _labels, _run

INFO = {"connected": True, "server_id": "SRVTEST1", "zotero_version": "10.0.6",
        "write_supported": True, "authorization": {"state": "required"}}
PAPER = {"key": "ITEMAAAA", "version": 5,
         "data": {"itemType": "journalArticle", "title": "A paper without a PDF", "date": "2024",
                  "abstractNote": "An abstract, not the full paper", "creators": [{"name": "Researcher"}]}}


def _page(items, *, offset=0, total=None, next_offset=None):
    return {"items": items, "offset": offset, "limit": 20, "total": len(items) if total is None else total,
            "next_offset": next_offset}


@pytest.fixture
def workbench(knowledge_base, monkeypatch):
    from nicegui import context, ui
    from app.ui.views import zotero_view as view

    root = context.client.content
    before = set(root.default_slot.children)

    def unexpected(*args, **kwargs):
        raise AssertionError("Unexpected Zotero operation; tests must use explicit service doubles")

    for name in ("status", "search", "list_collections", "list_tags", "get_item", "get_children",
                 "get_fulltext", "get_links", "export_citations", "preview_import", "import_library"):
        monkeypatch.setattr(view.zotero, name, unexpected)
    for name in ("authorize", "preview_changes", "apply_changes"):
        monkeypatch.setattr(view.writes, name, unexpected)
    monkeypatch.setattr(view.zotero, "status", lambda **kwargs: deepcopy(INFO))
    monkeypatch.setattr(view.zotero, "list_collections", lambda **kwargs: _page([]))
    monkeypatch.setattr(view.zotero, "search", lambda **kwargs: _page([deepcopy(PAPER)]))
    monkeypatch.setattr(view.zotero, "get_item", lambda key, **kwargs: {"item": deepcopy(PAPER)})
    notices = []
    monkeypatch.setattr(ui, "notify", lambda message, **kwargs: notices.append(message))
    with ui.column() as container:
        bench = view.ZoteroWorkbench()
        bench.render_middle()
        bench.render_right()
        yield bench, container, view, notices
    for child in list(root.default_slot.children):
        if child not in before:
            child.delete()


def _last_dialog(container):
    return _find(container.client.content, "Dialog")[-1]


def test_zotero_reading_without_pdf_does_not_import_or_authorize(workbench):
    bench, container, _, _ = workbench
    assert bench.results is None
    _run(bench.connect)
    assert PAPER["data"]["title"] in _labels(container)
    _run(lambda: bench.open_item(PAPER["key"]))
    assert PAPER["data"]["abstractNote"] in _labels(container)
    assert t("zotero.write_off") in _labels(container)
    assert not _buttons(container)[t("zotero.edit_metadata")].enabled
    assert bench.last_import is None and bench.plan is None


def test_zotero_disabled_state_keeps_actionable_error(workbench, monkeypatch):
    bench, container, view, _ = workbench
    monkeypatch.setattr(view.zotero, "status", lambda **kwargs: {
        "connected": False, "error": {"code": "ZOTERO_DISABLED", "message": "Enable local communication"}})
    _run(bench.connect)
    assert not bench.can_read and bench.results is None
    assert "ZOTERO_DISABLED" in bench.error
    assert not _buttons(container)[t("zotero.search")].enabled


def test_zotero_search_scope_and_pagination_are_explicit(workbench, monkeypatch):
    bench, _, view, _ = workbench
    bench.info = deepcopy(INFO)
    calls = []

    def search(**kwargs):
        calls.append(kwargs)
        return _page([deepcopy(PAPER)], offset=kwargs["offset"], total=45, next_offset=40)

    monkeypatch.setattr(view.zotero, "search", search)
    bench.query, bench.collection_key, bench.tag = "ultrasound", "COLLAAAA", "read"
    bench.selected[PAPER["key"]] = deepcopy(PAPER)
    _run(lambda: bench.search(20))
    assert calls == [{"query": "ultrasound", "collection_key": "COLLAAAA", "tag": "read",
                      "limit": 20, "offset": 20, "server_id": "SRVTEST1"}]
    assert bench.results["next_offset"] == 40 and not bench.selected
    bench.query = bench.tag = None
    _run(bench.search)
    assert calls[-1]["query"] == "" and calls[-1]["tag"] is None


def test_zotero_notes_render_as_text_without_active_html(workbench, monkeypatch):
    bench, container, view, _ = workbench
    bench.info = deepcopy(INFO)
    note = {"key": "NOTEAAAA", "version": 2, "data": {"itemType": "note", "parentItem": PAPER["key"],
            "note": '<p>Read &amp; compare</p><script>window.evil = true</script><img src="https://invalid.example/image">'}}
    monkeypatch.setattr(view.zotero, "get_item", lambda *args, **kwargs: {"item": note})
    _run(lambda: bench.open_item("NOTEAAAA"))
    assert "Read & compare" in _labels(container)
    assert all("window.evil" not in text for text in _labels(container))
    assert not _find(container, "Image")
    assert view.note_text("<p>First</p><p>Second</p>") == "First\n\nSecond"


def test_zotero_annotations_and_fulltext_keep_attachment_identity(workbench, monkeypatch):
    bench, container, view, _ = workbench
    bench.info = deepcopy(INFO)
    bench.current = {"key": "ATTAAAAA", "version": 3, "data": {"itemType": "attachment", "title": "PDF", "parentItem": PAPER["key"]}}
    calls = []

    def children(key, **kwargs):
        calls.append((key, kwargs))
        return _page([])

    monkeypatch.setattr(view.zotero, "get_children", children)
    monkeypatch.setattr(view.zotero, "get_fulltext", lambda key, **kwargs: {
        "content": "Indexed fragment", "coverage": {"indexedPages": 1, "totalPages": 3},
        "complete": False, "offset": 0, "next_offset": 12000})
    _run(bench.load_children)
    assert calls[0][0] == "ATTAAAAA" and calls[0][1]["server_id"] == "SRVTEST1"
    _run(bench.load_fulltext)
    assert "Indexed fragment" in _labels(container)
    assert any(t("zotero.incomplete") in value for value in _labels(container))


def test_zotero_empty_selection_never_falls_back_to_library(workbench):
    bench, container, _, notices = workbench
    bench.info = deepcopy(INFO)
    _run(lambda: bench.import_dialog(item_keys=[]))
    assert not _find(container, "Dialog")
    assert notices[-1] == t("zotero.select_first")


def test_zotero_import_uses_selected_available_attachments_once(workbench, monkeypatch):
    bench, container, view, _ = workbench
    bench.info = deepcopy(INFO)
    calls = []
    preview = {"items": [
        {"attachment_key": "ATTAAAAA", "filename": "paper.pdf", "available": True, "duplicate": False},
        {"attachment_key": "ATTBBBBB", "filename": "missing.pdf", "available": False, "reason": "file_missing"},
    ], "importable_count": 1, "duplicate_count": 0, "unavailable_count": 1, "skipped_count": 0, "skipped": []}
    monkeypatch.setattr(view.zotero, "preview_import", lambda **kwargs: deepcopy(preview))

    def import_library(**kwargs):
        calls.append(kwargs)
        return {"items": [{"file_id": 9, "filename": "paper.pdf", "file_status": "pending"}], "task_ids": [12],
                "accepted_count": 1, "duplicate_count": 0, "failed_count": 0, "skipped_count": 0, "failures": []}

    monkeypatch.setattr(view.zotero, "import_library", import_library)
    task_statuses = iter(("processing", "completed"))
    monkeypatch.setattr(view.task_service, "get_tasks_by_ids", lambda ids: [
        {"id": 12, "status": next(task_statuses), "progress": 25}
    ])
    monkeypatch.setattr(view.file_service, "get_file_by_id", lambda file_id: {"status": "pending"})
    timers = []

    def make_timer(interval, callback):
        timer = SimpleNamespace(interval=interval, callback=callback, is_deleted=False, cancelled=False)
        timer.cancel = lambda: setattr(timer, "cancelled", True)
        timers.append(timer)
        return timer

    monkeypatch.setattr(view.ui, "timer", make_timer)
    async def open_import():
        with container:
            await bench.import_dialog(item_keys=[PAPER["key"]])

    _run(open_import)
    dialog = _last_dialog(container)
    boxes = _find(dialog, "Checkbox")
    assert [box.enabled for box in boxes] == [True, False]
    button = _buttons(dialog)[t("zotero.import_selected")]
    _run(_click(button))
    _run(_click(button))
    assert calls == [{"attachment_keys": ["ATTAAAAA"], "collection_mode": "none", "collections": [],
                      "server_id": "SRVTEST1", "max_items": 50}]
    assert any("processing" in label and "25%" in label for label in _labels(dialog))
    assert t("zotero.accepted_hint") in _labels(dialog)
    assert len(timers) == 1 and timers[0].interval == 2.0
    _run(timers[0].callback)
    assert any("completed" in label for label in _labels(dialog))
    assert timers[0].cancelled


def test_zotero_note_preview_locks_instance_and_apply_only_uses_plan(workbench, monkeypatch):
    bench, container, view, notices = workbench
    settings.get_settings().zotero_write_enabled = True
    bench.info = deepcopy(INFO)
    plans, applied = [], []

    def preview(changes, **kwargs):
        plans.append((deepcopy(changes), kwargs))
        return {"plan_id": "fixed-plan", "zotero": {"server_id": "SRVTEST1"}, "changes": [
            {"action": "create_note", "key": "NEWNOTE1", "version": 0, "before": {}, "after": {"note": "Summary"}}]}

    def apply(plan_id, **kwargs):
        applied.append((plan_id, kwargs))
        return {"successful_count": 0, "unchanged_count": 0, "failed_count": 0, "pending_count": 1,
                "completed": False, "items": [{"key": "NEWNOTE1", "status": "uncertain"}]}

    monkeypatch.setattr(view.writes, "preview_changes", preview)
    monkeypatch.setattr(view.writes, "apply_changes", apply)
    bench.note_dialog(parent_key=PAPER["key"])
    draft = _last_dialog(container)
    _find(draft, "Textarea")[0].value = "Summary"
    bench.info["server_id"] = "ANOTHERINSTANCE"
    _run(_click(_buttons(draft)[t("zotero.preview_changes")]))
    assert plans[0][1] == {"server_id": "SRVTEST1"}
    assert plans[0][0][0]["parent_item_key"] == PAPER["key"]
    plan = _last_dialog(container)
    button = _buttons(plan)[t("zotero.apply")]
    _run(_click(button))
    assert applied == []
    _find(plan, "Checkbox")[0].value = True
    _run(_click(button))
    _run(_click(button))
    assert applied == [("fixed-plan", {"confirmed": True})] * 2
    assert len(plans) == 1 and bench.write_result["pending_count"] == 1
    bench.note_dialog()
    assert notices[-1] == t("zotero.pending_plan")


def test_zotero_properties_editor_preserves_structured_sources(workbench):
    _, container, _, _ = workbench
    from app.ui.components import file_properties_dialog

    sources = [{"server_id": "SRVTEST1", "item_key": "ITEMAAAA", "attachment_key": "ATTAAAAA", "item_version": 5}]
    original = {"title": "Old title", "zotero_key": "ITEMAAAA", "zotero_sources": sources}
    saved = []
    dialog = file_properties_dialog("paper.pdf", original, saved.append)
    inputs = _find(dialog, "Input")
    assert [field.value for field in inputs] == ["title", "Old title"]
    inputs[1].value = "Edited title"
    _run(_click(_buttons(dialog)[t("chunk_dialog.btn_save")]))
    assert saved == [{**original, "title": "Edited title"}]
    assert saved[0]["zotero_sources"] == sources


def test_zotero_citation_export_is_bound_to_its_selection(workbench, monkeypatch):
    bench, container, view, _ = workbench
    bench.info = deepcopy(INFO)
    calls = []

    def export(keys, **kwargs):
        calls.append((keys, kwargs))
        return {"format": "bibtex", "content": "@article{ITEMAAAA, title={Example}}"}

    monkeypatch.setattr(view.zotero, "export_citations", export)

    async def open_export():
        with container:
            await bench.citations_dialog([PAPER["key"]])

    _run(open_export)
    dialog = _last_dialog(container)
    bench.info["server_id"] = "OTHERINSTANCE"
    _run(_click(_buttons(dialog)[t("zotero.export")]))
    assert calls == [([PAPER["key"]], {"format": "bibtex", "style": "apa", "locale": "zh-CN", "server_id": "SRVTEST1"})]
    assert _find(dialog, "Code")[0].content == "@article{ITEMAAAA, title={Example}}"


def test_zotero_authorize_cli_budget_covers_zotero_dialog(monkeypatch, tmp_path):
    from app import cli

    config = SimpleNamespace(missing=False, config={"api": {"admin_key": "a" * 32}},
                             data_dir=tmp_path, db_path=tmp_path / "kb.db")
    monkeypatch.setattr(cli, "_load_config_info", lambda args: config)
    monkeypatch.delenv("PIECE_API_KEY", raising=False)
    client = cli._client_for(argparse.Namespace(zotero_command="authorize"))
    assert client.timeout >= 130 and client.api_key == "a" * 32
    assert cli._client_for(argparse.Namespace(zotero_command="search")).timeout == 10


def test_zotero_mcp_settings_and_credentials_are_independent(knowledge_base):
    from app.ui.handlers.settings_handlers import SettingsHandlers
    from indexing.services import config_service

    form = {}
    handler = SettingsHandlers(form)
    handler.init_settings_form()
    original = settings.get_settings().mcp.model_copy()
    form["mcp_zotero_enabled"] = True
    form["mcp_zotero_port"] = 12388
    form["zotero_write_enabled"] = True
    form["mcp_include_images_default"] = True
    assert handler.save_settings_form("mcp", notify=False)
    saved = config_service.get_saved_settings()
    assert not saved.mcp.zotero_enabled and not saved.zotero_write_enabled
    assert handler.dirty_groups == ("zotero",)
    assert handler.save_settings_form("zotero", notify=False)
    saved = config_service.get_saved_settings()
    assert saved.mcp.zotero_enabled and saved.mcp.zotero_port == 12388
    assert settings.get_settings().zotero_write_enabled
    assert not settings.get_settings().mcp.zotero_enabled  # 等重启，不伪装已在监听
    assert saved.mcp.api_key == original.api_key and saved.mcp.index_api_key == original.index_api_key
    assert saved.mcp.zotero_api_key not in {saved.mcp.api_key, saved.mcp.index_api_key, saved.api.admin_key}
    settings.get_settings().mcp.auth_enabled = False
    assert settings.is_mcp_auth_enabled("zotero") and not settings.is_mcp_auth_enabled("retrieval")


def test_zotero_only_mcp_config_works_for_all_client_templates(knowledge_base):
    from app.ui.views import mcp_config_view as view

    config = settings.get_settings().mcp
    config.zotero_port = 12388
    config.zotero_api_key = "z" * 32
    config.auth_enabled = False
    for client in view.MCP_CLIENTS:
        rendered = view._get_config_json(client, {"piece-zotero"})
        assert "piece-zotero" in rendered, client["id"]
        assert "piece-index" not in rendered and "piece-kb" not in rendered, client["id"]
        assert "12388" in rendered and config.zotero_api_key in rendered, client["id"]
        if client.get("format") == "toml":
            assert set(tomllib.loads(rendered)["mcp_servers"]) == {"piece-zotero"}
        elif client.get("format") != "bash":
            assert view._find_server(json.loads(rendered), "piece-zotero") is not None


def test_zotero_mcp_listener_is_loopback_even_with_public_piece_host(knowledge_base, monkeypatch):
    from app import server

    settings.get_settings().mcp.zotero_enabled = True
    monkeypatch.setattr(server, "MCP_HOST", "0.0.0.0")
    received = {}
    runtime = SimpleNamespace(settings=settings.get_settings(), mark_mcp_services=lambda statuses, failures: received.update(statuses=statuses))

    class Manager:
        stopping = False

        async def start(self, services, *, host, runtime):
            received["host"] = host
            self.servers = [SimpleNamespace(started=True, should_exit=False) for _ in services]
            self.tasks = [asyncio.get_running_loop().create_future() for _ in services]

    asyncio.run(server.start_mcp_servers(runtime, Manager()))
    assert received["host"]["piece-zotero"] == "127.0.0.1"
    assert received["host"]["piece-kb"] == "0.0.0.0"
    assert received["statuses"]["zotero"]["url"] == "http://127.0.0.1:8688/mcp"
