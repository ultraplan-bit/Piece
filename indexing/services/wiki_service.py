"""独立 Wiki：Markdown 真源、派生索引、逐页提交和可恢复审计。

文件格式为 --- 包围的 JSON frontmatter（YAML 1.2 子集）及原样 Markdown。
只管理 data_path/wiki 的直接子文件；UUID 是身份，文件名/标题可变。
SQLite 操作意向先持久化，实际原文件原子捕获到恢复路径，再排他发布新页面。
捕获后中断会明确报告缺失/恢复状态；重建只读文件，绝不把审计快照写回 MD。
批次按页提交，失败项允许原键原样重试；恢复文件不参与页面索引。
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from uuid import UUID, uuid4, uuid5

from .. import wiki_models as models
from ..knowledge_models import normalize_name
from ..database import get_db_cursor
from ..settings import get_settings
from .errors import BusinessError
from .knowledge_common import (
    _check_size, _digest, _json, _library_id, _safe_url, _validate,
    evidence_states, library_id, source_values,
)
from .task_service import serialized_mutation

PAGE_FIELDS = ("kind", "title", "summary", "body", "aliases", "status")
SUMMARY_FIELDS = ("id", "kind", "title", "summary", "aliases", "status", "revision", "created_at", "updated_at",
                  "content_hash", "path", "index_status")
MAX_FILE_BYTES = 8 * 1024 * 1024
LINK = re.compile(r"piece://wiki/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?![0-9a-fA-F-])")


def _now():
    return datetime.now(timezone.utc).isoformat()


def wiki_path():
    base = get_settings().get_data_path().resolve()
    root = base / "wiki"
    if root.is_symlink() or root.is_junction() or root.resolve().parent != base:
        raise BusinessError("UNSAFE_PATH", "Wiki 目录不能是符号链接或目录联接")
    if root.exists() and not root.is_dir():
        raise BusinessError("UNSAFE_PATH", "Wiki 存储路径不是目录")
    return root


def _path(name):
    if not name or name in (".", "..") or any(char in name for char in ("/", "\\", ":", "\x00")):
        raise BusinessError("UNSAFE_PATH", "Wiki 文件只能位于 Wiki 目录内")
    path = wiki_path() / name
    if path.is_symlink() or path.is_junction() or path.resolve().parent != wiki_path().resolve():
        raise BusinessError("UNSAFE_PATH", "Wiki 文件不能是链接或越过目录边界")
    if path.exists() and not path.is_file():
        raise BusinessError("UNSAFE_PATH", "Wiki 目标不是普通文件")
    return path


def _bytes(path):
    path = _path(path.name)
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise BusinessError("FILE_INVALID", "Wiki 文件超过 8 MiB 上限", data={"path": path.name})
        return data
    except FileNotFoundError as exc:
        raise BusinessError("FILE_MISSING", "Wiki 文件缺失；不会返回数据库旧正文", data={"path": path.name}) from exc
    except OSError as exc:
        raise BusinessError("FILE_READ_FAILED", "Wiki 文件无法读取", data={"path": path.name}) from exc


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复 JSON 字段")
        result[key] = value
    return result


def _decode(data, path):
    try:
        text = data.decode("utf-8")
        # 允许外部编辑器使用 CRLF；哈希仍是原始字节，不归一化。
        match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
        if not match:
            raise ValueError("缺少 JSON frontmatter")
        metadata = json.loads(match[1], object_pairs_hook=_unique_json)
        if not isinstance(metadata, dict) or "body" in metadata:
            raise ValueError("正文只能在 frontmatter 后")
        document = _validate(models.PageDocument, {**metadata, "body": text[match.end():]}).model_dump()
        for item in document["evidence"]:
            item.pop("expected_content_hash", None)
            if item["source_kind"] == "external":
                _safe_url(item["source_url"])
        _json(document).encode("utf-8")
        return {**document, "content_hash": _hash(data), "path": path.name}
    except (UnicodeError, ValueError, TypeError, BusinessError) as exc:
        raise BusinessError("FILE_INVALID", "Wiki 文件的 UTF-8、JSON frontmatter 或页面字段无效",
                            data={"path": path.name}) from exc


def _encode(document):
    data = copy.deepcopy(document)
    body = data.pop("body")
    for key in ("content_hash", "path", "index_status"):
        data.pop(key, None)
    # 审计 JSON 会排序字段；编码本身也必须规范化，否则重放后的 bytes 与 after_hash 不同。
    result = ("---\n" + json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n---\n" + body).encode("utf-8")
    if len(result) > MAX_FILE_BYTES:
        raise BusinessError("INPUT_TOO_LARGE", "Wiki 页面最多 8 MiB，请拆分页面")
    return result


def _error(exc, **context):
    return {"code": exc.code if isinstance(exc, BusinessError) else "INDEX_FAILED",
            "message": str(exc) if isinstance(exc, BusinessError) else "派生索引/审计更新失败，可重试或重建索引",
            **context, **(exc.data if isinstance(exc, BusinessError) and isinstance(exc.data, dict) else {})}


def _scan():
    # ponytail: 显式操作时扫描平面 Wiki 目录；库大到扫描成为瓶颈时再引入文件清单，不做常驻监听。
    pages, errors, duplicate_ids, evidence_owners = {}, [], set(), {}
    root = wiki_path()
    if not root.exists():
        return pages, errors
    for path in sorted(root.iterdir(), key=lambda p: p.name):
        if path.suffix.lower() != ".md":
            continue
        try:
            record = _decode(_bytes(path), path)
            page_id = record["id"]
            if page_id in pages or page_id in duplicate_ids:
                duplicate_ids.add(page_id)
                errors.append({"code": "DUPLICATE_PAGE_ID", "page_id": page_id, "path": path.name,
                               "message": "多个文件声明同一页面身份；拒绝猜测真源"})
                continue
            pages[page_id] = record
            for item in record["evidence"]:
                previous = evidence_owners.get(item["id"])
                if previous is not None:
                    duplicate_ids.update((previous, page_id))
                    errors.append({"code": "DUPLICATE_EVIDENCE_ID", "page_id": page_id, "path": path.name,
                                   "message": "证据身份在多个页面重复"})
                evidence_owners[item["id"]] = page_id
        except BusinessError as exc:
            errors.append(_error(exc, path=path.name))
    for page_id in duplicate_ids:
        pages.pop(page_id, None)
    return pages, errors


def _indexed(cursor):
    return {row["id"]: dict(row) for row in cursor.execute("SELECT id,path,content_hash FROM wiki_pages")}


def _with_status(page, indexes):
    indexed = indexes.get(page["id"])
    status = "current" if indexed and indexed["content_hash"] == page["content_hash"] and indexed["path"] == page["path"] else "stale"
    return {**page, "index_status": status}


def _read_page(page_id, pages=None, errors=None):
    if pages is None:
        pages, errors = _scan()
    if page_id in pages:
        return pages[page_id]
    for issue in errors or []:
        if issue.get("page_id") == page_id:
            raise BusinessError(issue["code"], issue["message"], data=issue)
    with get_db_cursor() as cursor:
        row = cursor.execute("SELECT path FROM wiki_pages WHERE id=?", (page_id,)).fetchone()
    if row:
        for issue in errors or []:
            if issue.get("path") == row["path"]:
                raise BusinessError(issue["code"], issue["message"], data=issue)
        raise BusinessError("FILE_MISSING", "Wiki 文件缺失或身份已改变；不会读取旧缓存", data={"id": page_id, "path": row["path"]})
    raise BusinessError("NOT_FOUND", "Wiki 页面不存在", data={"id": page_id})


def _check_version(page, revision, digest):
    if page["revision"] != revision:
        raise BusinessError("VERSION_CONFLICT", "页面版本已变化，请重读后比较",
                            data={"id": page["id"], "current_revision": page["revision"], "expected_revision": revision})
    if page["content_hash"] != digest:
        raise BusinessError("CONTENT_CONFLICT", "Markdown 已被修改，请重读后比较，不要盲目覆盖",
                            data={"id": page["id"], "current_content_hash": page["content_hash"]})


def _sync_directory(directory):
    # Windows 不提供可移植的目录 fsync；同目录替换仍是原子的。
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _recovery_path(operation_id):
    """只保存被实际替换/删除的原文件，不参与页面扫描，也不自动覆盖 MD。"""
    return _path(f".wiki-recovery-{UUID(operation_id)}.bak")


def _recovery_details(operation):
    if operation["before_hash"] is None:
        return {}
    recovery = _recovery_path(operation["id"])
    if not recovery.exists():
        return {}
    return {"recovery_path": str(recovery), "recovery_retained": True}


def _recovery_issue(operation):
    details = _recovery_details(operation)
    if details:
        return {"code": "RECOVERY_REQUIRED", "page_id": operation["page_id"], "request_key": operation["request_key"],
                "message": "未完成操作保留了实际原文件；请原键重试或比较恢复文件，重建索引不会覆盖 Markdown", **details}
    return None


def _restore_capture(recovery, path):
    """冲突时仅在原路径仍空缺时恢复捕获文件；绝不覆盖编辑器的新保存。"""
    try:
        os.link(recovery, _path(path.name))
        _sync_directory(path.parent)
    except FileExistsError:
        pass


def _capture_original(path, expected_hash, operation_id):
    """先原子移走实际文件，再验哈希；不存在“验完旧文件却覆盖新文件”的窗口。

    恢复文件永久保留，因此即使编辑器持有旧 inode/句柄并在移动后继续写入，
    字节仍在恢复路径中，而不是随替换/unlink丢失。发布只用排他创建。
    """
    recovery = _recovery_path(operation_id)
    if recovery.exists():
        if path.exists():
            raise BusinessError("CONTENT_CONFLICT", "捕获后原路径已重新出现，不会覆盖；请比较页面和恢复文件",
                                data={"path": path.name, "recovery_path": str(recovery)})
    else:
        if _hash(_bytes(path)) != expected_hash:
            raise BusinessError("CONTENT_CONFLICT", "文件在提交前变化，未覆盖")
        os.rename(_path(path.name), recovery)
    # captured inode 才是实际移走的文件；预检读取的副本不能替代它。
    # 重试也必须同步：上一次可能恰好在移动后、fsync 前中断。
    # Windows 的 FlushFileBuffers 需要可写句柄，r+b 不改文件内容。
    with recovery.open("r+b") as source:
        os.fsync(source.fileno())
    _sync_directory(path.parent)
    if _hash(_bytes(recovery)) != expected_hash:
        _restore_capture(recovery, path)
        raise BusinessError("CONTENT_CONFLICT", "捕获时检测到外部编辑，已保留实际文件；请重读并合并",
                            data={"path": path.name, "recovery_path": str(recovery)})
    return recovery


def _atomic_write(path, data, expected_hash):
    """写入安全临时文件；更新捕获旧文件，之后排他发布，保留可恢复的旧 inode。"""
    path = _path(path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    recovery = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".wiki-", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        path = _path(path.name)
        if expected_hash is not None:
            operation_id = _decode(data, path)["operation_id"]
            if operation_id is None:
                raise BusinessError("INVALID_INPUT", "更新文件必须有已持久化的操作身份")
            recovery = _capture_original(path, expected_hash, operation_id)
        # 无论新建还是更新，都不替换任何已存在目标，包括编辑器在捕获后新保存的文件。
        os.link(temporary, _path(path.name))
        _sync_directory(path.parent)
        if recovery is not None and _hash(_bytes(recovery)) != expected_hash:
            raise BusinessError("CONTENT_CONFLICT", "发布期间旧文件仍被外部编辑；新页面已发布，外部内容保留在恢复文件",
                                data={"path": path.name, "recovery_path": str(recovery)})
    except FileExistsError as exc:
        raise BusinessError("FILE_EXISTS", "目标文件已存在，拒绝覆盖；捕获的旧文件仍保留",
                            data={"path": path.name, **({"recovery_path": str(recovery)} if recovery else {})}) from exc
    except OSError as exc:
        raise BusinessError("FILE_WRITE_FAILED", "Wiki 文件写入或同步失败，请按回执读回；捕获文件不会丢弃",
                            data={"path": path.name, **({"recovery_path": str(recovery)} if recovery else {})}) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_delete(path, expected_hash, operation_id):
    """删除通过捕获实际文件完成，不 unlink 可能刚被编辑器换入的新文件。"""
    path = _path(path.name)
    try:
        recovery = _capture_original(path, expected_hash, operation_id)
        if path.exists():
            raise BusinessError("CONTENT_CONFLICT", "删除期间原路径被外部重新保存，没有覆盖新文件",
                                data={"path": path.name, "recovery_path": str(recovery)})
    except OSError as exc:
        raise BusinessError("FILE_WRITE_FAILED", "无法捕获待删除文件；请读回并检查恢复文件",
                            data={"path": path.name, "recovery_path": str(_recovery_path(operation_id))}) from exc


def _links(page):
    return sorted({str(UUID(value)) for value in LINK.findall(page["body"])})


def _index_page(cursor, page):
    from retrieval.nodes.preprocess_node import tokenize_query
    cursor.execute("DELETE FROM wiki_pages WHERE id=? OR path=?", (page["id"], page["path"]))
    fields = ("id", "path", "kind", "title", "summary", "status", "revision", "content_hash", "created_at", "updated_at")
    values = {field: page[field] for field in fields}
    values.update(title_norm=normalize_name(page["title"]), aliases_json=_json(page["aliases"]),
                  aliases_norm_json=_json([normalize_name(alias) for alias in page["aliases"]]))
    cursor.execute(f"INSERT INTO wiki_pages ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values()))
    cursor.executemany("INSERT INTO wiki_links(source_id,target_id) VALUES (?,?)", [(page["id"], target) for target in _links(page)])
    cursor.executemany("INSERT INTO wiki_evidence(id,page_id,source_library_id,source_file_id) VALUES (?,?,?,?)",
                       [(item["id"], page["id"], item["source_library_id"], item["source_file_id"]) for item in page["evidence"]])
    cursor.execute("INSERT INTO wiki_fts(page_id,title,summary,body) VALUES (?,?,?,?)",
                   (page["id"], *(" ".join(tokenize_query(page[field])) for field in ("title", "summary", "body"))))


def _snapshot(page):
    return {key: value for key, value in page.items() if key not in ("content_hash", "path", "index_status")}


def _operation(cursor, request_key, page_id):
    row = cursor.execute("SELECT * FROM wiki_operations WHERE request_key=? AND page_id=?", (request_key, page_id)).fetchone()
    return dict(row) if row else None


def _is_applied(operation, pages, scan_errors):
    if operation["state"] == "abandoned":
        return False
    if operation["state"] == "applied":
        return True
    page = pages.get(operation["page_id"])
    if operation["after_hash"] is None:
        # 缺失路径不是执行证明：文件可能外部改名后格式损坏而被扫描排除。
        # 必须找到本操作实际捕获且哈希匹配的原文件，并且扫描没有未识别文件。
        if scan_errors or page is not None or _path(operation["path"]).exists():
            return False
        recovery = _recovery_path(operation["id"])
        return recovery.exists() and _hash(_bytes(recovery)) == operation["before_hash"]
    return page is not None and (page["content_hash"] == operation["after_hash"] or page.get("operation_id") == operation["id"])


def _finish_operation(operation, pages):
    with get_db_cursor(write=True) as cursor:
        if operation["after_hash"] is None:
            cursor.execute("DELETE FROM wiki_pages WHERE id=?", (operation["page_id"],))
        else:
            page = _read_page(operation["page_id"], pages, [])
            _index_page(cursor, page)
        cursor.execute("UPDATE wiki_operations SET state='applied' WHERE id=?", (operation["id"],))


def _settle_pending(page_id, pages, scan_errors):
    with get_db_cursor() as cursor:
        operations = [dict(row) for row in cursor.execute("SELECT * FROM wiki_operations WHERE page_id=? AND state='pending'", (page_id,))]
    for operation in operations:
        if _is_applied(operation, pages, scan_errors):
            _finish_operation(operation, pages)
        else:
            # 仅在新请求已验证当前页面的版本/hash后废弃旧意向，不使失败请求永久锁死页面。
            with get_db_cursor(write=True) as cursor:
                cursor.execute("UPDATE wiki_operations SET state='abandoned' WHERE id=?", (operation["id"],))


def _request_row(key):
    with get_db_cursor() as cursor:
        row = cursor.execute("SELECT request_hash,result_json FROM wiki_requests WHERE request_key=?", (key,)).fetchone()
    return row


def _store_result(key, result):
    with get_db_cursor(write=True) as cursor:
        cursor.execute("UPDATE wiki_requests SET result_json=? WHERE request_key=?", (_json(result), key))


def _new_result(payload):
    batch_id = str(uuid4())
    result = {"dry_run": payload.dry_run, "committed": False, "partial": False, "index_status": "current",
              "library_id": library_id(), "batch_id": batch_id, "refs": {}, "pages": [], "evidence": [],
              "counts": {"created": 0, "updated": 0, "reused": 0}, "errors": []}
    for index, item in enumerate(payload.pages):
        updating = isinstance(item, models.PageUpdate)
        page_id = item.id if updating else str(uuid5(UUID(batch_id), f"page-{index}"))
        result["pages"].append({"id": page_id, "action": "updated" if updating else "created", "committed": False,
                                "submitted_fields": sorted(item.model_fields_set - {"id", "ref", "expected_revision", "expected_content_hash"})})
        if not updating:
            result["refs"][item.ref] = {"id": page_id, "kind": "page"}
    return result


def _evidence_key(item):
    return _digest({key: item.get(key) for key in ("source_kind", "stance", "source_library_id", "source_file_id",
                    "source_chunk_id", "source_url", "quote")})


def _prepare_page(payload, item, entry, result, index, actor):
    pages, errors = _scan()
    updating = isinstance(item, models.PageUpdate)
    before = _read_page(item.id, pages, errors) if updating else None
    if updating:
        _check_version(before, item.expected_revision, item.expected_content_hash)
    page_id = entry["id"]
    operation_id = str(uuid5(UUID(result["batch_id"]), f"operation-{index}"))
    now = _now()
    after = _snapshot(before) if before else {"format": 1, "id": page_id, "evidence": [], "created_at": now}
    after.update(item.model_dump(exclude_unset=updating, exclude={"id", "ref", "expected_revision", "expected_content_hash"}))
    after.update(revision=before["revision"] + 1 if before else 1, updated_at=now, operation_id=operation_id)
    evidence_result = []
    with get_db_cursor() as cursor:
        for evidence_index, evidence in enumerate(payload.evidence):
            owner_id = evidence.page.id or result["refs"][evidence.page.ref]["id"]
            if owner_id != page_id:
                continue
            try:
                values = source_values(cursor, evidence)
            except BusinessError as exc:
                exc.data = {**(exc.data or {}), "section": "evidence", "index": evidence_index, "page_id": page_id}
                raise
            key = _evidence_key(values)
            reused = next((old for old in after["evidence"] if _evidence_key(old) == key), None)
            evidence_id = reused["id"] if reused else str(uuid5(UUID(result["batch_id"]), f"evidence-{evidence_index}"))
            if reused is None:
                after["evidence"].append({"id": evidence_id, **values, "created_at": now})
            evidence_result.append({"id": evidence_id, "page_id": page_id, "action": "reused" if reused else "created"})
    _validate(models.PageDocument, after)
    path = before["path"] if before else f"{page_id}.md"
    encoded = _encode(after)
    operation = {"id": operation_id, "request_key": payload.request_key, "page_id": page_id, "path": path,
                 "before_hash": before["content_hash"] if before else None, "after_hash": _hash(encoded),
                 "before_json": _json(_snapshot(before)) if before else None, "after_json": _json(after),
                 "result_json": _json({"evidence": evidence_result}),
                 "state": "pending", "reason": payload.reason, "actor": actor}
    return operation, evidence_result


def _insert_operation(operation):
    with get_db_cursor(write=True) as cursor:
        cursor.execute(f"INSERT INTO wiki_operations ({','.join(operation)}) VALUES ({','.join('?' for _ in operation)})", list(operation.values()))


def _recount(result):
    for name in (name for name in result["counts"] if name in ("created", "updated", "reused")):
        result["counts"][name] = sum(item["action"] == name for item in result["pages"] if item.get("committed") or result["dry_run"])
        result["counts"][name] += sum(item["action"] == name for item in result["evidence"])
    result["committed"] = not result["dry_run"] and any(item.get("committed") for item in result["pages"])
    result["partial"] = bool(result["errors"])
    if not result["errors"]:
        result["index_status"] = "current"
    return result


@serialized_mutation
def apply(data, *, actor="internal"):
    _check_size(data)
    payload = _validate(models.ApplyInput, data)
    canonical = payload.model_dump(exclude={"request_key", "dry_run"})
    canonical["pages"] = [page.model_dump(exclude_unset=isinstance(page, models.PageUpdate)) for page in payload.pages]
    digest = _digest({"operation": "apply", **canonical})
    row = _request_row(payload.request_key) if payload.request_key and not payload.dry_run else None
    if row and row["request_hash"] != digest:
        raise BusinessError("REQUEST_CONFLICT", "同一请求键已用于不同内容，不可修改输入后复用")
    result = json.loads(row["result_json"]) if row else _new_result(payload)
    result["errors"], result["evidence"] = [], []
    if not payload.dry_run and row is None:
        with get_db_cursor(write=True) as cursor:
            cursor.execute("INSERT INTO wiki_requests(request_key,request_hash,result_json) VALUES (?,?,?)",
                           (payload.request_key, digest, _json(result)))
    for index, (item, entry) in enumerate(zip(payload.pages, result["pages"])):
        operation = None
        evidence_result = []
        try:
            with get_db_cursor() as cursor:
                operation = _operation(cursor, payload.request_key, entry["id"]) if row else None
            if operation is None:
                operation, evidence_result = _prepare_page(payload, item, entry, result, index, actor)
                if not payload.dry_run:
                    pages, scan_errors = _scan()
                    _settle_pending(entry["id"], pages, scan_errors)
                    _insert_operation(operation)
            else:
                if operation["state"] == "abandoned":
                    raise BusinessError("REQUEST_SUPERSEDED", "未提交的请求已被后续显式修订取代，不会覆盖新内容")
                evidence_result = json.loads(operation["result_json"])["evidence"]
            after = json.loads(operation["after_json"])
            entry.update(revision=after["revision"], content_hash=operation["after_hash"], path=operation["path"])
            for ref in result["refs"].values():
                if ref["id"] == entry["id"]:
                    ref.update(revision=after["revision"], content_hash=operation["after_hash"])
            if payload.dry_run:
                result["evidence"].extend(evidence_result)
                continue
            pages, scan_errors = _scan()
            if not _is_applied(operation, pages, scan_errors):
                if row:
                    with get_db_cursor() as cursor:
                        for evidence in payload.evidence:
                            owner_id = evidence.page.id or result["refs"][evidence.page.ref]["id"]
                            if owner_id == entry["id"]:
                                values = source_values(cursor, evidence)
                                stored = next(ev for ev in after["evidence"] if _evidence_key(ev) == _evidence_key(values))
                                if values["content_hash"] != stored["content_hash"]:
                                    raise BusinessError("SOURCE_CHANGED", "未完成请求的来源已改变，不会提交旧快照")
                _atomic_write(_path(operation["path"]), _encode(after), operation["before_hash"])
                entry["committed"] = True
                pages, scan_errors = _scan()
            entry["committed"] = True
            result["evidence"].extend(evidence_result)
            if operation["state"] != "applied":
                _finish_operation(operation, pages)
            entry["index_status"] = "current"
        except (BusinessError, OSError, sqlite3.Error) as exc:
            if operation is not None and not payload.dry_run:
                pages, scan_errors = _scan()
                entry["committed"] = entry.get("committed", False) or _is_applied(operation, pages, scan_errors)
            entry["index_status"] = "stale" if entry.get("committed") else "failed"
            result["index_status"] = "stale" if entry.get("committed") else "failed"
            result["errors"].append(_error(exc, page_id=entry["id"], section="pages", index=index))
            if entry.get("committed"):
                existing_ids = {item["id"] for item in result["evidence"]}
                result["evidence"].extend(item for item in evidence_result if item["id"] not in existing_ids)
        if operation is not None and not payload.dry_run:
            entry.update(_recovery_details(operation))
    _recount(result)
    if not payload.dry_run:
        try:
            _store_result(payload.request_key, result)
        except (sqlite3.Error, OSError) as exc:
            result["errors"].append(_error(exc))
            result.update(partial=True, index_status="stale" if result["committed"] else "failed")
    return result


def _page_summary(page):
    return {key: page[key] for key in SUMMARY_FIELDS}


def _listing(query, search=False):
    pages, errors = _scan()
    with get_db_cursor() as cursor:
        indexes = _indexed(cursor)
        current_library = _library_id(cursor)
    for page_id, index in indexes.items():
        if page_id not in pages and not any(issue.get("path") == index["path"] for issue in errors):
            errors.append({"code": "FILE_MISSING", "page_id": page_id, "path": index["path"], "message": "索引对应文件缺失或身份变化"})
    items = [_with_status(page, indexes) for page in pages.values()
             if (query.kind is None or page["kind"] == query.kind) and (query.status is None or page["status"] == query.status)]
    if search:
        from retrieval.nodes.preprocess_node import tokenize_query
        wanted = normalize_name(query.query)
        tokens = set(tokenize_query(wanted))
        match = " OR ".join('"' + token.replace('"', '""') + '"' for token in sorted(tokens))
        with get_db_cursor() as cursor:
            matched_ids = {row[0] for row in cursor.execute("SELECT page_id FROM wiki_fts WHERE wiki_fts MATCH ?", (match,))} if match else set()
        def rank(page):
            names = [normalize_name(page["title"]), *map(normalize_name, page["aliases"])]
            if wanted in names:
                return 0
            if any(wanted in name for name in names):
                return 1
            if page["index_status"] == "current":
                return 2 if page["id"] in matched_ids else 3
            # 外部改动未刷新时仍只返回真实文件命中，旧 FTS 不能制造幽灵结果。
            content = normalize_name(page["title"] + "\n" + page["summary"] + "\n" + page["body"])
            return 2 if wanted in content or tokens.intersection(tokenize_query(content)) else 3
        ranks = {page["id"]: rank(page) for page in items}
        items = [page for page in items if ranks[page["id"]] < 3]
        items.sort(key=lambda page: (ranks[page["id"]], page["id"]))
    else:
        items.sort(key=lambda page: page["id"])
    stale = bool(errors) or set(indexes) != set(pages) or any(_with_status(page, indexes)["index_status"] != "current" for page in pages.values())
    return {"library_id": current_library, "pages": [_page_summary(page) for page in items[query.offset:query.offset + query.limit]],
            "total": len(items), "limit": query.limit, "offset": query.offset, "errors": errors,
            "index_status": "stale" if stale else "current"}


def list_pages(**kwargs):
    return _listing(_validate(models.ListInput, kwargs))


def search_pages(**kwargs):
    return _listing(_validate(models.SearchInput, kwargs), search=True)


def _find_evidence(evidence_id, pages, errors):
    for page in pages.values():
        for item in page["evidence"]:
            if item["id"] == evidence_id:
                return page, item
    with get_db_cursor() as cursor:
        row = cursor.execute("SELECT page_id FROM wiki_evidence WHERE id=?", (evidence_id,)).fetchone()
    if row:
        _read_page(row["page_id"], pages, errors)
    raise BusinessError("NOT_FOUND", "Wiki 证据不存在", data={"id": evidence_id})


def get_record(**kwargs):
    query = _validate(models.GetInput, kwargs)
    pages, errors = _scan()
    with get_db_cursor() as cursor:
        indexes = _indexed(cursor)
        current_library = _library_id(cursor)
    if query.kind == "evidence":
        page, evidence = _find_evidence(query.id, pages, errors)
        with get_db_cursor() as cursor:
            item = evidence_states(cursor, [evidence])[0]
        item.update(page_id=page["id"], page_revision=page["revision"], page_content_hash=page["content_hash"])
        return {"library_id": current_library, "kind": "evidence", "record": item,
                "index_status": _with_status(page, indexes)["index_status"]}
    page = _with_status(_read_page(query.id, pages, errors), indexes)
    with get_db_cursor() as cursor:
        evidence = evidence_states(cursor, page["evidence"][query.offset:query.offset + query.limit])
    for item in evidence:
        item.update(page_id=page["id"], page_revision=page["revision"], page_content_hash=page["content_hash"])
    def navigation(ids, reverse=False):
        items = []
        for target in ids[query.offset:query.offset + query.limit]:
            items.append({"source_id": target if reverse else page["id"], "target_id": page["id"] if reverse else target,
                          "title": pages[target]["title"] if target in pages else None, "exists": target in pages})
        return {"items": items, "total": len(ids), "limit": query.limit, "offset": query.offset}
    record = {key: value for key, value in page.items() if key != "evidence"}
    return {"library_id": current_library, "kind": "page", "record": record,
            "evidence": {"items": evidence, "total": len(page["evidence"]), "limit": query.limit, "offset": query.offset},
            "has_evidence": bool(page["evidence"]), "links": navigation(_links(page)),
            "backlinks": navigation(sorted(p["id"] for p in pages.values() if page["id"] in _links(p)), True),
            "index_status": page["index_status"], "errors": errors}


def references(**kwargs):
    query = _validate(models.ReferencesInput, kwargs)
    pages, errors = _scan()
    items = []
    with get_db_cursor() as cursor:
        indexes = _indexed(cursor)
        for page in pages.values():
            for item in page["evidence"]:
                if (item["source_library_id"], item["source_file_id"]) == (query.source_library_id, query.source_file_id):
                    items.append({**item, "page_id": page["id"], "owner_kind": "page", "owner": _page_summary(_with_status(page, indexes))})
        items.sort(key=lambda item: item["id"])
        total = len(items)
        items = evidence_states(cursor, items[query.offset:query.offset + query.limit])
        return {"library_id": _library_id(cursor), "evidence": items, "total": total,
                "limit": query.limit, "offset": query.offset, "errors": errors}


def lint(**kwargs):
    query = _validate(models.LintInput, kwargs)
    pages, errors = _scan()
    selected = sorted((page for page in pages.values() if query.page_ids is None or page["id"] in query.page_ids), key=lambda page: page["id"])
    issues = list(errors)
    with get_db_cursor() as cursor:
        indexes = _indexed(cursor)
        for page_id, index in indexes.items():
            if page_id not in pages and (query.page_ids is None or page_id in query.page_ids):
                issues.append({"code": "FILE_MISSING_OR_INVALID", "page_id": page_id, "path": index["path"]})
        for operation in (dict(row) for row in cursor.execute("SELECT * FROM wiki_operations WHERE state='pending'")):
            if query.page_ids is None or operation["page_id"] in query.page_ids:
                if not _is_applied(operation, pages, errors) and (issue := _recovery_issue(operation)):
                    issues.append(issue)
        for page in selected[query.offset:query.offset + query.limit]:
            if _with_status(page, indexes)["index_status"] != "current":
                issues.append({"code": "INDEX_STALE", "page_id": page["id"]})
            for target in _links(page):
                if target not in pages:
                    issues.append({"code": "BROKEN_BODY_LINK", "page_id": page["id"], "target_id": target})
            for item in evidence_states(cursor, page["evidence"]):
                if item["location_status"] in ("changed", "missing"):
                    issues.append({"code": "SOURCE_" + item["location_status"].upper(), "page_id": page["id"], "evidence_id": item["id"]})
    return {"issues": issues, "checked_pages": len(selected[query.offset:query.offset + query.limit]), "total_pages": len(selected),
            "limit": query.limit, "offset": query.offset, "truncated": False, "read_only": True, "semantic_review": False}


def history(**kwargs):
    query = _validate(models.HistoryInput, kwargs)
    pages, scan_errors = _scan()
    with get_db_cursor() as cursor:
        rows = [dict(row) for row in cursor.execute("SELECT * FROM wiki_operations WHERE page_id=? ORDER BY created_at DESC,rowid DESC", (query.id,))]
    applied = [row for row in rows if _is_applied(row, pages, scan_errors)]
    if not applied and query.id not in pages:
        raise BusinessError("NOT_FOUND", "没有此 Wiki 页面的历史")
    items = []
    for row in applied[query.offset:query.offset + query.limit]:
        before, after = json.loads(row["before_json"] or "null"), json.loads(row["after_json"] or "null")
        items.append({"id": row["id"], "page_id": row["page_id"], "before": before, "after": after,
                      "before_revision": before["revision"] if before else None,
                      "after_revision": after["revision"] if after else None,
                      "reason": row["reason"], "actor": row["actor"], "batch_id": row["request_key"], "created_at": row["created_at"],
                      **_recovery_details(row)})
    return {"history": items, "total": len(applied), "limit": query.limit, "offset": query.offset,
            "includes_external_edits": False, "hint": "只记录经 Piece 提交的修订；外部编辑在下一次提交的 before 快照中保留"}


def request_result(request_key):
    query = _validate(models.RequestInput, {"request_key": request_key})
    row = _request_row(query.request_key)
    if row is None:
        raise BusinessError("NOT_FOUND", "没有该 Wiki 请求；超时不等于失败，可原键原样重试")
    result = json.loads(row["result_json"])
    pages, scan_errors = _scan()
    with get_db_cursor() as cursor:
        indexes = _indexed(cursor)
        operations = {row["page_id"]: dict(row) for row in cursor.execute("SELECT * FROM wiki_operations WHERE request_key=?", (query.request_key,))}
    errors = []
    result["evidence"] = []
    for entry in result["pages"]:
        operation = operations.get(entry["id"])
        if not operation:
            errors.append({"code": "NOT_COMMITTED", "page_id": entry["id"], "message": "页面尚未提交，可原键原样重试"})
            continue
        entry.update(_recovery_details(operation))
        entry["committed"] = _is_applied(operation, pages, scan_errors)
        if not entry["committed"]:
            errors.append(_recovery_issue(operation) or {"code": "NOT_COMMITTED", "page_id": entry["id"],
                                                        "message": "页面尚未提交，可原键原样重试"})
        if entry["committed"]:
            result["evidence"].extend(json.loads(operation["result_json"])["evidence"])
        if operation["after_json"]:
            after = json.loads(operation["after_json"])
            entry.update(revision=after["revision"], content_hash=operation["after_hash"])
            for ref in result["refs"].values():
                if ref["id"] == entry["id"]:
                    ref.update(revision=after["revision"], content_hash=operation["after_hash"])
        if operation["state"] != "applied" and entry["committed"]:
            errors.append({"code": "INDEX_STALE", "page_id": entry["id"], "message": "文件已提交，索引/审计待恢复，请重建索引"})
    result["errors"] = errors
    if errors and scan_errors:
        result["scan_errors"] = scan_errors
    result["index_status"] = "stale" if errors else "current"
    return _recount(result)


@serialized_mutation
def rebuild_index():
    pages, errors = _scan()
    scan_errors = list(errors)
    recovered = 0
    try:
        with get_db_cursor(write=True) as cursor:
            cursor.execute("DELETE FROM wiki_pages")
            # 显式清空派生 FTS，也恢复人为清除缓存后的索引；审计表不动。
            cursor.execute("DELETE FROM wiki_fts")
            for page in pages.values():
                _index_page(cursor, page)
            pending = [dict(row) for row in cursor.execute("SELECT * FROM wiki_operations WHERE state='pending'")]
            for operation in pending:
                if _is_applied(operation, pages, scan_errors):
                    cursor.execute("UPDATE wiki_operations SET state='applied' WHERE id=?", (operation["id"],))
                    recovered += 1
                elif issue := _recovery_issue(operation):
                    errors.append(issue)
        return {"committed": True, "dry_run": False, "partial": bool(errors), "index_status": "stale" if errors else "current",
                "indexed_pages": len(pages), "recovered_operations": recovered, "errors": errors, "rewrote_files": False}
    except (sqlite3.Error, OSError) as exc:
        return {"committed": False, "dry_run": False, "partial": True, "index_status": "failed", "indexed_pages": 0,
                "recovered_operations": 0, "errors": [*errors, _error(exc)], "rewrote_files": False}


def _delete_impact(payload, page, evidence, pages, scan_errors, exclude_operation_id=None):
    if scan_errors:
        raise BusinessError("SCAN_INCOMPLETE", "存在无法识别的 Wiki 文件，无法完整预览删除影响；请先修复文件",
                            data={"errors": scan_errors})
    with get_db_cursor() as cursor:
        history_count = cursor.execute("SELECT COUNT(*) FROM wiki_operations WHERE page_id=? AND id!=?",
                                       (page["id"], exclude_operation_id or "")).fetchone()[0]
    incoming = sorted(other["id"] for other in pages.values() if page["id"] in _links(other))
    counts = {"pages": int(payload.kind == "page"), "evidence": len(page["evidence"]) if evidence is None else 1,
              "outgoing_links": len(_links(page)) if evidence is None else 0,
              "affected_backlinks": len(incoming) if evidence is None else 0, "history": history_count}
    token = _digest({"kind": payload.kind, "id": payload.id, "page_hash": page["content_hash"],
                     "incoming": incoming, "history_count": history_count})
    return {"kind": payload.kind, "id": payload.id, "page_id": page["id"], "revision": page["revision"],
            "content_hash": page["content_hash"], "counts": counts, "impact_token": token,
            "deletes_files": payload.kind == "page", "clears_online_history": False, "retains_history": True,
            "backups_affected": False, "hint": "删除页面不会修改其它页面的链接或图谱；历史快照仍保留"}


@serialized_mutation
def delete(data, *, actor="internal"):
    payload = _validate(models.DeleteInput, data)
    if not payload.dry_run and not payload.confirmed:
        raise BusinessError("CONFIRMATION_REQUIRED", "删除必须先预览并经用户确认")
    digest = _digest({"operation": "delete", **payload.model_dump(exclude={"request_key", "dry_run", "confirmed"})})
    row = _request_row(payload.request_key) if payload.request_key and not payload.dry_run else None
    if row and row["request_hash"] != digest:
        raise BusinessError("REQUEST_CONFLICT", "同一请求键已用于不同内容，不可修改输入后复用")
    pages, errors = _scan()
    scan_errors = errors
    if row:
        result = json.loads(row["result_json"])
        with get_db_cursor() as cursor:
            operation = _operation(cursor, payload.request_key, result["page_id"])
        if operation is None:
            raise BusinessError("PENDING_OPERATION", "删除操作记录缺失，请检查审计记录")
    else:
        if payload.kind == "page":
            page, evidence = _read_page(payload.id, pages, errors), None
            _check_version(page, payload.expected_revision, payload.expected_content_hash)
        else:
            page, evidence = _find_evidence(payload.id, pages, errors)
            _check_version(page, page["revision"], payload.expected_content_hash)
        if not payload.dry_run:
            _settle_pending(page["id"], pages, errors)
        impact = _delete_impact(payload, page, evidence, pages, errors)
        result = {"dry_run": payload.dry_run, "committed": False, "partial": False, "index_status": "current",
                  "library_id": library_id(), "batch_id": str(uuid4()), "refs": {}, "pages": [], "evidence": [],
                  "errors": [], "actor": actor, **impact}
        if payload.dry_run:
            return result
        if payload.impact_token != impact["impact_token"]:
            raise BusinessError("IMPACT_CONFLICT", "删除影响范围已变化，请重新预览和确认")
        operation_id = str(uuid4())
        after = None
        if evidence is not None:
            after = _snapshot(page)
            after["evidence"] = [item for item in after["evidence"] if item["id"] != evidence["id"]]
            after.update(revision=page["revision"] + 1, updated_at=_now(), operation_id=operation_id)
        operation = {"id": operation_id, "request_key": payload.request_key, "page_id": page["id"], "path": page["path"],
                     "before_hash": page["content_hash"], "after_hash": _hash(_encode(after)) if after else None,
                     "before_json": _json(_snapshot(page)), "after_json": _json(after) if after else None,
                     "result_json": _json({"evidence": [{"id": evidence["id"], "page_id": page["id"], "action": "deleted"}] if evidence else []}),
                     "state": "pending", "reason": "删除页面" if after is None else "删除页面证据", "actor": actor}
        result["pages"] = [{"id": page["id"], "action": "deleted" if after is None else "updated", "committed": False,
                            "revision": after["revision"] if after else page["revision"], "content_hash": operation["after_hash"]}]
        if evidence:
            result["evidence"] = [{"id": evidence["id"], "page_id": page["id"], "action": "deleted"}]
        # 请求和意向一起持久化，之后才允许改文件。
        with get_db_cursor(write=True) as cursor:
            cursor.execute("INSERT INTO wiki_requests(request_key,request_hash,result_json) VALUES (?,?,?)", (payload.request_key, digest, _json(result)))
            cursor.execute(f"INSERT INTO wiki_operations ({','.join(operation)}) VALUES ({','.join('?' for _ in operation)})", list(operation.values()))
    result["errors"] = []
    try:
        if operation["state"] == "abandoned":
            raise BusinessError("REQUEST_SUPERSEDED", "未提交的删除已被后续显式修订取代")
        if not _is_applied(operation, pages, scan_errors):
            # 尚未执行的原键重试仍需校验当前文件及影响，不能沿用旧 token/计数。
            current = pages.get(operation["page_id"])
            recovery = _recovery_path(operation["id"])
            if current is None and recovery.exists() and not _path(operation["path"]).exists():
                current = _decode(_bytes(recovery), _path(operation["path"]))
            if current is None:
                current = _read_page(operation["page_id"], pages, scan_errors)
            _check_version(current, payload.expected_revision if payload.kind == "page" else current["revision"],
                           payload.expected_content_hash)
            if current["id"] != operation["page_id"]:
                raise BusinessError("CONTENT_CONFLICT", "恢复文件身份不匹配，不会继续删除")
            evidence = next((item for item in current["evidence"] if item["id"] == payload.id), None) if payload.kind == "evidence" else None
            if payload.kind == "evidence" and evidence is None:
                raise BusinessError("NOT_FOUND", "待删除证据已变化，请重新读取")
            impact = _delete_impact(payload, current, evidence, {**pages, current["id"]: current}, scan_errors,
                                    exclude_operation_id=operation["id"])
            if payload.impact_token != impact["impact_token"]:
                raise BusinessError("IMPACT_CONFLICT", "未执行删除的影响范围已变化，请重新预览和确认")
            path = _path(operation["path"])
            if operation["after_json"] is None:
                _atomic_delete(path, operation["before_hash"], operation["id"])
            else:
                _atomic_write(path, _encode(json.loads(operation["after_json"])), operation["before_hash"])
            pages, scan_errors = _scan()
        result["committed"] = result["pages"][0]["committed"] = True
        if operation["state"] != "applied":
            _finish_operation(operation, pages)
        result.update(partial=False, index_status="current")
    except (BusinessError, OSError, sqlite3.Error) as exc:
        pages, scan_errors = _scan()
        result["committed"] = result["pages"][0]["committed"] = _is_applied(operation, pages, scan_errors)
        result.update(partial=True, index_status="stale" if result["committed"] else "failed")
        result["errors"].append(_error(exc, page_id=operation["page_id"]))
    result.update(_recovery_details(operation))
    result["pages"][0].update(_recovery_details(operation))
    try:
        _store_result(payload.request_key, result)
    except (sqlite3.Error, OSError) as exc:
        result.update(partial=True, index_status="stale" if result["committed"] else "failed")
        result["errors"].append(_error(exc))
    return result


def file_reference_count(file_id):
    """只统计 Wiki 的本库引用；读取真实 MD，不要求图谱存在。"""
    current_library = library_id()
    pages, scan_errors = _scan()
    return sum(item["source_library_id"] == current_library and item["source_file_id"] == file_id
               for page in pages.values() for item in page["evidence"])
