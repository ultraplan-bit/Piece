"""文件库资料表与"连续阅读 / 卡片管理"切换的离线回归（不使用网络与真实嵌入）。"""

import asyncio
from types import SimpleNamespace

import pytest


def _state(**overrides):
    state = {
        "selected_file_id": None, "search_keyword": "", "files_data": [],
        "filtered_files": [], "chunks_data": [], "chunk_page": 1, "chunk_page_size": 50,
        "source_pane_open": False, "source_page": None,
    }
    state.update(overrides)
    return state


def _has_class(container, name):
    return any(name in element._classes for element in container.descendants())


# ==================== 资料表列值 ====================

def test_file_type_date_and_summary_helpers():
    from app.ui.views.files_view import _file_date, _file_summary, _file_type_label

    assert _file_type_label({"filename": "paper.PDF", "original_file_type": None}) == "PDF"
    assert _file_type_label({"filename": "note", "original_file_type": "docx"}) == "DOCX"
    assert _file_type_label({"filename": "no-extension"}) == "NO-EXTENSION"
    assert _file_date({"created_at": "2024-05-06 07:08:09"}) == "2024-05-06"
    assert _file_date({}) == ""
    assert _file_summary({"metadata": '{"author": ["甲", "乙"], "year": 2024}'}) == "甲, 乙 · 2024"
    assert _file_summary({"metadata": None}) == ""


# ==================== 打开阅读 / 返回资料表 ====================

def test_open_reader_loads_chunks_then_invokes_layout_callback(monkeypatch):
    from app.ui.handlers.file_handlers import FileHandlers

    state = _state()
    refs: dict = {}
    handler = FileHandlers(state, refs)
    # 默认连续阅读，处理器负责补齐这一项
    assert state["file_reading_mode"] == "reading"

    loaded, calls = [], []

    async def load(file_id, **kwargs):
        loaded.append(file_id)
        state["selected_file_id"] = file_id

    monkeypatch.setattr(handler, "load_chunks", load)
    refs["open_reader"] = lambda: calls.append("open")
    refs["open_library"] = lambda: calls.append("library")

    async def scenario():
        await handler.open_reader(7)
        handler.open_library()

    asyncio.run(scenario())
    assert loaded == [7]
    assert calls == ["open", "library"]


async def _noop():
    return None


def test_layout_callbacks_are_optional(monkeypatch):
    from app.ui.handlers.file_handlers import FileHandlers

    state = _state()
    handler = FileHandlers(state, {})
    monkeypatch.setattr(handler, "load_chunks", lambda file_id, **kwargs: _noop())
    # 未注入布局回调时不应报错，只退化为普通预览
    asyncio.run(handler.open_reader(1))
    handler.open_library()


# ==================== 阅读形态切换 ====================

def test_set_file_reading_mode_validates_and_refreshes():
    from app.ui.handlers.chunk_handlers import ChunkHandlers

    state = _state()
    refreshed = {"toggle": 0, "inspector": 0}
    refs = {
        "reading_mode_toggle": SimpleNamespace(refresh=lambda: refreshed.update(toggle=refreshed["toggle"] + 1)),
        "chunk_inspector": SimpleNamespace(refresh=lambda: refreshed.update(inspector=refreshed["inspector"] + 1)),
    }
    handler = ChunkHandlers(state, refs, lambda: None)

    handler.set_file_reading_mode("cards")
    assert state["file_reading_mode"] == "cards"
    assert refreshed == {"toggle": 1, "inspector": 1}

    # 非法值不写状态、不触发重建
    handler.set_file_reading_mode("bogus")
    assert state["file_reading_mode"] == "cards"
    assert refreshed == {"toggle": 1, "inspector": 1}


# ==================== 渲染 ====================

@pytest.mark.parametrize("batch_mode", [False, True])
def test_library_table_renders_compact_rows_not_cards(knowledge_base, batch_mode):
    from nicegui import ui
    from app.ui.handlers.file_handlers import FileHandlers
    from app.ui.views.files_view import render_files_middle
    from indexing.services import file_service

    file_service.create_empty_file("compact-table")
    state, refs = _state(library_mode="all"), {}
    handler = FileHandlers(state, refs)
    asyncio.run(handler.load_files())
    state["batch_mode"] = batch_mode
    file = state["filtered_files"][0]
    filename = "长文件名与无空格标识LongUnbrokenFilename" * 8 + ".md"
    file.update(filename=filename, original_file_type="epub", metadata='{"author":"作者", "year":2026}')
    state["collections_by_file"] = {file["id"]: ["长集合名称" * 8]}

    with ui.column() as container:
        render_files_middle(state, refs, handler)

    assert _has_class(container, "library-row")
    assert _has_class(container, "library-table-header")
    assert _has_class(container, "library-grid-batch") == batch_mode
    assert _has_class(container, "library-row-check") == batch_mode
    name = next(element for element in container.descendants() if "library-file-name" in element._classes)
    assert isinstance(name, ui.label) and name.text == filename
    metadata = next(element for element in container.descendants() if "library-row-meta" in element._classes)
    assert all(_has_class(metadata, column) for column in ("library-col-type", "library-col-status", "library-col-date"))
    assert any(getattr(element, "text", "") == "作者 · 2026" for element in container.descendants())
    assert any(getattr(element, "text", "") == "长集合名称" * 8 for element in container.descendants())
    # 不再使用大卡片式列表项
    assert not _has_class(container, "workspace-list-item")
    assert refs["file_list_container"] and refs["file_pagination"] and refs["file_scroll"]
    container.delete()


