"""工具工作区的行为回归；使用临时库与真实 NiceGUI 元素，不连接外部服务。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_import_dialogs import _click, _find, _labels, _run


def _recall_state():
    return {
        "query": "old", "filenames": "", "collection_names": [], "include_descendants": True,
        "has_run": True, "results": [],
        "last_run": {"conditions": {"query": "old", "filenames": [], "collections": [], "include_descendants": True}, "at": "09:00"},
    }


def test_recall_edit_marks_old_results_without_rebuilding_them(knowledge_base):
    from nicegui import ui
    from app.ui.views import recall_test_view as module

    state, refs = _recall_state(), {}
    with ui.column() as page:
        module.render_recall_test_middle(state, refs)
        module.render_recall_test_right(state, refs)
    original_results = refs["recall_results"].targets[0].container
    query = _find(page, "Textarea")[0]

    async def edit():
        query.value = "new"
    _run(edit)

    assert module.t("recall_test.stale") in _labels(page)
    assert refs["recall_results"].targets[0].container is original_results
    assert not original_results.is_deleted
    assert query._props["aria-keyshortcuts"] == "Control+Enter Meta+Enter"
    page.delete()


def test_recall_expansion_and_raw_state_survive_view_rebuild(knowledge_base):
    from nicegui import ui
    from app.ui.views import recall_test_view as module

    state = _recall_state()
    state["results"] = [{"chunk_id": 7, "doc_title": "Card", "score": .8, "routes": [],
                         "doc": {"chunk_id": 7, "file_id": 1, "filename": "a.md", "chunk_text": "Body"}}]
    refs = {}
    with ui.column() as page:
        module.render_recall_test_right(state, refs)
    expansion = _find(page, "Expansion")[0]

    async def expand():
        expansion.value = True
    _run(expand)
    toggle = next(button for button in _find(page, "Button") if button._props.get("icon") == "code")
    _run(_click(toggle))
    state["results_scroll"] = 80
    page.clear()
    with page:
        module.render_recall_test_right(state, refs)
    assert _find(page, "Expansion")[0].value is True
    assert state["selected_result"] == "7"
    assert state["raw_results"]["7"] is True
    assert any(button._props.get("icon") == "article" for button in _find(page, "Button"))
    assert state["results_scroll"] == 80
    page.delete()


def test_recall_reports_real_pipeline_stages_and_freezes_query(monkeypatch):
    from app.ui.views import recall_test_view as module

    state, observations = _recall_state(), []
    refs = {"refresh_upload_activity": lambda: observations.append((state["is_running"], state.get("stage"), state.get("running_query")))}

    async def search(*args, **kwargs):
        state["query"] = "edited while running"
        return {"candidates": [{"chunk_id": 1, "doc_title": "Card", "score": 1}], "stats": {}}

    async def read(*args, **kwargs):
        observations.append((state["is_running"], state["stage"], state["running_query"]))
        return {"1": {"chunk_id": 1, "chunk_text": "Body"}}

    monkeypatch.setattr(module, "resolve_database_keywords", search)
    monkeypatch.setattr(module, "run_sync", read)
    asyncio.run(module._run_search(state, refs))
    assert observations[0] == (True, "recall_test.stage_search", "old")
    assert (True, "recall_test.stage_read", "old") in observations
    assert observations[-1][0] is False
    assert state["last_run"]["conditions"]["query"] == "old"
    assert module.is_stale(state)


def test_log_filters_use_visible_redacted_message_and_module():
    from app.ui.views.logs_view import filter_logs, new_log_count

    first = {"timestamp": "1", "module": "piece.sync", "message": "password=secret done"}
    second = {"timestamp": "2", "module": "piece.index", "message": "Index done"}
    assert filter_logs([first, second], "secret") == []
    assert filter_logs([first, second], "DONE", "SYNC") == [first]
    assert new_log_count([first], [first, second]) == 1
    assert new_log_count([first], [second]) == 1  # 缓冲滚动时总数可以保持不变
    assert new_log_count([first, first], [first, first, first]) == 1


def test_logs_unread_only_counts_new_filtered_rows_while_reading(monkeypatch):
    from app.ui.views import logs_view as module

    old = {"timestamp": "1", "module": "m", "message": "old"}
    new = {"timestamp": "2", "module": "m", "message": "new"}
    other = {"timestamp": "3", "module": "other", "message": "new"}
    monkeypatch.setattr(module, "get_log_buffer", lambda **kwargs: [other, new, old])
    monkeypatch.setattr(module, "count_log_buffer", lambda **kwargs: 3)
    refreshes, scrolls = [], []
    refs = {"log_filter": {"level": None}, "log_module": "m", "stats": {}, "logs_filtered": [old],
            "log_scroll": {"position": 100, "size": 1000, "container": 300},
            "logs_scroll_area": SimpleNamespace(is_deleted=False, scroll_to=lambda **kwargs: scrolls.append(kwargs)),
            "logs_new_notice": SimpleNamespace(refresh=lambda: refreshes.append(True))}
    module._refresh_logs(refs)
    assert refs["logs_unread"] == 1
    assert refs["logs_data"] == [old, new]
    assert scrolls[-1] == {"pixels": 100}
    refs["logs_data"] = [old]
    module._refresh_logs(refs)
    assert refs["logs_unread"] == 1
    assert refs["logs_data"] == [old]
    module._jump_latest(refs)
    assert refs["logs_unread"] == 0 and scrolls[-1] == {"percent": 1.0}
    assert refs["logs_data"] == [old, new]


def test_single_log_copy_stays_redacted(monkeypatch):
    from app.ui.views import logs_view as module

    copy = AsyncMock(return_value=True)
    monkeypatch.setattr(module, "write_clipboard", copy)
    monkeypatch.setattr(module.ui, "notify", lambda *args, **kwargs: None)
    asyncio.run(module._copy_entry({"message": "token=private", "module": "m"}))
    assert "private" not in copy.await_args.args[0]
    assert "token=***" in copy.await_args.args[0]


def test_mcp_pinned_client_moves_to_top_and_survives_rebuild(knowledge_base):
    from nicegui import ui
    from app.ui.views import mcp_config_view as module

    selected, refs = {"value": "cursor"}, {}
    with ui.column() as page:
        module.render_mcp_config_middle(selected, refs, lambda value: None)
    pin = next(button for button in _find(page, "Button") if button._props.get("aria-label") == module.t("mcp_config.pin") + " Zed")
    _run(_click(pin))
    assert "zed" in selected["pinned"]
    assert _labels(page).index("Zed") < _labels(page).index("Cursor")
    page.clear()
    with page:
        module.render_mcp_config_middle(selected, refs, lambda value: None)
    assert _labels(page).index("Zed") < _labels(page).index("Cursor")
    page.delete()


def test_skill_export_paths_render_relative_and_open_only_exported_ids(knowledge_base, tmp_path, monkeypatch):
    from nicegui import ui
    from app.ui.views import skill_view as module

    javascript = AsyncMock(return_value=True)
    monkeypatch.setattr(ui, "run_javascript", javascript)
    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    state = {"export_dir": str(tmp_path / "export")}
    with ui.column() as page:
        module.render_skill_right({"value": "piece-search"}, state)
    export = next(button for button in _find(page, "Button") if button._props.get("label") == module.t("skills.export_current"))
    _run(_click(export))
    assert "piece-search/SKILL.md" in _labels(page)
    assert all("SKILL.md/SKILL.md" not in value for value in _labels(page))
    opener = next(button for button in _find(page, "Button") if button._props.get("label") == module.t("skills.open_directory"))
    _run(_click(opener))
    assert '["piece-search"]' in javascript.await_args.args[0]
    page.delete()


def test_sync_failure_summary_precedes_logs_and_can_filter(knowledge_base):
    from nicegui import ui
    from app.ui.views import cloud_sync_view as module

    state = {"last_result": {"error_details": ["doc.md: timeout"]},
             "logs": [{"type": "info", "message": "started"}, {"type": "error", "message": "failed"}]}
    refs = {}
    with ui.column() as page:
        module.render_cloud_sync_right(state, refs, SimpleNamespace(clear_logs=lambda: None))
    assert _labels(page).index("doc.md: timeout") < _labels(page).index("failed")
    switch = _find(page, "Switch")[0]

    async def errors_only():
        switch.value = True
    _run(errors_only)
    assert "failed" in _labels(page) and "started" not in _labels(page)
    page.delete()


def test_transient_recall_is_counted_in_existing_task_activity():
    from app.ui.views.task_activity import active_task_count

    state = {"task_progress": {1: {}}, "uploading_count": 2}
    assert active_task_count(state, {"recall_run_state": {"is_running": True}}) == 4
    assert active_task_count(state, {"recall_run_state": {"is_running": False}}) == 3


def test_native_directory_open_rejects_foreign_origin_and_invalid_outputs(tmp_path, monkeypatch):
    from app.window_host import _DirectoryApi
    import app.window_host as module

    calls = []
    monkeypatch.setattr(module.os, "startfile", calls.append, raising=False)
    api = _DirectoryApi("http://127.0.0.1:8689/")
    api._window = SimpleNamespace(get_current_url=lambda: "https://example.com/")
    with pytest.raises(PermissionError):
        api.open_directory(str(tmp_path), ["piece-index"])
    api._window = SimpleNamespace(get_current_url=lambda: "http://127.0.0.1:8689/")
    for invalid in ([], ["../escape"], ["file; echo unsafe"]):
        with pytest.raises(ValueError):
            api.open_directory(str(tmp_path), invalid)
    with pytest.raises(FileNotFoundError):
        api.open_directory(str(tmp_path), ["piece-index"])
    assert calls == []
    target = tmp_path / "piece-index"
    target.mkdir()
    (target / "SKILL.md").write_text("Skill", encoding="utf-8")
    assert api.open_directory(str(tmp_path), ["piece-index"]) is True
    assert calls == [str(tmp_path.resolve())]


def test_native_directory_picker_only_returns_explicit_selection(tmp_path, monkeypatch):
    import sys
    from app.window_host import _DirectoryApi

    calls = []
    fake_webview = SimpleNamespace(FileDialog=SimpleNamespace(FOLDER="folder"))
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    api = _DirectoryApi("http://localhost:8689/")
    api._window = SimpleNamespace(get_current_url=lambda: "http://localhost:8689/", create_file_dialog=lambda *args, **kwargs: calls.append((args, kwargs)))
    assert api.choose_directory(str(tmp_path)) is None
    assert calls == [(("folder",), {"directory": str(tmp_path)})]
    api._window.create_file_dialog = lambda *args, **kwargs: [str(tmp_path)]
    assert api.choose_directory() == str(tmp_path.resolve())
