import json
import xml.etree.ElementTree as ET
from copy import deepcopy

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
             "source_library_id": None, "source_file_id": 12, "source_chunk_id": 34, "source_title": "来源",
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


def test_graphml_roundtrip_types_owners_and_namespace():
    encoded = exchange.encode_graphml(snapshot())
    assert "xmlns=\"http://graphml.graphdrawing.org/xmlns\"" in encoded
    assert "示例 &lt;图谱&gt;" in encoded
    decoded = exchange.decode_graphml(encoded)
    assert decoded == exchange.decode_json(exchange.encode_json(snapshot()))
    assert decoded["entities"][0]["aliases"] == []
    assert type(decoded["entities"][0]["revision"]) is int
    evidence = {item["id"]: item for item in decoded["evidence"]}
    assert evidence[ER]["page_number"] == 3 and type(evidence[ER]["page_number"]) is int
    assert evidence[EO]["page_number"] is None


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


def test_graphml_rejects_unknown_properties_bad_types_and_mismatched_endpoints():
    xml = exchange.encode_graphml(snapshot())
    mutations = (
        xml.replace('attr.name="title"', 'attr.name="unknown"', 1),
        xml.replace('<data key="node.revision">1</data>', '<data key="node.revision">true</data>', 1),
        xml.replace('source="' + A + '"', 'source="' + B + '"', 1),
        xml.replace('<data key="node.piece.evidence_json">[]</data>',
                    '<data key="node.piece.evidence_json">{}</data>', 1),
    )
    for invalid in mutations:
        with pytest.raises(ValueError):
            exchange.decode_graphml(invalid)

    root = ET.fromstring(xml)
    node = next(element for element in root.iter() if element.tag.endswith("node"))
    node.set("external", "no")
    with pytest.raises(ValueError):
        exchange.decode_graphml(ET.tostring(root, encoding="unicode"))


def test_graphml_rejects_dtd_entity_and_payload_limits():
    with pytest.raises(ValueError):
        exchange.decode_graphml('<!DOCTYPE x [<!ENTITY e "boom">]><graphml/>')
    with pytest.raises(ValueError):
        exchange.decode_json(" " * (exchange.MAX_BYTES + 1))
    with pytest.raises(ValueError):
        exchange.decode_graphml(" " * (exchange.MAX_BYTES + 1))


def test_encoders_reject_invalid_snapshot_fields_and_limits():
    bad = snapshot()
    bad["evidence"][0]["dedup_key"] = "derived"
    with pytest.raises(ValueError):
        exchange.encode_json(bad)
    bad = snapshot()
    bad["entities"][0]["aliases"] = "[\"not a list\"]"
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
