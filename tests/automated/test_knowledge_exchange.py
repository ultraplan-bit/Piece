import json
import xml.etree.ElementTree as ET
from copy import deepcopy
from uuid import UUID, uuid4, uuid5

import pytest

from indexing import knowledge_exchange as exchange


A, B, R, EO, ER = (
    "00000000-0000-0000-0000-000000000001",
    "00000000-0000-0000-0000-000000000002",
    "00000000-0000-0000-0000-000000000003",
    "00000000-0000-0000-0000-000000000004",
    "00000000-0000-0000-0000-000000000005",
)


def snapshot():
    return {
        "graph": {"id": A, "name": "示例 <图谱>"},
        "entities": [
            {"id": B, "kind": "entity", "title": "乙", "summary": "summary", "aliases": ["别名"],
             "status": "active", "revision": 2, "created_at": "2026-01-01", "updated_at": "2026-01-02"},
            {"id": A, "kind": "concept", "title": "甲", "summary": "", "aliases": [], "status": "disputed",
             "revision": 1, "created_at": "2025-01-01", "updated_at": "2025-01-01"},
        ],
        "relations": [{"id": R, "source_id": A, "predicate": "supports", "target_id": B,
                       "description": "关系", "qualifier": "", "basis": "explicit", "status": "active",
                       "revision": 1, "created_at": "2026-01-01", "updated_at": "2026-01-01"}],
        "evidence": [
            {"id": EO, "object_id": A, "relation_id": None, "source_kind": "user", "stance": "supports",
             "source_library_id": None, "source_file_id": None, "source_chunk_id": None, "source_title": None,
             "source_url": None, "heading_path": None, "page_number": None, "quote": "引文",
             "content_hash": None, "created_at": "2026-01-03"},
            {"id": ER, "object_id": None, "relation_id": R, "source_kind": "external", "stance": "context",
             "source_library_id": None, "source_file_id": None, "source_chunk_id": None, "source_title": "来源",
             "source_url": "https://example.test", "heading_path": "章节", "page_number": 3, "quote": "证据",
             "content_hash": "abc", "created_at": "2026-01-04"},
        ],
    }


def test_json_roundtrip_is_stable_sorted_and_strips_envelope():
    data = snapshot()
    encoded = exchange.encode_json(data)
    payload = json.loads(encoded)
    assert payload["format"] == "piece-knowledge-graph" and payload["format_version"] == 1
    assert [item["id"] for item in payload["entities"]] == [A, B]
    assert [item["id"] for item in payload["evidence"]] == [EO, ER]
    assert exchange.encode_json(exchange.decode_json(encoded)) == encoded
    assert exchange.decode_json(encoded) == {key: payload[key] for key in ("graph", "entities", "relations", "evidence")}


def test_graphml_projection_is_plain_typed_and_stable():
    data = snapshot()
    data["entities"][0]["summary"] = '引号 " 与 <符号> & 换行\n第二行'
    data["relations"].append({**data["relations"][0], "id": EO, "predicate": "related_to"})
    data["evidence"] = []
    encoded = exchange.encode_graphml(data)
    root = ET.fromstring(encoded)
    assert root.tag == exchange._tag("graphml")
    assert "示例 &lt;图谱&gt;" in encoded
    keys = {key.get("id"): key for key in root.findall(exchange._tag("key"))}
    assert keys["edge.symmetric"].get("attr.type") == "boolean"
    assert {key.get("attr.name") for key in keys.values()}.isdisjoint({
        "id", "source_id", "target_id", "aliases", "revision", "created_at", "updated_at", "piece.evidence_json"})
    assert '<data key="node.label">甲</data>' in encoded
    assert '<data key="edge.symmetric">true</data>' in encoded
    assert '<data key="edge.symmetric">false</data>' in encoded
    decoded = exchange.decode_graphml(encoded)
    assert decoded["graph"] == data["graph"]
    for source, projected in zip(sorted(data["entities"], key=lambda item: item["id"]), decoded["entities"]):
        assert all(projected[field] == source[field] for field in ("id", "kind", "title", "summary", "status"))
        assert projected["aliases"] == [] and projected["revision"] == 1
        assert projected["created_at"] == projected["updated_at"] == ""
    assert decoded["evidence"] == []
    assert exchange.encode_graphml(decoded) == encoded


def test_graphml_discards_evidence_and_aliases_without_mutating_snapshot():
    data = snapshot()
    original = deepcopy(data)
    encoded = exchange.encode_graphml(data)
    assert data == original
    assert "别名" not in encoded and "引文" not in encoded and "example.test" not in encoded
    assert exchange.decode_graphml(encoded)["evidence"] == []


