"""补齐图谱/Wiki 的空间、画布、检查、来源引用和历史刷新覆盖。"""
import asyncio
from copy import deepcopy
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from indexing.services import knowledge_service as graph_service, wiki_service


def apply(service, **parts):
    return service.apply({"request_key": str(uuid4()), "reason": "刷新覆盖回归", **parts})


def delete(service, kind, record):
    payload = {"kind": kind, "id": record["id"], "expected_revision": record["revision"]}
    if service is wiki_service:
        payload["expected_content_hash"] = record["content_hash"]
    preview = service.delete(payload)
    service.delete({**payload, "request_key": str(uuid4()), "impact_token": preview["impact_token"],
                    "dry_run": False, "confirmed": True})


def graph_view():
    from app.ui.views.graph_view import GraphWorkbench
    view = GraphWorkbench()
    view.refresh_list = Mock()
    view.refresh_detail = Mock()
    view.refresh_modes = Mock()
    view.refresh_workspace = Mock()
    view.refresh_graph = Mock()
    view.refresh_graph_stats = Mock()
    view.refresh_edge_table = Mock()
    view.remember_positions = AsyncMock()
    return view


def fake_chart():
    return SimpleNamespace(is_deleted=False, options={}, run_chart_method=Mock(),
                           _props=SimpleNamespace(suspend_updates=nullcontext))


@pytest.mark.parametrize("total_key", ["total", "total_pages", "total_objects"])
def test_auxiliary_pagination_moves_back_without_mutating_query(total_key):
    view = graph_view()
    params = {"limit": 25, "offset": 25}
    function = Mock(side_effect=[{total_key: 25}, {total_key: 25}])
    data, adjusted = asyncio.run(view.read_page(function, params, total_key))
    assert data[total_key] == 25 and adjusted["offset"] == 0
    assert params["offset"] == 25
    assert [call.kwargs["offset"] for call in function.call_args_list] == [25, 0]


def test_space_changes_preserve_selection_and_deleted_space_falls_back(knowledge_base):
    view = graph_view()

    async def scenario():
        await view.load_graphs()
        default = view.graph_id
        view._space_picker = SimpleNamespace(is_deleted=False, set_options=Mock())
        created = graph_service.create_graph("外部创建空间")
        await view.poll()
        assert view.graph_id == default
        assert any(item["id"] == created["id"] for item in view.graphs)
        graph_service.rename_graph(created["id"], "外部改名空间")
        await view.poll()
        view._space_picker.set_options.assert_called_with({item["id"]: item["name"] for item in view.graphs})
        assert next(item for item in view.graphs if item["id"] == created["id"])["name"] == "外部改名空间"
        await view.select_graph(created["id"])
        made = apply(graph_service, graph_id=created["id"], objects=[{"ref": "a", "kind": "concept", "title": "将删除"}])
        await view.select("object", made["objects"][0]["id"])
        preview = graph_service.delete_graph(created["id"])
        graph_service.delete_graph(created["id"], confirmed=True, impact_token=preview["impact_token"])
        await view.poll()
        assert view.graph_id == default
        assert view.detail is None and view.selected_object is None and view.graph is None
        assert all(item["id"] != created["id"] for item in view.graphs)
        assert view.results["graph_id"] == default

    asyncio.run(scenario())


