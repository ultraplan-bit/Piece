"""探索现场、分页与后台刷新之间的行为回归。"""
import asyncio
from copy import deepcopy
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock

from indexing.services import knowledge_service as service
from app.ui.views.graph_view import GraphWorkbench


def write(**parts):
    for index, relation in enumerate(parts.get("relations", [])):
        relation.setdefault("ref", f"r{index}")
    return service.apply({"request_key": uuid4().hex, "reason": "探索回归", **parts})


def test_selection_history_and_source_return_do_not_recenter(knowledge_base):
    result = write(objects=[{"ref": key, "kind": "concept", "title": key} for key in ("A", "B", "C")],
                   relations=[{"source": {"ref": "A"}, "target": {"ref": key}, "predicate": "related_to",
                               "description": key, "basis": "explicit"} for key in ("B", "C")])
    a, b = (result["refs"][key]["id"] for key in ("A", "B"))
    view = GraphWorkbench()

    async def scenario():
        await view.select("object", a)
        await view.set_mode("graph")
        view.positions[a] = (52, 31)
        view.viewport = {"zoom": 1.7, "center": [40, 60]}
        original = deepcopy(view.graph)
        await view.select("object", b)
        assert view.mode == "graph" and view.inspector_open
        assert view.graph == original and view.graph["root_id"] == a
        assert view.viewport["zoom"] == 1.7
        await view.back_exploration()
        assert view.selected_object == a and view.positions[a] == (52, 31)
        assert view.viewport == {"zoom": 1.7, "center": [40, 60]}
        await view.select("object", b)
        await view.view_graph()
        assert view.graph["root_id"] == b
        await view.back_exploration()
        assert view.graph["root_id"] == a and view.selected_object == b
    asyncio.run(scenario())


def test_more_than_one_hundred_neighbors_survive_poll_and_collapse(knowledge_base):
    root = write(objects=[{"ref": "root", "kind": "concept", "title": "中心"}])["refs"]["root"]["id"]
    for offset in range(0, 120, 20):
        write(objects=[{"ref": str(i), "kind": "entity", "title": f"节点{i}"} for i in range(offset, offset + 20)],
              relations=[{"source": {"id": root}, "target": {"ref": str(i)}, "predicate": "depends_on",
                          "description": "邻居", "basis": "explicit"} for i in range(offset, offset + 20)])
    view = GraphWorkbench()

    async def scenario():
        await view.select("object", root)
        await view.set_mode("graph")
        assert len(view.graph["nodes"]) == 100
        offset = view.graph["next_neighbor_offset"]
        other = next(node["id"] for node in view.graph["nodes"] if node["id"] != root)
        await view.select("object", other)
        await view.load_graph(expand=True, neighbor_offset=offset, expand_root=root)
        assert len(view.graph["nodes"]) == 121
        assert len(view.graph["edges"]) == 120
        await view.poll_graph(lambda: True)
        assert len(view.graph["nodes"]) == 121
        assert len(view.graph["edges"]) == 120
        view.selected_object = root
        await view.collapse_selected()
        assert len(view.graph["nodes"]) == 100
        assert view.graph["next_neighbor_offset"] is not None
    asyncio.run(scenario())


def test_overview_node_and_edge_pagination_survives_poll(knowledge_base, monkeypatch):
    result = write(objects=[{"ref": str(i), "kind": "concept", "title": str(i)} for i in range(6)],
                   relations=[{"source": {"ref": str(i)}, "target": {"ref": str(j)}, "predicate": "related_to",
                               "description": "连接", "basis": "explicit"} for i in range(6) for j in range(i + 1, 6)])
    original = service.graph_overview

    def small_pages(**kwargs):
        return original(**{**kwargs, "max_nodes": 2, "max_edges": 2})

    monkeypatch.setattr(service, "graph_overview", small_pages)
    view = GraphWorkbench()
    view.mode = "graph"

    async def scenario():
        await view.show_global_graph()
        while view.graph["next_offset"] is not None:
            await view.load_overview(more=True)
        while view.graph["next_edge_offset"] is not None:
            await view.load_overview(more_edges=True)
        assert len(view.graph["nodes"]) == 6 and len(view.graph["edges"]) == 15
        assert view.graph["next_offset"] is None and not view.graph["truncated"]
        await view.poll_graph(lambda: True)
        assert len(view.graph["nodes"]) == 6 and len(view.graph["edges"]) == 15
        assert view.graph["next_offset"] is None and not view.graph["truncated"]
        # 关系排序变化不能让后台分页产生缺少端点的关系。
        object_id = result["refs"]["0"]["id"]
        preview = service.delete({"kind": "object", "id": object_id, "expected_revision": 1})
        service.delete({"kind": "object", "id": object_id, "expected_revision": 1,
                        "request_key": uuid4().hex, "impact_token": preview["impact_token"],
                        "dry_run": False, "confirmed": True})
        await view.poll_graph(lambda: True)
        ids = {node["id"] for node in view.graph["nodes"]}
        assert object_id not in ids
        assert all(edge["source_id"] in ids and edge["target_id"] in ids for edge in view.graph["edges"])
        view.graph_chart_options()
    asyncio.run(scenario())