def test_json_rejects_unknown_missing_version_duplicate_ids_and_bad_owners():
    data = snapshot()
    payload = {"format": "piece-knowledge-graph", "format_version": 1, **data}
    bads = []
    bad = deepcopy(payload)
    bad["extra"] = 1
    bads.append(bad)
    bad = deepcopy(payload)
    bad["format_version"] = True
    bads.append(bad)
    bad = deepcopy(payload)
    bad["relations"][0]["extra"] = 1
    bads.append(bad)
    bad = deepcopy(payload)
    bad["evidence"][0]["object_id"] = "00000000-0000-0000-0000-000000000099"
    bads.append(bad)
    bad = deepcopy(payload)
    bad["relations"][0]["id"] = A
    bads.append(bad)
    for bad in bads:
        with pytest.raises(ValueError):
            exchange.decode_json(json.dumps(bad))
    with pytest.raises(ValueError):
        exchange.decode_json('{"format":"piece-knowledge-graph","format":"piece-knowledge-graph"}')


def test_graphml_accepts_schema_extra_attributes_order_and_defaults():
    xml = f'''<graphml xmlns="{exchange.GRAPHML_NS}"
        xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="{exchange.GRAPHML_NS} graphml.xsd">
      <key id="label" for="all" attr.name="label" attr.type="string"/>
      <key id="kind" for="node" attr.name="kind"><default>entity</default></key>
      <key id="weight" for="edge" attr.name="weight" attr.type="double"/>
      <key id="color" for="node" attr.name="color" attr.type="string"/>
      <graph id="G" edgedefault="directed" parse.nodes="2">
        <edge id="e0" source="n0" target="n1" directed="true" extra="ignored">
          <data key="label">supports</data><data key="weight">2.5</data>
        </edge>
        <node id="n1" extra="ignored"><data key="label">乙</data></node>
        <node id="n0"><data key="color">green</data><data key="label">甲</data></node>
      </graph>
    </graphml>'''
    decoded = exchange.decode_graphml(xml)
    assert decoded == exchange.decode_graphml(xml)
    assert decoded["graph"]["name"] == "导入图谱"
    nodes = {item["title"]: item for item in decoded["entities"]}
    relation = decoded["relations"][0]
    assert nodes["甲"]["kind"] == "entity" and nodes["甲"]["status"] == "active"
    assert relation["source_id"] == nodes["甲"]["id"] and relation["target_id"] == nodes["乙"]["id"]
    assert relation["predicate"] == "supports" and relation["basis"] == "inference"
    assert exchange._uuid(relation["id"], "edge.id") == relation["id"]


def test_graphml_accepts_missing_edge_and_graph_ids():
    xml = exchange.encode_graphml(snapshot()).replace(f' id="{A}" edgedefault=', ' edgedefault=')
    xml = xml.replace(f'<edge id="{R}"', '<edge')
    decoded = exchange.decode_graphml(xml)
    assert decoded["entities"][0]["id"] == A
    assert exchange._uuid(decoded["relations"][0]["id"], "edge.id")
    assert decoded == exchange.decode_graphml(xml)


@pytest.mark.parametrize("old,new", [
    ('attr.name="label"', 'attr.name="unknown"'),
    ('id="node.label" for="node" attr.name="label" attr.type="string"',
     'id="node.label" for="node" attr.name="label" attr.type="int"'),
    ('<data key="edge.symmetric">false</data>', '<data key="edge.symmetric">maybe</data>'),
    ('<data key="edge.symmetric">false</data>', '<data key="edge.symmetric">true</data>'),
    ('source="' + A + '"', 'source="missing"'),
    ('target="' + B + '"', 'target="' + A + '"'),
    ('<data key="edge.label">supports</data>', '<data key="edge.label">UNKNOWN</data>'),
    ('<data key="node.label">甲</data>', '<data key="node.label"> </data>'),
    ('<data key="node.kind">concept</data>', '<data key="node.kind">invalid</data>'),
    ('edgedefault="directed"', 'edgedefault="undirected"'),
    ('<edge id=', '<edge directed="false" id='),
    ('<data key="node.label">甲</data>', '<data key="node.label">甲</data><data key="node.label">重复</data>'),
    ('<data key="node.label">甲</data>', '<data key="missing">甲</data>'),
    ('attr.name="summary"', 'attr.name="piece.evidence_json"'),
])
def test_graphml_rejects_invalid_known_values_and_structure(old, new):
    xml = exchange.encode_graphml(snapshot())
    with pytest.raises(ValueError):
        exchange.decode_graphml(xml.replace(old, new, 1))


