"""文件库层级范围、分页与 NiceGUI 元素回归，全部使用临时新库。"""

import asyncio
from types import SimpleNamespace

import pytest


def _state():
    return {"selected_file_id": None, "search_keyword": "", "files_data": [],
            "filtered_files": [], "chunks_data": [], "chunk_page": 1, "chunk_page_size": 50,
            "source_pane_open": False, "source_page": None}


def test_file_library_uses_shared_pagination_and_keeps_reader(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.handlers.file_handlers import FileHandlers
    from indexing.services import collection_service as collections, file_service as files

    notices = []
    monkeypatch.setattr(ui, "notify", lambda text, **kwargs: notices.append(text))
    root = collections.create_collection("人工智能")["collection_id"]
    child = collections.create_collection("检索/增强", parent_id=root)["collection_id"]
    sibling = collections.create_collection("知识图谱", parent_id=root)["collection_id"]
    first = files.create_empty_file("one")["file_id"]
    second = files.create_empty_file("two")["file_id"]
    loose = files.create_empty_file("loose")["file_id"]
    collections.set_file_collections(first, [child, sibling])
    collections.set_file_collections(second, [child])
    monkeypatch.setattr(files, "get_files_list", lambda *args, **kwargs: pytest.fail("GUI must not load all files"))
    state = _state()
    handler = FileHandlers(state, {})
    state["file_page_size"] = 1

    async def scenario():
        await handler.load_files()
        await handler.set_library_mode("all")
        assert state["file_total"] == 3 and len(state["filtered_files"]) == 1
        await handler.on_collection_change([root])
        assert state["tree_pages"][str(root)]["total"] == 0
        await handler.toggle_collection(child)
        assert state["tree_pages"][str(child)]["total"] == 2
        selected = state["tree_pages"][str(child)]["files"][0]["id"]
        await handler.load_chunks(selected)
        await handler.go_to_tree_page(child, 2)
        assert state["selected_file_id"] == selected
        assert len(state["files_data"]) == 3  # 两个可见分支的文档 + 独立阅读对象
        assert selected in {f["id"] for f in state["files_data"]}
        await handler.toggle_collection(child)
        assert [file["id"] for file in state["filtered_files"]] == [loose]
        assert state["selected_file_id"] == selected
        await handler.on_collection_change([], uncategorized=True)
        assert state["file_total"] == 1
        assert state["filtered_files"][0]["id"] == loose
        await handler.on_collection_change([child])
        collections.rename_collection(child, "重命名仍然选中")
        collections.move_collection(child, None)
        await handler.load_collections()
        assert state["active_collection_ids"] == [child]
        current = next(c for c in state["collections"] if c["id"] == child)
        assert current["path"] == [{"id": child, "name": "重命名仍然选中"}]
        await handler.on_search_change(SimpleNamespace(args="one"))
        assert state["file_total"] == 1 and state["file_page"] == 1
        assert state["filtered_files"][0]["id"] == first
        await handler._delete_collection(child)
        assert state["active_collection_ids"] == []
        assert files.get_file_by_id(first) and files.get_file_by_id(second)
        assert any("已删除" in str(notice) for notice in notices)

    asyncio.run(scenario())


def test_tree_uses_ids_and_rejects_disconnected_cycles(knowledge_base):
    from app.ui.resource_tree import collection_tree_nodes
    from indexing.services import collection_service as collections

    root = collections.create_collection("a/b")["collection_id"]
    child = collections.create_collection("child", parent_id=root)["collection_id"]
    nodes = collection_tree_nodes(collections.list_collections())
    assert len(nodes) == 1 and nodes[0]["id"] == str(root)
    assert nodes[0]["name"] == "a/b" and nodes[0]["file_count"] == 0
    assert nodes[0]["label"] == "a/b (0)"
    assert nodes[0]["children"][0]["id"] == str(child)
    bad = [dict(id=1, parent_id=2, name="a", file_count=0),
           dict(id=2, parent_id=1, name="b", file_count=0)]
    with pytest.raises(ValueError, match="资料集结构"):
        collection_tree_nodes(bad)
    with pytest.raises(ValueError, match="资料集结构"):
        collection_tree_nodes([dict(id=1, parent_id=99, name="a", file_count=0)])


def test_file_library_and_collection_editor_render(knowledge_base):
    from nicegui import ui
    from app.ui.components import collection_manage_dialog
    from app.ui.handlers.file_handlers import FileHandlers
    from app.ui.handlers.chunk_handlers import ChunkHandlers
    from app.ui.views.files_view import render_files_middle, render_files_right
    from app.ui.views.sidebar import render_sidebar
    from indexing.services import collection_service, file_service

    root = collection_service.create_collection("很长的集合名称" * 8)["collection_id"]
    file_id = file_service.create_empty_file("a very long filename")["file_id"]
    collection_service.set_file_collections(file_id, [root])
    state, refs = _state(), {}
    handler = FileHandlers(state, refs)
    chunks = ChunkHandlers(state=state, ui_refs=refs, on_refresh_files=handler.load_files)
    handler.set_chunk_handlers(chunks)
    asyncio.run(handler.load_files())
    asyncio.run(handler.on_collection_change([root]))

    with ui.column() as container:
        render_sidebar(current_view={"value": "files"}, state=state, ui_refs=refs, file_handlers=handler)
        render_files_middle(state, refs, handler)
        render_files_right(state, refs, chunks, handler)
        dialog = collection_manage_dialog(state["collections"], handler._create_collection,
                                          handler._rename_collection, handler._move_collection,
                                          handler._delete_collection, handler._preview_delete_collection,
                                          selected_id=root)
    assert refs["catalog_rows"][f"c:{root}"]._props["aria-selected"] == "true"
    assert not any(element._props.get("data-collection-id") for element in refs["sidebar_container"].descendants())
    assert refs["file_pagination"] and refs["file_scroll"] and refs["chunk_scroll"]
    # 资料表与阅读区提供明确的阅读形态入口；默认连续阅读
    assert refs["reading_mode_toggle"] and refs["file_list_container"]
    assert state["file_reading_mode"] == "reading"
    stats = refs["stats_label"]
    tooltip = next(element for element in refs["sidebar_container"].descendants()
                   if isinstance(element, ui.tooltip) and element.text == stats.text)
    stats.set_text("123.4 GB / 88888/99999 已索引")
    assert tooltip.text == stats.text
    dialog.delete()
    container.delete()


def test_late_file_query_cannot_overwrite_new_scope(knowledge_base, monkeypatch):
    from app.ui.handlers import file_handlers

    state = _state()
    handler = file_handlers.FileHandlers(state, {})

    async def scenario():
        first_started, finish_first = asyncio.Event(), asyncio.Event()

        async def run_sync(function, *args, **kwargs):
            if function is file_handlers.file_service.get_files_list_paginated:
                if kwargs["name"] == "old":
                    first_started.set()
                    await finish_first.wait()
                    return {"files": [{"id": 1, "filename": "old"}], "total": 1}
                return {"files": [{"id": 2, "filename": "new"}], "total": 1}
            return {}

        monkeypatch.setattr(file_handlers, "run_sync", run_sync)
        state["search_keyword"] = "old"
        old = asyncio.create_task(handler.load_file_page())
        await first_started.wait()
        state["search_keyword"] = "new"
        await handler.load_file_page()
        finish_first.set()
        await old
        assert state["filtered_files"] == [{"id": 2, "filename": "new"}]

    asyncio.run(scenario())
