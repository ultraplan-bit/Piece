"""统一资源树：直系内容、按需分页、别名身份和就地资料集操作。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ui.handlers.file_handlers import FileHandlers
from app.ui.resource_tree import document_key, visible_tree_rows
from indexing.services import collection_service as collections, file_service as files


def _handler():
    state = {"selected_file_id": None, "search_keyword": "", "files_data": [], "filtered_files": [],
             "chunks_data": [], "chunk_scroll": 0, "source_pane_open": False, "source_page": None}
    return FileHandlers(state, {})


def _fixture():
    root = collections.create_collection("项目")["collection_id"]
    child = collections.create_collection("子资料集", parent_id=root)["collection_id"]
    direct = files.create_empty_file("本层文档", collection_ids=[root])["file_id"]
    nested = files.create_empty_file("下层文档", collection_ids=[child])["file_id"]
    loose = files.create_empty_file("未归类文档")["file_id"]
    return root, child, direct, nested, loose


def test_tree_only_queries_visible_branches_and_direct_documents(knowledge_base, monkeypatch):
    root, child, direct, nested, loose = _fixture()
    handler = _handler()
    calls = []
    original = files.get_files_list_paginated
    def read(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(files, "get_files_list_paginated", read)

    async def scenario():
        await handler.load_files()
        assert [file["id"] for file in handler.state["filtered_files"]] == [loose]
        assert len(calls) == 1 and calls[0]["uncategorized"]
        await handler.toggle_collection(root)
        assert {file["id"] for file in handler.state["filtered_files"]} == {direct, loose}
        assert len(calls) == 2 and calls[-1]["collection_ids"] == [root]
        await handler.toggle_collection(child)
        assert {file["id"] for file in handler.state["filtered_files"]} == {direct, nested, loose}
        assert len(calls) == 3 and calls[-1]["collection_ids"] == [child]
        await handler.toggle_collection(root)
        assert [file["id"] for file in handler.state["filtered_files"]] == [loose]
        assert handler.state["tree_pages"].keys() == {"root"}
    asyncio.run(scenario())
    assert all(call["include_descendants"] is False for call in calls)


def test_tree_aliases_have_distinct_rows_but_one_document_identity(knowledge_base):
    root, child, direct, nested, loose = _fixture()
    collections.update_file_collections([direct], [child], mode="add")
    handler = _handler()
    handler.state["expanded_collection_ids"] = [str(root), str(child)]
    asyncio.run(handler.load_files())
    rows = [row for row in visible_tree_rows(handler.state) if row["kind"] == "file"]
    aliases = [row for row in rows if row["file"]["id"] == direct]
    assert {row["key"] for row in aliases} == {document_key(root, direct), document_key(child, direct)}
    assert len(handler.state["filtered_files"]) == 3
    handler.state["visible_file_rows"] = [(row["key"], row["file"]["id"]) for row in rows]
    handler.select_all_visible()
    assert handler.state["batch_selected_ids"] == {direct, nested, loose}


def test_branch_paging_and_flat_modes_keep_reader(knowledge_base):
    root, child, direct, nested, loose = _fixture()
    for index in range(4):
        files.create_empty_file(f"extra-{index}", collection_ids=[root])
    handler = _handler()
    handler.state.update(file_page_size=2, selected_file_id=nested, chunk_scroll=125, source_pane_open=True)
    handler.state["expanded_collection_ids"] = [str(root)]
    async def scenario():
        await handler.load_files()
        branch = handler.state["tree_pages"][str(root)]
        assert branch["total"] == 5 and len(branch["files"]) == 2
        first = {file["id"] for file in branch["files"]}
        await handler.go_to_tree_page(root, 2)
        assert not first.intersection(file["id"] for file in handler.state["tree_pages"][str(root)]["files"])
        assert handler.state["selected_file_id"] == nested and handler.state["chunk_scroll"] == 125
        assert handler.state["source_pane_open"]
        await handler.set_library_mode("all")
        assert handler.state["file_total"] == 7
        await handler.set_library_mode("uncategorized")
        assert [file["id"] for file in handler.state["filtered_files"]] == [loose]
    asyncio.run(scenario())


def test_collections_and_documents_share_one_tree_not_the_sidebar(knowledge_base):
    from nicegui import ui
    from app.ui.views.files_view import render_files_middle
    from app.ui.views.sidebar import render_sidebar

    root, child, direct, nested, loose = _fixture()
    handler = _handler()
    handler.state["expanded_collection_ids"] = [str(root), str(child)]
    asyncio.run(handler.load_files())
    with ui.column() as container:
        render_sidebar({"value": "files"}, handler.ui_refs, handler.state, handler)
        render_files_middle(handler.state, handler.ui_refs, handler)
    sidebar = handler.ui_refs["sidebar_container"]
    assert not any(element._props.get("data-collection-id") for element in sidebar.descendants())
    rows = handler.ui_refs["catalog_rows"]
    assert {f"c:{root}", f"c:{child}", document_key(root, direct), document_key(child, nested), document_key(None, loose)} == set(rows)
    assert rows[document_key(child, nested)]._props["data-parent-row"] == f"c:{child}"
    assert rows[f"c:{child}"]._props["data-parent-row"] == f"c:{root}"
    assert all(row._props["role"] == "treeitem" for row in rows.values())
    assert all("resource-file-row" in row.classes for key, row in rows.items() if key.startswith("f:"))
    assert sum(row._props["tabindex"] == "0" for row in rows.values()) == 1
    container.delete()


def test_navigation_and_mode_controls_keep_identity_during_updates(knowledge_base):
    from nicegui import ui
    from app.i18n import t
    from app.ui.views.files_view import render_files_middle
    from app.ui.views.sidebar import render_app_header, render_sidebar

    root, *_ = _fixture()
    handler = _handler()
    asyncio.run(handler.load_files())
    view = {"value": "files"}
    with ui.column() as container:
        header = render_app_header(view, lambda view: None)
        sidebar = render_sidebar(view, handler.ui_refs, handler.state, handler)
        render_files_middle(handler.state, handler.ui_refs, handler)
    nav_ids = [element.id for element in handler.ui_refs["sidebar_container"].descendants()
               if "workspace-nav-item" in element.classes]
    header_element = next(element for element in container.descendants() if "app-header" in element.classes)
    header_ids = [element.id for element in header_element.descendants() if isinstance(element, ui.button)]
    modes = handler.ui_refs["library_mode_buttons"]
    handler.select_collection(root)
    assert handler.ui_refs["library_mode_buttons"] is modes
    assert all(not button.is_deleted for button in modes.values())
    assert modes["tree"]._props["aria-pressed"] == "true"
    for destination in ("wiki", "settings", "files"):
        view["value"] = destination
        sidebar.refresh()
        header.refresh()
        assert nav_ids == [element.id for element in handler.ui_refs["sidebar_container"].descendants()
                           if "workspace-nav-item" in element.classes]
        assert not header_element.is_deleted
        assert header_ids == [element.id for element in header_element.descendants() if isinstance(element, ui.button)]
        title = next(element for element in header_element.descendants()
                     if isinstance(element, ui.label) and "app-view-title" in element.classes)
        assert title.text == t(f"sidebar.{destination}")
        search = next(element for element in header_element.descendants()
                      if isinstance(element, ui.button) and element._props.get("icon") == "search")
        assert search.visible == (destination != "settings")
    container.delete()


def test_inline_edit_preserves_invalid_name_and_does_not_reset_reader(knowledge_base, monkeypatch):
    from app.ui.handlers import file_handlers
    monkeypatch.setattr(file_handlers.ui, "run_javascript", lambda *a, **k: None)
    root, child, direct, nested, loose = _fixture()
    handler = _handler()
    handler.state.update(selected_file_id=direct, chunk_scroll=88)
    async def scenario():
        await handler.load_files()
        await handler.begin_collection_edit(parent_id=root)
        edit = handler.state["collection_edit"]
        await handler.commit_collection_edit("子资料集", expected=edit)
        assert handler.state["collection_edit"] is edit and edit["error"]
        assert edit["name"] == "子资料集"
        await handler.commit_collection_edit("新资料集", expected=edit)
        assert handler.state["collection_edit"] is None
        created = next(item for item in handler.state["collections"] if item["name"] == "新资料集")
        assert created["parent_id"] == root
        await handler.begin_collection_edit(created["id"])
        await handler.commit_collection_edit("就地重命名")
        assert next(item for item in handler.state["collections"] if item["id"] == created["id"])["name"] == "就地重命名"
        assert handler.state["selected_file_id"] == direct and handler.state["chunk_scroll"] == 88
    asyncio.run(scenario())


def test_refresh_does_not_destroy_inline_editor(knowledge_base, monkeypatch):
    from app.ui.handlers import file_handlers
    monkeypatch.setattr(file_handlers.ui, "run_javascript", lambda *a, **k: None)
    root, *_ = _fixture()
    handler = _handler()
    async def scenario():
        await handler.load_files()
        await handler.begin_collection_edit(root)
        edit = handler.state["collection_edit"]
        edit["name"] = "未保存"
        refresh = Mock()
        handler.ui_refs["file_list_container"] = SimpleNamespace(refresh=refresh)
        await handler.load_files()
        refresh.assert_not_called()
        assert handler.state["collection_edit"] is edit and edit["name"] == "未保存"
        handler.cancel_collection_edit()
        assert handler.state["collection_edit"] is None
    asyncio.run(scenario())


def test_return_to_tree_clears_search_and_restores_tree_scroll(knowledge_base):
    root, child, *_ = _fixture()
    handler = _handler()
    async def scenario():
        await handler.load_files()
        await handler.toggle_collection(root)
        handler.state["tree_scroll"] = 230
        await handler.on_search_change(SimpleNamespace(args="本层"))
        handler.select_collection(child)
        await handler.toggle_collection(child)
        assert handler.state["active_collection_ids"] == [root]
        assert str(child) not in handler.state["expanded_collection_ids"]
        await handler.clear_search()
        assert handler.state["tree_scroll"] == 230 and handler.state["search_keyword"] == ""
        await handler.on_search_change(SimpleNamespace(args="下层"))
        await handler.set_library_mode("tree")
        assert handler.state["search_keyword"] == "" and handler.state["tree_scroll"] == 230
    asyncio.run(scenario())


def test_collapse_all_keeps_reader_and_cross_branch_selection(knowledge_base):
    root, child, direct, nested, loose = _fixture()
    handler = _handler()
    handler.state.update(expanded_collection_ids=[str(root), str(child)], selected_file_id=nested,
                         source_pane_open=True, source_page={"page": 2}, chunk_scroll=160,
                         batch_mode=True, batch_selected_ids={direct, nested})

    async def scenario():
        await handler.load_files()
        await handler.collapse_all_collections()
        assert handler.state["expanded_collection_ids"] == []
        assert handler.state["tree_pages"].keys() == {"root"}
        assert [file["id"] for file in handler.state["filtered_files"]] == [loose]
        assert handler.state["selected_file_id"] == nested
        assert handler.state["source_page"] == {"page": 2} and handler.state["chunk_scroll"] == 160
        assert handler.state["batch_selected_ids"] == {direct, nested}
        handler.state["expanded_collection_ids"] = [str(root)]
        handler.state["collection_edit"] = {"id": root}
        await handler.collapse_all_collections()
        assert handler.state["expanded_collection_ids"] == [str(root)]

    asyncio.run(scenario())


def test_internal_drop_adds_membership_and_rejects_other_windows(knowledge_base, monkeypatch):
    from app.ui.handlers import file_handlers
    monkeypatch.setattr(file_handlers.ui, "notify", lambda *a, **k: None)
    root, child, direct, nested, loose = _fixture()
    handler = _handler()
    async def scenario():
        await handler.load_files()
        event = SimpleNamespace(client=SimpleNamespace(id="this-window"), args={
            "client_id": "other-window", "file_ids": [direct], "collection_id": child})
        await handler.handle_document_drop(event)
        assert {item["id"] for item in collections.get_file_collections(direct)} == {root}
        event.args["client_id"] = "this-window"
        await handler.handle_document_drop(event)
        assert {item["id"] for item in collections.get_file_collections(direct)} == {root, child}
        await handler.remove_from_collection(direct, child)
        assert {item["id"] for item in collections.get_file_collections(direct)} == {root}
    asyncio.run(scenario())