def test_graphml_rejects_duplicate_ids_nested_graphs_and_multiple_graphs():
    xml = exchange.encode_graphml(snapshot())
    for mutation in ("duplicate_node", "duplicate_edge", "nested", "multiple"):
        root = ET.fromstring(xml)
        graph = root.find(exchange._tag("graph"))
        assert graph is not None
        node, edge = graph.find(exchange._tag("node")), graph.find(exchange._tag("edge"))
        assert node is not None and edge is not None
        if mutation == "duplicate_node":
            graph.append(deepcopy(node))
        elif mutation == "duplicate_edge":
            graph.append(deepcopy(edge))
        elif mutation == "nested":
            ET.SubElement(node, exchange._tag("graph"), {"edgedefault": "directed"})
        else:
            root.append(deepcopy(graph))
        with pytest.raises(ValueError):
            exchange.decode_graphml(ET.tostring(root, encoding="unicode"))


def test_graphml_rejects_dtd_entity_and_graph_limits(monkeypatch):
    with pytest.raises(ValueError):
        exchange.decode_graphml('<!DOCTYPE x [<!ENTITY e "boom">]><graphml/>')
    xml = exchange.encode_graphml(snapshot())
    monkeypatch.setattr(exchange, "MAX_NODES", 1)
    with pytest.raises(ValueError):
        exchange.decode_graphml(xml)
    monkeypatch.setattr(exchange, "MAX_NODES", 10_000)
    monkeypatch.setattr(exchange, "MAX_EDGES", 0)
    with pytest.raises(ValueError):
        exchange.decode_graphml(xml)


@pytest.mark.parametrize("invalid", ["\x00", "\x01", chr(0xFFFE), chr(0xFFFF)])
def test_graphml_rejects_illegal_xml_characters_but_json_preserves_them(invalid):
    data = snapshot()
    data["entities"][0]["summary"] = invalid
    assert exchange.decode_json(exchange.encode_json(data))["entities"][1]["summary"] == invalid
    with pytest.raises(ValueError, match="XML 1.0"):
        exchange.encode_graphml(data)


def test_encoders_reject_invalid_snapshot_fields_and_limits():
    bad = snapshot()
    bad["evidence"][0]["dedup_key"] = "derived"
    with pytest.raises(ValueError):
        exchange.encode_json(bad)
    bad = snapshot()
    bad["entities"][0]["aliases"] = '["not a list"]'
    with pytest.raises(ValueError):
        exchange.encode_graphml(bad)
    bad = snapshot()
    bad["relations"][0]["source_id"] = bad["relations"][0]["id"]
    with pytest.raises(ValueError):
        exchange.encode_json(bad)
    bad = snapshot()
    bad["entities"] = bad["entities"] * 5001
    with pytest.raises(ValueError):
        exchange.encode_json(bad)


def test_service_graphml_counts_fresh_metadata_and_json_preservation(knowledge_base):
    from indexing.services import knowledge_service as knowledge

    imported = knowledge.import_graph({"format": "json", "document": exchange.encode_json(snapshot()),
                                       "dry_run": False, "confirmed": True})
    source_id = imported["graph_id"]
    full = knowledge.export_graph(graph_id=source_id, format="json")
    assert full["evidence"] == 2
    portable = knowledge.export_graph(graph_id=source_id, format="graphml")
    assert portable["entities"] == 2 and portable["relations"] == 1 and portable["evidence"] == 0
    preview = knowledge.import_graph({"format": "graphml", "document": portable["content"], "graph_name": "通用图谱"})
    assert preview["committed"] is False and preview["counts"]["evidence"] == 0
    result = knowledge.import_graph({"format": "graphml", "document": portable["content"], "graph_name": "通用图谱",
                                     "dry_run": False, "confirmed": True})
    assert result["committed"] is True and result["counts"] == {"entities": 2, "relations": 1, "evidence": 0}
    projected = exchange.decode_json(knowledge.export_graph(graph_id=result["graph_id"], format="json")["content"])
    assert projected["evidence"] == []
    for record in projected["entities"] + projected["relations"]:
        assert record["revision"] == 1
        assert record["created_at"] and record["updated_at"]
        assert record["created_at"] not in {"2025-01-01", "2026-01-01"}
    assert all(item["aliases"] == [] for item in projected["entities"])
    assert knowledge.export_graph(graph_id=source_id, format="json") == full


