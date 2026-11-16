"""纯图谱 payload 编解码；不访问数据库或服务。"""

import json
import re
import xml.etree.ElementTree as ET
from uuid import UUID, NAMESPACE_URL, uuid5


FORMAT = "piece-knowledge-graph"
FORMAT_VERSION = 1
GRAPHML_NS = "http://graphml.graphdrawing.org/xmlns"
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
        for field in ("object_id", "relation_id", "source_library_id"):
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
    content = json.dumps({"format": FORMAT, "format_version": FORMAT_VERSION, **_ordered(checked)},
                         ensure_ascii=False, separators=(",", ":"))
    _input_text(content)
    return content


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


SYMMETRIC = frozenset({"related_to", "contradicts"})
GRAPHML_FIELDS = {
    "graph": {"name": "string"},
    "node": dict.fromkeys(("label", "kind", "summary", "status"), "string"),
    "edge": {**dict.fromkeys(("label", "description", "qualifier", "basis", "status"), "string"),
             "symmetric": "boolean"},
}


def _xml_data(parent, key, value):
    text = ("true" if value else "false") if type(value) is bool else value
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text) or any(ord(char) in {0xFFFE, 0xFFFF} for char in text):
        raise ValueError("GraphML 属性包含 XML 1.0 不允许的字符")
    ET.SubElement(parent, _tag("data"), {"key": key}).text = text


def encode_graphml(snapshot):
    if not isinstance(snapshot, dict) or set(snapshot) != {"graph", "entities", "relations", "evidence"}:
        raise ValueError("snapshot 必须包含 graph/entities/relations/evidence")
    checked = _ordered(_snapshot(snapshot["graph"], snapshot["entities"], snapshot["relations"], snapshot["evidence"]))
    root = ET.Element(_tag("graphml"))
    for scope, fields in GRAPHML_FIELDS.items():
        for name, value_type in fields.items():
            ET.SubElement(root, _tag("key"), {"id": f"{scope}.{name}", "for": scope,
                                            "attr.name": name, "attr.type": value_type})
    graph = ET.SubElement(root, _tag("graph"), {"id": checked["graph"]["id"], "edgedefault": "directed"})
    _xml_data(graph, "graph.name", checked["graph"]["name"])
    for entity in checked["entities"]:
        node = ET.SubElement(graph, _tag("node"), {"id": entity["id"]})
        for name in GRAPHML_FIELDS["node"]:
            _xml_data(node, f"node.{name}", entity["title" if name == "label" else name])
    for relation in checked["relations"]:
        edge = ET.SubElement(graph, _tag("edge"), {"id": relation["id"], "source": relation["source_id"],
                                                 "target": relation["target_id"]})
        values = {**relation, "label": relation["predicate"], "symmetric": relation["predicate"] in SYMMETRIC}
        for name in GRAPHML_FIELDS["edge"]:
            _xml_data(edge, f"edge.{name}", values[name])
    ET.register_namespace("", GRAPHML_NS)
    content = ET.tostring(root, encoding="unicode", xml_declaration=True)
    _input_text(content)
    return content


def _graphml_id(value, namespace, scope):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"GraphML {scope} 缺少有效 ID")
    try:
        return str(UUID(value))
    except ValueError:
        return str(uuid5(namespace, f"{scope}:{value}"))


