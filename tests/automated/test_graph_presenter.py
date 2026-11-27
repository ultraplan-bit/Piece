"""图谱工作台纯展示逻辑回归：节点只有 concept/entity，边只有语义关系。"""
from copy import deepcopy
from uuid import uuid4

import pytest

from app.ui.views.graph_presenter import KINDS, TYPE_MARKS, graph_options, graph_projection, merge_graph


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

    # 全局关系文字默认收起，节点避让应从首次显示起生效，不依赖交互脚本的 show 变化。
    graph["overview"] = True
    overview = graph_options(graph)["series"][0]
    assert overview["labelLayout"]["hideOverlap"] is True


def test_overview_uses_three_dimensional_sphere_without_surface_shell():
    graph = {
        "root_id": "b",
        "overview": True,
        "nodes": [{"id": node_id, "title": node_id, "kind": "concept"}
                  for node_id in ("a", "b", "c")],
        "edges": [{"id": "ab", "source_id": "b", "target_id": "a"},
                  {"id": "ac", "source_id": "a", "target_id": "c"}],
    }

    options = graph_options(graph, selected="a", layout="sphere3d")
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


def test_overview_defaults_to_stable_2d_and_packs_disconnected_nodes():
    graph = sample_graph()
    graph["overview"] = True
    graph["nodes"].append({"id": "isolated", "title": "Isolated", "kind": "concept"})
    first = graph_options(graph)
    second = graph_options({**graph, "nodes": list(reversed(graph["nodes"])), "edges": list(reversed(graph["edges"]))})
    positions = lambda options: {node["id"]: (node["x"], node["y"]) for node in options["series"][0]["data"]}
    assert "globe" not in first
    assert first["series"][0]["type"] == "graph"
    assert positions(first) == positions(second)
    assert len(set(positions(first).values())) == 3


def test_structural_groups_preserve_every_relation_direction_and_isolated_member():
    nodes = [{"id": f"n{i:03}", "title": f"Node {i}", "kind": "concept"} for i in range(100)]
    edges = [{"id": f"e{i}", "source_id": "n000", "target_id": f"n{i:03}",
              "predicate": "supports" if i % 2 else "contradicts", "symmetric": False}
             for i in range(1, 90)]
    edges.append({"id": "reverse", "source_id": "n089", "target_id": "n000", "predicate": "supports", "symmetric": False})
    graph = {"root_id": "n000", "nodes": nodes, "edges": edges, "overview": True}
    projection = graph_projection(graph)
    assert len(projection["nodes"]) < 20
    isolated = next(group for group in projection["groups"] if group["isolated"])
    assert set(isolated["member_ids"]) == {f"n{i:03}" for i in range(90, 100)}
    represented = [ident for group in projection["groups"] for ident in group["internal_edge_ids"]]
    represented += [ident for edge in projection["edges"] for ident in edge.get("relation_ids", [edge["id"]])]
    assert sorted(represented) == sorted(edge["id"] for edge in edges)
    originals = {edge["id"]: edge for edge in edges}
    for edge in projection["edges"]:
        for ident in edge.get("relation_ids", [edge["id"]]):
            original = originals[ident]
            assert edge["source_id"] == projection["node_to_group"].get(original["source_id"], original["source_id"])
            assert edge["target_id"] == projection["node_to_group"].get(original["target_id"], original["target_id"])
            assert edge["predicate"] == original["predicate"]
    expanded = graph_projection(graph, expanded_groups=[isolated["id"]])
    assert set(isolated["member_ids"]) <= {node["id"] for node in expanded["nodes"]}
    for group in projection["groups"]:
        if group["id"] != isolated["id"]:
            assert expanded["layout_positions"][group["id"]] == projection["layout_positions"][group["id"]]
    full = graph_projection(graph, expanded_groups=[group["id"] for group in projection["groups"]])
    assert {node["id"] for node in full["nodes"]} == {node["id"] for node in nodes}
    assert {edge["id"] for edge in full["edges"]} == set(originals)


def test_hierarchy_retains_cycles_multiple_parents_and_original_direction():
    graph = {"root_id": "a", "nodes": [{"id": node, "title": node, "kind": "concept"} for node in "abcde"],
             "edges": [{"id": source + target, "source_id": source, "target_id": target, "predicate": "is_a"}
                       for source, target in [("a", "b"), ("b", "a"), ("c", "a"), ("c", "d"), ("d", "e")]]}
    options = graph_options(graph, layout="hierarchy")
    nodes = {node["id"]: node for node in options["series"][0]["data"]}
    assert nodes["a"]["cycle_id"] == nodes["b"]["cycle_id"] and nodes["a"]["cycle_id"]
    assert nodes["c"]["y"] > nodes["d"]["y"] > nodes["e"]["y"]
    assert [(edge["source"], edge["target"]) for edge in options["series"][0]["links"]] == [
        (edge["source_id"], edge["target_id"]) for edge in graph["edges"]]
    assert set(options["piece_meta"]["cycles"][0]["relation_ids"]) == {"ab", "ba"}
    graph["edges"][0]["predicate"] = "depends_on"
    mixed = graph_options(graph, layout="hierarchy")
    assert mixed["piece_meta"]["layout_warning"] == "hierarchy_requires_single_relation"
    assert len(mixed["series"][0]["links"]) == len(graph["edges"])


