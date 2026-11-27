"""知识层的原子写入、版本与确定性查询；不调用模型、网络或任务队列。"""

import hashlib
import json
from uuid import uuid4


from ..database import get_db_cursor
from .. import knowledge_models as models
from ..knowledge_exchange import SYMMETRIC
from .errors import BusinessError
from .task_service import serialized_mutation

from .knowledge_common import (
    _json, _digest, _validate, _check_size, _library_id, library_id,
    content_hash, normalize_newlines, source_values, evidence_states as _evidence_states,
)
TABLES = {"object": "knowledge_objects", "relation": "knowledge_relations",
          "evidence": "knowledge_evidence"}
OBJECT_FIELDS = ("kind", "title", "summary", "aliases", "status")
RELATION_FIELDS = ("source_id", "predicate", "target_id", "description", "qualifier", "basis", "status")
SUMMARY_COLUMNS = "id,kind,title,summary,status,revision,created_at,updated_at"


def _graph_id(cursor, value=None):
    if value is None:
        row = cursor.execute("SELECT id FROM knowledge_graphs WHERE is_default=1").fetchone()
        if row is None:
            raise BusinessError("NOT_FOUND", "没有默认知识图谱")
        return row["id"]
    row = cursor.execute("SELECT id FROM knowledge_graphs WHERE id=?", (value,)).fetchone()
    if row is None:
        raise BusinessError("NOT_FOUND", f"知识图谱不存在：{value}")
    return row["id"]


def list_graphs():
    with get_db_cursor() as cursor:
        rows = cursor.execute("SELECT * FROM knowledge_graphs ORDER BY is_default DESC,name,id").fetchall()
        return {"library_id": _library_id(cursor), "graphs": [dict(row) for row in rows]}


@serialized_mutation
def create_graph(name):
    name = _validate(models.GraphCreateInput, {"name": name}).name
    with get_db_cursor(write=True) as cursor:
        graph_id = str(uuid4())
        try:
            cursor.execute("INSERT INTO knowledge_graphs(id,name) VALUES (?,?)", (graph_id, name))
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise BusinessError("KNOWLEDGE_CONFLICT", "同名知识图谱已存在") from exc
            raise
        return {"id": graph_id, "name": name, "is_default": 0}


@serialized_mutation
def rename_graph(graph_id, name):
    data = _validate(models.GraphRenameInput, {"graph_id": graph_id, "name": name})
    with get_db_cursor(write=True) as cursor:
        _graph_id(cursor, data.graph_id)
        try:
            cursor.execute("UPDATE knowledge_graphs SET name=?,updated_at=strftime('%Y-%m-%d %H:%M:%f','now') WHERE id=?",
                           (data.name, data.graph_id))
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise BusinessError("KNOWLEDGE_CONFLICT", "同名知识图谱已存在") from exc
            raise
        return dict(cursor.execute("SELECT * FROM knowledge_graphs WHERE id=?", (data.graph_id,)).fetchone())


@serialized_mutation
def delete_graph(graph_id, *, confirmed=False, impact_token=None):
    data = _validate(models.GraphDeleteInput, {
        "graph_id": graph_id, "confirmed": confirmed, "impact_token": impact_token,
    })
    with get_db_cursor(write=True) as cursor:
        cursor.execute("BEGIN IMMEDIATE")
        _graph_id(cursor, data.graph_id)
        graph = cursor.execute("SELECT id,name,is_default FROM knowledge_graphs WHERE id=?",
                               (data.graph_id,)).fetchone()
        if graph is None:
            raise BusinessError("NOT_FOUND", "知识空间不存在")
        graph = dict(graph)
        if graph["is_default"]:
            raise BusinessError("INVALID_INPUT", "默认知识空间不可删除")
        entities = cursor.execute("SELECT id,revision FROM knowledge_objects WHERE graph_id=? ORDER BY id",
                                  (data.graph_id,)).fetchall()
        relations = cursor.execute("SELECT id,revision FROM knowledge_relations WHERE graph_id=? ORDER BY id",
                                   (data.graph_id,)).fetchall()
        evidence = cursor.execute("SELECT id FROM knowledge_evidence WHERE graph_id=? ORDER BY id",
                                  (data.graph_id,)).fetchall()
        history = cursor.execute("SELECT id,object_id,relation_id,after_revision FROM knowledge_revisions WHERE graph_id=? ORDER BY id",
                                 (data.graph_id,)).fetchall()
        requests = cursor.execute("SELECT request_key FROM knowledge_requests WHERE graph_id=? ORDER BY request_key",
                                  (data.graph_id,)).fetchall()
        counts = {"entities": len(entities), "relations": len(relations), "evidence": len(evidence),
                  "history": len(history), "requests": len(requests)}
        impact_token = _digest({"graph_id": data.graph_id, "name": graph["name"],
                                "entities": [list(row) for row in entities],
                                "relations": [list(row) for row in relations],
                                "evidence": [row[0] for row in evidence],
                                "history": [list(row) for row in history],
                                "requests": [row[0] for row in requests]})
        if data.confirmed and impact_token != data.impact_token:
            raise BusinessError("IMPACT_CONFLICT", "知识空间内容已变化，请重新预览和确认")
        result = {"graph_id": data.graph_id, "graph_name": graph["name"], "counts": counts,
                  "impact_token": impact_token, "committed": False}
        if data.confirmed:
            cursor.execute("DELETE FROM knowledge_graphs WHERE id=?", (data.graph_id,))
            result["committed"] = True
        return result


def export_graph(**kwargs):
    query = _validate(models.ExportInput, kwargs)
    from .. import knowledge_exchange as exchange

    with get_db_cursor() as cursor:
        cursor.execute("BEGIN")
        graph_id = _graph_id(cursor, query.graph_id)
        graph = dict(cursor.execute("SELECT id,name FROM knowledge_graphs WHERE id=?", (graph_id,)).fetchone())
        entities = []
        for row in cursor.execute("SELECT * FROM knowledge_objects WHERE graph_id=? ORDER BY id", (graph_id,)):
            item = dict(row)
            item.pop("graph_id")
            item["aliases"] = json.loads(item.pop("aliases_json"))
            item.pop("title_norm")
            item.pop("aliases_norm_json")
            entities.append(item)
        relations = []
        for row in cursor.execute("SELECT * FROM knowledge_relations WHERE graph_id=? ORDER BY id", (graph_id,)):
            item = dict(row)
            item.pop("graph_id")
            relations.append(item)
        evidence = []
        if query.format == "json":
            for row in cursor.execute("SELECT * FROM knowledge_evidence WHERE graph_id=? ORDER BY id", (graph_id,)):
                item = dict(row)
                item.pop("graph_id")
                item.pop("dedup_key")
                evidence.append(item)
        snapshot = {"graph": graph, "entities": entities, "relations": relations, "evidence": evidence}
    try:
        content = exchange.encode_json(snapshot) if query.format == "json" else exchange.encode_graphml(snapshot)
    except ValueError as exc:
        raise BusinessError("INVALID_INPUT", f"图谱导出失败：{exc}") from exc
    return {"graph_id": graph_id, "format": query.format, "content": content,
            "entities": len(entities), "relations": len(relations), "evidence": len(evidence)}


def _decode_import(data):
    from .. import knowledge_exchange as exchange
    try:
        return exchange.decode_json(data.document) if data.format == "json" else exchange.decode_graphml(data.document)
    except ValueError as exc:
        raise BusinessError("INVALID_INPUT", str(exc)) from exc


