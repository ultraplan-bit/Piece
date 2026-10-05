"""知识 GUI 展示、安全、控制器与真实 NiceGUI 构建回归。"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.ui.views.knowledge_presenter import (
    KINDS, PendingWrite, graph_options, local_source, merge_graph,
    safe_destination, safe_knowledge_html,
)


def sample_graph():
    nodes = [{"id": str(uuid4()), "title": "标题<script>alert(1)</script>", "kind": kind} for kind in KINDS]
    edges = [{"id": str(uuid4()), "source_id": nodes[0]["id"], "target_id": nodes[1]["id"],
              "kind": kind, "predicate": predicate, "symmetric": symmetric}
             for kind, predicate, symmetric in (("link", None, False), ("relation", "related_to", True),
                                                ("relation", "depends_on", False))]
    return {"root_id": nodes[0]["id"], "nodes": nodes, "edges": edges, "truncated": False,
            "max_nodes": 100, "max_edges": 300, "depth": 1}


@pytest.mark.parametrize("dark", [False, True])
def test_graph_ids_direction_parallel_edges_and_safe_tooltips(dark):
    graph = sample_graph()
    options = graph_options(graph, dark=dark)
    series = options["series"][0]
    assert [n["id"] for n in series["data"]] == [n["id"] for n in graph["nodes"]]
    assert [e["id"] for e in series["links"]] == [e["id"] for e in graph["edges"]]
    assert [e["edge_kind"] for e in series["links"]] == ["link", "relation", "relation"]
    assert [e["symbol"][1] for e in series["links"]] == ["arrow", "none", "arrow"]
    assert len({e["lineStyle"]["curveness"] for e in series["links"]}) == 3
    assert len({n["symbol"] for n in series["data"]}) == 5
    assert options["tooltip"]["renderMode"] == "html" and options["tooltip"]["enterable"] is False
    assert "el.textContent = p.data.tooltip_text" in options["tooltip"][":formatter"]
    assert series["layout"] == "none" and options["animation"] is False
    assert sum(n["label"]["show"] for n in series["data"]) == 1
    subset = deepcopy(graph)
    subset["nodes"] = graph["nodes"][:2]
    filtered = graph_options(subset, dark=dark)["series"][0]
    assert filtered["data"][1]["itemStyle"] == series["data"][1]["itemStyle"]


def test_graph_expansion_obeys_combined_budget_and_keeps_positions():
    current = sample_graph()
    current["max_nodes"], current["max_edges"] = 5, 3
    incoming = sample_graph()
    merged = merge_graph(current, incoming)
    assert merged["truncated"]
    assert len(merged["nodes"]) == 5 and len(merged["edges"]) == 3
    assert merged["nodes"] == current["nodes"]
    ids = {n["id"] for n in merged["nodes"]}
    assert all({e["source_id"], e["target_id"]} <= ids for e in merged["edges"])
    node = current["nodes"][0]["id"]
    output = graph_options(current, positions={node: (123, 456)})
    assert (output["series"][0]["data"][0]["x"], output["series"][0]["data"][0]["y"]) == (123, 456)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///C:/secret", "//example.com", "/working/private",
                                  "https://user:secret@example.com", "https://example.com:bad", "https://x/\\evil",
                                  "https://x/\n", "piece://knowledge/../../secret", "piece://knowledge/not-a-uuid"])
def test_unsafe_destinations_rejected(url):
    assert safe_destination(url) is None


def test_safe_markdown_links_are_internal_or_explicit_external():
    ident = str(uuid4())
    assert safe_destination("piece://knowledge/" + ident.upper()) == ("knowledge", ident)
    assert safe_destination("piece://knowledge/" + ident + "/bad") is None
    source = ('<script>alert(1)</script><img src=x onerror=alert(2)>'
              '<a href="javascript:alert(3)" onclick="evil()">bad</a>'
              f'<a href="piece://knowledge/{ident}">内链</a>'
              '<a href="https://example.com/?a=1&amp;b=2">外链</a>')
    safe = safe_knowledge_html(source)
    assert "<script" not in safe and "<img" not in safe and "onclick" not in safe and "onerror" not in safe
    assert "javascript:" not in safe and 'href="piece:' not in safe
    assert f'data-knowledge-id="{ident}"' in safe
    assert 'rel="noopener noreferrer"' in safe


def test_cross_library_and_missing_sources_never_open_local_integer_ids():
    library = str(uuid4())
    evidence = {"source_kind": "piece", "source_library_id": library, "source_file_id": 1,
                "source_chunk_id": 2, "location_status": "current"}
    assert local_source(evidence, library) == (1, 2)
    assert local_source(evidence, str(uuid4())) is None
    for status in ("missing", "unresolved", "unverified"):
        assert local_source({**evidence, "location_status": status}, library) is None
    assert local_source({**evidence, "source_file_id": True}, library) is None


def test_pending_request_retains_original_payload_and_key():
    draft = {"reason": "人工修订", "objects": [{"id": str(uuid4()), "expected_revision": 1, "body": "草稿"}]}
    request = PendingWrite(draft)
    key = request.key
    draft["objects"][0]["body"] = "后续修改"
    request.uncertain = True
    assert request.key == key and request.payload["objects"][0]["body"] == "草稿"
    assert request.payload["objects"][0]["expected_revision"] == 1


def test_knowledge_controller_service_selection_filters_and_empty_graph(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.knowledge_view import KnowledgeWorkbench
    from indexing.services import knowledge_service as service

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    result = service.apply({"request_key": str(uuid4()), "reason": "test", "objects": [
        {"ref": "a", "kind": "concept", "title": "中文长标题" * 20, "status": "disputed"},
        {"ref": "b", "kind": "entity", "title": "实体", "body": ""}]})
    oid = result["refs"]["a"]["id"]
    workbench = KnowledgeWorkbench()

    async def scenario():
        assert workbench.graph is None
        workbench.kind, workbench.status = "concept", "disputed"
        await workbench.search()
        assert [item["id"] for item in workbench.results["objects"]] == [oid]
        assert "body" not in workbench.results["objects"][0]
        await workbench.select("object", oid)
        assert workbench.detail["record"]["body"] == ""
        await workbench.load_graph(center=True)
        assert len(workbench.graph["nodes"]) == 1
        assert workbench.graph["edges"] == []
        await workbench.graph_click(SimpleNamespace(args={"data": {"id": oid}, "dataType": "node"}))
        assert workbench.selected_object == oid
        await workbench.select("object", str(uuid4()))
        assert workbench.error and workbench.detail["record"]["id"] == oid
    asyncio.run(scenario())


def test_real_nicegui_pages_forms_graph_and_evidence_construct(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.knowledge_view import KnowledgeWorkbench
    from indexing.services import knowledge_service as service

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    result = service.apply({"request_key": str(uuid4()), "reason": "test", "objects": [
        {"ref": "a", "kind": "concept", "title": "知识页", "body": "|a|b|\n|-|-|\n|1|2|\n\n$x^2$"}]})
    oid = result["refs"]["a"]["id"]
    workbench = KnowledgeWorkbench()
    asyncio.run(workbench.select("object", oid))
    asyncio.run(workbench.search())
    with ui.column() as container:
        workbench.render_middle()
        workbench.render_right()
        workbench.edit_object(deepcopy(workbench.detail["record"]))
        workbench.edit_edge("relation", oid)
        workbench.edit_edge("link", oid)
        workbench.edit_evidence("object", oid)
        workbench.mode = "graph"
        workbench.graph = service.graph(root_id=oid)
        workbench.render_graph()
        assert workbench.chart._props["options"]["series"][0]["data"][0]["id"] == oid
        assert "chart:click" in {listener.type for listener in workbench.chart._event_listeners.values()}
    container.delete()


def test_committed_but_deleted_before_readback_is_not_reported_verified(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.knowledge_view import KnowledgeWorkbench, text
    from indexing.services import knowledge_service as service

    notifications = []
    monkeypatch.setattr(ui, "notify", lambda message, **kwargs: notifications.append((message, kwargs)))
    result = service.apply({"request_key": str(uuid4()), "reason": "test", "objects": [
        {"ref": "a", "kind": "concept", "title": "被并发删除"}]})
    oid = result["refs"]["a"]["id"]
    payload = {"kind": "object", "id": oid, "expected_revision": 1}
    preview = service.delete(payload)
    service.delete({**payload, "request_key": str(uuid4()), "impact_token": preview["impact_token"],
                    "dry_run": False, "confirmed": True})
    asyncio.run(KnowledgeWorkbench().after_write(result))
    assert notifications[-1] == (text("saved_unread"), {"type": "warning"})
    assert not any(message == text("saved") for message, _ in notifications)


def test_source_chunk_navigation_uses_actual_sorted_page(knowledge_base):
    from indexing.database import get_db_cursor
    from indexing.services import file_service
    from indexing.services.errors import BusinessError

    fid = file_service.create_empty_file("分页来源")["file_id"]
    with get_db_cursor(write=True) as cursor:
        for index in range(60):
            cursor.execute("INSERT INTO chunks(file_id,doc_title,chunk_text,chunk_index) VALUES(?,?,?,?)",
                           (fid, "同名标题", str(index), index * 10))
            if index == 55:
                target = cursor.lastrowid
    page = file_service.get_chunks_paginated(fid, chunk_id=target)
    assert page["page"] == 2 and any(item["id"] == target for item in page["chunks"])
    other = file_service.create_empty_file("另一文件")["file_id"]
    with pytest.raises(BusinessError, match="不属于"):
        file_service.get_chunks_paginated(other, chunk_id=target)


def test_graph_relation_counts_native_clicks_and_review_candidates(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.knowledge_view import KnowledgeWorkbench, text
    from indexing.services import knowledge_service as service

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    result = service.apply({"request_key": str(uuid4()), "reason": "test", "objects": [
        {"ref": "a", "kind": "concept", "title": "共同名称", "status": "disputed"},
        {"ref": "b", "kind": "entity", "title": "歧义候选", "aliases": ["共同名称"], "summary": "候选说明"}],
        "relations": [{"ref": "r", "source": {"ref": "a"}, "target": {"ref": "b"},
                       "predicate": "related_to", "description": "关系断言", "basis": "inference"}],
        "evidence": [{"relation": {"ref": "r"}, "source_kind": "user", "quote": "用户证据"}]})
    oid, rid = result["refs"]["a"]["id"], result["refs"]["r"]["id"]
    workbench = KnowledgeWorkbench()
    workbench.graph = service.graph(root_id=oid)
    assert workbench.graph["edges"][0]["evidence_count"] == 1

    async def scenario():
        # 图本身没有 value；只信任已返回图中对应边的 kind，不信任点击载荷。
        await workbench.graph_click(SimpleNamespace(args={"dataType": "edge", "data": {"id": rid, "edge_kind": "object"}}))
        assert workbench.detail["kind"] == "relation" and workbench.detail["record"]["id"] == rid
        await workbench.graph_click(SimpleNamespace(args={"dataType": "edge", "data": {"id": str(uuid4())}}))
        assert workbench.detail["record"]["id"] == rid
        workbench.mode = "review"
        await workbench.show_status("disputed")
        assert workbench.mode == "pages" and workbench.results["objects"][0]["id"] == oid
    asyncio.run(scenario())
    workbench.checks = service.lint()
    with ui.column() as container:
        workbench.render_review()
        workbench.edge_table(workbench.graph)
        labels = [getattr(e, "text", "") for e in container.descendants()]
        assert "候选说明" in labels and text("evidence_count", count=1) in labels
    container.delete()


def test_bilingual_knowledge_keys_match():
    base = Path(__file__).resolve().parents[2] / "app/i18n/locales"
    zh = json.loads((base / "zh.json").read_text(encoding="utf-8"))["knowledge"]
    en = json.loads((base / "en.json").read_text(encoding="utf-8"))["knowledge"]
    assert set(zh) == set(en)
    assert all(zh.values()) and all(en.values())