def test_reader_defaults_to_continuous_then_switches_to_cards(knowledge_base):
    from nicegui import ui
    from app.i18n import t
    from app.ui.handlers.chunk_handlers import ChunkHandlers
    from app.ui.handlers.file_handlers import FileHandlers
    from app.ui.views.files_view import render_files_right
    from indexing.services import file_service

    file_id = file_service.create_empty_file("reading-mode")["file_id"]
    file_info = file_service.get_file_by_id(file_id)
    state = _state(
        selected_file_id=file_id,
        files_data=[file_info],
        filtered_files=[file_info],
        chunks_data=[{
            "id": 987654, "doc_title": "第一节", "chunk_text": "连续阅读正文。",
            "heading_path": "reading-mode / 第1页",
        }],
        total_chunks=1, total_chunk_pages=1,
    )
    refs: dict = {}
    file_handlers = FileHandlers(state, refs)
    chunks = ChunkHandlers(state, refs, file_handlers.load_files)
    file_handlers.set_chunk_handlers(chunks)

    with ui.column() as container:
        render_files_right(state, refs, chunks, file_handlers)
    assert state["file_reading_mode"] == "reading"
    # 文档模式与列表显隐分离，工具栏不再有同义的“资料”模式。
    view_switch = next(element for element in container.descendants() if "reader-view-switch" in element._classes)
    assert [element.text for element in view_switch.descendants() if isinstance(element, ui.button)] == [
        t("library.view_reading"), t("library.view_compare"),
    ]
    list_toggles = [element for element in container.descendants()
                    if isinstance(element, ui.button) and element._props.get("aria-label") == t("workspace.results")]
    assert len(list_toggles) == 1
    # 连续阅读：文档式块，带滚动跟随锚点，且不是卡片
    assert _has_class(container, "library-reading-block")
    assert _has_class(container, "chunk-anchor")
    assert not _has_class(container, "chunk-sheet")
    assert not any(getattr(element, "text", "") in (t("files.export_working"), t("files.export_original"))
                   for element in container.descendants())
    container.delete()

    # 切到卡片管理：复用既有切片卡片
    state["file_reading_mode"] = "cards"
    with ui.column() as container:
        render_files_right(state, refs, chunks, file_handlers)
    assert _has_class(container, "chunk-sheet")
    assert not _has_class(container, "library-reading-block")
    container.delete()


# ==================== 行交互：单击预览 / 双击与回车阅读 ====================

def test_file_row_click_previews_and_double_click_opens_reader(knowledge_base):
    from nicegui import ui
    from app.i18n import t
    from app.ui.handlers.file_handlers import FileHandlers
    from app.ui.views.files_view import render_files_middle
    from indexing.services import file_service

    file_service.create_empty_file("row-interaction")
    state, refs = _state(library_mode="all"), {}
    handler = FileHandlers(state, refs)
    asyncio.run(handler.load_files())

    with ui.column() as container:
        render_files_middle(state, refs, handler)

    row = next(element for element in container.descendants()
               if "library-row" in element._classes and element._props.get("role") == "button")
    types = {listener.type for listener in row._event_listeners.values()}
    # 单击预览（click）、双击与回车打开阅读；不再用 keydown.enter 抢焦点。
    assert {"click", "dblclick", "keydown"} <= types
    assert "keydown.enter" not in types
    assert t("library.row_hint") in row._props.get("aria-label", "")
    assert "row-interaction" in row._props["aria-label"]

    # 行内"打开阅读"按钮阻断单击冒泡，避免误触预览/阅读切换。
    open_buttons = [element for element in row.descendants()
                    if isinstance(element, ui.button)
                    and element._props.get("aria-label") == t("library.open_reader")]
    assert len(open_buttons) == 1
    assert any(listener.type == "click.stop"
               for listener in open_buttons[0]._event_listeners.values())
    container.delete()

    # 批量模式下回车仍可切换选中，但不触发"打开阅读"。
    state["batch_mode"] = True
    with ui.column() as container:
        render_files_middle(state, refs, handler)
    row = next(element for element in container.descendants()
               if "library-row" in element._classes and element._props.get("role") == "button")
    types = {listener.type for listener in row._event_listeners.values()}
    assert "keydown" in types and "dblclick" not in types
    container.delete()