def test_late_position_read_cannot_reverse_two_node_clicks(monkeypatch):
    """第一次点击保存视角较慢时，第二次点击仍然拥有最终选择权。"""
    view = GraphWorkbench()
    view.mode = "graph"

    async def scenario():
        gates = [asyncio.Event(), asyncio.Event()]
        entered = []

        async def positions():
            index = len(entered)
            entered.append(index)
            if index < 2:
                await gates[index].wait()

        async def scoped_call(function, **params):
            if "kind" in params:
                return {"kind": params["kind"], "record": {"id": params["id"]}}
            return {"root_id": params.get("root_id"), "nodes": [], "edges": []}

        monkeypatch.setattr(view, "remember_positions", positions)
        monkeypatch.setattr(view, "_scoped_call", scoped_call)
        monkeypatch.setattr(view, "highlight_selection", lambda: None)
        monkeypatch.setattr(view, "update_chart", lambda: None)
        monkeypatch.setattr(view, "show_inspector", lambda: None)
        first = asyncio.create_task(view.select("object", "earlier"))
        await asyncio.sleep(0)
        latest = asyncio.create_task(view.select("object", "latest"))
        await asyncio.sleep(0)
        gates[1].set()
        await latest
        assert view.selected_object == "latest"
        gates[0].set()
        await first
        assert view.selected_object == "latest"
        assert view.detail["record"]["id"] == "latest"
        assert len(view.exploration_history) == 1

    asyncio.run(scenario())


def test_late_filter_cannot_replace_newer_filter(monkeypatch):
    view = GraphWorkbench()
    view.overview = True
    loads = []

    async def scenario():
        gates = [asyncio.Event(), asyncio.Event()]
        entered = []

        async def positions():
            index = len(entered)
            entered.append(index)
            await gates[index].wait()

        async def load(**kwargs):
            loads.append(view.predicates)

        monkeypatch.setattr(view, "remember_positions", positions)
        monkeypatch.setattr(view, "load_overview", load)
        first = asyncio.create_task(view.reload_on_change("predicates", ["supports"]))
        await asyncio.sleep(0)
        latest = asyncio.create_task(view.reload_on_change("predicates", ["contradicts"]))
        await asyncio.sleep(0)
        gates[1].set()
        await latest
        gates[0].set()
        await first
        assert view.predicates == ["contradicts"]
        assert loads == [["contradicts"]]

    asyncio.run(scenario())


def test_old_graph_action_does_not_change_new_graph_layout(monkeypatch):
    view = GraphWorkbench()
    view.graph_id = "old"

    async def scenario():
        release = asyncio.Event()
        monkeypatch.setattr(view, "remember_positions", release.wait)
        monkeypatch.setattr(view, "search", AsyncMock())
        monkeypatch.setattr(view, "load_overview", AsyncMock())
        old_action = asyncio.create_task(view.change_layout("hierarchy"))
        await asyncio.sleep(0)
        await view.select_graph("new")
        release.set()
        await old_action
        assert view.graph_id == "new"
        assert view.layout == "auto"
        assert view.exploration_history == []

    asyncio.run(scenario())


def test_old_chart_position_response_does_not_overwrite_replacement_chart(monkeypatch):
    from nicegui import ui

    view = GraphWorkbench()
    view.graph_id = "old"
    view.chart = SimpleNamespace(id=1, is_deleted=False)

    async def scenario():
        release = asyncio.Event()

        async def javascript(*args, **kwargs):
            await release.wait()
            return {"points": {"old": [99, 99]}, "center": [12, 13], "zoom": 4}

        monkeypatch.setattr(ui, "run_javascript", javascript)
        pending = asyncio.create_task(view.remember_positions())
        await asyncio.sleep(0)
        view.chart = SimpleNamespace(id=2, is_deleted=False)
        view.graph_id = "new"
        view.positions = {"new": (1, 2)}
        view.viewport = {"zoom": 1}
        release.set()
        await pending
        assert view.positions == {"new": (1, 2)}
        assert view.viewport == {"zoom": 1}

    asyncio.run(scenario())


