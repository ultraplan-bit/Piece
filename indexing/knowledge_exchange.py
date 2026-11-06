"""纯图谱 payload 编解码；不访问数据库或服务。"""

import json
import re
import xml.etree.ElementTree as ET
from uuid import UUID


FORMAT = "piece-knowledge-graph"
FORMAT_VERSION = 1
GRAPHML_NS = "http://graphml.graphdrawing.org/xmlns"
MAX_BYTES = 2 * 1024 * 1024
MAX_NODES = 10_000
MAX_EDGES = 20_000

ENTITY_FIELDS = ("id", "kind", "title", "summary", "aliases", "status", "revision", "created_at", "updated_at")
RELATION_FIELDS = ("id", "source_id", "predicate", "target_id", "description", "qualifier", "basis", "status", "revision", "created_at", "updated_at")
EVIDENCE_FIELDS = ("id", "object_id", "relation_id", "source_kind", "stance", "source_library_id", "source_file_id", "source_chunk_id", "source_title", "source_url", "heading_path", "page_number", "quote", "content_hash", "created_at")
OPTIONAL_STRINGS = {"source_library_id", "source_title", "source_url", "heading_path", "content_hash"}
OPTIONAL_INTS = {"source_file_id", "source_chunk_id", "page_number"}


def _json_loads(text):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"重复 JSON 字段：{key}")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=unique_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"非法 JSON 数值：{value}")))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("无效 JSON") from exc


def _input_text(text):
    if not isinstance(text, str):
        raise ValueError("输入必须是字符串")
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("输入不是有效 UTF-8") from exc
    if len(raw) > MAX_BYTES:
        raise ValueError("payload 超过 2 MiB")
    return raw


def _keys(record, fields, label):
    if not isinstance(record, dict) or set(record) != set(fields):
        raise ValueError(f"{label}字段不完整或包含未知字段")


def _string(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} 不是有效 UTF-8 字符串") from exc


def _uuid(value, field):
    _string(value, field)
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field} 必须是 UUID") from exc


def _validate_record(record, fields, kind):
    _keys(record, fields, kind)
    for field, value in record.items():
        if field == "id":
            _uuid(value, f"{kind}.id")
        elif field == "aliases":
            if not isinstance(value, list) or any(not isinstance(alias, str) for alias in value):
                raise ValueError("entity.aliases 必须是字符串数组")
        elif field == "revision":
            if type(value) is not int or value < 1:
                raise ValueError(f"{kind}.revision 必须是正整数")
        elif kind == "evidence" and field in OPTIONAL_STRINGS | {"object_id", "relation_id"}:
            if value is not None:
                if field in {"object_id", "relation_id", "source_library_id"}:
                    _uuid(value, f"{kind}.{field}")
                else:
                    _string(value, f"{kind}.{field}")
        elif kind == "evidence" and field in OPTIONAL_INTS:
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{kind}.{field} 必须是正整数或 null")
        else:
            _string(value, f"{kind}.{field}")

    enums = {
        "kind": {"concept", "entity"}, "status": {"active", "disputed", "outdated"},
        "predicate": {"is_a", "part_of", "depends_on", "applies_to", "supports", "contradicts", "related_to"},
        "basis": {"explicit", "synthesis", "inference", "user_statement"},
        "source_kind": {"piece", "external", "user"}, "stance": {"supports", "contradicts", "context"},
    }
    for field, values in enums.items():
        if field in record and record[field] not in values:
            raise ValueError(f"{kind}.{field} 值无效")
    if kind == "relation" and record["source_id"] == record["target_id"]:
        raise ValueError("relation 不允许自连接")
    if kind == "evidence" and ((record["object_id"] is None) == (record["relation_id"] is None)):
        raise ValueError("证据必须且只能有一个 owner")


