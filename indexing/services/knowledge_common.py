"""Wiki 与图谱共用的输入、来源定位和引文校验；不访问两者的业务记录。"""

import hashlib
import json
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError

from ..database import get_db_cursor
from .errors import BusinessError
from .page_render import page_number_from_heading

MAX_REQUEST_BYTES = 512 * 1024


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def normalize_newlines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def content_hash(text):
    """来源正文哈希只归一化换行；不是 Wiki 文件的字节哈希。"""
    return hashlib.sha256(normalize_newlines(text).encode("utf-8")).hexdigest()


def _validate(schema, data):
    if isinstance(data, schema):
        return data
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        issues = []
        for error in exc.errors(include_input=False, include_context=False):
            loc = list(error["loc"])
            issue = {"field": ".".join(map(str, loc)), "message": error["msg"]}
            if isinstance(data, dict) and len(loc) >= 2 and loc[0] in ("pages", "objects", "relations", "evidence"):
                try:
                    item = data[loc[0]][int(loc[1])]
                    issue.update(section=loc[0], index=int(loc[1]))
                    issue.update({key: item[key] for key in ("ref", "id") if key in item})
                except (KeyError, IndexError, TypeError, ValueError):
                    pass
            issues.append(issue)
        raise BusinessError("INVALID_INPUT", "知识输入无效", data={"issues": issues}) from exc


def _check_size(data):
    if isinstance(data, BaseModel):
        data = data.model_dump(exclude_unset=True)
    try:
        size = len(_json(data).encode("utf-8"))
    except (ValueError, TypeError, UnicodeError) as exc:
        raise BusinessError("INVALID_INPUT", "输入必须是有效 JSON") from exc
    if size > MAX_REQUEST_BYTES:
        raise BusinessError("INPUT_TOO_LARGE", "知识批次最多 512 KiB，请拆批提交")


def _library_id(cursor):
    return cursor.execute("SELECT library_id FROM library_metadata WHERE singleton=1").fetchone()[0]


def library_id():
    with get_db_cursor() as cursor:
        return _library_id(cursor)


def _safe_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value):
            raise ValueError("unsafe URL")
        parsed.port
    except (ValueError, TypeError) as exc:
        raise BusinessError("INVALID_SOURCE", "外部出处仅接受不含凭据的安全 http/https URL") from exc
    return value


def source_values(cursor, item):
    """验证共同来源字段并产生快照；所有者由各自服务提供。"""
    values = item.model_dump(exclude={"object", "relation", "page", "expected_content_hash"})
    values.update(content_hash=None, heading_path=None, page_number=None)
    values["quote"] = normalize_newlines(item.quote)
    if item.source_kind == "piece":
        if item.source_library_id == _library_id(cursor):
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
    return values


def evidence_states(cursor, rows):
    current_library = _library_id(cursor)
    chunk_ids = list({row["source_chunk_id"] for row in rows
                      if row["source_kind"] == "piece" and row["source_library_id"] == current_library})
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
        elif item["source_library_id"] != current_library:
            state = "unresolved"
        else:
            chunk = chunks.get(item["source_chunk_id"])
            if chunk is None or chunk["file_id"] != item["source_file_id"]:
                state = "missing"
            else:
                state = "current" if (content_hash(chunk["chunk_text"]) == item["content_hash"]
                                      and normalize_newlines(item["quote"]) in normalize_newlines(chunk["chunk_text"])) else "changed"
                item.update(current_source_title=chunk["doc_title"], current_heading_path=chunk["heading_path"],
                            current_page_number=page_number_from_heading(chunk["heading_path"]))
        item["location_status"] = state
        result.append(item)
    return result
