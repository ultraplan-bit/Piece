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
        workbench.mode = "review"
        await workbench.show_status("disputed")
        assert workbench.mode == "objects" and workbench.results["objects"][0]["id"] == oid
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