def test_batch_count_marks_cross_page_selection():
    from app.i18n import t
    from app.ui.views.files_view import _batch_count_text

    # 全在当前页：沿用原有计数文案
    assert _batch_count_text({1, 2}, {1, 2, 3}) == t("workspace.selected", count=2)
    # 勾选跨页：文案显式点明范围
    assert _batch_count_text({1, 3}, {1, 2}) == t("workspace.selected_cross_page", count=2)


# ==================== 每文件阅读位置保留 ====================

def _position_handler(monkeypatch, state, scrolled):
    from app.ui.handlers import file_handlers as fh
    from types import SimpleNamespace

    refs = {"chunk_scroll": SimpleNamespace(is_deleted=False,
                                            scroll_to=lambda pixels: scrolled.append(pixels))}
    handler = fh.FileHandlers(state, refs)

    async def fake_run_sync(func, *args, **kwargs):
        return {"chunks": [{"id": 9}], "total": 1, "total_pages": 1, "page": 1}

    monkeypatch.setattr(fh, "run_sync", fake_run_sync)
    return handler


def test_reading_position_is_remembered_per_file(monkeypatch):
    state = _state(selected_file_id=1, chunk_scroll=420,
                   files_data=[{"id": 1}, {"id": 2}], chunk_page_size=50)
    scrolled = []
    handler = _position_handler(monkeypatch, state, scrolled)

    asyncio.run(handler.load_chunks(2))
    assert state["chunk_scroll_by_file"][1] == 420
    assert state["chunk_scroll"] == 0

    # 模拟在文件 2 滚动后切回文件 1：恢复文件 1 的位置，并记住文件 2 的位置。
    state["chunk_scroll"] = 150
    asyncio.run(handler.load_chunks(1))
    assert state["chunk_scroll_by_file"][2] == 150
    assert state["chunk_scroll"] == 420
    assert scrolled[-1] == 420


def test_chunk_locator_overrides_saved_reading_position(monkeypatch):
    state = _state(selected_file_id=1, chunk_scroll=420,
                   files_data=[{"id": 1}], chunk_page_size=50)
    state["chunk_scroll_by_file"] = {1: 420}
    scrolled = []
    handler = _position_handler(monkeypatch, state, scrolled)

    # 知识来源定位优先于旧位置：给定 chunk_id 时回到顶部，交由锚点滚动。
    asyncio.run(handler.load_chunks(1, chunk_id=9))
    assert state["chunk_scroll"] == 0
    assert scrolled == []


def test_reading_restores_chunk_page_before_scroll(monkeypatch):
    from app.ui.handlers import file_handlers as fh

    state = _state(selected_file_id=1, chunk_page=3, chunk_scroll=420,
                   files_data=[{"id": 1}, {"id": 2}])
    handler = fh.FileHandlers(state, {})
    requests = []

    async def load(func, file_id, **kwargs):
        requests.append((file_id, kwargs["page"]))
        return {"chunks": [{"id": 9}], "total": 150, "total_pages": 3, "page": kwargs["page"]}

    monkeypatch.setattr(fh, "run_sync", load)
    asyncio.run(handler.load_chunks(2))
    asyncio.run(handler.load_chunks(1))
    assert requests == [(2, 1), (1, 3)]
    assert state["chunk_page"] == 3 and state["chunk_scroll"] == 420


def test_late_same_file_request_cannot_replace_newer_selection(monkeypatch):
    from app.ui.handlers import file_handlers as fh

    state = _state(files_data=[{"id": 1}, {"id": 2}])
    handler = fh.FileHandlers(state, {})
    pending = []

    async def load(func, file_id, **kwargs):
        future = asyncio.get_running_loop().create_future()
        pending.append(future)
        return await future

    monkeypatch.setattr(fh, "run_sync", load)

    async def scenario():
        tasks = []
        for file_id in (1, 2, 1):
            tasks.append(asyncio.create_task(handler.load_chunks(file_id)))
            await asyncio.sleep(0)
        for index in (2, 1, 0):
            pending[index].set_result({"chunks": [{"id": index}], "total": 1, "total_pages": 1, "page": 1})
            await asyncio.sleep(0)
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    assert state["selected_file_id"] == 1
    assert state["chunks_data"] == [{"id": 2}]