def test_deleted_space_keeps_draft_and_cannot_redirect_save_to_default(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.i18n import t
    from test_graph_ui_writes import click, newest_button
    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    view = graph_view()
    container = ui.column()

    async def scenario():
        await view.load_graphs()
        default = view.graph_id
        space = graph_service.create_graph("草稿所属空间")
        await view.select_graph(space["id"])
        with container:
            view.edit_object()
        title = next(element for element in reversed(list(container.client.elements.values()))
                     if isinstance(element, ui.input) and element._props.get("label") == t("knowledge.title_field"))
        reason = next(element for element in reversed(list(container.client.elements.values()))
                      if isinstance(element, ui.input) and element._props.get("label") == t("knowledge.reason"))
        title.value, reason.value = "不能写入默认空间的草稿", "草稿测试"
        preview = graph_service.delete_graph(space["id"])
        graph_service.delete_graph(space["id"], confirmed=True, impact_token=preview["impact_token"])
        await view.poll()
        assert view.graph_id == default
        assert not title.is_deleted and title.value == "不能写入默认空间的草稿"
        with container:
            await click(newest_button(ui, t("knowledge.save")))
        assert graph_service.list_objects(graph_id=default)["total"] == 0
        assert not title.is_deleted and title.value == "不能写入默认空间的草稿"
        dialog = next(element for element in reversed(list(container.client.elements.values())) if isinstance(element, ui.dialog))
        dialog.delete()
        container.delete()

    asyncio.run(scenario())


def test_local_canvas_updates_in_place_and_preserves_exploration(knowledge_base):
    made = apply(graph_service, objects=[{"ref": ref, "kind": "concept", "title": ref} for ref in "abc"],
                 relations=[{"ref": "ab", "source": {"ref": "a"}, "target": {"ref": "b"},
                             "predicate": "related_to", "description": "连接", "basis": "inference"},
                            {"ref": "bc", "source": {"ref": "b"}, "target": {"ref": "c"},
                             "predicate": "related_to", "description": "展开", "basis": "inference"}])
    a, b, c = (made["refs"][ref]["id"] for ref in "abc")
    view = graph_view()

    async def scenario():
        view.mode, view.selected_object = "graph", a
        await view.load_graph()
        view.selected_object = b
        await view.load_graph(expand=True)
        assert len(view._graph_queries) == 2
        chart = view.chart = fake_chart()
        view.positions = {a: (12, 34), b: (56, 78)}
        view.viewport = {"zoom": 1.7, "center": [20, 30]}
        updated = apply(graph_service, objects=[{"id": b, "expected_revision": 1, "title": "已改名"},
                                              {"ref": "d", "kind": "entity", "title": "新展开节点"}],
                        relations=[{"ref": "bd", "source": {"id": b}, "target": {"ref": "d"},
                                    "predicate": "related_to", "description": "新关系", "basis": "inference"}])
        relation = graph_service.get_record(kind="relation", id=made["refs"]["bc"]["id"])["record"]
        delete(graph_service, "relation", relation)
        await view.poll()
        assert view.graph["root_id"] == a
        assert {node["id"] for node in view.graph["nodes"]} == {a, b, updated["refs"]["d"]["id"]}
        assert c not in {node["id"] for node in view.graph["nodes"]}
        assert next(node for node in view.graph["nodes"] if node["id"] == b)["title"] == "已改名"
        assert view.chart is chart
        series = chart.run_chart_method.call_args.args[1]["series"][0]
        assert series["zoom"] == 1.7 and series["center"] == [20, 30]
        assert next(node for node in series["data"] if node["id"] == a)["x"] == 12
        calls = chart.run_chart_method.call_count
        await view.poll()
        assert chart.run_chart_method.call_count == calls
        delete(graph_service, "object", graph_service.get_record(kind="object", id=a)["record"])
        await view.poll()
        assert view.graph is None

    asyncio.run(scenario())


def test_overview_updates_without_selection_or_camera_reset(knowledge_base):
    made = apply(graph_service, objects=[{"ref": "a", "kind": "concept", "title": "旧节点"}])
    view = graph_view()

    async def scenario():
        view.mode = "graph"
        view.overview = True
        await view.load_overview()
        chart = view.chart = fake_chart()
        added = apply(graph_service, objects=[{"ref": "b", "kind": "entity", "title": "新节点"}])
        await view.poll()
        assert view.detail is None and view.graph["total_nodes"] == 2
        assert view.chart is chart
        payload = chart.run_chart_method.call_args.args[1]
        assert set(payload) == {"series"}
        assert {node["id"] for series in payload["series"][1:] for node in series["data"]} == {
            made["refs"]["a"]["id"], added["refs"]["b"]["id"]}
        view.refresh_graph_stats.assert_called_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["mode", "range", "space"])
def test_late_canvas_read_cannot_overwrite_new_view(knowledge_base, monkeypatch, change):
    from app.ui.views import graph_view as module
    apply(graph_service, objects=[{"ref": "a", "kind": "concept", "title": "旧节点"}])
    view = graph_view()

    async def scenario():
        view.mode, view.overview = "graph", True
        await view.load_overview()
        original = deepcopy(view.graph)
        entered, release = asyncio.Event(), asyncio.Event()
        io_bound = module.run.io_bound

        async def delayed(function, *args, **kwargs):
            if function is graph_service.graph_overview:
                entered.set()
                await release.wait()
            return await io_bound(function, *args, **kwargs)

        monkeypatch.setattr(module.run, "io_bound", delayed)
        apply(graph_service, objects=[{"ref": "b", "kind": "entity", "title": "迟到节点"}])
        polling = asyncio.create_task(view.poll())
        await entered.wait()
        if change == "mode":
            view.mode = "objects"
        elif change == "range":
            view._graph_generation += 1
        else:
            view.graph_id = graph_service.create_graph("切换空间")["id"]
        release.set()
        await polling
        assert view.graph == original

    asyncio.run(scenario())


