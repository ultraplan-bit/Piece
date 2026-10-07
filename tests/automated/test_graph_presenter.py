"""图谱工作台纯展示逻辑回归：节点只有 concept/entity，边只有语义关系。"""
from copy import deepcopy
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


def test_graph_only_two_node_kinds_and_relation_edges():
    assert KINDS == ("concept", "entity")
    assert set(TYPE_MARKS) == set(KINDS)


@pytest.mark.parametrize("dark", [False, True])
def test_graph_ids_direction_parallel_edges_and_safe_tooltips(dark):
    graph = sample_graph()
    options = graph_options(graph, dark=dark)
    series = options["series"][0]
    assert [n["id"] for n in series["data"]] == [n["id"] for n in graph["nodes"]]
    assert [e["id"] for e in series["links"]] == [e["id"] for e in graph["edges"]]
    # 只有语义关系边：对称关系无箭头，普通关系有箭头。
    assert [e["symbol"][1] for e in series["links"]] == ["none", "arrow"]
    assert all(e["lineStyle"]["type"] == "solid" for e in series["links"])
    assert len({e["lineStyle"]["curveness"] for e in series["links"]}) == 2
    assert len({n["symbol"] for n in series["data"]}) == 2
    assert options["tooltip"]["renderMode"] == "html" and options["tooltip"]["enterable"] is False
    assert "el.textContent = p.data.tooltip_text" in options["tooltip"][":formatter"]
    assert series["layout"] == "none" and options["animation"] is False
    assert sum(n["label"]["show"] for n in series["data"]) == 1
    assert [entry["name"] for entry in options["legend"]["data"]] == list(KINDS)
    subset = deepcopy(graph)
    subset["nodes"] = graph["nodes"][:2]
    filtered = graph_options(subset, dark=dark)["series"][0]
    assert filtered["data"][1]["itemStyle"] == series["data"][1]["itemStyle"]


def test_graph_expansion_obeys_combined_budget_and_keeps_positions():
    current = sample_graph()
    current["max_nodes"], current["max_edges"] = 2, 2
    incoming = sample_graph()
    merged = merge_graph(current, incoming)
    # 已到预算：不追加任何节点或边，只标记截断。
    assert merged["truncated"]
    assert merged["nodes"] == current["nodes"] and merged["edges"] == current["edges"]
    ids = {n["id"] for n in merged["nodes"]}
    assert all({e["source_id"], e["target_id"]} <= ids for e in merged["edges"])
    # 尚有余量时按节点/边预算追加。
    room = merge_graph({**current, "max_nodes": 4, "max_edges": 4}, incoming)
    assert len(room["nodes"]) == 4 and len(room["edges"]) == 4
    node = current["nodes"][0]["id"]
    output = graph_options(current, positions={node: (123, 456)})
    assert (output["series"][0]["data"][0]["x"], output["series"][0]["data"][0]["y"]) == (123, 456)