def _import_conflicts(cursor, graph_id, snapshot):
    entities = {row["id"]: row for row in cursor.execute(
        "SELECT id,kind,title,summary,aliases_json,status,revision,created_at,updated_at FROM knowledge_objects WHERE graph_id=?",
        (graph_id,))}
    relations = {row["id"]: row for row in cursor.execute(
        "SELECT * FROM knowledge_relations WHERE graph_id=?", (graph_id,))}
    relation_fields = ("source_id", "predicate", "target_id", "qualifier")
    relation_keys = {tuple(row[key] for key in relation_fields) for row in relations.values()}
    evidence = cursor.execute("SELECT id,dedup_key FROM knowledge_evidence WHERE graph_id=?", (graph_id,)).fetchall()
    evidence_ids = {row["id"] for row in evidence}
    evidence_keys = {row["dedup_key"] for row in evidence}
    conflicts = []
    for item in snapshot["entities"]:
        existing = entities.get(item["id"])
        if existing is not None:
            current = dict(existing)
            current["aliases"] = json.loads(current.pop("aliases_json"))
            conflicts.append({"kind": "entity", "id": item["id"],
                              "reason": "identical" if all(current[key] == item[key] for key in item) else "different"})
    for item in snapshot["relations"]:
        existing = relations.get(item["id"])
        unique_key = tuple(item[key] for key in relation_fields)
        if existing is not None:
            conflicts.append({"kind": "relation", "id": item["id"],
                              "reason": "identical" if all(existing[key] == item[key] for key in item) else "different"})
        elif unique_key in relation_keys:
            conflicts.append({"kind": "relation", "id": item["id"], "reason": "unique_key"})
        relation_keys.add(unique_key)
    for item in snapshot["evidence"]:
        if item["id"] in evidence_ids:
            conflicts.append({"kind": "evidence", "id": item["id"], "reason": "id"})
        dedup = _digest({key: item.get(key) for key in (
            "object_id", "relation_id", "source_kind", "stance", "source_library_id", "source_file_id",
            "source_chunk_id", "source_url", "quote")})
        if dedup in evidence_keys:
            conflicts.append({"kind": "evidence", "id": item["id"], "reason": "duplicate"})
        evidence_keys.add(dedup)
    return conflicts


@serialized_mutation
def import_graph(data, *, actor="internal"):
    request = _validate(models.ImportInput, data)
    snapshot = _decode_import(request)
    if request.graph_name is not None:
        snapshot["graph"]["name"] = request.graph_name
    for item in snapshot["relations"]:
        if item["predicate"] in SYMMETRIC:
            item["source_id"], item["target_id"] = sorted((item["source_id"], item["target_id"]))
    with get_db_cursor(write=True) as cursor:
        cursor.execute("BEGIN IMMEDIATE")
        target_id = _graph_id(cursor, request.target_graph_id) if request.target_graph_id is not None else str(uuid4())
        if request.target_graph_id is None:
            target_name = _validate(models.GraphCreateInput, {"name": snapshot["graph"]["name"]}).name
            if cursor.execute("SELECT 1 FROM knowledge_graphs WHERE name=?", (target_name,)).fetchone():
                raise BusinessError("KNOWLEDGE_CONFLICT", "同名知识图谱已存在；请指定目标图谱或修改图名")
            cursor.execute("INSERT INTO knowledge_graphs(id,name) VALUES (?,?)", (target_id, target_name))
        else:
            target_name = cursor.execute("SELECT name FROM knowledge_graphs WHERE id=?", (target_id,)).fetchone()[0]
        snapshot["graph"]["name"] = target_name
        conflicts = _import_conflicts(cursor, target_id, snapshot)
        if conflicts:
            cursor.connection.rollback()
            return {"dry_run": request.dry_run, "committed": False, "graph_id": target_id,
                    "graph_name": target_name, "conflicts": conflicts,
                    "counts": {key: len(snapshot[key]) for key in ("entities", "relations", "evidence")}}
        for item in snapshot["entities"]:
            values = {key: item[key] for key in ("kind", "title", "summary", "aliases", "status", "revision", "created_at", "updated_at")}
            if request.format == "graphml":
                for field in ("created_at", "updated_at"):
                    values.pop(field)
            _save_record(cursor, "object", item["id"], values, target_id)
        for item in snapshot["relations"]:
            values = {key: item[key] for key in ("source_id", "predicate", "target_id", "description", "qualifier", "basis", "status", "revision", "created_at", "updated_at")}
            if request.format == "graphml":
                for field in ("created_at", "updated_at"):
                    values.pop(field)
            _save_record(cursor, "relation", item["id"], values, target_id)
        batch_id = str(uuid4())
        for item in snapshot["evidence"]:
            source = _validate(models.SourceInput, {key: item[key] for key in models.SourceInput.model_fields if key in item})
            # 验证原始引文，不让输入模型的去首尾空白掩盖不匹配；保留全部快照字段。
            source.quote = item["quote"]
            source_values(cursor, source)
            values = {key: item[key] for key in (
                "object_id", "relation_id", "source_kind", "stance", "source_library_id", "source_file_id",
                "source_chunk_id", "source_title", "source_url", "heading_path", "page_number", "quote", "content_hash", "created_at")}
            values["graph_id"] = target_id
            values["dedup_key"] = _digest({key: values.get(key) for key in (
                "object_id", "relation_id", "source_kind", "stance", "source_library_id", "source_file_id",
                "source_chunk_id", "source_url", "quote")})
            _insert(cursor, "knowledge_evidence", {"id": item["id"], **values})
        for kind, records in (("object", snapshot["entities"]), ("relation", snapshot["relations"])):
            for item in records:
                record = _record(cursor, kind, item["id"], target_id)
                _revision(cursor, kind, None, record, f"导入 {request.format}", batch_id, actor)
        result = {"dry_run": request.dry_run, "committed": not request.dry_run, "graph_id": target_id,
                  "graph_name": target_name, "conflicts": [],
                  "counts": {key: len(snapshot[key]) for key in ("entities", "relations", "evidence")}}
        if request.dry_run:
            cursor.connection.rollback()
        return result


def _locator(section, index, item):
    """逐条处理时的批次位置；报错可直接定位到第几条记录。"""
    context = {"section": section, "index": index}
    for key in ("ref", "id"):
        value = getattr(item, key, None)
        if value is not None:
            context[key] = value
    for key in ("object", "relation"):
        target = getattr(item, key, None)
        owner = getattr(target, "ref", None) or getattr(target, "id", None)
        if owner is not None:
            context[key] = owner
    return context


def _locate(exc, section, index, item):
    """把 BusinessError 补上批次位置后再抛出，便于定位具体记录。"""
    data = exc.data if isinstance(exc.data, dict) else {}
    exc.data = {**data, **_locator(section, index, item)}
    return exc


def _record(cursor, kind, record_id, graph_id=None):
    graph_id = _graph_id(cursor, graph_id)
    row = cursor.execute(f"SELECT * FROM {TABLES[kind]} WHERE id=? AND graph_id=?", (record_id, graph_id)).fetchone()
    if row is None:
        raise BusinessError("NOT_FOUND", f"知识{kind}不存在：{record_id}")
    result = dict(row)
    if kind == "object":
        result["aliases"] = json.loads(result.pop("aliases_json"))
        result.pop("title_norm")
        result.pop("aliases_norm_json")
    if kind == "evidence":
        result.pop("dedup_key")
    return result