@pytest.mark.parametrize("feature", ["graph", "wiki"])
def test_checks_and_references_update_without_selected_detail(knowledge_base, feature):
    from app.ui.views.wiki_view import WikiWorkbench
    from indexing.services import file_service, chunk_service
    view = graph_view() if feature == "graph" else WikiWorkbench()
    service = graph_service if feature == "graph" else wiki_service
    key, kind, record_kind = ("objects", "concept", "object") if feature == "graph" else ("pages", "topic", "page")
    view.refresh_list = Mock()
    view.refresh_checks = Mock()
    view.refresh_references = Mock()
    view.refresh_workspace = Mock()
    fid = file_service.create_empty_file("引用来源")["file_id"]
    chunk_service.create_chunk_add_task(fid, "来源", "来源引文")
    knowledge_base.drain()
    chunk = file_service.get_chunks_paginated(fid)["chunks"][0]
    library = service.library_id()
    first = apply(service, **{key: [{"ref": "a", "kind": kind, "title": "首条记录"}],
                             "evidence": [{record_kind: {"ref": "a"}, "source_kind": "piece", "source_library_id": library,
                                           "source_file_id": fid, "source_chunk_id": chunk["id"], "quote": chunk["chunk_text"]}]})

    async def scenario():
        view.mode = "objects" if feature == "graph" else "review"
        if feature == "graph":
            await view.load_graphs()
            view.review_open = True
        await view.run_checks()
        await view.file_references(fid)
        view.refresh_checks.reset_mock()
        view.refresh_references.reset_mock()
        await view.poll()
        view.refresh_checks.assert_not_called()
        view.refresh_references.assert_not_called()
        apply(service, **{key: [{"ref": "b", "kind": kind, "title": "第二条记录"}],
                          "evidence": [{record_kind: {"ref": "b"}, "source_kind": "piece", "source_library_id": library,
                                        "source_file_id": fid, "source_chunk_id": chunk["id"], "quote": chunk["chunk_text"]}]})
        await view.poll()
        assert view.detail is None
        assert view.checks[view.check_total_key] == 2
        assert view.references_data["total"] == 2
        view.refresh_checks.assert_called_once()
        view.refresh_references.assert_called_once()
        record = service.get_record(kind=record_kind, id=first[key][0]["id"])["record"]
        delete(service, record_kind, record)
        await view.poll()
        assert view.checks[view.check_total_key] == 1 and view.references_data["total"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("feature", ["graph", "wiki"])
def test_closed_reference_panel_is_not_resurrected_by_late_read(knowledge_base, monkeypatch, feature):
    from app.ui.views.wiki_view import WikiWorkbench
    view = graph_view() if feature == "graph" else WikiWorkbench()
    service = graph_service if feature == "graph" else wiki_service
    view.references_data = {"evidence": [], "total": 0, "limit": 25, "offset": 0}
    view._references_params = {"source_library_id": service.library_id(), "source_file_id": 1, "limit": 25, "offset": 0}
    view.refresh_references = Mock()

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        original = view.references_data

        def reference_result(**kwargs):
            return {**original, "total": 1}

        from app.ui.views import knowledge_common
        io_bound = knowledge_common.run.io_bound

        async def delayed(function, *args, **kwargs):
            if function is reference_result:
                entered.set()
                await release.wait()
            return await io_bound(function, *args, **kwargs)

        monkeypatch.setattr(service, "references", reference_result)
        monkeypatch.setattr(knowledge_common.run, "io_bound", delayed)
        task = asyncio.create_task(view.poll_auxiliary(lambda: True, checks_visible=False))
        await entered.wait()
        view.close_references()
        release.set()
        await task
        assert view.references_data is None and view._references_params is None
        view.refresh_references.assert_not_called()

    asyncio.run(scenario())


@pytest.mark.parametrize("feature", ["graph", "wiki"])
def test_open_history_updates_and_closed_history_stops_querying(knowledge_base, monkeypatch, feature):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench
    view = graph_view() if feature == "graph" else WikiWorkbench()
    service = graph_service if feature == "graph" else wiki_service
    key, kind, record_kind = ("objects", "concept", "object") if feature == "graph" else ("pages", "topic", "page")
    made = apply(service, **{key: [{"ref": "a", "kind": kind, "title": "历史记录"}]})
    ident = made[key][0]["id"]
    container = ui.column()

    async def scenario():
        from nicegui import core
        monkeypatch.setattr(core, "loop", asyncio.get_running_loop())
        with container:
            await view.show_history(record_kind, ident)
        dialog = next(element for element in reversed(list(container.client.elements.values())) if isinstance(element, ui.dialog))
        timer = next(element for element in dialog.descendants() if isinstance(element, ui.timer))
        callback = timer.callback
        assert callback is not None
        try:
            record = service.get_record(kind=record_kind, id=ident)["record"]
            update = {"id": ident, "expected_revision": record["revision"], "title": "外部新增修订"}
            if feature == "wiki":
                update["expected_content_hash"] = record["content_hash"]
            apply(service, **{key: [update]})
            await callback()
            expansions = [element for element in dialog.descendants() if isinstance(element, ui.expansion)]
            assert len(expansions) == 2
            assert any(element._props["label"].startswith("2 · ") for element in expansions)
            history = Mock(wraps=service.history)
            monkeypatch.setattr(service, "history", history)
            dialog.close()
            await callback()
            history.assert_not_called()
        finally:
            dialog.delete()
            container.delete()

    asyncio.run(scenario())