def test_hierarchy_predicate_change_relayouts_after_position_readback(monkeypatch):
    view = GraphWorkbench()
    view.mode, view.layout, view.overview = "graph", "hierarchy", True
    view.predicates = ["is_a"]
    view.graph = {"root_id": "a", "nodes": [], "edges": [], "overview": True,
                  "total_nodes": 0, "total_edges": 0, "truncated": False}

    async def scenario():
        async def remember():
            view.positions = {"a": (90, 600)}
            view.viewport = {"zoom": 3, "center": [90, 600]}

        async def call(*args, **kwargs):
            return deepcopy(view.graph)

        monkeypatch.setattr(view, "remember_positions", remember)
        monkeypatch.setattr(view, "_scoped_call", call)
        await view.reload_on_change("predicates", ["part_of"])
        assert view.positions == {} and view.viewport == {}
        await view.load_overview()
        assert view.positions == {"a": (90, 600)} and view.viewport["zoom"] == 3

    asyncio.run(scenario())


def test_clear_hierarchy_filters_resets_layout_after_readback_but_global_keeps_view(monkeypatch):
    view = GraphWorkbench()
    view.mode, view.layout, view.overview = "graph", "hierarchy", True
    view.predicates = ["part_of"]
    view.graph = {"root_id": "a", "nodes": [], "edges": [], "overview": True,
                  "total_nodes": 0, "total_edges": 0, "truncated": False}

    async def scenario():
        async def remember():
            view.positions = {"a": (90, 600)}
            view.viewport = {"zoom": 3, "center": [90, 600]}

        async def call(*args, **kwargs):
            return deepcopy(view.graph)

        monkeypatch.setattr(view, "remember_positions", remember)
        monkeypatch.setattr(view, "_scoped_call", call)
        await view.clear_graph_filters()
        assert view.layout == "auto" and view.predicates is None
        assert view.positions == {} and view.viewport == {}
        view.predicates = ["supports"]
        await view.clear_graph_filters()
        assert view.positions == {"a": (90, 600)}
        assert view.viewport == {"zoom": 3, "center": [90, 600]}

    asyncio.run(scenario())


def test_leaving_hierarchy_restores_previous_overview_filters(knowledge_base):
    write(objects=[{"ref": key, "kind": "concept", "title": key} for key in ("A", "B", "C")],
          relations=[
              {"source": {"ref": "A"}, "target": {"ref": "B"}, "predicate": "is_a",
               "description": "分类关系", "basis": "explicit"},
              {"source": {"ref": "A"}, "target": {"ref": "C"}, "predicate": "related_to",
               "description": "一般关系", "basis": "explicit"},
          ])
    view = GraphWorkbench()
    view.mode = "graph"

    async def scenario():
        await view.show_global_graph()
        assert view.predicates is None and len(view.graph["edges"]) == 2
        await view.show_hierarchy()
        assert view.layout == "hierarchy" and view.predicates == ["is_a"]
        await view.show_global_graph()
        assert view.layout == "auto" and view.predicates is None
        assert len(view.graph["edges"]) == 2
        assert view._hierarchy_return_state is None

    asyncio.run(scenario())


def test_old_layout_position_response_does_not_overwrite_new_layout(monkeypatch):
    from nicegui import ui

    view = GraphWorkbench()
    view.layout = "hierarchy"
    view.chart = SimpleNamespace(id=1, is_deleted=False)

    async def scenario():
        release = asyncio.Event()

        async def javascript(*args, **kwargs):
            await release.wait()
            return {"points": {"a": [99, 99]}, "center": [12, 13], "zoom": 4}

        monkeypatch.setattr(ui, "run_javascript", javascript)
        pending = asyncio.create_task(view.remember_positions())
        await asyncio.sleep(0)
        view.layout = "auto"
        view.positions, view.viewport = {"a": (1, 2)}, {"zoom": 1}
        release.set()
        await pending
        assert view.positions == {"a": (1, 2)} and view.viewport == {"zoom": 1}

    asyncio.run(scenario())
