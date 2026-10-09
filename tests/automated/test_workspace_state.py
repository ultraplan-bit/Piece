"""目录选择与浏览器工作区恢复的离线契约。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ui.workspace_state import (
    MIN_NAVIGATION_WIDTH, MIN_RESULTS_WIDTH, RESULTS_WIDTHS, restore_workspace, storage_key, workspace_snapshot,
)


def state():
    return {"navigation_width": 208, "search_keyword": "", "chunk_scroll_by_file": {}, "chunk_page_by_file": {}}


def test_workspace_roundtrip_preserves_catalog_and_reading_position(tmp_path):
    original = state() | {
        "selected_file_id": 12, "active_collection_ids": [3], "expanded_collection_ids": ["1", "3"],
        "file_page": 2, "file_scroll": 240, "tree_scroll": 80, "chunk_page": 3, "chunk_scroll": 460,
        "search_keyword": "研究", "sort_key": "filename", "include_descendants": False,
        "source_zoom": 125, "source_follow": False, "library_view": "compare", "file_reading_mode": "reading",
        "library_mode": "tree", "tree_page_numbers": {"root": 2, "3": 4}, "focused_tree_key": "f:3:12",
        "secret": "not-persisted", "draft": "not-persisted",
    }
    widths = RESULTS_WIDTHS | {"files": 380, "wiki": 300}
    snapshot = workspace_snapshot(original, widths)
    restored, restored_widths = state(), RESULTS_WIDTHS.copy()
    restore_workspace(snapshot, restored, restored_widths)
    assert restored_widths == widths
    assert restored["selected_file_id"] == 12
    assert restored["chunk_scroll_by_file"] == {12: 460}
    assert restored["chunk_page_by_file"] == {12: 3}
    assert restored["source_pane_open"] and restored["source_zoom"] == 125
    assert restored["active_collection_ids"] == [3] and restored["file_page"] == 2
    assert restored["search_keyword"] == "研究" and not restored["include_descendants"]
    assert restored["library_mode"] == "tree" and restored["tree_page_numbers"] == {"root": 2, "3": 4}
    assert restored["focused_tree_key"] == "f:3:12"
    assert "secret" not in str(snapshot) and "draft" not in str(snapshot)
    assert storage_key(tmp_path / "a") != storage_key(tmp_path / "b")
    assert str(tmp_path) not in storage_key(tmp_path)


@pytest.mark.parametrize("saved", [None, [], "bad", {"widths": [], "files": "bad"}])
def test_invalid_workspace_preferences_are_ignored(saved):
    values, widths = state(), RESULTS_WIDTHS.copy()
    restore_workspace(saved, values, widths)
    assert widths == RESULTS_WIDTHS
    assert values["navigation_width"] == 208


def test_preferences_validate_ids_and_never_collapse_catalog():
    values, widths = state(), RESULTS_WIDTHS.copy()
    restore_workspace({
        "widths": {"files": 0, "wiki": float("nan"), "graph": 10**1000},
        "navigation_width": True,
        "files": {"library_mode": "tree", "selected_file_id": True, "file_page": -2, "source_zoom": "huge",
                  "active_collection_ids": [True, "2", 3, -1], "expanded_collection_ids": ["bad", "2", False],
                  "search_keyword": [], "sort_key": "sql", "source_follow": "false"},
    }, values, widths)
    assert widths["files"] == MIN_RESULTS_WIDTH
    assert widths["wiki"] == RESULTS_WIDTHS["wiki"] and widths["graph"] == 800
    assert values["navigation_width"] == 208 and values["file_page"] == 1
    assert "selected_file_id" not in values
    assert values["active_collection_ids"] == [3] and values["expanded_collection_ids"] == ["2"]
    assert values["search_keyword"] == "" and "sort_key" not in values
    assert "source_follow" not in values and values["source_zoom"] == 100


@pytest.mark.parametrize("saved", [52, 120, 159, 160])
def test_restored_sidebar_width_never_reopens_in_icon_width(saved):
    values, widths = state(), RESULTS_WIDTHS.copy()
    restore_workspace({"navigation_width": saved}, values, widths)
    assert values["navigation_width"] == MIN_NAVIGATION_WIDTH


def file_handler():
    from app.ui.handlers.file_handlers import FileHandlers
    values = {"selected_file_id": 1, "filtered_files": [{"id": i} for i in range(1, 6)]}
    handler = FileHandlers(values, {})
    return handler, values


def test_modifier_selection_keeps_preview_and_extends_visible_range():
    handler, values = file_handler()
    handler.load_chunks = AsyncMock()
    asyncio.run(handler.select_file(3, additive=True))
    assert values["batch_mode"] and values["batch_selected_ids"] == {1, 3}
    handler.load_chunks.assert_not_awaited()
    asyncio.run(handler.select_file(5, extend=True))
    assert values["batch_selected_ids"] == {1, 3, 4, 5}
    assert values["selected_file_id"] == 1


def test_page_selection_preserves_other_pages_and_does_not_rebuild_rows():
    handler, values = file_handler()
    values["batch_mode"] = True
    values["batch_selected_ids"] = {99}
    refreshes = []
    handler.ui_refs["file_list_container"] = SimpleNamespace(refresh=lambda: refreshes.append(True))
    handler.toggle_select_all()
    assert handler.is_all_selected() and values["batch_selected_ids"] == {1, 2, 3, 4, 5, 99}
    handler.toggle_select_all()
    assert values["batch_selected_ids"] == {99}
    handler.toggle_file_selection(3)
    assert values["batch_selected_ids"] == {3, 99}
    assert not refreshes