def _snapshot(graph, entities, relations, evidence):
    _keys(graph, ("id", "name"), "graph")
    _uuid(graph["id"], "graph.id")
    _string(graph["name"], "graph.name")
    if not all(isinstance(items, list) for items in (entities, relations, evidence)):
        raise ValueError("entities、relations、evidence 必须是数组")
    if len(entities) > MAX_NODES or len(relations) > MAX_EDGES:
        raise ValueError("图谱节点或边数量超限")

    ids = set()
    entity_ids, relation_ids = set(), set()
    for records, fields, kind, own_ids in (
        (entities, ENTITY_FIELDS, "entity", entity_ids),
        (relations, RELATION_FIELDS, "relation", relation_ids),
        (evidence, EVIDENCE_FIELDS, "evidence", None),
    ):
        for record in records:
            _validate_record(record, fields, kind)
            record_id = str(UUID(record["id"]))
            if record_id in ids:
                raise ValueError(f"重复记录 ID：{record['id']}")
            ids.add(record_id)
            if own_ids is not None:
                own_ids.add(record_id)

    for relation in relations:
        source, target = str(UUID(relation["source_id"])), str(UUID(relation["target_id"]))
        if source not in entity_ids or target not in entity_ids or source == target:
            raise ValueError("关系端点不存在或关系为自连接")
    for item in evidence:
        object_id, relation_id = item["object_id"], item["relation_id"]
        if (object_id is None) == (relation_id is None):
            raise ValueError("证据必须且只能有一个 owner")
        owner = object_id if object_id is not None else relation_id
        allowed = entity_ids if object_id is not None else relation_ids
        if str(UUID(owner)) not in allowed:
            raise ValueError("证据 owner 不存在或类型不匹配")
    canonical_entities = [{field: item[field] for field in ENTITY_FIELDS} for item in entities]
    canonical_relations = [{field: item[field] for field in RELATION_FIELDS} for item in relations]
    canonical_evidence = [{field: item[field] for field in EVIDENCE_FIELDS} for item in evidence]
    for item in canonical_entities + canonical_relations + canonical_evidence:
        item["id"] = str(UUID(item["id"]))
    for item in canonical_relations:
        item["source_id"] = str(UUID(item["source_id"]))
        item["target_id"] = str(UUID(item["target_id"]))
    for item in canonical_evidence:
        for field in ("object_id", "relation_id"):
            if item[field] is not None:
                item[field] = str(UUID(item[field]))
    return {
        "graph": {"id": str(UUID(graph["id"])), "name": graph["name"]},
        "entities": canonical_entities,
        "relations": canonical_relations,
        "evidence": canonical_evidence,
    }


def _ordered(snapshot):
    return {"graph": snapshot["graph"], **{
        key: sorted(snapshot[key], key=lambda record: record["id"])
        for key in ("entities", "relations", "evidence")}}


def encode_json(snapshot):
    graph = snapshot.get("graph") if isinstance(snapshot, dict) else None
    if not isinstance(snapshot, dict) or set(snapshot) != {"graph", "entities", "relations", "evidence"}:
        raise ValueError("snapshot 必须包含 graph/entities/relations/evidence")
    checked = _snapshot(graph, snapshot["entities"], snapshot["relations"], snapshot["evidence"])
    return json.dumps({"format": FORMAT, "format_version": FORMAT_VERSION, **_ordered(checked)},
                      ensure_ascii=False, separators=(",", ":"))


def decode_json(text):
    _input_text(text)
    payload = _json_loads(text)
    if not isinstance(payload, dict) or set(payload) != {"format", "format_version", "graph", "entities", "relations", "evidence"}:
        raise ValueError("JSON 根字段无效")
    if payload["format"] != FORMAT or type(payload["format_version"]) is not int or payload["format_version"] != FORMAT_VERSION:
        raise ValueError("不支持的知识图谱格式或版本")
    return _snapshot(payload["graph"], payload["entities"], payload["relations"], payload["evidence"])


def _tag(name):
    return f"{{{GRAPHML_NS}}}{name}"


def _key_id(scope, name):
    return f"{scope}.{name}"


def _graphml_keys():
    return [("graph", "name")] + [("node", field) for field in ENTITY_FIELDS + ("piece.evidence_json",)] + [
        ("edge", field) for field in RELATION_FIELDS + ("piece.evidence_json",)]