def test_graphml_networkx_import_and_reexport_preserve_plain_attributes():
    nx = pytest.importorskip("networkx")
    data = snapshot()
    data["relations"].append({**data["relations"][0], "id": "00000000-0000-0000-0000-000000000006",
                              "predicate": "related_to"})
    graph = nx.parse_graphml(exchange.encode_graphml(data))
    assert graph.number_of_nodes() == 2 and graph.number_of_edges() == 2
    assert graph.is_directed() and graph.is_multigraph()
    assert graph.nodes[A]["label"] == "甲" and graph.graph["name"] == "示例 <图谱>"
    edges = {attributes["label"]: attributes for _, _, attributes in graph.edges(data=True)}
    assert edges["supports"]["symmetric"] is False and edges["related_to"]["symmetric"] is True
    rewritten = "\n".join(nx.generate_graphml(graph, edge_id_from_attribute="id"))
    decoded = exchange.decode_graphml(rewritten)
    assert {item["id"] for item in decoded["entities"]} == {A, B}
    assert {item["predicate"] for item in decoded["relations"]} == {"supports", "related_to"}
    assert decoded["evidence"] == []


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("invalid", ["empty_quote", "unsafe_url", "missing_chunk", "missing_foreign_title"])
def test_import_validates_sources_and_rolls_back(knowledge_base, dry_run, invalid):
    from indexing.services import knowledge_service as knowledge
    from indexing.services.errors import BusinessError

    data = snapshot()
    data["evidence"] = [data["evidence"][0]]
    evidence = data["evidence"][0]
    if invalid == "empty_quote":
        evidence["quote"] = " \n "
    elif invalid == "unsafe_url":
        evidence.update(source_kind="external", source_title="网页", source_url="javascript:alert(1)")
    else:
        evidence.update(source_kind="piece", source_file_id=1, source_chunk_id=1,
                        source_library_id=knowledge.library_id() if invalid == "missing_chunk" else str(uuid4()))
    before = knowledge.list_graphs()
    with pytest.raises(BusinessError):
        knowledge.import_graph({"format": "json", "document": exchange.encode_json(data),
                                "dry_run": dry_run, "confirmed": not dry_run})
    assert knowledge.list_graphs() == before
    assert knowledge.list_objects()["total"] == 0


@pytest.mark.parametrize("format", ["json", "graphml"])
def test_import_reports_incoming_symmetric_duplicates_without_writing(knowledge_base, format):
    from indexing.services import knowledge_service as knowledge

    data = snapshot()
    data["evidence"] = []
    data["relations"][0]["predicate"] = "related_to"
    data["relations"].append({**data["relations"][0], "id": ER, "source_id": B, "target_id": A})
    document = exchange.encode_json(data) if format == "json" else exchange.encode_graphml(data)
    before = knowledge.list_graphs()
    for dry_run in (True, False):
        result = knowledge.import_graph({"format": format, "document": document,
                                         "dry_run": dry_run, "confirmed": not dry_run})
        assert not result["committed"]
        assert result["conflicts"] == [{"kind": "relation", "id": ER, "reason": "unique_key"}]
        assert knowledge.list_graphs() == before


@pytest.mark.parametrize("dry_run", [True, False])
def test_import_rejects_duplicate_graph_names_before_confirmation(knowledge_base, dry_run):
    from indexing.services import knowledge_service as knowledge
    from indexing.services.errors import BusinessError

    knowledge.create_graph(snapshot()["graph"]["name"])
    before = knowledge.list_graphs()
    with pytest.raises(BusinessError, match="同名"):
        knowledge.import_graph({"format": "json", "document": exchange.encode_json(snapshot()),
                                "dry_run": dry_run, "confirmed": not dry_run})
    assert knowledge.list_graphs() == before


@pytest.mark.parametrize("local", [True, False])
def test_json_import_preserves_evidence_snapshots_and_valid_preview_is_read_only(knowledge_base, local):
    from indexing.services import knowledge_service as knowledge
    from test_knowledge_service import _seed_chunk

    data = snapshot()
    data["evidence"] = [data["evidence"][0]]
    evidence = data["evidence"][0]
    file_id, chunk_id = _seed_chunk(knowledge_base, evidence["quote"]) if local else (1, 2)
    evidence.update(source_kind="piece", source_title="原始出处", source_file_id=file_id,
                    source_chunk_id=chunk_id, source_library_id=knowledge.library_id() if local else str(uuid4()),
                    heading_path="第 3 页", page_number=3, content_hash="old-source-hash")
    document = exchange.encode_json(data)
    before = knowledge.list_graphs()
    preview = knowledge.import_graph({"format": "json", "document": document})
    assert not preview["committed"] and preview["conflicts"] == []
    assert knowledge.list_graphs() == before
    result = knowledge.import_graph({"format": "json", "document": document, "dry_run": False, "confirmed": True})
    record = knowledge.get_record(graph_id=result["graph_id"], kind="evidence", id=evidence["id"])["record"]
    assert {key: record[key] for key in exchange.EVIDENCE_FIELDS} == evidence
    assert record["location_status"] == ("changed" if local else "unresolved")


