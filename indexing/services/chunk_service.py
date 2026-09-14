"""卡片业务：共享校验、持久化受理、原子发布和工作文件同步。"""

import logging
import os
import tempfile
from pathlib import Path

from ..database import get_db_cursor
from ..utils import run_sync, serialize_float32
from ..repositories import ChunkRepository, FileRepository
from . import task_service, file_service
from .task_service import serialized_mutation
from .errors import BusinessError
from .chunking.utils import HEADING_SEPARATOR, MAX_HEADING_LEVEL, heading_path_from_doc_title
from .page_render import PAGE_VIEWABLE_FORMATS, page_number_from_heading
from .rate_limiter import estimate_tokens, get_rate_limiter
from .embedding_client import get_embeddings_model

logger = logging.getLogger(__name__)
_chunk_repo = ChunkRepository()
_file_repo = FileRepository()
MAX_BATCH_CHUNKS = 50
MAX_BATCH_CHARACTERS = 200000


def normalize_chunk(doc_title, chunk_text):
    if not isinstance(doc_title, str) or not isinstance(chunk_text, str) or not doc_title.strip() or not chunk_text.strip():
        raise BusinessError("INVALID_INPUT", "标题和内容不能为空")
    if len(doc_title) + len(chunk_text) > MAX_BATCH_CHARACTERS:
        raise BusinessError("INVALID_INPUT", "标题和正文合计不能超过 200000 字符")
    return doc_title.strip(), chunk_text.strip()


def _title_path(chunk, doc_title):
    file_info = _file_repo.find_by_id(chunk["file_id"])
    if (page_number_from_heading(chunk.get("heading_path")) is not None and file_info
            and f".{file_info.get('original_file_type')}".lower() in PAGE_VIEWABLE_FORMATS):
        return {"heading_path": chunk["heading_path"], "heading_level": chunk["heading_level"]}
    return heading_path_from_doc_title(doc_title)


@serialized_mutation
def rebuild_working_file(file_id):
    file_info = file_service.get_file_by_id(file_id)
    if not file_info:
        return
    path = file_service.managed_path(file_info["file_path"])
    chunks = file_service.get_chunks_by_file_id(file_id) or []
    lines = []
    has_pages = f".{file_info.get('original_file_type')}".lower() in PAGE_VIEWABLE_FORMATS
    for chunk in chunks:
        text = chunk["chunk_text"]
        if text.strip().startswith("#"):
            lines.append(f"{text}\n\n")
            continue
        heading_path = chunk["heading_path"]
        if has_pages and page_number_from_heading(heading_path) is not None:
            heading_path = heading_path_from_doc_title(chunk["doc_title"])["heading_path"]
        segments = [p for p in (heading_path or "").split(HEADING_SEPARATOR) if p]
        title = segments[-1].lstrip("#").strip() if segments else chunk["doc_title"]
        level = max(2, min(int(chunk["heading_level"] or 2), MAX_HEADING_LEVEL))
        lines.extend((f"{'#' * level} {title}\n\n", f"{text}\n\n"))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".rebuild-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write("".join(lines))
        os.replace(temp_name, path)
        with get_db_cursor(write=True) as cursor:
            cursor.execute("UPDATE files SET working_dirty = 0 WHERE id = ?", (file_id,))
    finally:
        Path(temp_name).unlink(missing_ok=True)


def recover_working_files():
    with get_db_cursor() as cursor:
        cursor.execute("SELECT id FROM files WHERE working_dirty = 1")
        ids = [r[0] for r in cursor.fetchall()]
    for file_id in ids:
        rebuild_working_file(file_id)


def get_chunk_by_id(chunk_id):
    return _chunk_repo.find_by_id(chunk_id)


def get_chunks_count_by_file_id(file_id):
    return _chunk_repo.count_by_file_id(file_id)


@serialized_mutation
def delete_chunk(chunk_id):
    chunk = get_chunk_by_id(chunk_id)
    if not chunk:
        return {"success": False, "error": "Chunk 不存在"}
    file_id = chunk["file_id"]
    task_service.ensure_file_idle(file_id)
    if _chunk_repo.count_by_file_id(file_id) == 1:
        return {"success": file_service.delete_file(file_id), "file_id": file_id, "file_deleted": True}
    with get_db_cursor(write=True) as cursor:
        cursor.execute("DELETE FROM vec_chunks WHERE chunk_id = ?", (chunk_id,))
        cursor.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))
        cursor.execute("UPDATE files SET working_dirty = 1 WHERE id = ?", (file_id,))
    rebuild_working_file(file_id)
    return {"success": True, "file_id": file_id, "file_deleted": False}


