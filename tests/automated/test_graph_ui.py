"""图谱 GUI 展示、安全、控制器与真实 NiceGUI 构建回归。"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.ui.views.graph_presenter import KINDS, TYPE_MARKS, graph_options, merge_graph


def sample_graph():
    nodes = [{"id": str(uuid4()), "title": "标题<script>alert(1)</script>", "kind": kind} for kind in KINDS]
    edges = [{"id": str(uuid4()), "source_id": nodes[0]["id"], "target_id": nodes[1]["id"],
              "predicate": predicate, "symmetric": symmetric}
             for predicate, symmetric in (("related_to", True), ("depends_on", False))]
    return {"root_id": nodes[0]["id"], "nodes": nodes, "edges": edges, "truncated": False,
            "max_nodes": 100, "max_edges": 300, "depth": 1}


def test_graph_workbench_has_no_wiki_body_editing():
    from app.ui.views.graph_view import GraphWorkbench
    workbench = GraphWorkbench()
    # 图谱不渲染 Wiki 正文：基类 markdown 未实现，且没有页面维护入口。
    with pytest.raises(NotImplementedError):
        workbench.markdown("正文")
    for name in ("internal_link", "edit_page", "render_page_links"):
        assert not hasattr(workbench, name), name
    assert set(KINDS) == {"concept", "entity"}
    assert all(symbol for _, symbol in TYPE_MARKS.values())


def test_knowledge_controller_service_selection_filters_and_empty_graph(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.graph_view import GraphWorkbench
    from indexing.services import knowledge_service as service

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    result = service.apply({"request_key": str(uuid4()), "reason": "test", "objects": [
        {"ref": "a", "kind": "concept", "title": "中文长标题" * 20, "status": "disputed"},
        {"ref": "b", "kind": "entity", "title": "实体"}]})
    oid = result["refs"]["a"]["id"]
    workbench = GraphWorkbench()

    async def scenario():
        assert workbench.graph is None
        workbench.kind, workbench.status = "concept", "disputed"
        await workbench.search()
        assert [item["id"] for item in workbench.results["objects"]] == [oid]
        assert "body" not in workbench.results["objects"][0]
        await workbench.select("object", oid)
        await workbench.load_graph(center=True)
        assert len(workbench.graph["nodes"]) == 1
        assert workbench.graph["edges"] == []
        await workbench.graph_click(SimpleNamespace(args={"data": {"id": oid}, "dataType": "node"}))
        assert workbench.selected_object == oid
        await workbench.select("object", str(uuid4()))
        assert workbench.error and workbench.detail["record"]["id"] == oid
    asyncio.run(scenario())


def test_switching_to_graph_tab_loads_graph_for_selected_object(knowledge_base, monkeypatch):
    """在「实体」标签选中实体后切到「图谱」标签，必须直接以它为根出图。

    回归：切标签只重绘工作区，不取图；图谱标签因此停在「先选一个实体」，
    用户得手动点一次刷新范围。切到图谱标签时图与选中实体不一致就要重新取。
    """
    from nicegui import ui
    from app.ui.views.graph_view import GraphWorkbench
    from indexing.services import knowledge_service as service

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    result = service.apply({"request_key": str(uuid4()), "reason": "test", "objects": [
        {"ref": "a", "kind": "concept", "title": "甲"},
        {"ref": "b", "kind": "entity", "title": "乙"}],
        "relations": [{"ref": "r", "source": {"ref": "a"}, "target": {"ref": "b"},
                       "predicate": "related_to", "description": "关系", "basis": "inference"}]})
    a, b = result["refs"]["a"]["id"], result["refs"]["b"]["id"]
    workbench = GraphWorkbench()

    async def scenario():
        # 「实体」标签下选中甲：此时还没有图。
        await workbench.select("object", a)
        assert workbench.graph is None
        await workbench.set_mode("graph")
        assert workbench.mode == "graph"
        assert workbench.graph and workbench.graph["root_id"] == a
        assert {node["id"] for node in workbench.graph["nodes"]} == {a, b}

        # 已是以甲为根的图，再切一次图谱标签不应重复取图。
        calls = []
        original = workbench.load_graph

        async def counting(**kwargs):
            calls.append(kwargs)
            await original(**kwargs)
        workbench.load_graph = counting
        await workbench.set_mode("graph")
        assert calls == []

        # 换选乙后切回图谱标签，应改以乙为根。
        await workbench.set_mode("objects")
        await workbench.select("object", b)
        await workbench.set_mode("graph")
        assert workbench.graph["root_id"] == b
    asyncio.run(scenario())


def test_real_nicegui_graph_forms_construct(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.graph_view import GraphWorkbench
    from indexing.services import knowledge_service as service

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    result = service.apply({"request_key": str(uuid4()), "reason": "test", "objects": [
        {"ref": "a", "kind": "concept", "title": "图节点", "summary": "只有摘要，没有正文"}]})
    oid = result["refs"]["a"]["id"]
    workbench = GraphWorkbench()
    asyncio.run(workbench.select("object", oid))
    asyncio.run(workbench.search())
    with ui.column() as container:
        workbench.render_middle()
        workbench.render_right()
        workbench.edit_object(deepcopy(workbench.detail["record"]))
        workbench.edit_edge(oid)
        workbench.edit_evidence("object", oid)
        workbench.mode = "graph"
        workbench.graph = service.graph(root_id=oid, edge_types=["relation"])
        workbench.render_graph()
        assert workbench.chart._props["options"]["series"][0]["data"][0]["id"] == oid
        assert "chart:click" in {listener.type for listener in workbench.chart._event_listeners.values()}
        # 画布铺满整栏：不写死像素高度，由 graph-canvas 的绝对定位吃掉剩余高度；
        # 关系表格收进默认收起的抽屉，展开时才占用画布高度。
        assert "graph-canvas" in workbench.chart.classes
        assert not any(name.startswith("h-[") for name in workbench.chart.classes)
        drawer = next(element for element in container.descendants()
                      if isinstance(element, ui.expansion) and element.text == workbench.text("table"))
        assert drawer.value is False
        stage = next(element for element in container.descendants()
                     if "graph-stage" in element.classes)
        assert "flex-1" in stage.classes and "min-h-0" in stage.classes
    container.delete()


def test_graph_tab_shows_empty_hint_and_references_drawer(knowledge_base, monkeypatch):
    """图谱标签的空态与被引用面板都走不滚动的那条分支。

    图谱标签不套滚动区（画布要吃掉整栏高度），所以这两条分支与「实体」标签
    的渲染路径不同，必须单独覆盖：空态要给出选根提示，被引用面板要能列出引用方。
    """
    from nicegui import ui
    from app.ui.views.graph_view import GraphWorkbench
    from indexing.services import knowledge_service as service

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    result = service.apply({"request_key": str(uuid4()), "reason": "test", "objects": [
        {"ref": "a", "kind": "concept", "title": "被引对象", "summary": "摘要"}]})
    oid = result["refs"]["a"]["id"]
    # 被引用面板按 (库, 文件) 反查证据；user 来源不需要真实切片即可建立归属。
    service.apply({"request_key": str(uuid4()), "reason": "test", "evidence": [
        {"object": {"id": oid}, "source_kind": "user", "quote": "用户陈述"}]})
    library = service.list_objects(limit=1)["library_id"]

    workbench = GraphWorkbench()
    workbench.mode = "graph"
    # 被引用面板按 (库, 文件) 反查证据，由页面在打开文件时注入；这里直接给出结果，
    # 让首帧渲染就走到图谱标签下的那条分支（该分支不套滚动区，与「实体」标签不同）。
    workbench.references_data = service.references(source_library_id=library,
                                                   source_file_id=1, limit=25, offset=0)
    with ui.column() as container:
        workbench.render_right()
        # 空态：没有图时给出选根提示，而不是空白。
        labels = [getattr(element, "text", "") for element in container.descendants()]
        assert workbench.text("choose_root") in labels
        assert workbench.chart is None
        # 被引用面板在图谱标签下同样渲染出引用方入口。
        assert workbench.text("references") in labels
    container.delete()


def test_finite_pair_rejects_non_finite_layout_and_viewport_values():
    """NaN/Infinity 的坐标与视口中心必须被挡掉。

    回归：缩放只给 zoom 不给原点时，ECharts 会把 center 写成 NaN；这些值一旦被
    存进 positions/viewport 再回灌下一次渲染，节点会被摆到画布之外，表现为
    「点一下缩放图就不见了」。只接受有限数值即可挡住这条路径。
    """
    from app.ui.views.graph_view import finite_pair
    assert finite_pair([1, 2]) and finite_pair([1.5, -3.25])
    for bad in ([float("nan"), 0], [0, float("inf")], [float("-inf"), 1],
                [1], [1, 2, 3], "1,2", None, {"x": 1}, [True, 1]):
        assert not finite_pair(bad), bad


def test_graph_zoom_dispatches_origin_and_filters_reload(knowledge_base, monkeypatch):
    """缩放必须带缩放原点；深度与筛选变化必须重取子图。

    回归两处：① graphRoam 只给 zoom 时 roam 控制器算不出中心，会把 center 写成
    NaN，图不动且之后连滚轮缩放也失效（ECharts 自己的滚轮处理始终带 origin）。
    ② 深度与筛选只写属性、不重取图，用户改成一跳/两跳后画面毫无变化。
    """
    from unittest.mock import AsyncMock
    from nicegui import ui
    from app.ui.views.graph_view import GraphWorkbench
    from indexing.services import knowledge_service as service

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    # 链式图：一跳只到「中」，两跳才到「端」，深度是否真的生效可直接观察。
    result = service.apply({"request_key": str(uuid4()), "reason": "test", "objects": [
        {"ref": "root", "kind": "concept", "title": "链根"},
        {"ref": "mid", "kind": "entity", "title": "链中"},
        {"ref": "end", "kind": "concept", "title": "链端"}],
        "relations": [{"ref": "r1", "source": {"ref": "root"}, "target": {"ref": "mid"},
                       "predicate": "part_of", "description": "根到中", "basis": "inference"},
                      {"ref": "r2", "source": {"ref": "mid"}, "target": {"ref": "end"},
                       "predicate": "depends_on", "status": "disputed",
                       "description": "中到端", "basis": "inference"}]})
    root = result["refs"]["root"]["id"]

    workbench = GraphWorkbench()
    with ui.column() as container:
        workbench.mode = "graph"
        workbench.graph = service.graph(root_id=root, edge_types=["relation"])
        workbench.render_graph()
        # 测试环境没有事件循环，refreshable 的刷新无法调度；只验证状态流转。
        workbench.refresh_graph = lambda: None
        workbench.refresh_edge_table = lambda: None

        async def scenario():
            # 缩放派发必须同时给出 originX/originY，否则 center 会变成 NaN。
            scripts = []

            async def capture(script, *args, **kwargs):
                scripts.append(script)
                return None
            monkeypatch.setattr(ui, "run_javascript", capture)
            await workbench.zoom_graph(1.25)
            assert len(scripts) == 1
            assert "graphRoam" in scripts[0] and "originX" in scripts[0] and "originY" in scripts[0]

            # 深度变化即重取子图：两跳多出「链端」。
            workbench.selected_object = root
            await workbench.reload_on_change("depth", 2)
            assert workbench.depth == 2
            assert workbench.graph["depth"] == 2
            assert {node["title"] for node in workbench.graph["nodes"]} == {"链根", "链中", "链端"}

            await workbench.reload_on_change("depth", 1)
            assert workbench.graph["depth"] == 1
            assert {node["title"] for node in workbench.graph["nodes"]} == {"链根", "链中"}

            # 筛选变化同样重取：先回到两跳让 disputed 的「中到端」进入范围。
            await workbench.reload_on_change("depth", 2)
            assert any(edge["status"] == "disputed" for edge in workbench.graph["edges"])
            await workbench.reload_on_change("relation_statuses", ["active"])
            assert workbench.relation_statuses == ["active"]
            assert workbench.graph["edges"] and all(edge["status"] == "active" for edge in workbench.graph["edges"])
            await workbench.reload_on_change("relation_statuses", None)
            assert any(edge["status"] == "disputed" for edge in workbench.graph["edges"])
        asyncio.run(scenario())
    container.delete()


def test_graph_relation_counts_native_clicks_and_review_candidates(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.graph_view import GraphWorkbench
    from indexing.services import knowledge_service as service

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    result = service.apply({"request_key": str(uuid4()), "reason": "test", "objects": [
        {"ref": "a", "kind": "concept", "title": "共同名称", "status": "disputed"},
        {"ref": "b", "kind": "entity", "title": "歧义候选", "aliases": ["共同名称"], "summary": "候选说明"}],
        "relations": [{"ref": "r", "source": {"ref": "a"}, "target": {"ref": "b"},
                       "predicate": "related_to", "description": "关系断言", "basis": "inference"}],
        "evidence": [{"relation": {"ref": "r"}, "source_kind": "user", "quote": "用户证据"}]})
    oid, rid = result["refs"]["a"]["id"], result["refs"]["r"]["id"]
    workbench = GraphWorkbench()
    workbench.graph = service.graph(root_id=oid, edge_types=["relation"])
    assert workbench.graph["edges"][0]["evidence_count"] == 1

    async def scenario():
        # 图本身没有 value；只信任已返回图中对应边的 id，不信任点击载荷。
        await workbench.graph_click(SimpleNamespace(args={"dataType": "edge", "data": {"id": rid, "edge_kind": "object"}}))
        assert workbench.detail["kind"] == "relation" and workbench.detail["record"]["id"] == rid
        await workbench.graph_click(SimpleNamespace(args={"dataType": "edge", "data": {"id": str(uuid4())}}))
        assert workbench.detail["record"]["id"] == rid
        await workbench.show_status("disputed")
        assert workbench.mode == "objects" and workbench.results["objects"][0]["id"] == oid
        await workbench.set_mode("review")
        assert workbench.mode == "objects" and workbench.review_open
    asyncio.run(scenario())
    workbench.checks = service.lint()
    with ui.column() as container:
        workbench.render_review()
        workbench.edge_table(workbench.graph)
        labels = [getattr(e, "text", "") for e in container.descendants()]
        assert "候选说明" in labels
    container.delete()


def test_bilingual_feature_namespaces_match():
    base = Path(__file__).resolve().parents[2] / "app/i18n/locales"
    zh = json.loads((base / "zh.json").read_text(encoding="utf-8"))
    en = json.loads((base / "en.json").read_text(encoding="utf-8"))
    for namespace in ("knowledge", "wiki", "graph", "sidebar"):
        assert set(zh[namespace]) == set(en[namespace]), namespace
        assert all(zh[namespace].values()) and all(en[namespace].values()), namespace


def test_graph_compare_source_renders_chunk_body(knowledge_base, monkeypatch):
    """图谱证据的“对照当前原文”必须弹出对话框并渲染切片正文。

    基类的 compare_source 曾调用只有 Wiki 实现的 markdown 钩子，图谱侧必抛
    NotImplementedError；改用阅读器同一套 chunk_markdown 渲染。
    """
    from nicegui import ui
    from app.ui.views.graph_view import GraphWorkbench
    from app.i18n import t
    from indexing.database import get_db_cursor
    from indexing.services import knowledge_service as service

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    quote = "原始引用快照"
    with get_db_cursor(write=True) as cursor:
        cursor.execute("INSERT INTO files (file_hash, filename, file_path) VALUES (?,?,?)",
                       (uuid4().hex, "资料.md", str(knowledge_base.path / "资料.md")))
        file_id = cursor.lastrowid
        cursor.execute("INSERT INTO chunks (file_id, doc_title, chunk_text, chunk_index) VALUES (?,?,?,?)",
                       (file_id, "资料", quote + " 之后的当前正文", 0))
        chunk_id = cursor.lastrowid
        library = cursor.execute("SELECT library_id FROM library_metadata WHERE singleton=1").fetchone()[0]
    created = service.apply({"request_key": str(uuid4()), "reason": "test", "objects": [
        {"ref": "a", "kind": "concept", "title": "对象"}],
        "evidence": [{"object": {"ref": "a"}, "source_kind": "piece",
                      "source_library_id": library, "source_file_id": file_id,
                      "source_chunk_id": chunk_id, "quote": quote}]})
    oid, eid = created["refs"]["a"]["id"], created["evidence"][0]["id"]

    workbench = GraphWorkbench()
    asyncio.run(workbench.select("object", oid))
    evidence = service.get_record(kind="evidence", id=eid)["record"]
    assert evidence["location_status"] == "current"

    container = ui.column()

    async def compare():
        with container:
            await workbench.compare_source(deepcopy(evidence))
    asyncio.run(compare())

    # 对话框被 _open_dialog 挂到页面根容器，因此从所属 client 的元素里取。
    elements = list(container.client.elements.values())
    captions = [e.text for e in elements if isinstance(e, ui.label)]
    assert t("knowledge.snapshot") in captions and t("knowledge.current_body") in captions
    assert quote in captions
    bodies = [e._props.get("innerHTML", "") for e in elements if isinstance(e, ui.markdown)]
    assert any("之后的当前正文" in body for body in bodies), bodies
    container.delete()
