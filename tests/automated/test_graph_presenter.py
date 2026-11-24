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
    assert series["layout"] == "none" and options["animation"] is True
    assert options["animationDurationUpdate"] == 320
    assert all(edge["lineStyle"]["opacity"] == 0.42 for edge in series["links"])
    assert all(edge["lineStyle"]["width"] == 0.9 for edge in series["links"])
    # 分层环布局：根居中，其余按跳数向外分环，不再挤在同一条水平轴上。
    by_id = {n["id"]: n for n in series["data"]}
    assert (by_id[graph["root_id"]]["x"], by_id[graph["root_id"]]["y"]) == (0, 0)
    assert len({(n["x"], n["y"]) for n in series["data"]}) == len(series["data"])
    assert any(n["y"] != 0 for n in series["data"] if n["id"] != graph["root_id"])
    # 度数越大节点越大；标签不再只显示根一个。
    assert by_id[graph["root_id"]]["symbolSize"] >= by_id[graph["nodes"][1]["id"]]["symbolSize"]
    assert sum(n["label"]["show"] for n in series["data"]) >= 1
    assert all(n["itemStyle"]["borderWidth"] == 1.5 for n in series["data"])
    assert all(n["itemStyle"]["borderColor"] == options["backgroundColor"] for n in series["data"])
    assert [entry["name"] for entry in options["legend"]["data"]] == list(KINDS)
    subset = deepcopy(graph)
    subset["nodes"] = graph["nodes"][:2]
    filtered = graph_options(subset, dark=dark)["series"][0]
    assert filtered["data"][1]["itemStyle"] == series["data"][1]["itemStyle"]


@pytest.mark.parametrize("dark", [False, True])
def test_local_graph_labels_avoid_collisions_in_dense_graph(dark):
    graph = {"root_id": "n0",
             "nodes": [{"id": f"n{i}", "title": f"节点 {i}", "kind": "concept"} for i in range(70)],
             "edges": [{"id": f"e{i}", "source_id": f"n{i}", "target_id": f"n{i + 1}",
                        "predicate": "depends_on", "description": "完整描述不放在线上"} for i in range(69)]}
    options = graph_options(graph, dark=dark, label=lambda value: "依赖" if value == "depends_on" else value)
    series = options["series"][0]
    for edge in series["links"]:
        assert edge["name"] == edge["label"]["formatter"] == "依赖"
        assert edge["tooltip_text"].endswith(" · 依赖")
        assert edge["emphasis"]["label"]["show"] is True
    assert any(edge["label"]["show"] for edge in series["links"])
    assert any(not edge["label"]["show"] for edge in series["links"])
    assert series["edgeLabel"]["position"] == "middle" and series["edgeLabel"]["distance"] == 0
    assert series["edgeLabel"]["textBorderColor"] == options["backgroundColor"]
    assert series["edgeLabel"]["opacity"] < series["emphasis"]["edgeLabel"]["opacity"]
    assert series["emphasis"]["edgeLabel"]["show"] is True
    assert series["emphasis"]["focus"] == "adjacency"
    assert any(node["label"]["show"] for node in series["data"])
    assert any(not node["label"]["show"] for node in series["data"])


def test_collision_suppression_preserves_native_edge_labels_and_hover_reveals():
    graph = {
        "root_id": "root",
        "nodes": [{"id": "root", "title": "Root", "kind": "concept"},
                  {"id": "child", "title": "Child", "kind": "entity"}],
        "edges": [{"id": "edge", "source_id": "root", "target_id": "child", "predicate": "depends_on"}],
    }
    series = graph_options(graph, positions={"root": (0, 0), "child": (0, 24)})["series"][0]

    assert series["data"][0]["label"]["show"] is True
    assert series["links"][0]["label"]["show"] is False
    assert series["emphasis"]["edgeLabel"]["show"] is True
    assert "labelLayout" not in series


def test_overview_uses_three_dimensional_sphere_without_surface_shell():
    graph = {
        "root_id": "b",
        "overview": True,
        "nodes": [{"id": node_id, "title": node_id, "kind": "concept"}
                  for node_id in ("a", "b", "c")],
        "edges": [{"id": "ab", "source_id": "b", "target_id": "a"},
                  {"id": "ac", "source_id": "a", "target_id": "c"}],
    }

    options = graph_options(graph, selected="a")
    nodes = {node["id"]: node for series in options["series"][1:] for node in series["data"]}
    edges = options["series"][0]

    assert options["globe"]["show"] is False
    assert options["globe"]["viewControl"]["rotateSensitivity"] == 0.9
    assert [series["type"] for series in options["series"]] == ["lines3D", "scatter3D", "scatter3D"]
    assert nodes["a"]["value"] == (0.0, 0.0, 1)
    assert nodes["b"]["value"] != nodes["a"]["value"]
    assert edges["coordinateSystem"] == "globe"
    assert "edgeLabel" not in edges and all("label" not in edge for edge in edges["data"])
    assert edges["data"][0]["coords"] == [nodes["b"]["value"], nodes["a"]["value"]]


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
