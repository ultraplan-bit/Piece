"""原页阅读控件：页码边界、手动浏览暂停跟随、缩放及隔离元数据。"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.ui.handlers import chunk_handlers


def handler():
    return chunk_handlers.ChunkHandlers({
        "selected_file_id": 1, "source_pane_open": True, "source_follow": True,
        "source_page": {"file_id": 1, "page": 1, "total_pages": 4, "url": "/pages/1.png", "caption": "PDF"},
        "files_data": [{"id": 1, "filename": "book.md", "original_file_type": "pdf", "original_file_path": "book.pdf"}],
        "chunks_data": [{"id": 20, "heading_path": "book / 第2页"}],
    }, {}, lambda: None)


@pytest.mark.parametrize("page", [0, 5, "2", None, True])
def test_invalid_page_does_not_render_or_pause_following(monkeypatch, page):
    view = handler()
    field = SimpleNamespace(is_deleted=False, set_value=Mock())
    view.ui_refs["source_page_input"] = field
    monkeypatch.setattr(chunk_handlers.ui, "notify", Mock())
    render = AsyncMock()
    monkeypatch.setattr(view, "_show_source_page", render)
    asyncio.run(view.navigate_source(page))
    render.assert_not_awaited()
    field.set_value.assert_called_once_with("1")
    assert view.state["source_follow"] is True


def test_manual_navigation_pauses_following_and_uses_pending_target(monkeypatch):
    view = handler()
    render = AsyncMock()
    monkeypatch.setattr(view, "_show_source_page", render)
    asyncio.run(view.navigate_source(2))
    assert view.state["source_follow"] is False
    render.assert_awaited_with(2, notify_on_failure=True)
    view._source_request = {"file_id": 1, "page": 2}
    asyncio.run(view.step_source(1))
    render.assert_awaited_with(3, notify_on_failure=True)


def test_paused_follow_ignores_scroll_and_resume_uses_reading_position(monkeypatch):
    view = handler()
    view.state["source_follow"] = False
    render = AsyncMock()
    monkeypatch.setattr(view, "_show_source_page", render)
    asyncio.run(view.handle_chunk_scroll(SimpleNamespace(args={"file_id": 1, "chunk_id": 20})))
    render.assert_not_awaited()
    monkeypatch.setattr(view, "_reading_page", AsyncMock(return_value=3))
    asyncio.run(view.set_source_follow(True))
    assert view.state["source_follow"] is True
    render.assert_awaited_once_with(3)


def test_zoom_is_bounded_and_updates_existing_image():
    view = handler()
    image = SimpleNamespace(is_deleted=False, style=Mock())
    view.ui_refs["source_image"] = image
    view.set_source_zoom(400)
    assert view.state["source_zoom"] == 300
    image.style.assert_called_with("width: 300%; max-width: none")
    view.set_source_zoom(25)
    assert view.state["source_zoom"] == 50
    assert view.ui_refs["source_image"] is image


def test_render_returns_real_total_without_opening_pdf_in_ui(monkeypatch):
    pdf, png = Path("original.pdf"), Path("page.png")
    count, render = Mock(return_value=4), Mock(return_value=png)
    monkeypatch.setattr(chunk_handlers, "resolve_source_pdf", Mock(return_value=pdf))
    monkeypatch.setattr(chunk_handlers, "run_preview_page_count", count)
    monkeypatch.setattr(chunk_handlers, "render_pdf_page", render)
    assert handler()._render_source_page({}, 2) == (png, 4)
    count.assert_called_with(pdf)
    render.assert_called_once_with(pdf, 2, chunk_handlers.VIEW_DPI)
    assert handler()._render_source_page({}, 5) is None
    assert render.call_count == 1


def test_comparison_capability_uses_original_not_working_extension():
    view = handler()
    assert view.can_compare()
    view.state["files_data"][0]["original_file_type"] = None
    assert view.can_compare()
    view.state["files_data"][0]["original_file_path"] = None
    assert not view.can_compare()