def test_import_validates_original_quote_without_trimming(knowledge_base):
    from indexing.services import knowledge_service as knowledge
    from indexing.services.errors import BusinessError
    from test_knowledge_service import _seed_chunk

    data = snapshot()
    data["evidence"] = [data["evidence"][0]]
    evidence = data["evidence"][0]
    file_id, chunk_id = _seed_chunk(knowledge_base, evidence["quote"])
    evidence.update(source_kind="piece", source_title="本库来源", source_library_id=knowledge.library_id(),
                    source_file_id=file_id, source_chunk_id=chunk_id, quote=" " + evidence["quote"] + " ")
    with pytest.raises(BusinessError) as error:
        knowledge.import_graph({"format": "json", "document": exchange.encode_json(data)})
    assert error.value.code == "QUOTE_MISMATCH"


def test_evidence_delete_confirmation_is_bound_to_graph(knowledge_base):
    from indexing.services import knowledge_service as knowledge
    from indexing.services.errors import BusinessError

    data = snapshot()
    graphs = []
    for name in ("空间 A", "空间 B"):
        data["evidence"][0]["quote"] = name
        result = knowledge.import_graph({"format": "json", "document": exchange.encode_json(data),
                                         "graph_name": name, "dry_run": False, "confirmed": True})
        graphs.append(result["graph_id"])
    previews = [knowledge.delete({"graph_id": graph_id, "kind": "evidence", "id": EO, "dry_run": True})
                for graph_id in graphs]
    assert previews[0]["impact_token"] != previews[1]["impact_token"]
    deletion = {"graph_id": graphs[1], "kind": "evidence", "id": EO, "dry_run": False,
                "confirmed": True, "request_key": str(uuid4()), "impact_token": previews[0]["impact_token"]}
    with pytest.raises(BusinessError) as error:
        knowledge.delete(deletion)
    assert error.value.code == "IMPACT_CONFLICT"
    assert knowledge.get_record(graph_id=graphs[1], id=EO)["record"]["quote"] == "空间 B"
    deletion["impact_token"] = previews[1]["impact_token"]
    assert knowledge.delete(deletion)["committed"]
    assert knowledge.get_record(graph_id=graphs[0], id=EO)["record"]["quote"] == "空间 A"


@pytest.mark.parametrize("explicit_id", ["edge-1", str(uuid5(UUID(A), "anonymous-edge:0"))])
def test_graphml_generated_ids_avoid_all_explicit_ids(explicit_id):
    data = snapshot()
    data["evidence"] = []
    data["relations"].append({**data["relations"][0], "id": ER, "predicate": "part_of"})
    root = ET.fromstring(exchange.encode_graphml(data))
    graph = root.find(exchange._tag("graph"))
    assert graph is not None
    edges = graph.findall(exchange._tag("edge"))
    edges[0].attrib.pop("id")
    edges[1].set("id", explicit_id)
    document = ET.tostring(root, encoding="unicode")
    decoded = exchange.decode_graphml(document)
    assert len({item["id"] for item in decoded["relations"]}) == 2
    assert exchange.decode_graphml(document) == decoded


@pytest.mark.parametrize("format", ["json", "graphml"])
def test_large_graphs_export_and_reimport_without_byte_limit(knowledge_base, format):
    from indexing.services import knowledge_service as knowledge

    data = snapshot()
    data["entities"] = [{**data["entities"][0], "id": str(uuid4()), "summary": "x" * 4000} for _ in range(600)]
    data["relations"] = []
    data["evidence"] = []
    document = exchange.encode_json(data)
    assert len(document.encode("utf-8")) > 2 * 1024 * 1024
    imported = knowledge.import_graph({"format": "json", "document": document, "dry_run": False, "confirmed": True})
    exported = knowledge.export_graph(graph_id=imported["graph_id"], format=format)
    assert len(exported["content"].encode("utf-8")) > 2 * 1024 * 1024
    arguments = {"format": format, "document": exported["content"], "graph_name": "大图目标"}
    preview = knowledge.import_graph(arguments)
    assert not preview["committed"] and preview["conflicts"] == []
    result = knowledge.import_graph({**arguments, "dry_run": False, "confirmed": True})
    assert result["committed"] and result["counts"] == {"entities": 600, "relations": 0, "evidence": 0}
    assert knowledge.list_objects(graph_id=result["graph_id"])["total"] == 600