def test_expanded_group_identity_survives_new_nodes_and_bridge_edges():
    graph = {"root_id": "n000", "overview": True,
             "nodes": [{"id": f"n{i:03}", "title": str(i), "kind": "concept"} for i in range(90)],
             "edges": [{"id": str(i), "source_id": f"n{i:03}", "target_id": f"n{i + 1:03}", "predicate": "depends_on"}
                       for i in range(89)]}
    first = graph_projection(graph)
    graph["nodes"].append({"id": "new-hub", "title": "New hub", "kind": "concept"})
    graph["edges"].extend({"id": f"new-{i}", "source_id": "new-hub", "target_id": f"n{i:03}", "predicate": "depends_on"}
                           for i in range(90))
    second = graph_projection(graph, grouping=first["groups"], expanded_groups=[first["groups"][0]["id"]])
    assert {group["id"]: group["member_ids"] for group in first["groups"]} == {
        group["id"]: group["member_ids"] for group in second["groups"] if group["id"] in {old["id"] for old in first["groups"]}}
    assert set(first["groups"][0]["member_ids"]) <= {node["id"] for node in second["nodes"]}


def test_hierarchy_only_projects_relation_endpoints_and_reports_excluded_nodes():
    graph = {"root_id": "isolated", "overview": True,
             "nodes": [{"id": node, "title": node, "kind": "concept"} for node in ("a", "b", "isolated")],
             "edges": [{"id": "ab", "source_id": "a", "target_id": "b", "predicate": "is_a"}]}
    hierarchy = graph_options(graph, layout="hierarchy")
    assert {node["id"] for node in hierarchy["series"][0]["data"]} == {"a", "b"}
    assert hierarchy["piece_meta"]["excluded_nodes"] == ["isolated"]
    assert hierarchy["piece_meta"]["excluded_count"] == 1
    assert {node["id"] for node in graph_options(graph)["series"][0]["data"]} == {"a", "b", "isolated"}
    empty = graph_options({**graph, "edges": []}, layout="hierarchy")
    assert empty["series"][0]["data"] == [] and empty["piece_meta"]["excluded_count"] == 3


def test_broad_hierarchy_wraps_peers_and_keeps_parent_bands_separate():
    graph = {"root_id": "root", "overview": True,
             "nodes": [{"id": node, "title": "长中文名称用于阅读关系与来源证据" + node, "kind": "entity"}
                       for node in ["root", *[f"n{i:02}" for i in range(33)]]],
             "edges": [{"id": str(i), "source_id": f"n{i:02}", "target_id": "root", "predicate": "part_of"}
                       for i in range(33)]}
    for size in ((1100, 800), (580, 750), (350, 750)):
        options = graph_options(graph, layout="hierarchy", canvas_size=size)
        nodes = options["series"][0]["data"]
        parent = next(node for node in nodes if node["id"] == "root")
        children = [node for node in nodes if node["id"] != "root"]
        assert len({node["y"] for node in children}) >= 3
        assert min(node["y"] for node in children) > parent["y"]
        assert {node["hierarchy_rank"] for node in children} == {1}
        assert all(node["label"]["position"] == "inside" and node["label"]["show"] for node in nodes)
        assert all(len(node["hierarchy_label"].splitlines()) <= 2 for node in nodes)
        assert len(options["series"][0]["links"]) == 33
        assert options["piece_meta"]["visible_nodes"] == 34
        assert all(node["title"] in node["tooltip_text"] for node in nodes)


def test_detail_labels_and_reduced_motion_preserve_selection_and_manual_positions():
    graph = sample_graph()
    node, edge = graph["nodes"][0]["id"], graph["edges"][0]["id"]
    options = graph_options(graph, selected=node, selected_edge=edge, label_density="focus",
                            reduced_motion=True, positions={node: (321, 456)})
    assert options["animation"] is False
    series = options["series"][0]
    assert (series["data"][0]["x"], series["data"][0]["y"]) == (321, 456)
    assert series["data"][0]["itemStyle"]["borderWidth"] > series["data"][1]["itemStyle"]["borderWidth"]
    assert series["links"][0]["label"]["show"] is True


def test_merge_refreshes_existing_metadata_and_tracks_neighbor_page_owner():
    original = sample_graph()
    incoming = deepcopy(original)
    incoming["root_id"] = original["nodes"][1]["id"]
    incoming["nodes"][0]["title"] = "Updated title"
    incoming["edges"][0]["evidence_count"] = 7
    incoming.update(depth=2, neighbor_offset=20, next_neighbor_offset=40, total_neighbors=60)
    result = merge_graph(original, incoming)
    assert result["root_id"] == original["root_id"]
    assert result["neighbor_root_id"] == incoming["root_id"]
    assert result["depth"] == 2 and result["next_neighbor_offset"] == 40
    assert result["neighbor_pages"][incoming["root_id"]]["total_neighbors"] == 60
    assert result["nodes"][0]["title"] == "Updated title"
    assert result["edges"][0]["evidence_count"] == 7
    result["nodes"][0]["title"] = "Another edit"
    assert incoming["nodes"][0]["title"] == "Updated title"