def _version(record, expected):
    if record["revision"] != expected:
        raise BusinessError("VERSION_CONFLICT", "版本已变化，请重读后比较，不要盲目覆盖",
                            data={"id": record["id"], "expected_revision": expected,
                                  "current_revision": record["revision"]})


def _insert(cursor, table, values):
    fields = list(values)
    cursor.execute(f"INSERT INTO {table} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                   [values[field] for field in fields])


def _save_record(cursor, kind, record_id, values, graph_id, updating=False):
    values = dict(values)
    aliases = list(values.get("aliases") or [])
    if kind == "object":
        values["title_norm"] = models.normalize_name(values["title"])
        values["aliases_norm_json"] = _json([models.normalize_name(v) for v in aliases])
        values["aliases_json"] = _json(values.pop("aliases"))
    if updating:
        cursor.execute(f"UPDATE {TABLES[kind]} SET " + ",".join(f"{field}=?" for field in values)
                       + ",updated_at=strftime('%Y-%m-%d %H:%M:%f','now') WHERE id=? AND graph_id=?",
                       [*values.values(), record_id, graph_id])
    else:
        _insert(cursor, TABLES[kind], {"id": record_id, "graph_id": graph_id, **values})
    _sync_fts(cursor, kind, record_id, graph_id, values, aliases)
    return _record(cursor, kind, record_id, graph_id)


def _sync_fts(cursor, kind, record_id, graph_id, values, aliases):
    """图谱派生全文索引随记录同步；只维护图谱自己的 FTS 表，不触碰资料层索引。

    实体收标题、别名、摘要；关系收描述与适用条件。关系端点标题不复制进索引，
    端点改名时无需回填，避免留下过期副本。
    """
    from retrieval.nodes.preprocess_node import tokenize_query

    def tokens(text):
        return " ".join(tokenize_query(text or ""))

    if kind == "object":
        cursor.execute("DELETE FROM knowledge_fts WHERE graph_id=? AND object_id=?", (graph_id, record_id))
        cursor.execute("INSERT INTO knowledge_fts(graph_id,object_id,title,aliases,summary) VALUES (?,?,?,?,?)",
                       (graph_id, record_id, tokens(values["title"]), tokens(" ".join(aliases)), tokens(values["summary"])))
    elif kind == "relation":
        cursor.execute("DELETE FROM knowledge_relation_fts WHERE graph_id=? AND relation_id=?", (graph_id, record_id))
        cursor.execute("INSERT INTO knowledge_relation_fts(graph_id,relation_id,description,qualifier) VALUES (?,?,?,?)",
                       (graph_id, record_id, tokens(values["description"]), tokens(values["qualifier"])))


def _revision(cursor, kind, before, after, reason, batch_id, actor):
    _insert(cursor, "knowledge_revisions", {
        "graph_id": after["graph_id"], f"{kind}_id": after["id"], "before_revision": before["revision"] if before else None,
        "after_revision": after["revision"], "before_json": _json(before) if before else None,
        "after_json": _json(after), "reason": reason, "batch_id": batch_id, "actor": actor,
    })


def _target(cursor, target, refs, kind, graph_id):
    if target.ref is not None:
        entry = refs.get(target.ref)
        if not entry or entry["kind"] != kind:
            raise BusinessError("INVALID_REFERENCE", f"ref 不存在或类型错误：{target.ref}")
        return entry["id"]
    record = _record(cursor, kind, target.id, graph_id)
    if record["graph_id"] != graph_id:
        raise BusinessError("INVALID_REFERENCE", "图谱记录不能引用其他知识图谱中的实体")
    return target.id


def _request(cursor, graph_id, key, digest):
    row = cursor.execute("SELECT request_hash,result_json FROM knowledge_requests WHERE graph_id=? AND request_key=?",
                         (graph_id, key)).fetchone()
    if row:
        if row["request_hash"] != digest:
            raise BusinessError("REQUEST_CONFLICT", "同一请求键已用于不同内容；不能修改输入后复用键")
        return json.loads(row["result_json"])
    return None


def _remember(cursor, graph_id, key, digest, result):
    # 仅存 ID、版本、计数及提交字段名；不得存正文、标题、证据快照或删除前内容。
    _insert(cursor, "knowledge_requests", {"graph_id": graph_id, "request_key": key,
                                             "request_hash": digest, "result_json": _json(result)})


def _canonical_apply(payload, graph_id):
    data = payload.model_dump(exclude={"request_key", "dry_run"})
    data["graph_id"] = graph_id
    for name in ("objects", "relations"):
        data[name] = [item.model_dump(exclude_unset=isinstance(item, models.Update)) for item in getattr(payload, name)]
    return {"operation": "apply", **data}


def _evidence_values(cursor, item, refs, graph_id):
    owner_kind = "object" if item.object is not None else "relation"
    owner = _target(cursor, item.object or item.relation, refs, owner_kind, graph_id)
    values = source_values(cursor, item)
    values.update(graph_id=graph_id, **{f"{owner_kind}_id": owner})
    values["dedup_key"] = _digest({key: values.get(key) for key in (
        "object_id", "relation_id", "source_kind", "stance", "source_library_id", "source_file_id",
        "source_chunk_id", "source_url", "quote")})
    return values


@serialized_mutation
def apply(data, *, actor="internal"):
    """一次本地事务；dry_run 执行同样的校验后回滚，不消耗请求键。"""
    _check_size(data)
    payload = _validate(models.ApplyInput, data)
    with get_db_cursor(write=True) as cursor:
        cursor.execute("BEGIN IMMEDIATE")
        graph_id = _graph_id(cursor, payload.graph_id)
        digest = _digest(_canonical_apply(payload, graph_id))
        if payload.request_key and not payload.dry_run:
            existing = _request(cursor, graph_id, payload.request_key, digest)
            if existing is not None:
                return existing
        batch_id, refs = str(uuid4()), {}
        result = {"committed": not payload.dry_run, "dry_run": payload.dry_run,
                  "library_id": _library_id(cursor), "graph_id": graph_id, "batch_id": batch_id, "refs": refs,
                  "objects": [], "relations": [], "evidence": [],
                  "counts": {"created": 0, "updated": 0, "reused": 0}}
        all_refs, all_updates = set(), set()
        for item in [*payload.objects, *payload.relations]:
            if isinstance(item, models.Update):
                if item.id in all_updates:
                    raise BusinessError("INVALID_REFERENCE", "同一批次不能重复更新同一 ID")
                all_updates.add(item.id)
            else:
                if item.ref in all_refs:
                    raise BusinessError("INVALID_REFERENCE", f"批次 ref 必须全局唯一：{item.ref}")
                all_refs.add(item.ref)

        def report(name, record, action, ref=None, submitted=None):
            entry = {"id": record["id"], "action": action}
            if "revision" in record:
                entry["revision"] = record["revision"]
            if submitted is not None:
                entry["submitted_fields"] = sorted(submitted.model_fields_set - {"id", "ref", "expected_revision"})
            result[name].append(entry)
            result["counts"][action] += 1
            if ref is not None:
                refs[ref] = {"id": record["id"], "kind": "object" if name == "objects" else "relation",
                             "revision": record["revision"]}

        for index, item in enumerate(payload.objects):
            updating = isinstance(item, models.ObjectUpdate)
            try:
                before = _record(cursor, "object", item.id, graph_id) if updating else None
                if updating:
                    _version(before, item.expected_revision)
                    values = {field: before[field] for field in OBJECT_FIELDS}
                    values.update(item.model_dump(exclude_unset=True, exclude={"id", "expected_revision"}))
                else:
                    values = item.model_dump(exclude={"ref"})
                values["revision"] = before["revision"] + 1 if before else 1
                record = _save_record(cursor, "object", item.id if updating else str(uuid4()), values, graph_id, updating)
            except BusinessError as exc:
                raise _locate(exc, "objects", index, item) from None
            _revision(cursor, "object", before, record, payload.reason, batch_id, actor)
            report("objects", record, "updated" if updating else "created", None if updating else item.ref, submitted=item)

        for index, item in enumerate(payload.relations):
            updating = isinstance(item, models.RelationUpdate)
            try:
                before = _record(cursor, "relation", item.id, graph_id) if updating else None
                if updating:
                    _version(before, item.expected_revision)
                    values = {field: before[field] for field in RELATION_FIELDS}
                    values.update(item.model_dump(exclude_unset=True, exclude={"id", "expected_revision", "source", "target"}))
                else:
                    values = item.model_dump(exclude={"ref", "source", "target"})
                for endpoint in ("source", "target"):
                    if getattr(item, endpoint) is not None:
                        values[f"{endpoint}_id"] = _target(cursor, getattr(item, endpoint), refs, "object", graph_id)
                if values["source_id"] == values["target_id"]:
                    raise BusinessError("INVALID_REFERENCE", "语义关系不允许自连接")
                if values["predicate"] in SYMMETRIC:
                    values["source_id"], values["target_id"] = sorted((values["source_id"], values["target_id"]))
                existing = cursor.execute("""SELECT * FROM knowledge_relations
                    WHERE graph_id=? AND source_id=? AND predicate=? AND target_id=? AND qualifier=?""",
                                          (graph_id, *(values[key] for key in ("source_id", "predicate", "target_id", "qualifier")))).fetchone()
            except BusinessError as exc:
                raise _locate(exc, "relations", index, item) from None
            if existing and (not updating or existing["id"] != item.id):
                if updating or any(existing[field] != values[field] for field in RELATION_FIELDS):
                    # 冲突回执带上已有关系的可读摘要和版本，调用方可直接决定是复用还是修订，不必再读一次。
                    titles = dict(cursor.execute("SELECT id,title FROM knowledge_objects WHERE graph_id=? AND id IN (?,?)",
                                                 (graph_id, existing["source_id"], existing["target_id"])).fetchall())
                    summary = {key: existing[key] for key in ("id", "revision", *RELATION_FIELDS)}
                    summary["display"] = _relation_display(existing, titles)
                    raise BusinessError("KNOWLEDGE_CONFLICT",
                                        f"相同关系与适用条件已存在且内容不同（{summary['display']}，revision {existing['revision']}），"
                                        "请读取后显式修订",
                                        data={**_locator("relations", index, item), "id": existing["id"], "existing": summary})
                report("relations", dict(existing), "reused", item.ref, submitted=item)
                continue
            values["revision"] = before["revision"] + 1 if before else 1
            record = _save_record(cursor, "relation", item.id if updating else str(uuid4()), values, graph_id, updating)
            _revision(cursor, "relation", before, record, payload.reason, batch_id, actor)
            report("relations", record, "updated" if updating else "created", None if updating else item.ref, submitted=item)

        for index, item in enumerate(payload.evidence):
            try:
                values = _evidence_values(cursor, item, refs, graph_id)
                row = cursor.execute("SELECT id FROM knowledge_evidence WHERE graph_id=? AND dedup_key=?",
                                     (graph_id, values["dedup_key"])).fetchone()
            except BusinessError as exc:
                raise _locate(exc, "evidence", index, item) from None
            record = {"id": row["id"] if row else str(uuid4())}
            if row is None:
                _insert(cursor, "knowledge_evidence", {**record, **values})
            report("evidence", record, "reused" if row else "created")

        if payload.dry_run:
            cursor.connection.rollback()
        else:
            _remember(cursor, graph_id, payload.request_key, digest, result)
        return result


def _evidence_page(cursor, kind, record_id, graph_id, limit, offset):
    where = f"graph_id=? AND {kind}_id=?"
    total = cursor.execute(f"SELECT COUNT(*) FROM knowledge_evidence WHERE {where}", (graph_id, record_id)).fetchone()[0]
    rows = cursor.execute(f"SELECT * FROM knowledge_evidence WHERE {where} ORDER BY id LIMIT ? OFFSET ?", (graph_id, record_id, limit, offset)).fetchall()
    return {"items": _evidence_states(cursor, rows), "total": total, "limit": limit, "offset": offset}


def list_objects(**kwargs):
    query = _validate(models.ListInput, kwargs)
    with get_db_cursor() as cursor:
        graph_id = _graph_id(cursor, query.graph_id)
    conditions, args = ["graph_id=?"], [graph_id]
    for field in ("kind", "status"):
        if getattr(query, field) is not None:
            conditions.append(f"{field}=?")
            args.append(getattr(query, field))
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with get_db_cursor() as cursor:
        cursor.execute("BEGIN")
        total = cursor.execute("SELECT COUNT(*) FROM knowledge_objects" + where, args).fetchone()[0]
        rows = cursor.execute(f"SELECT {SUMMARY_COLUMNS} FROM knowledge_objects" + where + " ORDER BY id LIMIT ? OFFSET ?",
                              [*args, query.limit, query.offset]).fetchall()
        return {"library_id": _library_id(cursor), "graph_id": graph_id, "objects": [dict(row) for row in rows],
                "total": total, "limit": query.limit, "offset": query.offset}


def search_objects(**kwargs):
    query = _validate(models.SearchInput, kwargs)
    with get_db_cursor() as cursor:
        graph_id = _graph_id(cursor, query.graph_id)
    from retrieval.nodes.preprocess_node import tokenize_query
    name = models.normalize_name(query.query)
    # instr 将 %、_ 等当普通字符；FTS tokens 总是引用，不暴露 MATCH 表达式。
    tokens = tokenize_query(query.query)
    match = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    exact = "(o.title_norm=? OR EXISTS(SELECT 1 FROM json_each(o.aliases_norm_json) a WHERE a.value=?))"
    contains = "(instr(o.title_norm,?)>0 OR EXISTS(SELECT 1 FROM json_each(o.aliases_norm_json) a WHERE instr(a.value,?)>0))"
    candidates, where_args = [exact, contains], [name, name, name, name]
    # FTS 档单独带出 bm25：标题权重最高、别名次之、摘要最低，命中质量参与排序，
    # 不再让所有 FTS 命中并列同一档后仅按 id 排。
    cte, cte_args, join, rank = "", [], "", ""
    if match:
        cte = ("WITH fts_hit AS (SELECT object_id, graph_id, bm25(knowledge_fts, 0.0, 0.0, 5.0, 3.0, 1.0) AS rank "
               "FROM knowledge_fts WHERE knowledge_fts MATCH ? AND graph_id=?) ")
        cte_args = [match, graph_id]
        join = " LEFT JOIN fts_hit ON fts_hit.object_id = o.id AND fts_hit.graph_id=o.graph_id"
        candidates.append("fts_hit.object_id IS NOT NULL")
        rank = "fts_hit.rank, "
    where = "o.graph_id=? AND (" + " OR ".join(candidates) + ")"
    filter_args = []
    for field in ("kind", "status"):
        if getattr(query, field) is not None:
            where += f" AND o.{field}=?"
            filter_args.append(getattr(query, field))
    with get_db_cursor() as cursor:
        cursor.execute("BEGIN")
        total = cursor.execute(cte + "SELECT COUNT(*) FROM knowledge_objects o" + join + " WHERE " + where,
                               [*cte_args, graph_id, *where_args, *filter_args]).fetchone()[0]
        rows = cursor.execute(cte + f"SELECT {','.join('o.' + c for c in SUMMARY_COLUMNS.split(','))} FROM knowledge_objects o" + join
                              + " WHERE " + where
                              + f" ORDER BY CASE WHEN {exact} THEN 0 WHEN {contains} THEN 1 ELSE 2 END,{rank}o.id LIMIT ? OFFSET ?",
                              [*cte_args, graph_id, *where_args, *filter_args, name, name, name, name, query.limit, query.offset]).fetchall()
        return {"library_id": _library_id(cursor), "graph_id": graph_id, "objects": [dict(row) for row in rows],
                "total": total, "limit": query.limit, "offset": query.offset}


def search_relations(**kwargs):
    """按关系自身的描述与适用条件检索关系；端点标题实时读取，不复制进索引。"""
    query = _validate(models.RelationSearchInput, kwargs)
    from retrieval.nodes.preprocess_node import tokenize_query
    tokens = tokenize_query(query.query)
    match = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    with get_db_cursor() as cursor:
        cursor.execute("BEGIN")
        graph_id = _graph_id(cursor, query.graph_id)
        result = {"library_id": _library_id(cursor), "graph_id": graph_id, "relations": [], "total": 0,
                  "limit": query.limit, "offset": query.offset}
        if not match:
            return result
        cte = ("WITH hits AS (SELECT relation_id, graph_id, bm25(knowledge_relation_fts, 0.0, 0.0, 3.0, 1.0) AS rank "
               "FROM knowledge_relation_fts WHERE knowledge_relation_fts MATCH ? AND graph_id=?) ")
        where, args = "hits.relation_id IS NOT NULL AND r.graph_id=?", [match, graph_id, graph_id]
        if query.status is not None:
            where += " AND r.status=?"
            args.append(query.status)
        result["total"] = cursor.execute(
            cte + "SELECT COUNT(*) FROM knowledge_relations r JOIN hits ON hits.relation_id=r.id WHERE " + where, args).fetchone()[0]
        rows = cursor.execute(
            cte + "SELECT r.*, hits.rank FROM knowledge_relations r JOIN hits ON hits.relation_id=r.id AND hits.graph_id=r.graph_id WHERE " + where
            + " ORDER BY hits.rank, r.id LIMIT ? OFFSET ?", [*args, query.limit, query.offset]).fetchall()
        endpoint_ids = sorted({row[key] for row in rows for key in ("source_id", "target_id")})
        titles = dict(cursor.execute(
            f"SELECT id,title FROM knowledge_objects WHERE graph_id=? AND id IN ({','.join('?' for _ in endpoint_ids)})",
            [graph_id, *endpoint_ids]).fetchall()) if endpoint_ids else {}
        for row in rows:
            item = {key: row[key] for key in ("id", "source_id", "predicate", "target_id", "description",
                                              "qualifier", "basis", "status", "revision", "created_at", "updated_at")}
            item["source_title"] = titles.get(row["source_id"])
            item["target_title"] = titles.get(row["target_id"])
            item["display"] = _relation_display(row, titles)
            result["relations"].append(item)
        return result


def _relation_display(relation, titles):
    source = titles.get(relation["source_id"], relation["source_id"])
    target = titles.get(relation["target_id"], relation["target_id"])
    end = "—" if relation["predicate"] in SYMMETRIC else "→"
    return f"{source} —{relation['predicate']}{end} {target}"


def _detect_kind(cursor, record_id, graph_id):
    """按 UUID 在图谱内探测记录类型；全部缺失时报 NOT_FOUND，不猜。"""
    for kind, table in TABLES.items():
        if cursor.execute(f"SELECT 1 FROM {table} WHERE id=? AND graph_id=?", (record_id, graph_id)).fetchone():
            return kind
    raise BusinessError("NOT_FOUND", f"知识记录不存在：{record_id}", data={"id": record_id})


def get_record(**kwargs):
    query = _validate(models.GetInput, kwargs)
    with get_db_cursor() as cursor:
        cursor.execute("BEGIN")
        graph_id = _graph_id(cursor, query.graph_id)
        kind = query.kind or _detect_kind(cursor, query.id, graph_id)
        item = _record(cursor, kind, query.id, graph_id)
        if kind == "evidence":
            item = _evidence_states(cursor, [item])[0]
        result = {"library_id": _library_id(cursor), "graph_id": graph_id, "kind": kind, "record": item}
        if kind in ("object", "relation"):
            result["evidence"] = _evidence_page(cursor, kind, query.id, graph_id, query.limit, query.offset)
            result["has_evidence"] = result["evidence"]["total"] > 0
        if kind == "relation":
            result["record"]["symmetric"] = item["predicate"] in SYMMETRIC
            titles = dict(cursor.execute("SELECT id,title FROM knowledge_objects WHERE graph_id=? AND id IN (?,?)",
                                         (graph_id, item["source_id"], item["target_id"])).fetchall())
            result["record"]["display"] = _relation_display(item, titles)
        return result


def references(**kwargs):
    query = _validate(models.ReferencesInput, kwargs)
    with get_db_cursor() as cursor:
        cursor.execute("BEGIN")
        graph_id = _graph_id(cursor, query.graph_id)
        args = (graph_id, query.source_library_id, query.source_file_id)
        where = "graph_id=? AND source_library_id=? AND source_file_id=?"
        total = cursor.execute("SELECT COUNT(*) FROM knowledge_evidence WHERE " + where, args).fetchone()[0]
        rows = cursor.execute("SELECT * FROM knowledge_evidence WHERE " + where + " ORDER BY id LIMIT ? OFFSET ?",
                              (*args, query.limit, query.offset)).fetchall()
        items = _evidence_states(cursor, rows)
        for item in items:
            kind = "object" if item["object_id"] else "relation"
            owner = _record(cursor, kind, item[f"{kind}_id"], graph_id)
            item["owner"] = {key: owner[key] for key in (("id", "kind", "title", "summary", "status", "revision")
                                                        if kind == "object" else ("id", *RELATION_FIELDS, "revision"))}
            item["owner_kind"] = kind
        return {"library_id": _library_id(cursor), "graph_id": graph_id, "evidence": items, "total": total,
                "limit": query.limit, "offset": query.offset}


def history(**kwargs):
    query = _validate(models.HistoryInput, kwargs)
    with get_db_cursor() as cursor:
        cursor.execute("BEGIN")
        graph_id = _graph_id(cursor, query.graph_id)
        _record(cursor, query.kind, query.id, graph_id)
        where = f"graph_id=? AND {query.kind}_id=?"
        total = cursor.execute("SELECT COUNT(*) FROM knowledge_revisions WHERE " + where, (graph_id, query.id)).fetchone()[0]
        rows = cursor.execute("SELECT * FROM knowledge_revisions WHERE " + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                              (graph_id, query.id, query.limit, query.offset)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["before"] = json.loads(item.pop("before_json") or "null")
            item["after"] = json.loads(item.pop("after_json"))
            items.append(item)
        return {"graph_id": graph_id, "history": items, "total": total, "limit": query.limit, "offset": query.offset}


def request_result(request_key, graph_id=None):
    query = _validate(models.RequestInput, {"request_key": request_key, "graph_id": graph_id})
    with get_db_cursor() as cursor:
        graph_id = _graph_id(cursor, query.graph_id)
        row = cursor.execute("SELECT result_json FROM knowledge_requests WHERE graph_id=? AND request_key=?",
                             (graph_id, query.request_key)).fetchone()
        if row is None:
            raise BusinessError("NOT_FOUND", "没有已提交的请求结果；超时不等于失败，可原样重试")
        return json.loads(row[0])


def _impact(cursor, kind, record_id, graph_id):
    record = _record(cursor, kind, record_id, graph_id)
    queries = [(kind, f"SELECT id{',revision' if kind in ('object', 'relation') else ''} FROM {TABLES[kind]} WHERE graph_id=? AND id=?", [graph_id, record_id])]
    if kind == "object":
        relations = "SELECT id FROM knowledge_relations WHERE graph_id=? AND (source_id=? OR target_id=?)"
        queries.extend([
            ("relations", "SELECT id,revision FROM knowledge_relations WHERE graph_id=? AND (source_id=? OR target_id=?)", [graph_id, record_id, record_id]),
            ("evidence", f"SELECT id FROM knowledge_evidence WHERE graph_id=? AND (object_id=? OR relation_id IN ({relations}))", [graph_id, record_id, graph_id, record_id, record_id]),
            ("history", f"SELECT id FROM knowledge_revisions WHERE graph_id=? AND (object_id=? OR relation_id IN ({relations}))", [graph_id, record_id, graph_id, record_id, record_id]),
        ])
    elif kind == "relation":
        queries.extend([
            ("evidence", "SELECT id FROM knowledge_evidence WHERE graph_id=? AND relation_id=?", [graph_id, record_id]),
            ("history", "SELECT id FROM knowledge_revisions WHERE graph_id=? AND relation_id=?", [graph_id, record_id]),
        ])
    counts, digest = {}, hashlib.sha256(graph_id.encode("utf-8"))
    for name, sql, args in queries:
        counts[name] = 0
        for row in cursor.execute(sql + " ORDER BY id", args):
            digest.update(_json([name, *row]).encode("utf-8"))
            digest.update(b"\n")
            counts[name] += 1
    return {"graph_id": graph_id, "kind": kind, "id": record_id, "revision": record.get("revision"),
            "counts": counts, "impact_token": digest.hexdigest(), "deletes_files": False,
            "clears_online_history": kind in ("object", "relation"), "backups_affected": False}


@serialized_mutation
def delete(data, *, actor="internal"):
    payload = _validate(models.DeleteInput, data)
    with get_db_cursor(write=True) as cursor:
        cursor.execute("BEGIN IMMEDIATE")
        graph_id = _graph_id(cursor, payload.graph_id)
        digest = _digest({"operation": "delete", "graph_id": graph_id,
                          **payload.model_dump(exclude={"request_key", "dry_run", "confirmed", "graph_id"})})
        if not payload.dry_run:
            if not payload.confirmed:
                raise BusinessError("CONFIRMATION_REQUIRED", "删除必须先预览并经用户确认")
            existing = _request(cursor, graph_id, payload.request_key, digest)
            if existing is not None:
                return existing
        record = _record(cursor, payload.kind, payload.id, graph_id)
        if payload.kind in ("object", "relation"):
            _version(record, payload.expected_revision)
        impact = _impact(cursor, payload.kind, payload.id, graph_id)
        if payload.dry_run:
            return {"dry_run": True, "committed": False, **impact}
        if payload.impact_token != impact["impact_token"]:
            raise BusinessError("IMPACT_CONFLICT", "删除影响范围已变化，请重新预览和确认")
        cursor.execute(f"DELETE FROM {TABLES[payload.kind]} WHERE graph_id=? AND id=?", (graph_id, payload.id))
        result = {"dry_run": False, "committed": True, "graph_id": graph_id, **impact, "actor": actor}
        _remember(cursor, graph_id, payload.request_key, digest, result)
        return result


def _relation_filter(predicates, statuses):
    filters, params = "", []
    for field, values in (("predicate", predicates), ("status", statuses)):
        if values is not None:
            filters += f" AND {field} IN ({','.join('?' for _ in values)})" if values else " AND 0"
            params.extend(values)
    return filters, params


def _graph_edge_details(cursor, graph_id, nodes, edges):
    titles = {node["id"]: node["title"] for node in nodes}
    counts = dict(cursor.execute(
        f"SELECT relation_id,COUNT(*) FROM knowledge_evidence WHERE graph_id=? AND relation_id IN ({','.join('?' for _ in edges)}) GROUP BY relation_id",
        [graph_id, *[edge["id"] for edge in edges]],
    ).fetchall()) if edges else {}
    for edge in edges:
        edge["kind"] = "relation"
        edge["symmetric"] = edge["predicate"] in SYMMETRIC
        edge["evidence_count"] = counts.get(edge["id"], 0)
        edge["display"] = _relation_display(edge, titles)


def _neighbor_page(cursor, query, graph_id):
    filters, params = _relation_filter(query.predicates, query.statuses)
    sql = ("SELECT DISTINCT CASE WHEN source_id=? THEN target_id ELSE source_id END AS neighbor "
           "FROM knowledge_relations WHERE graph_id=? AND (source_id=? OR target_id=?)" + filters)
    args = [query.root_id, graph_id, query.root_id, query.root_id, *params]
    total = cursor.execute("SELECT COUNT(*) FROM (" + sql + ")", args).fetchone()[0]
    size = max(0, query.max_nodes - 1)
    page = cursor.execute(sql + " ORDER BY neighbor LIMIT ? OFFSET ?", [*args, size, query.neighbor_offset]).fetchall()
    ids = {query.root_id, *(row["neighbor"] for row in page)}
    marks = ",".join("?" for _ in ids)
    rows = cursor.execute(f"SELECT {SUMMARY_COLUMNS} FROM knowledge_objects WHERE graph_id=? AND id IN ({marks})",
                          [graph_id, *sorted(ids)]).fetchall()
    nodes = sorted((dict(row) for row in rows), key=lambda node: (node["id"] != query.root_id, node["id"]))
    rows = cursor.execute("SELECT * FROM knowledge_relations WHERE graph_id=? AND (source_id=? OR target_id=?) "
                          f"AND source_id IN ({marks}) AND target_id IN ({marks})" + filters + " ORDER BY id LIMIT ?",
                          [graph_id, query.root_id, query.root_id, *sorted(ids), *sorted(ids), *params, query.max_edges + 1]).fetchall()
    edges = [dict(row) for row in rows[:query.max_edges]]
    _graph_edge_details(cursor, graph_id, nodes, edges)
    for node in nodes:
        node["neighbor_count"] = total if node["id"] == query.root_id else cursor.execute(
            "SELECT COUNT(DISTINCT CASE WHEN source_id=? THEN target_id ELSE source_id END) "
            "FROM knowledge_relations WHERE graph_id=? AND (source_id=? OR target_id=?)" + filters,
            [node["id"], graph_id, node["id"], node["id"], *params]).fetchone()[0]
    end = query.neighbor_offset + len(page)
    return {"library_id": _library_id(cursor), "graph_id": graph_id, "root_id": query.root_id,
            "nodes": nodes, "edges": edges, "truncated": total > len(page) or len(rows) > query.max_edges,
            "depth": 1, "max_nodes": query.max_nodes, "max_edges": query.max_edges,
            "neighbor_offset": query.neighbor_offset, "total_neighbors": total,
            "next_neighbor_offset": end if size and end < total else None,
            "hint": "仅返回当前相邻节点页；可继续加载下一页或从节点展开，关系受单次上限约束"}


def graph(**kwargs):
    query = _validate(models.GraphInput, kwargs)
    with get_db_cursor() as cursor:
        cursor.execute("BEGIN")
        graph_id = _graph_id(cursor, query.graph_id)
        _record(cursor, "object", query.root_id, graph_id)
        if query.depth == 1:
            return _neighbor_page(cursor, query, graph_id)
        if query.neighbor_offset:
            raise BusinessError("INVALID_INPUT", "相邻节点分页只支持一跳图谱")
        node_ids, edges, seen_edges = {query.root_id}, [], set()
        frontier, truncated = [query.root_id], False
        for _ in range(query.depth):
            following = set()
            for node in sorted(frontier):
                candidates = []
                for kind in sorted(set(query.edge_types)):
                    filters, params = "", []
                    if kind == "relation":
                        for field, values in (("predicate", query.predicates), ("status", query.statuses)):
                            if values is not None:
                                filters += f" AND {field} IN ({','.join('?' for _ in values)})" if values else " AND 0"
                                params.extend(values)
                    for endpoint in ("source_id", "target_id"):
                        rows = cursor.execute(f"SELECT * FROM {TABLES[kind]} WHERE graph_id=? AND {endpoint}=?" + filters
                                              + " ORDER BY id LIMIT ?", [graph_id, node, *params, query.max_edges + 1]).fetchall()
                        if len(rows) > query.max_edges:
                            truncated = True
                        for row in rows:
                            item = dict(row)
                            item["kind"] = kind
                            item["symmetric"] = kind == "relation" and item["predicate"] in SYMMETRIC
                            candidates.append(item)
                for edge in sorted(candidates, key=lambda e: (e["kind"], e["id"])):
                    identity = (edge["kind"], edge["id"])
                    if identity in seen_edges:
                        continue
                    seen_edges.add(identity)
                    additions = {edge["source_id"], edge["target_id"]} - node_ids
                    if len(edges) >= query.max_edges or len(node_ids) + len(additions) > query.max_nodes:
                        truncated = True
                        continue
                    edges.append(edge)
                    node_ids.update(additions)
                    following.update(additions)
            frontier = following
            if not frontier:
                break
        rows = cursor.execute(f"SELECT {SUMMARY_COLUMNS} FROM knowledge_objects WHERE graph_id=? AND id IN ({','.join('?' for _ in node_ids)}) ORDER BY id",
                              [graph_id, *sorted(node_ids)]).fetchall()
        nodes = sorted((dict(row) for row in rows), key=lambda row: (row["id"] != query.root_id, row["id"]))
        relation_ids = [edge["id"] for edge in edges if edge["kind"] == "relation"]
        if relation_ids:
            titles = {node["id"]: node["title"] for node in nodes}
            counts = dict(cursor.execute(
                f"SELECT relation_id,COUNT(*) FROM knowledge_evidence WHERE graph_id=? AND relation_id IN ({','.join('?' for _ in relation_ids)}) GROUP BY relation_id",
                [graph_id, *relation_ids]).fetchall())
            for edge in edges:
                if edge["kind"] == "relation":
                    edge["evidence_count"] = counts.get(edge["id"], 0)
                    edge["display"] = _relation_display(edge, titles)
        return {"library_id": _library_id(cursor), "graph_id": graph_id, "root_id": query.root_id, "nodes": nodes, "edges": edges,
                "truncated": truncated, "depth": query.depth, "max_nodes": query.max_nodes, "max_edges": query.max_edges,
                "hint": "仅返回有界局部邻域；可选择返回节点继续展开，计数不是全图总数"}


def graph_overview(**kwargs):
    """Degree-ordered node pages and independent edge pages over the loaded node scope.

    On a new node page callers reset edge_offset to zero. Relationship pages use
    load_nodes=False and the complete known_node_ids; edge_scope identifies that scope.
    """
    query = _validate(models.GraphOverviewInput, kwargs)
    filters, params = _relation_filter(query.predicates, query.statuses)

    with get_db_cursor() as cursor:
        cursor.execute("BEGIN")
        graph_id = _graph_id(cursor, query.graph_id)
        total_nodes = cursor.execute("SELECT COUNT(*) FROM knowledge_objects WHERE graph_id=?", (graph_id,)).fetchone()[0]
        total_edges = cursor.execute("SELECT COUNT(*) FROM knowledge_relations WHERE graph_id=?" + filters,
                                     [graph_id, *params]).fetchone()[0]
        degree_cte = ("WITH selected_relations AS (SELECT source_id,target_id FROM knowledge_relations WHERE graph_id=?" + filters + "),"
                      " degrees AS (SELECT object_id,COUNT(*) AS degree FROM ("
                      "SELECT source_id AS object_id,target_id AS neighbor FROM selected_relations UNION "
                      "SELECT target_id AS object_id,source_id AS neighbor FROM selected_relations) GROUP BY object_id) ")
        total_isolated = cursor.execute(degree_cte + "SELECT COUNT(*) FROM knowledge_objects o LEFT JOIN degrees d ON d.object_id=o.id "
                                        "WHERE o.graph_id=? AND COALESCE(d.degree,0)=0", [graph_id, *params, graph_id]).fetchone()[0]
        filtered_nodes = total_isolated if query.isolated_only else total_nodes
        rows = cursor.execute(degree_cte + "SELECT o." + SUMMARY_COLUMNS.replace(",", ",o.") + ",COALESCE(d.degree,0) AS neighbor_count "
                              "FROM knowledge_objects o LEFT JOIN degrees d ON d.object_id=o.id WHERE o.graph_id=? "
                              + ("AND COALESCE(d.degree,0)=0 " if query.isolated_only else "")
                              + "ORDER BY neighbor_count DESC,o.id LIMIT ? OFFSET ?",
                              [graph_id, *params, graph_id, query.max_nodes, query.offset]).fetchall() if query.load_nodes else []
        nodes = [dict(row) for row in rows]
        node_ids = {node["id"] for node in nodes} | set(query.known_node_ids)
        edges = []
        scope_edges = 0
        scope_nodes = nodes
        if node_ids:
            ordered_ids = sorted(node_ids)
            placeholders = ",".join("?" for _ in ordered_ids)
            scope_nodes = [dict(row) for row in cursor.execute(
                f"SELECT {SUMMARY_COLUMNS} FROM knowledge_objects WHERE graph_id=? AND id IN ({placeholders})",
                [graph_id, *ordered_ids]).fetchall()]
            node_ids = {node["id"] for node in scope_nodes}
            ordered_ids = sorted(node_ids)
            placeholders = ",".join("?" for _ in ordered_ids)
            scope_sql = (" FROM knowledge_relations WHERE graph_id=? AND source_id IN (" + placeholders
                         + ") AND target_id IN (" + placeholders + ")" + filters)
            scope_args = [graph_id, *ordered_ids, *ordered_ids, *params]
            scope_edges = cursor.execute("SELECT COUNT(*)" + scope_sql, scope_args).fetchone()[0]
            rows = cursor.execute(
                "SELECT *" + scope_sql + " ORDER BY id LIMIT ? OFFSET ?",
                [*scope_args, query.max_edges, query.edge_offset],
            ).fetchall()
            edges = [dict(row) for row in rows]
            _graph_edge_details(cursor, graph_id, scope_nodes, edges)
        node_end, edge_end = query.offset + len(nodes), query.edge_offset + len(edges)
        edge_scope = _digest({"graph_id": graph_id, "nodes": sorted(node_ids),
                              "predicates": query.predicates, "statuses": query.statuses})
        return {"library_id": _library_id(cursor), "graph_id": graph_id, "root_id": nodes[0]["id"] if nodes else None, "nodes": nodes, "edges": edges,
                "overview": True, "truncated": len(node_ids) < filtered_nodes or scope_edges > len(edges), "depth": 0,
                "max_nodes": query.max_nodes, "max_edges": query.max_edges,
                "total_nodes": total_nodes, "total_edges": total_edges,
                "filtered_nodes": filtered_nodes, "total_isolated": total_isolated, "scope_edges": scope_edges,
                "load_nodes": query.load_nodes, "isolated_only": query.isolated_only,
                "offset": query.offset, "next_offset": node_end if node_end < filtered_nodes else None,
                "edge_offset": query.edge_offset, "next_edge_offset": edge_end if edge_end < scope_edges else None,
                "edge_scope": edge_scope,
                "hint": "节点按关联度分页；关系按当前已载节点范围独立分页，追加节点后需从关系第一页继续读取"}


def lint(**kwargs):
    query = _validate(models.LintInput, kwargs)
    with get_db_cursor() as cursor:
        graph_id = _graph_id(cursor, query.graph_id)
    where, args = " WHERE graph_id=?", [graph_id]
    if query.object_ids is not None:
        where += " AND id IN (" + ",".join("?" for _ in query.object_ids) + ")" if query.object_ids else " AND 0"
        args.extend(query.object_ids)
    issues, truncated = [], False
    with get_db_cursor() as cursor:
        cursor.execute("BEGIN")
        total = cursor.execute("SELECT COUNT(*) FROM knowledge_objects" + where, args).fetchone()[0]
        objects = cursor.execute("SELECT * FROM knowledge_objects" + where + " ORDER BY id LIMIT ? OFFSET ?",
                                 [*args, query.limit, query.offset]).fetchall()
        for obj in objects:
            oid = obj["id"]
            # 每个对象的附属记录也有上限，避免高出度页将 lint 变成无界全库扫描。
            relations = cursor.execute(
                "SELECT id,source_id,predicate,target_id,description FROM knowledge_relations WHERE graph_id=? AND (source_id=? OR target_id=?) ORDER BY id LIMIT 301",
                (graph_id, oid, oid)).fetchall()
            if len(relations) > 300:
                truncated = True
            if not relations:
                issues.append({"code": "ISOLATED_OBJECT", "object_id": oid, "message": "孤立对象不一定有误"})
            relation_ids = [row["id"] for row in relations[:300]]
            missing_evidence = [row for row in relations[:300]
                                if not cursor.execute("SELECT 1 FROM knowledge_evidence WHERE graph_id=? AND relation_id=? LIMIT 1", (graph_id, row["id"])).fetchone()]
            if missing_evidence:
                endpoints = {row[column] for row in missing_evidence for column in ("source_id", "target_id")}
                titles = dict(cursor.execute(
                    f"SELECT id,title FROM knowledge_objects WHERE graph_id=? AND id IN ({','.join('?' for _ in endpoints)})",
                    [graph_id, *sorted(endpoints)]).fetchall())
                for row in missing_evidence:
                    issues.append({"code": "RELATION_WITHOUT_EVIDENCE", "relation_id": row["id"],
                                   "predicate": row["predicate"], "description": row["description"],
                                   "source_id": row["source_id"], "source_title": titles.get(row["source_id"]),
                                   "target_id": row["target_id"], "target_title": titles.get(row["target_id"])})
            evidence_where = "graph_id=? AND object_id=?"
            evidence_args = [graph_id, oid]
            if relation_ids:
                evidence_where += f" OR (graph_id=? AND relation_id IN ({','.join('?' for _ in relation_ids)}))"
                evidence_args.append(graph_id)
                evidence_args.extend(relation_ids)
            evidence = cursor.execute("SELECT * FROM knowledge_evidence WHERE " + evidence_where + " ORDER BY id LIMIT 201", evidence_args).fetchall()
            if len(evidence) > 200:
                truncated = True
            for item in _evidence_states(cursor, evidence[:200]):
                if item["location_status"] in ("changed", "missing"):
                    issues.append({"code": "SOURCE_" + item["location_status"].upper(), "evidence_id": item["id"],
                                   "object_id": item["object_id"], "relation_id": item["relation_id"]})
            names = list(dict.fromkeys([obj["title_norm"], *json.loads(obj["aliases_norm_json"])]))
            marks = ",".join("?" for _ in names)
            candidates = cursor.execute(f"""SELECT id,kind,title,summary FROM knowledge_objects
                WHERE graph_id=? AND id != ? AND (title_norm IN ({marks}) OR EXISTS
                (SELECT 1 FROM json_each(aliases_norm_json) a WHERE a.value IN ({marks})))
                ORDER BY id LIMIT 21""", [graph_id, oid, *names, *names]).fetchall()
            if len(candidates) > 20:
                truncated = True
            if candidates:
                issues.append({"code": "AMBIGUOUS_NAME", "object_id": oid,
                               "candidates": [dict(row) for row in candidates[:20]], "message": "同名/别名仅为歧义候选，不代表应合并"})
        unique = {_json(issue): issue for issue in issues}
        return {"issues": list(unique.values()), "checked_objects": len(objects), "total_objects": total,
                "limit": query.limit, "offset": query.offset, "truncated": truncated,
                "read_only": True, "semantic_review": False}


@serialized_mutation
def rebuild_index(graph_id=None):
    """仅重建指定图谱的派生全文索引，不改实体、关系、证据或历史。"""
    query = _validate(models.ScopedInput, {"graph_id": graph_id})
    from retrieval.nodes.preprocess_node import tokenize_query

    def tokens(text):
        return " ".join(tokenize_query(text or ""))

    with get_db_cursor(write=True) as cursor:
        graph_id = _graph_id(cursor, query.graph_id)
        cursor.execute("DELETE FROM knowledge_fts WHERE graph_id=?", (graph_id,))
        cursor.execute("DELETE FROM knowledge_relation_fts WHERE graph_id=?", (graph_id,))
        rows = cursor.execute("SELECT id,title,aliases_json,summary FROM knowledge_objects WHERE graph_id=? ORDER BY id", (graph_id,)).fetchall()
        cursor.executemany("INSERT INTO knowledge_fts(graph_id,object_id,title,aliases,summary) VALUES (?,?,?,?,?)",
                           [(graph_id, row["id"], tokens(row["title"]),
                             tokens(" ".join(json.loads(row["aliases_json"]))), tokens(row["summary"]))
                            for row in rows])
        relations = cursor.execute("SELECT id,description,qualifier FROM knowledge_relations WHERE graph_id=? ORDER BY id", (graph_id,)).fetchall()
        cursor.executemany("INSERT INTO knowledge_relation_fts(graph_id,relation_id,description,qualifier) VALUES (?,?,?,?)",
                           [(graph_id, row["id"], tokens(row["description"]), tokens(row["qualifier"])) for row in relations])
        return {"committed": True, "dry_run": False, "graph_id": graph_id, "indexed_objects": len(rows),
                "indexed_relations": len(relations), "index_status": "current",
                "rewrote_records": False, "library_id": _library_id(cursor)}


def file_reference_count(file_id):
    """删除资料时仅统计本库引用；资料删除不擦除引用快照。"""
    with get_db_cursor() as cursor:
        return cursor.execute("SELECT COUNT(*) FROM knowledge_evidence WHERE source_library_id=? AND source_file_id=?",
                              (_library_id(cursor), file_id)).fetchone()[0]

