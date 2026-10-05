"""知识层的原子写入、版本与确定性查询；不调用模型、网络或任务队列。"""

import hashlib
import json
import re
import sqlite3
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import ValidationError

from ..database import get_db_cursor
from .. import knowledge_models as models
from .errors import BusinessError
from .page_render import page_number_from_heading
from .task_service import serialized_mutation

MAX_REQUEST_BYTES = 512 * 1024
TABLES = {"object": "knowledge_objects", "relation": "knowledge_relations",
          "link": "knowledge_links", "evidence": "knowledge_evidence"}
SYMMETRIC = {"related_to", "contradicts"}
OBJECT_FIELDS = ("kind", "title", "summary", "body", "aliases", "status")
RELATION_FIELDS = ("source_id", "predicate", "target_id", "description", "qualifier", "basis", "status")
SUMMARY_COLUMNS = "id,kind,title,summary,status,revision,created_at,updated_at"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def content_hash(text):
    """与引文匹配一致，只统一换行，不模糊匹配或折叠空白。"""
    return hashlib.sha256(normalize_newlines(text).encode("utf-8")).hexdigest()


def normalize_newlines(text):
    """把 CRLF/CR 统一成 LF；引文校验与正文哈希都以这份文本为准。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _record_context(data, loc):
    """从原始输入取出记录级 ref/id，让批次中第几条出错可直接定位。"""
    if not isinstance(data, dict) or len(loc) < 2 or loc[0] not in ("objects", "relations", "links", "evidence"):
        return {}
    try:
        item = data[loc[0]][int(loc[1])]
    except (KeyError, IndexError, TypeError, ValueError):
        return {}
    if not isinstance(item, dict):
        return {}
    context = {"section": loc[0], "index": int(loc[1])}
    for key in ("ref", "id"):
        if item.get(key) is not None:
            context[key] = item[key]
    return context


def _validate(schema, data):
    if isinstance(data, schema):
        return data
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        # 不把整段正文、引用或原始输入装入错误响应。
        issues = []
        for error in exc.errors(include_input=False, include_context=False):
            loc = list(error["loc"])
            issue = {"field": ".".join(map(str, loc)), "message": error["msg"]}
            issue.update(_record_context(data, loc))
            issues.append(issue)
        raise BusinessError("INVALID_INPUT", "知识层输入无效", data={"issues": issues}) from exc


def _check_size(data):
    if isinstance(data, models.Input):
        data = data.model_dump(exclude_unset=True)
    try:
        size = len(_json(data).encode("utf-8"))
    except (ValueError, TypeError, UnicodeError) as exc:
        raise BusinessError("INVALID_INPUT", "输入必须是有效 JSON") from exc
    if size > MAX_REQUEST_BYTES:
        raise BusinessError("INPUT_TOO_LARGE", "知识批次最多 512 KiB，请拆批提交")


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


def _library_id(cursor):
    return cursor.execute("SELECT library_id FROM library_metadata WHERE singleton=1").fetchone()[0]


def library_id():
    """当前知识库 UUID；证据定位与反查都用它，不是服务连接身份。"""
    with get_db_cursor() as cursor:
        return _library_id(cursor)


def _record(cursor, kind, record_id):
    row = cursor.execute(f"SELECT * FROM {TABLES[kind]} WHERE id=?", (record_id,)).fetchone()
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


def _save_record(cursor, kind, record_id, values, updating=False):
    values = dict(values)
    if kind == "object":
        values["title_norm"] = models.normalize_name(values["title"])
        values["aliases_norm_json"] = _json([models.normalize_name(v) for v in values["aliases"]])
        values["aliases_json"] = _json(values.pop("aliases"))
    if updating:
        cursor.execute(f"UPDATE {TABLES[kind]} SET " + ",".join(f"{field}=?" for field in values)
                       + ",updated_at=strftime('%Y-%m-%d %H:%M:%f','now') WHERE id=?",
                       [*values.values(), record_id])
    else:
        _insert(cursor, TABLES[kind], {"id": record_id, **values})
    if kind == "object":
        from retrieval.nodes.preprocess_node import tokenize_query
        cursor.execute("DELETE FROM knowledge_fts WHERE object_id=?", (record_id,))
        cursor.execute("INSERT INTO knowledge_fts(object_id,title,summary,body) VALUES (?,?,?,?)",
                       (record_id, *(" ".join(tokenize_query(values[field])) for field in ("title", "summary", "body"))))
    return _record(cursor, kind, record_id)


def _revision(cursor, kind, before, after, reason, batch_id, actor):
    _insert(cursor, "knowledge_revisions", {
        f"{kind}_id": after["id"], "before_revision": before["revision"] if before else None,
        "after_revision": after["revision"], "before_json": _json(before) if before else None,
        "after_json": _json(after), "reason": reason, "batch_id": batch_id, "actor": actor,
    })


def _target(cursor, target, refs, kind):
    if target.ref is not None:
        entry = refs.get(target.ref)
        if not entry or entry["kind"] != kind:
            raise BusinessError("INVALID_REFERENCE", f"ref 不存在或类型错误：{target.ref}")
        return entry["id"]
    _record(cursor, kind, target.id)
    return target.id


def _request(cursor, key, digest):
    row = cursor.execute("SELECT request_hash,result_json FROM knowledge_requests WHERE request_key=?", (key,)).fetchone()
    if row:
        if row["request_hash"] != digest:
            raise BusinessError("REQUEST_CONFLICT", "同一请求键已用于不同内容；不能修改输入后复用键")
        return json.loads(row["result_json"])
    return None


def _remember(cursor, key, digest, result):
    # 仅存 ID、版本、计数及提交字段名；不得存正文、标题、证据快照或删除前内容。
    _insert(cursor, "knowledge_requests", {"request_key": key, "request_hash": digest, "result_json": _json(result)})


def _canonical_apply(payload):
    data = payload.model_dump(exclude={"request_key", "dry_run"})
    for name in ("objects", "relations"):
        data[name] = [item.model_dump(exclude_unset=isinstance(item, models.Update)) for item in getattr(payload, name)]
    return {"operation": "apply", **data}


def _safe_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value):
            raise ValueError("unsafe URL")
        parsed.port  # 同时校验无效端口。
    except (ValueError, TypeError) as exc:
        raise BusinessError("INVALID_SOURCE", "外部出处仅接受不含凭据的安全 http/https URL") from exc
    return value


def _evidence_values(cursor, item, refs, library_id):
    owner_kind = "object" if item.object is not None else "relation"
    owner = _target(cursor, item.object or item.relation, refs, owner_kind)
    values = item.model_dump(exclude={"object", "relation", "expected_content_hash"})
    values.update({f"{owner_kind}_id": owner, "content_hash": None, "heading_path": None, "page_number": None})
    values["quote"] = normalize_newlines(item.quote)
    if item.source_kind == "piece":
        if item.source_library_id == library_id:
            row = cursor.execute("""SELECT c.chunk_text,c.heading_path,c.doc_title,f.filename
                FROM chunks c JOIN files f ON f.id=c.file_id WHERE c.id=? AND f.id=?""",
                                 (item.source_chunk_id, item.source_file_id)).fetchone()
            if row is None:
                raise BusinessError("INVALID_SOURCE", "本库文件/卡片不存在或卡片不属于指定文件")
            current_hash = content_hash(row["chunk_text"])
            if item.expected_content_hash and item.expected_content_hash != current_hash:
                raise BusinessError("SOURCE_CHANGED", "卡片正文已变化，请重新读取来源")
            if values["quote"] not in normalize_newlines(row["chunk_text"]):
                raise BusinessError("QUOTE_MISMATCH", "引文不在当前卡片正文中，不接受模糊匹配")
            values.update(source_title=row["doc_title"], heading_path=row["heading_path"],
                          page_number=page_number_from_heading(row["heading_path"]), content_hash=current_hash)
        else:
            if not item.source_title:
                raise BusinessError("INVALID_SOURCE", "跨库未接入的来源需要可展示的 source_title")
            if item.expected_content_hash:
                raise BusinessError("INVALID_SOURCE", "不能验证未接入库的正文哈希")
    elif item.source_kind == "external":
        values["source_url"] = _safe_url(item.source_url)
    else:
        values["source_title"] = item.source_title or "用户陈述"
    values["dedup_key"] = _digest({key: values.get(key) for key in (
        "object_id", "relation_id", "source_kind", "stance", "source_library_id", "source_file_id",
        "source_chunk_id", "source_url", "quote")})
    return values


@serialized_mutation
def apply(data, *, actor="internal"):
    """一次本地事务；dry_run 执行同样的校验后回滚，不消耗请求键。"""
    _check_size(data)
    payload = _validate(models.ApplyInput, data)
    digest = _digest(_canonical_apply(payload))
    with get_db_cursor(write=True) as cursor:
        cursor.execute("BEGIN IMMEDIATE")
        if payload.request_key and not payload.dry_run:
            existing = _request(cursor, payload.request_key, digest)
            if existing is not None:
                return existing
        batch_id, refs = str(uuid4()), {}
        result = {"committed": not payload.dry_run, "dry_run": payload.dry_run,
                  "library_id": _library_id(cursor), "batch_id": batch_id, "refs": refs,
                  "objects": [], "relations": [], "links": [], "evidence": [],
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
                before = _record(cursor, "object", item.id) if updating else None
                if updating:
                    _version(before, item.expected_revision)
                    values = {field: before[field] for field in OBJECT_FIELDS}
                    values.update(item.model_dump(exclude_unset=True, exclude={"id", "expected_revision"}))
                else:
                    values = item.model_dump(exclude={"ref"})
                values["revision"] = before["revision"] + 1 if before else 1
                record = _save_record(cursor, "object", item.id if updating else str(uuid4()), values, updating)
            except BusinessError as exc:
                raise _locate(exc, "objects", index, item) from None
            _revision(cursor, "object", before, record, payload.reason, batch_id, actor)
            report("objects", record, "updated" if updating else "created", None if updating else item.ref, submitted=item)

        for index, item in enumerate(payload.relations):
            updating = isinstance(item, models.RelationUpdate)
            try:
                before = _record(cursor, "relation", item.id) if updating else None
                if updating:
                    _version(before, item.expected_revision)
                    values = {field: before[field] for field in RELATION_FIELDS}
                    values.update(item.model_dump(exclude_unset=True, exclude={"id", "expected_revision", "source", "target"}))
                else:
                    values = item.model_dump(exclude={"ref", "source", "target"})
                for endpoint in ("source", "target"):
                    if getattr(item, endpoint) is not None:
                        values[f"{endpoint}_id"] = _target(cursor, getattr(item, endpoint), refs, "object")
                if values["source_id"] == values["target_id"]:
                    raise BusinessError("INVALID_REFERENCE", "语义关系不允许自连接")
                if values["predicate"] in SYMMETRIC:
                    values["source_id"], values["target_id"] = sorted((values["source_id"], values["target_id"]))
                existing = cursor.execute("""SELECT * FROM knowledge_relations
                    WHERE source_id=? AND predicate=? AND target_id=? AND qualifier=?""",
                                          tuple(values[key] for key in ("source_id", "predicate", "target_id", "qualifier"))).fetchone()
            except BusinessError as exc:
                raise _locate(exc, "relations", index, item) from None
            if existing and (not updating or existing["id"] != item.id):
                if updating or any(existing[field] != values[field] for field in RELATION_FIELDS):
                    raise BusinessError("KNOWLEDGE_CONFLICT", "相同关系与适用条件已存在且内容不同，请读取后显式修订",
                                        data={**_locator("relations", index, item), "id": existing["id"]})
                report("relations", dict(existing), "reused", item.ref, submitted=item)
                continue
            values["revision"] = before["revision"] + 1 if before else 1
            record = _save_record(cursor, "relation", item.id if updating else str(uuid4()), values, updating)
            _revision(cursor, "relation", before, record, payload.reason, batch_id, actor)
            report("relations", record, "updated" if updating else "created", None if updating else item.ref, submitted=item)

        for index, item in enumerate(payload.links):
            try:
                source = _target(cursor, item.source, refs, "object")
                target = _target(cursor, item.target, refs, "object")
                if source == target:
                    raise BusinessError("INVALID_REFERENCE", "页面链接不允许自连接")
                row = cursor.execute("SELECT id FROM knowledge_links WHERE source_id=? AND target_id=?", (source, target)).fetchone()
            except BusinessError as exc:
                raise _locate(exc, "links", index, item) from None
            record = {"id": row["id"] if row else str(uuid4()), "source_id": source, "target_id": target}
            if row is None:
                _insert(cursor, "knowledge_links", record)
            report("links", record, "reused" if row else "created")

        for index, item in enumerate(payload.evidence):
            try:
                values = _evidence_values(cursor, item, refs, result["library_id"])
                row = cursor.execute("SELECT id FROM knowledge_evidence WHERE dedup_key=?", (values["dedup_key"],)).fetchone()
            except BusinessError as exc:
                raise _locate(exc, "evidence", index, item) from None
            record = {"id": row["id"] if row else str(uuid4())}
            if row is None:
                _insert(cursor, "knowledge_evidence", {**record, **values})
            report("evidence", record, "reused" if row else "created")

        if payload.dry_run:
            cursor.connection.rollback()
        else:
            _remember(cursor, payload.request_key, digest, result)
        return result


def _evidence_states(cursor, rows):
    library_id = _library_id(cursor)
    local = [row for row in rows if row["source_kind"] == "piece" and row["source_library_id"] == library_id]
    chunk_ids = list({row["source_chunk_id"] for row in local})
    chunks = {}
    if chunk_ids:
        chunks = {row["id"]: row for row in cursor.execute(
            f"SELECT c.id,c.file_id,c.chunk_text,c.doc_title,c.heading_path FROM chunks c JOIN files f ON f.id=c.file_id WHERE c.id IN ({','.join('?' for _ in chunk_ids)})",
            chunk_ids)}
    result = []
    for row in rows:
        item = dict(row)
        item.pop("dedup_key", None)
        if item["source_kind"] != "piece":
            state = "unverified"
        elif item["source_library_id"] != library_id:
            state = "unresolved"
        else:
            chunk = chunks.get(item["source_chunk_id"])
            if chunk is None or chunk["file_id"] != item["source_file_id"]:
                state = "missing"
            else:
                state = "current" if content_hash(chunk["chunk_text"]) == item["content_hash"] else "changed"
                item["current_source_title"] = chunk["doc_title"]
                item["current_heading_path"] = chunk["heading_path"]
                item["current_page_number"] = page_number_from_heading(chunk["heading_path"])
        item["location_status"] = state
        result.append(item)
    return result


def _evidence_page(cursor, kind, record_id, limit, offset):
    where = f"{kind}_id=?"
    total = cursor.execute(f"SELECT COUNT(*) FROM knowledge_evidence WHERE {where}", (record_id,)).fetchone()[0]
    rows = cursor.execute(f"SELECT * FROM knowledge_evidence WHERE {where} ORDER BY id LIMIT ? OFFSET ?", (record_id, limit, offset)).fetchall()
    return {"items": _evidence_states(cursor, rows), "total": total, "limit": limit, "offset": offset}


def list_objects(**kwargs):
    query = _validate(models.ListInput, kwargs)
    conditions, args = [], []
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
        return {"library_id": _library_id(cursor), "objects": [dict(row) for row in rows],
                "total": total, "limit": query.limit, "offset": query.offset}


def search_objects(**kwargs):
    query = _validate(models.SearchInput, kwargs)
    from retrieval.nodes.preprocess_node import tokenize_query
    name = models.normalize_name(query.query)
    # instr 将 %、_ 等当普通字符；FTS tokens 总是引用，不暴露 MATCH 表达式。
    tokens = tokenize_query(query.query)
    match = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    exact = "(o.title_norm=? OR EXISTS(SELECT 1 FROM json_each(o.aliases_norm_json) a WHERE a.value=?))"
    contains = "(instr(o.title_norm,?)>0 OR EXISTS(SELECT 1 FROM json_each(o.aliases_norm_json) a WHERE instr(a.value,?)>0))"
    candidates, args = [exact, contains], [name, name, name, name]
    if match:
        candidates.append("o.id IN (SELECT object_id FROM knowledge_fts WHERE knowledge_fts MATCH ?)")
        args.append(match)
    where = "(" + " OR ".join(candidates) + ")"
    for field in ("kind", "status"):
        if getattr(query, field) is not None:
            where += f" AND o.{field}=?"
            args.append(getattr(query, field))
    with get_db_cursor() as cursor:
        cursor.execute("BEGIN")
        total = cursor.execute("SELECT COUNT(*) FROM knowledge_objects o WHERE " + where, args).fetchone()[0]
        rows = cursor.execute(f"SELECT {','.join('o.' + c for c in SUMMARY_COLUMNS.split(','))} FROM knowledge_objects o WHERE " + where
                              + f" ORDER BY CASE WHEN {exact} THEN 0 WHEN {contains} THEN 1 ELSE 2 END,o.id LIMIT ? OFFSET ?",
                              [*args, name, name, name, name, query.limit, query.offset]).fetchall()
        return {"library_id": _library_id(cursor), "objects": [dict(row) for row in rows],
                "total": total, "limit": query.limit, "offset": query.offset}


def _relation_display(relation, titles):
    source = titles.get(relation["source_id"], relation["source_id"])
    target = titles.get(relation["target_id"], relation["target_id"])
    end = "—" if relation["predicate"] in SYMMETRIC else "→"
    return f"{source} —{relation['predicate']}{end} {target}"


def get_record(**kwargs):
    query = _validate(models.GetInput, kwargs)
    with get_db_cursor() as cursor:
        cursor.execute("BEGIN")
        item = _record(cursor, query.kind, query.id)
        if query.kind == "evidence":
            item = _evidence_states(cursor, [item])[0]
        result = {"library_id": _library_id(cursor), "kind": query.kind, "record": item}
        if query.kind in ("object", "relation"):
            result["evidence"] = _evidence_page(cursor, query.kind, query.id, query.limit, query.offset)
            result["has_evidence"] = result["evidence"]["total"] > 0
        if query.kind == "relation":
            result["record"]["symmetric"] = item["predicate"] in SYMMETRIC
            titles = dict(cursor.execute("SELECT id,title FROM knowledge_objects WHERE id IN (?,?)",
                                         (item["source_id"], item["target_id"])).fetchall())
            result["record"]["display"] = _relation_display(item, titles)
        return result


def references(**kwargs):
    query = _validate(models.ReferencesInput, kwargs)
    with get_db_cursor() as cursor:
        cursor.execute("BEGIN")
        args = (query.source_library_id, query.source_file_id)
        where = "source_library_id=? AND source_file_id=?"
        total = cursor.execute("SELECT COUNT(*) FROM knowledge_evidence WHERE " + where, args).fetchone()[0]
        rows = cursor.execute("SELECT * FROM knowledge_evidence WHERE " + where + " ORDER BY id LIMIT ? OFFSET ?",
                              (*args, query.limit, query.offset)).fetchall()
        items = _evidence_states(cursor, rows)
        for item in items:
            kind = "object" if item["object_id"] else "relation"
            owner = _record(cursor, kind, item[f"{kind}_id"])
            item["owner"] = {key: owner[key] for key in (("id", "kind", "title", "summary", "status", "revision")
                                                        if kind == "object" else ("id", *RELATION_FIELDS, "revision"))}
            item["owner_kind"] = kind
        return {"library_id": _library_id(cursor), "evidence": items, "total": total,
                "limit": query.limit, "offset": query.offset}


def history(**kwargs):
    query = _validate(models.HistoryInput, kwargs)
    with get_db_cursor() as cursor:
        cursor.execute("BEGIN")
        _record(cursor, query.kind, query.id)
        where = f"{query.kind}_id=?"
        total = cursor.execute("SELECT COUNT(*) FROM knowledge_revisions WHERE " + where, (query.id,)).fetchone()[0]
        rows = cursor.execute("SELECT * FROM knowledge_revisions WHERE " + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                              (query.id, query.limit, query.offset)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["before"] = json.loads(item.pop("before_json") or "null")
            item["after"] = json.loads(item.pop("after_json"))
            items.append(item)
        return {"history": items, "total": total, "limit": query.limit, "offset": query.offset}


def request_result(request_key):
    query = _validate(models.RequestInput, {"request_key": request_key})
    with get_db_cursor() as cursor:
        row = cursor.execute("SELECT result_json FROM knowledge_requests WHERE request_key=?", (query.request_key,)).fetchone()
        if row is None:
            raise BusinessError("NOT_FOUND", "没有已提交的请求结果；超时不等于失败，可原样重试")
        return json.loads(row[0])


def _impact(cursor, kind, record_id):
    record = _record(cursor, kind, record_id)
    queries = [(kind, f"SELECT id{',revision' if kind in ('object', 'relation') else ''} FROM {TABLES[kind]} WHERE id=?", [record_id])]
    if kind == "object":
        relations = "SELECT id FROM knowledge_relations WHERE source_id=? OR target_id=?"
        queries.extend([
            ("relations", "SELECT id,revision FROM knowledge_relations WHERE source_id=? OR target_id=?", [record_id, record_id]),
            ("links", "SELECT id FROM knowledge_links WHERE source_id=? OR target_id=?", [record_id, record_id]),
            ("evidence", f"SELECT id FROM knowledge_evidence WHERE object_id=? OR relation_id IN ({relations})", [record_id] * 3),
            ("history", f"SELECT id FROM knowledge_revisions WHERE object_id=? OR relation_id IN ({relations})", [record_id] * 3),
        ])
    elif kind == "relation":
        queries.extend([
            ("evidence", "SELECT id FROM knowledge_evidence WHERE relation_id=?", [record_id]),
            ("history", "SELECT id FROM knowledge_revisions WHERE relation_id=?", [record_id]),
        ])
    counts, digest = {}, hashlib.sha256()
    for name, sql, args in queries:
        counts[name] = 0
        for row in cursor.execute(sql + " ORDER BY id", args):
            digest.update(_json([name, *row]).encode("utf-8"))
            digest.update(b"\n")
            counts[name] += 1
    return {"kind": kind, "id": record_id, "revision": record.get("revision"),
            "counts": counts, "impact_token": digest.hexdigest(), "deletes_files": False,
            "clears_online_history": kind in ("object", "relation"), "backups_affected": False}


@serialized_mutation
def delete(data, *, actor="internal"):
    payload = _validate(models.DeleteInput, data)
    digest = _digest({"operation": "delete", **payload.model_dump(exclude={"request_key", "dry_run", "confirmed"})})
    with get_db_cursor(write=True) as cursor:
        cursor.execute("BEGIN IMMEDIATE")
        if not payload.dry_run:
            if not payload.confirmed:
                raise BusinessError("CONFIRMATION_REQUIRED", "删除必须先预览并经用户确认")
            existing = _request(cursor, payload.request_key, digest)
            if existing is not None:
                return existing
        record = _record(cursor, payload.kind, payload.id)
        if payload.kind in ("object", "relation"):
            _version(record, payload.expected_revision)
        impact = _impact(cursor, payload.kind, payload.id)
        if payload.dry_run:
            return {"dry_run": True, "committed": False, **impact}
        if payload.impact_token != impact["impact_token"]:
            raise BusinessError("IMPACT_CONFLICT", "删除影响范围已变化，请重新预览和确认")
        cursor.execute(f"DELETE FROM {TABLES[payload.kind]} WHERE id=?", (payload.id,))
        result = {"dry_run": False, "committed": True, **impact, "actor": actor}
        _remember(cursor, payload.request_key, digest, result)
        return result


def graph(**kwargs):
    query = _validate(models.GraphInput, kwargs)
    with get_db_cursor() as cursor:
        cursor.execute("BEGIN")
        _record(cursor, "object", query.root_id)
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
                        rows = cursor.execute(f"SELECT * FROM {TABLES[kind]} WHERE {endpoint}=?" + filters
                                              + " ORDER BY id LIMIT ?", [node, *params, query.max_edges + 1]).fetchall()
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
        rows = cursor.execute(f"SELECT {SUMMARY_COLUMNS} FROM knowledge_objects WHERE id IN ({','.join('?' for _ in node_ids)}) ORDER BY id",
                              sorted(node_ids)).fetchall()
        nodes = sorted((dict(row) for row in rows), key=lambda row: (row["id"] != query.root_id, row["id"]))
        relation_ids = [edge["id"] for edge in edges if edge["kind"] == "relation"]
        if relation_ids:
            titles = {node["id"]: node["title"] for node in nodes}
            counts = dict(cursor.execute(
                f"SELECT relation_id,COUNT(*) FROM knowledge_evidence WHERE relation_id IN ({','.join('?' for _ in relation_ids)}) GROUP BY relation_id",
                relation_ids).fetchall())
            for edge in edges:
                if edge["kind"] == "relation":
                    edge["evidence_count"] = counts.get(edge["id"], 0)
                    edge["display"] = _relation_display(edge, titles)
        return {"library_id": _library_id(cursor), "root_id": query.root_id, "nodes": nodes, "edges": edges,
                "truncated": truncated, "depth": query.depth, "max_nodes": query.max_nodes, "max_edges": query.max_edges,
                "hint": "仅返回有界局部邻域；可选择返回节点继续展开，计数不是全图总数"}


def lint(**kwargs):
    query = _validate(models.LintInput, kwargs)
    where, args = "", []
    if query.object_ids is not None:
        where = " WHERE id IN (" + ",".join("?" for _ in query.object_ids) + ")" if query.object_ids else " WHERE 0"
        args = query.object_ids
    issues, truncated = [], False
    with get_db_cursor() as cursor:
        cursor.execute("BEGIN")
        total = cursor.execute("SELECT COUNT(*) FROM knowledge_objects" + where, args).fetchone()[0]
        objects = cursor.execute("SELECT * FROM knowledge_objects" + where + " ORDER BY id LIMIT ? OFFSET ?",
                                 [*args, query.limit, query.offset]).fetchall()
        for obj in objects:
            oid = obj["id"]
            # 每个对象的附属记录也有上限，避免高出度页将 lint 变成无界全库扫描。
            links = cursor.execute("SELECT source_id,target_id FROM knowledge_links WHERE source_id=? OR target_id=? ORDER BY id LIMIT 301", (oid, oid)).fetchall()
            relations = cursor.execute(
                "SELECT id,source_id,predicate,target_id,description FROM knowledge_relations WHERE source_id=? OR target_id=? ORDER BY id LIMIT 301",
                (oid, oid)).fetchall()
            if len(links) > 300 or len(relations) > 300:
                truncated = True
            if not links and not relations:
                issues.append({"code": "ISOLATED_OBJECT", "object_id": oid, "message": "孤立对象不一定有误"})
            explicit = {link["target_id"] for link in links[:300] if link["source_id"] == oid}
            internal = {target.lower() for target in re.findall(
                r"piece://knowledge/([0-9a-fA-F-]{36})(?![0-9a-fA-F-])", obj["body"])}
            if len(internal) > 300:
                truncated = True
            for target in sorted(internal)[:300]:
                exists = cursor.execute("SELECT 1 FROM knowledge_objects WHERE id=?", (target,)).fetchone()
                if not exists:
                    issues.append({"code": "BROKEN_BODY_LINK", "object_id": oid, "target_id": target})
                # 附属列表可能已截断；不能据此断言正文目标没有显式链接。
                registered = cursor.execute(
                    "SELECT 1 FROM knowledge_links WHERE source_id=? AND target_id=?", (oid, target)).fetchone()
                if not registered:
                    issues.append({"code": "UNREGISTERED_BODY_LINK", "object_id": oid, "target_id": target})
            for target in sorted(explicit - internal):
                issues.append({"code": "LINK_NOT_IN_BODY", "object_id": oid, "target_id": target})
            relation_ids = [row["id"] for row in relations[:300]]
            missing_evidence = [row for row in relations[:300]
                                if not cursor.execute("SELECT 1 FROM knowledge_evidence WHERE relation_id=? LIMIT 1", (row["id"],)).fetchone()]
            if missing_evidence:
                endpoints = {row[column] for row in missing_evidence for column in ("source_id", "target_id")}
                titles = dict(cursor.execute(
                    f"SELECT id,title FROM knowledge_objects WHERE id IN ({','.join('?' for _ in endpoints)})",
                    sorted(endpoints)).fetchall())
                for row in missing_evidence:
                    issues.append({"code": "RELATION_WITHOUT_EVIDENCE", "relation_id": row["id"],
                                   "predicate": row["predicate"], "description": row["description"],
                                   "source_id": row["source_id"], "source_title": titles.get(row["source_id"]),
                                   "target_id": row["target_id"], "target_title": titles.get(row["target_id"])})
            evidence_where = "object_id=?"
            evidence_args = [oid]
            if relation_ids:
                evidence_where += f" OR relation_id IN ({','.join('?' for _ in relation_ids)})"
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
                WHERE id != ? AND (title_norm IN ({marks}) OR EXISTS
                (SELECT 1 FROM json_each(aliases_norm_json) a WHERE a.value IN ({marks})))
                ORDER BY id LIMIT 21""", [oid, *names, *names]).fetchall()
            if len(candidates) > 20:
                truncated = True
            if candidates:
                issues.append({"code": "AMBIGUOUS_NAME", "object_id": oid,
                               "candidates": [dict(row) for row in candidates[:20]], "message": "同名/别名仅为歧义候选，不代表应合并"})
        unique = {_json(issue): issue for issue in issues}
        return {"issues": list(unique.values()), "checked_objects": len(objects), "total_objects": total,
                "limit": query.limit, "offset": query.offset, "truncated": truncated,
                "read_only": True, "semantic_review": False}


def file_reference_count(file_id):
    """删除资料时仅统计本库引用；资料删除不擦除引用快照。"""
    with get_db_cursor() as cursor:
        return cursor.execute("SELECT COUNT(*) FROM knowledge_evidence WHERE source_library_id=? AND source_file_id=?",
                              (_library_id(cursor), file_id)).fetchone()[0]

