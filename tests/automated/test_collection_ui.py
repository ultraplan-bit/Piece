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
        assert state["file_total"] == 3 and len(state["filtered_files"]) == 1
        await handler.on_collection_change([root])
        assert state["file_total"] == 2
        assert len(state["filtered_files"]) == 1
        selected = state["filtered_files"][0]["id"]
        await handler.load_chunks(selected)
        await handler.go_to_file_page(2)
        assert state["selected_file_id"] == selected
        assert len(state["files_data"]) == 2  # 当前页 + 阅读对象，不是全库
        assert selected in {f["id"] for f in state["files_data"]}
        await handler.on_descendants_change(SimpleNamespace(value=False))
        assert state["file_page"] == 1 and state["file_total"] == 0
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
    from app.ui.views.sidebar import collection_tree_nodes
    from indexing.services import collection_service as collections

    root = collections.create_collection("a/b")["collection_id"]
    child = collections.create_collection("child", parent_id=root)["collection_id"]
    nodes = collection_tree_nodes(collections.list_collections())
    assert len(nodes) == 1 and nodes[0]["id"] == str(root)
    assert nodes[0]["children"][0]["id"] == str(child)
    bad = [dict(id=1, parent_id=2, name="a", file_count=0),
           dict(id=2, parent_id=1, name="b", file_count=0)]
    with pytest.raises(ValueError, match="集合结构"):
        collection_tree_nodes(bad)
    with pytest.raises(ValueError, match="集合结构"):
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
    state["active_collection_ids"] = [root]

    with ui.column() as container:
        render_sidebar(current_view={"value": "files"}, state=state, ui_refs=refs, file_handlers=handler,
                       **{key: lambda: None for key in (
                           "switch_to_files", "switch_to_recall_test", "switch_to_cloud_sync",
                           "switch_to_mcp_config", "switch_to_skills", "switch_to_logs", "switch_to_settings")})
        render_files_middle(state, refs, handler)
        render_files_right(state, refs, chunks, handler)
        dialog = collection_manage_dialog(state["collections"], handler._create_collection,
                                          handler._rename_collection, handler._move_collection,
                                          handler._delete_collection, handler._preview_delete_collection,
                                          selected_id=root)
    assert refs["tree_element"]._props["selected"] == str(root)
    assert refs["file_pagination"] and refs["file_scroll"] and refs["chunk_scroll"]
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