@serialized_mutation
def batch_delete_chunks(chunk_ids):
    ids = list(dict.fromkeys(chunk_ids))
    if not ids:
        return {"success": False, "deleted_count": 0, "failed_count": 0, "deleted_files": [], "errors": ["切片 ID 列表不能为空"]}
    result = {"success": True, "deleted_count": 0, "failed_count": 0, "deleted_files": [], "errors": []}
    for chunk_id in ids:
        try:
            deleted = delete_chunk(chunk_id)
            if not deleted["success"]:
                raise BusinessError("NOT_FOUND", deleted["error"])
            result["deleted_count"] += 1
            if deleted["file_deleted"]:
                result["deleted_files"].append(deleted["file_id"])
        except Exception as exc:
            result["failed_count"] += 1
            result["errors"].append(f"切片 {chunk_id}: {exc}")
    result["success"] = result["failed_count"] == 0
    return result


@serialized_mutation
def update_chunk_title(chunk_id, doc_title):
    chunk = get_chunk_by_id(chunk_id)
    if not chunk:
        return None
    doc_title, _ = normalize_chunk(doc_title, chunk["chunk_text"])
    task_service.ensure_file_idle(chunk["file_id"])
    path_info = _title_path(chunk, doc_title)
    with get_db_cursor(write=True) as cursor:
        cursor.execute("UPDATE chunks SET doc_title = ?, heading_path = ?, heading_level = ? WHERE id = ?",
                       (doc_title, path_info["heading_path"], path_info["heading_level"], chunk_id))
        cursor.execute("UPDATE files SET working_dirty = 1 WHERE id = ?", (chunk["file_id"],))
    rebuild_working_file(chunk["file_id"])
    return get_chunk_by_id(chunk_id)


@serialized_mutation
def create_chunk_update_task(chunk_id, chunk_text, doc_title=None, request_key=None):
    chunk = get_chunk_by_id(chunk_id)
    if not chunk:
        raise BusinessError("NOT_FOUND", "Chunk 不存在")
    title, text = normalize_chunk(doc_title if doc_title is not None else chunk["doc_title"], chunk_text)
    payload = {"chunk_id": chunk_id, "chunk_text": text}
    if doc_title is not None:
        payload["doc_title"] = title
    return task_service.create_task(f"更新切片: {title[:20]}", chunk["file_id"], task_type="chunk_update",
                                    input_data=payload, request_key=request_key)


@serialized_mutation
def create_chunk_add_task(file_id, doc_title, chunk_text, request_key=None):
    title, text = normalize_chunk(doc_title, chunk_text)
    if type(file_id) is not int or file_id <= 0:
        raise BusinessError("INVALID_INPUT", "file_id 必须为正整数")
    return task_service.create_task(f"新增切片: {title[:20]}", file_id, task_type="chunk_add",
                                    input_data={"doc_title": title, "chunk_text": text}, request_key=request_key)


def create_chunks(file_id, chunks, request_key=None):
    if not isinstance(chunks, list) or not 1 <= len(chunks) <= MAX_BATCH_CHUNKS:
        raise BusinessError("INVALID_INPUT", "每次批量写入 1–50 张卡片")
    normalized = []
    for item in chunks:
        if not isinstance(item, dict) or set(item) != {"doc_title", "chunk_text"}:
            raise BusinessError("INVALID_INPUT", "每项只允许 doc_title 和 chunk_text")
        if not isinstance(item["doc_title"], str) or not isinstance(item["chunk_text"], str):
            raise BusinessError("INVALID_INPUT", "标题和正文必须为字符串")
        normalized.append((item["doc_title"], item["chunk_text"]))
    if sum(len(t) + len(c) for t, c in normalized) > MAX_BATCH_CHARACTERS:
        raise BusinessError("INVALID_INPUT", "批量标题和正文合计不能超过 200000 字符")
    items, task_ids = [], []
    for index, (title, text) in enumerate(normalized):
        item = {"index": index, "doc_title": title.strip(), "task_id": None}
        try:
            key = f"{request_key}:{index}" if request_key else None
            task_id = create_chunk_add_task(file_id, title, text, request_key=key)
            item["task_id"] = task_id
            task_ids.append(task_id)
        except Exception as exc:
            item.update(error_message=str(exc), error_code=getattr(exc, "code", "ACCEPT_FAILED"))
        items.append(item)
    return {"file_id": file_id, "accepted_count": len(task_ids), "rejected_count": len(items) - len(task_ids),
            "items": items, "task_ids": task_ids, "request_key": request_key,
            "status": "accepted" if len(task_ids) == len(items) else "partial"}