def _xml_data(parent, key, value):
    data = ET.SubElement(parent, _tag("data"), {"key": key})
    data.text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def encode_graphml(snapshot):
    if not isinstance(snapshot, dict) or set(snapshot) != {"graph", "entities", "relations", "evidence"}:
        raise ValueError("snapshot 必须包含 graph/entities/relations/evidence")
    checked = _ordered(_snapshot(snapshot["graph"], snapshot["entities"], snapshot["relations"], snapshot["evidence"]))
    root = ET.Element(_tag("graphml"))
    for scope, name in _graphml_keys():
        ET.SubElement(root, _tag("key"), {"id": _key_id(scope, name), "for": scope,
                                           "attr.name": name, "attr.type": "string"})
    graph = ET.SubElement(root, _tag("graph"), {"id": checked["graph"]["id"], "edgedefault": "directed"})
    _xml_data(graph, _key_id("graph", "name"), checked["graph"]["name"])
    entity_evidence = {record["id"]: [] for record in checked["entities"]}
    relation_evidence = {record["id"]: [] for record in checked["relations"]}
    for item in checked["evidence"]:
        owner = item["object_id"] if item["object_id"] is not None else item["relation_id"]
        (entity_evidence if item["object_id"] is not None else relation_evidence)[owner].append(item)
    for entity in checked["entities"]:
        node = ET.SubElement(graph, _tag("node"), {"id": entity["id"]})
        for field in ENTITY_FIELDS:
            _xml_data(node, _key_id("node", field), entity[field])
        _xml_data(node, _key_id("node", "piece.evidence_json"), entity_evidence[entity["id"]])
    for relation in checked["relations"]:
        edge = ET.SubElement(graph, _tag("edge"), {"id": relation["id"], "source": relation["source_id"], "target": relation["target_id"]})
        for field in RELATION_FIELDS:
            _xml_data(edge, _key_id("edge", field), relation[field])
        _xml_data(edge, _key_id("edge", "piece.evidence_json"), relation_evidence[relation["id"]])
    ET.register_namespace("", GRAPHML_NS)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def decode_graphml(text):
    raw = _input_text(text)
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw, re.IGNORECASE):
        raise ValueError("GraphML 禁止 DTD/ENTITY")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("无效 GraphML XML") from exc
    if root.tag != _tag("graphml") or root.attrib or root.text and root.text.strip():
        raise ValueError("GraphML 根结构无效")
    keys, declarations = {}, set()
    graph_elements = []
    for child in root:
        if child.tag == _tag("graph"):
            graph_elements.append(child)
            continue
        if child.tag != _tag("key") or set(child.attrib) != {"id", "for", "attr.name", "attr.type"} or list(child):
            raise ValueError("GraphML 仅允许已知 key 声明")
        scope, name = child.get("for"), child.get("attr.name")
        if scope not in {"graph", "node", "edge"} or child.get("attr.type") != "string" or child.text and child.text.strip():
            raise ValueError("GraphML key 声明无效")
        expected = {"graph": {"name"}, "node": set(ENTITY_FIELDS) | {"piece.evidence_json"},
                    "edge": set(RELATION_FIELDS) | {"piece.evidence_json"}}
        if name not in expected[scope] or child.get("id") in keys or (scope, name) in declarations:
            raise ValueError("GraphML 包含未知或重复 key")
        keys[child.get("id")] = (scope, name)
        declarations.add((scope, name))
    if declarations != set(_graphml_keys()):
        raise ValueError("GraphML key 声明不完整")
    if len(graph_elements) != 1 or len(root) != len(keys) + 1:
        raise ValueError("GraphML 必须且只能有一个 graph")
    graph_element = graph_elements[0]
    if graph_element.tag != _tag("graph") or set(graph_element.attrib) != {"id", "edgedefault"} or graph_element.get("edgedefault") != "directed":
        raise ValueError("GraphML graph 结构无效")

    def read_data(parent, scope, expected_names):
        values = {}
        for child in parent:
            if scope == "graph" and child.tag in {_tag("node"), _tag("edge")}:
                continue
            if child.tag != _tag("data") or set(child.attrib) != {"key"} or list(child):
                raise ValueError(f"GraphML {scope} 中包含未知结构")
            key = keys.get(child.get("key"))
            if key is None or key[0] != scope or key[1] not in expected_names or key[1] in values:
                raise ValueError(f"GraphML {scope} 中包含未知或重复字段")
            values[key[1]] = _json_loads(child.text or "")
        if set(values) != set(expected_names):
            raise ValueError(f"GraphML {scope} 字段不完整")
        return values

    graph_data = read_data(graph_element, "graph", {"name"})
    _string(graph_data["name"], "graph.name")
    graph = {"id": graph_element.get("id"), "name": graph_data["name"]}
    entities, relations, evidence = [], [], []
    children = list(graph_element)
    if not children or children[0].tag != _tag("data"):
        raise ValueError("GraphML graph 缺少 name 数据")
    seen_edge = False
    for child in children:
        if child.tag == _tag("edge"):
            seen_edge = True
        elif child.tag == _tag("node") and seen_edge:
            raise ValueError("GraphML 节点必须位于边之前")
        elif child.tag != _tag("node") and child.tag != _tag("edge") and child.tag != _tag("data"):
            raise ValueError("GraphML graph 中包含未知节点")
    for child in graph_element:
        if child.tag == _tag("data"):
            continue
        if child.tag == _tag("node"):
            if set(child.attrib) != {"id"} or len(entities) >= MAX_NODES:
                raise ValueError("GraphML node 属性无效或节点数量超限")
            values = read_data(child, "node", set(ENTITY_FIELDS) | {"piece.evidence_json"})
            node_id = child.get("id")
            if values["id"] != node_id:
                raise ValueError("GraphML node id 与实体 id 不一致")
            entities.append({field: values[field] for field in ENTITY_FIELDS})
            evidence_json = values["piece.evidence_json"]
            if not isinstance(evidence_json, list):
                raise ValueError("piece.evidence_json 必须是数组")
            evidence.extend(evidence_json)
        elif child.tag == _tag("edge"):
            if set(child.attrib) != {"id", "source", "target"} or len(relations) >= MAX_EDGES:
                raise ValueError("GraphML edge 属性无效或边数量超限")
            values = read_data(child, "edge", set(RELATION_FIELDS) | {"piece.evidence_json"})
            edge_id = child.get("id")
            if values["id"] != edge_id or values["source_id"] != child.get("source") or values["target_id"] != child.get("target"):
                raise ValueError("GraphML edge id/端点与关系字段不一致")
            relations.append({field: values[field] for field in RELATION_FIELDS})
            evidence_json = values["piece.evidence_json"]
            if not isinstance(evidence_json, list):
                raise ValueError("piece.evidence_json 必须是数组")
            evidence.extend(evidence_json)
        else:
            raise ValueError("GraphML graph 中包含未知节点")
    return _snapshot(graph, entities, relations, evidence)
