"""Graph exploration can retrieve nodes and cross-page relationships without losing direction."""
from uuid import uuid4

from indexing.services import knowledge_service as service


def graph_fixture():
    result = service.apply({"request_key": str(uuid4()), "reason": "Graph pagination fixture", "objects": [
        {"ref": str(index), "title": f"Node {index}", "kind": "concept", "summary": "", "aliases": [], "status": "active"}
        for index in range(7)], "relations": [
        {"ref": f"edge{index}", "source": {"ref": "0"}, "target": {"ref": str(index)},
         "predicate": "depends_on", "description": "Dependency", "basis": "explicit"}
        for index in range(1, 6)]})
    return [result["refs"][str(index)]["id"] for index in range(7)], {edge["id"] for edge in result["relations"]}


def test_overview_node_pages_and_edge_pages_preserve_cross_page_relationships(knowledge_base):
    ids, relation_ids = graph_fixture()
    known, seen, offset, scopes = set(), {}, 0, []
    while True:
        page = service.graph_overview(max_nodes=2, max_edges=1, offset=offset, known_node_ids=sorted(known))
        added = {node["id"] for node in page["nodes"]}
        assert not known & added
        known.update(added)
        scopes.append(page["edge_scope"])
        for edge in page["edges"]:
            assert {edge["source_id"], edge["target_id"]} <= known
            seen[edge["id"]] = edge
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert known == set(ids)
    assert len(set(scopes)) == len(scopes)
    while page["next_edge_offset"] is not None:
        page = service.graph_overview(load_nodes=False, max_edges=1, known_node_ids=sorted(known),
                                      edge_offset=page["next_edge_offset"])
        assert not page["nodes"]
        assert page["edge_scope"] == scopes[-1]
        seen.update({edge["id"]: edge for edge in page["edges"]})
    assert set(seen) == relation_ids
    assert page["scope_edges"] == len(relation_ids)
    assert all(edge["source_id"] == ids[0] and edge["predicate"] == "depends_on" for edge in seen.values())


def test_isolated_overview_is_filterable_and_counts_neighbors_in_full_scope(knowledge_base):
    ids, _ = graph_fixture()
    page = service.graph_overview(max_nodes=1)
    assert page["nodes"][0]["id"] == ids[0]
    assert page["nodes"][0]["neighbor_count"] == 5
    assert page["total_isolated"] == 1
    isolated = service.graph_overview(isolated_only=True)
    assert [node["id"] for node in isolated["nodes"]] == [ids[6]]
    assert isolated["filtered_nodes"] == 1 and isolated["total_nodes"] == 7
    assert isolated["next_offset"] is None and not isolated["edges"]
    filtered = service.graph_overview(isolated_only=True, predicates=["supports"])
    assert filtered["filtered_nodes"] == 7


def test_neighbor_pages_keep_root_and_fetch_each_neighbor_once(knowledge_base):
    ids, relations = graph_fixture()
    offset, neighbors, edges = 0, set(), set()
    while True:
        page = service.graph(root_id=ids[0], depth=1, max_nodes=3, neighbor_offset=offset)
        assert page["nodes"][0]["id"] == ids[0]
        assert page["total_neighbors"] == 5
        current = {node["id"] for node in page["nodes"]} - {ids[0]}
        assert not current & neighbors
        neighbors.update(current)
        edges.update(edge["id"] for edge in page["edges"])
        if page["next_neighbor_offset"] is None:
            break
        offset = page["next_neighbor_offset"]
    assert neighbors == set(ids[1:6])
    assert edges == relations


def test_known_node_ids_are_scoped_to_the_selected_graph(knowledge_base):
    ids, _ = graph_fixture()
    other = service.create_graph("Separate")
    page = service.graph_overview(graph_id=other["id"], known_node_ids=ids, load_nodes=False)
    assert page["scope_edges"] == 0 and page["edges"] == [] and page["nodes"] == []