def decode_graphml(text):
    raw = _input_text(text)
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw, re.IGNORECASE):
        raise ValueError("GraphML 禁止 DTD/ENTITY")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("无效 GraphML XML") from exc
    if root.tag != _tag("graphml"):
        raise ValueError("GraphML 根结构无效")
    keys, declarations, graph_elements = {}, set(), []
    for child in root:
        if child.tag == _tag("desc"):
            continue
        if child.tag == _tag("graph"):
            graph_elements.append(child)
            continue
        if child.tag != _tag("key"):
            raise ValueError("GraphML 根中包含不支持的结构")
        key_id, scope, name = child.get("id"), child.get("for", "all"), child.get("attr.name")
        value_type = child.get("attr.type", "string")
        if not key_id or key_id in keys or scope not in {"all", "graph", "node", "edge"}:
            raise ValueError("GraphML key ID 重复或声明无效")
        if (scope, name) in declarations or value_type not in {"string", "boolean", "int", "long", "float", "double"}:
            raise ValueError("GraphML key 名称重复或类型无效")
        if name == "piece.evidence_json":
            raise ValueError("不支持旧版 Piece GraphML，请使用 JSON 或重新导出通用 GraphML")
        for target_scope in GRAPHML_FIELDS if scope == "all" else (scope,):
            expected_type = GRAPHML_FIELDS[target_scope].get(name)
            if expected_type is not None and value_type != expected_type:
                raise ValueError(f"GraphML {target_scope}.{name} 类型必须为 {expected_type}")
        defaults = child.findall(_tag("default"))
        if len(defaults) > 1 or any(item.tag not in {_tag("default"), _tag("desc")} for item in child):
            raise ValueError("GraphML key 默认值结构无效")
        if defaults and list(defaults[0]):
            raise ValueError("GraphML key 仅支持标量默认值")
        keys[key_id] = (scope, name, value_type, defaults[0].text or "" if defaults else None)
        declarations.add((scope, name))
    if len(graph_elements) != 1:
        raise ValueError("GraphML 必须且只能有一个 graph")
    graph_element = graph_elements[0]
    if graph_element.get("edgedefault") != "directed":
        raise ValueError("GraphML 仅支持单层有向图")

    def scalar(value, value_type):
        if value_type == "boolean":
            value = value.strip().lower()
            if value not in {"true", "false", "1", "0"}:
                raise ValueError("GraphML boolean 值无效")
            return value in {"true", "1"}
        return value

    def read_data(parent, scope):
        values = {}
        for declared_scope, name, value_type, default in keys.values():
            if declared_scope in {scope, "all"} and name in GRAPHML_FIELDS[scope] and default is not None:
                if name in values:
                    raise ValueError(f"GraphML {scope}.{name} 默认值重复")
                values[name] = scalar(default, value_type)
        seen = set()
        for child in parent:
            if child.tag == _tag("desc") or scope == "graph" and child.tag in {_tag("node"), _tag("edge")}:
                continue
            if child.tag != _tag("data"):
                raise ValueError(f"GraphML {scope} 中包含不支持的结构")
            key = keys.get(child.get("key"))
            if key is None or key[0] not in {scope, "all"}:
                raise ValueError("GraphML data 引用了未声明或作用域不匹配的 key")
            _, name, value_type, _ = key
            if name not in GRAPHML_FIELDS[scope]:
                continue
            if name in seen or list(child):
                raise ValueError(f"GraphML {scope}.{name} 重复或不是标量")
            seen.add(name)
            values[name] = scalar(child.text or "", value_type)
        return values

    graph_id = _graphml_id(graph_element.get("id", "graph"), NAMESPACE_URL, "graph")
    graph = {"id": graph_id, "name": read_data(graph_element, "graph").get("name", "导入图谱")}
    namespace = UUID(graph_id)
    entities, relations, node_ids = [], [], {}
    metadata = {"revision": 1, "created_at": "", "updated_at": ""}
    for node in graph_element.findall(_tag("node")):
        if len(entities) >= MAX_NODES:
            raise ValueError("GraphML 节点数量超限")
        raw_id = node.get("id")
        node_id = _graphml_id(raw_id, namespace, "node")
        if raw_id in node_ids:
            raise ValueError("GraphML node ID 重复")
        values = read_data(node, "node")
        title = values.get("label", "")
        if not title.strip():
            raise ValueError("GraphML node 缺少非空 label 标题")
        node_ids[raw_id] = node_id
        entities.append({"id": node_id, "kind": values.get("kind", "concept"), "title": title,
                         "summary": values.get("summary", ""), "aliases": [],
                         "status": values.get("status", "active"), **metadata})
    edge_elements = graph_element.findall(_tag("edge"))
    if len(edge_elements) > MAX_EDGES:
        raise ValueError("GraphML 边数量超限")
    used_ids = set(node_ids.values())
    used_ids.update(_graphml_id(edge.get("id"), namespace, "edge")
                    for edge in edge_elements if edge.get("id") is not None)
    anonymous_index = 0
    for edge in edge_elements:
        if edge.get("directed", "true") not in {"true", "1"}:
            raise ValueError("GraphML 不支持混合方向图")
        source, target = edge.get("source"), edge.get("target")
        if source not in node_ids or target not in node_ids:
            raise ValueError("GraphML 关系端点不存在")
        values = read_data(edge, "edge")
        predicate = values.get("label", "")
        if "symmetric" in values and values["symmetric"] != (predicate in SYMMETRIC):
            raise ValueError("GraphML symmetric 与关系类型不一致")
        if edge.get("id") is not None:
            relation_id = _graphml_id(edge.get("id"), namespace, "edge")
        else:
            while True:
                relation_id = str(uuid5(namespace, f"anonymous-edge:{anonymous_index}"))
                anonymous_index += 1
                if relation_id not in used_ids:
                    break
        used_ids.add(relation_id)
        relations.append({"id": relation_id,
                          "source_id": node_ids[source], "target_id": node_ids[target], "predicate": predicate,
                          "description": values.get("description", predicate), "qualifier": values.get("qualifier", ""),
                          "basis": values.get("basis", "inference"), "status": values.get("status", "active"), **metadata})
    return _snapshot(graph, entities, relations, [])