@serialized_mutation
def _publish_chunk(task, embedding_blob):
    payload = task["input"]
    file_id = task["file_id"]
    with get_db_cursor(write=True) as cursor:
        cursor.execute("SELECT status FROM tasks WHERE id = ?", (task["id"],))
        if cursor.fetchone()[0] != "processing":
            raise BusinessError("TASK_STATE", "任务已不在处理中，拒绝发布")
        if task["task_type"] == "chunk_add":
            path = heading_path_from_doc_title(payload["doc_title"])
            cursor.execute("SELECT COALESCE(MAX(chunk_index), -1) + 1 FROM chunks WHERE file_id = ?", (file_id,))
            index = cursor.fetchone()[0]
            cursor.execute("INSERT INTO chunks (file_id, doc_title, chunk_text, embedding, chunk_index, heading_path, heading_level) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)", (file_id, payload["doc_title"], payload["chunk_text"], embedding_blob,
                           index, path["heading_path"], path["heading_level"]))
            chunk_id = cursor.lastrowid
            cursor.execute("INSERT INTO vec_chunks (chunk_id, embedding) VALUES (?, ?)", (chunk_id, embedding_blob))
        else:
            chunk_id = payload["chunk_id"]
            cursor.execute("SELECT * FROM chunks WHERE id = ? AND file_id = ?", (chunk_id, file_id))
            row = cursor.fetchone()
            if not row:
                raise BusinessError("NOT_FOUND", "Chunk 不存在")
            chunk = dict(row)
            title = payload.get("doc_title", chunk["doc_title"])
            # 页定位只依赖原始 heading_path，不被可编辑标题覆盖。
            path = _title_path(chunk, title)
            cursor.execute("UPDATE chunks SET doc_title = ?, chunk_text = ?, embedding = ?, heading_path = ?, heading_level = ? WHERE id = ?",
                           (title, payload["chunk_text"], embedding_blob, path["heading_path"], path["heading_level"], chunk_id))
            cursor.execute("UPDATE vec_chunks SET embedding = ? WHERE chunk_id = ?", (embedding_blob, chunk_id))
        cursor.execute("UPDATE files SET status = 'indexed', working_dirty = 1, updated_at = ? WHERE id = ?",
                       (task_service.now_marker(), file_id))
        result = {"file_id": file_id, "chunk_id": chunk_id}
        task_service.update_task_status(task["id"], "completed", progress=100, result=result, cursor=cursor)
    return result


async def _process_chunk_task(task_id, expected_type):
    task = await run_sync(task_service.get_task, task_id)
    if not task or task["task_type"] != expected_type:
        raise BusinessError("INVALID_TASK", "无效的任务类型")
    try:
        await run_sync(task_service.update_task_status, task_id, "processing", progress=10)
        text = task["input"]["chunk_text"]
        await get_rate_limiter().acquire(estimate_tokens([text]))
        model = await run_sync(get_embeddings_model)
        vectors = await model.aembed_documents([text])
        if len(vectors) != 1:
            raise BusinessError("EMBEDDING_FAILED", "嵌入服务返回数量不匹配")
        result = await run_sync(_publish_chunk, task, serialize_float32(vectors[0]))
    except Exception as exc:
        await run_sync(task_service.update_task_status, task_id, "failed", error_message=str(exc), error_code=getattr(exc, "code", "PROCESSING_FAILED"))
        return
    # 产物与 completed 在同一事务提交；导出副本失败不可把已完成新增标成失败诱导重放。
    try:
        await run_sync(rebuild_working_file, result["file_id"])
    except Exception:
        logger.exception("工作文件同步失败，保留 working_dirty，启动或导出时重建")


async def process_chunk_add_task(task_id):
    await _process_chunk_task(task_id, "chunk_add")


async def process_chunk_update_task(task_id):
    await _process_chunk_task(task_id, "chunk_update")
