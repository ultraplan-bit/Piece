"""跨入口共享的任务受理、查询和安全维护。"""

import threading
from datetime import datetime
from functools import wraps

from ..repositories import TaskRepository
from ..database import get_db_cursor
from .errors import BusinessError

_task_repo = TaskRepository()
ACTIVE_STATUSES = ("pending", "processing")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
# ponytail: 个人知识库的短写操作统一串行；吞吐成为瓶颈时再按文件拆锁。
mutation_lock = threading.RLock()


def serialized_mutation(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with mutation_lock:
            return function(*args, **kwargs)
    return locked


def now_marker():
    return datetime.now().isoformat()


def ensure_file_idle(file_id):
    with get_db_cursor() as cursor:
        cursor.execute("SELECT id FROM tasks WHERE file_id = ? AND status IN ('pending', 'processing') LIMIT 1", (file_id,))
        if cursor.fetchone():
            raise BusinessError("FILE_BUSY", "文件有在途任务，请等待完成后操作")


def create_task(original_filename, file_id=None, *, task_type="file_index", input_data=None,
                request_key=None, cursor=None):
    if task_type not in {"file_index", "chunk_add", "chunk_update"}:
        raise BusinessError("INVALID_INPUT", "不支持的任务类型")
    if request_key is not None and (not isinstance(request_key, str) or not 1 <= len(request_key) <= 200):
        raise BusinessError("INVALID_INPUT", "请求键长度必须在 1–200 之间")
    with mutation_lock:
        return _task_repo.create(original_filename, file_id, task_type=task_type,
                                 input_data=input_data, request_key=request_key, cursor=cursor)


def get_task(task_id):
    return _task_repo.find_by_id(task_id)


def update_task_status(task_id, status, progress=None, error_message=None, file_id=None, *, result=None, error_code=None, cursor=None):
    return _task_repo.update_status(task_id, status, progress, error_message, file_id,
                                    result=result, error_code=error_code, cursor=cursor)


def claim_next_pending_task():
    with mutation_lock:
        return _task_repo.claim_next_pending()


def get_processing_tasks():
    return _task_repo.find_processing()


def mark_processing_tasks_failed(error_message):
    return _task_repo.mark_processing_failed(error_message)


def update_page_progress(task_id, current_page, total_pages, processed_chunks, progress, stage=None):
    return _task_repo.update_page_progress(task_id, current_page, total_pages, processed_chunks, progress, stage)


def get_tasks_by_ids(task_ids):
    return _task_repo.find_by_ids(task_ids)


def get_active_tasks():
    return _task_repo.find_active()


def get_active_or_updated_since(marker):
    return _task_repo.find_active_or_updated_since(marker)


def list_tasks(limit=20, offset=0, status=None, request_key=None):
    return _task_repo.list_tasks(limit, offset, status, request_key)


def get_task_by_request_key(request_key):
    if not request_key:
        return None
    with get_db_cursor() as cursor:
        cursor.execute("SELECT id FROM tasks WHERE request_key = ?", (request_key,))
        row = cursor.fetchone()
    return get_task(row[0]) if row else None


def task_summary(task_ids):
    if not isinstance(task_ids, list) or not 1 <= len(task_ids) <= 100 or any(type(i) is not int or i <= 0 for i in task_ids):
        raise BusinessError("INVALID_INPUT", "每次查询 1–100 个正整数任务 ID")
    ids = list(dict.fromkeys(task_ids))
    tasks = get_tasks_by_ids(ids)
    found = {t["id"] for t in tasks}
    missing = [i for i in ids if i not in found]
    unfinished = [t["id"] for t in tasks if t["status"] in ACTIVE_STATUSES]
    return {
        "tasks": tasks, "not_found": missing, "unfinished_task_ids": unfinished,
        "all_done": not missing and not unfinished, "all_succeeded": not missing and all(t["status"] == "completed" for t in tasks),
        "poll_after_ms": 500 if unfinished else 0,
        "summary": {**{s: sum(t["status"] == s for t in tasks) for s in (*ACTIVE_STATUSES, *TERMINAL_STATUSES)},
                    "not_found": len(missing), "total": len(ids)},
    }


def cancel_task(task_id):
    with mutation_lock:
        _task_repo.cancel(task_id)
        return get_task(task_id)


def is_cancel_requested(task_id):
    with get_db_cursor() as cursor:
        cursor.execute("SELECT stage FROM tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        return bool(row and row[0] == "cancelling")


@serialized_mutation
def finish_cancelled_task(task_id):
    """执行与清理退出后才解开文件互斥；不删除原件或既有索引。"""
    with get_db_cursor(write=True) as cursor:
        cursor.execute("SELECT status, file_id, task_type FROM tasks WHERE id = ?", (task_id,))
        task = cursor.fetchone()
        if not task or task["status"] not in ACTIVE_STATUSES:
            return
        update_task_status(task_id, "cancelled", error_message="用户取消任务", error_code="CANCELLED", cursor=cursor)
        if task["task_type"] == "file_index":
            cursor.execute("UPDATE files SET status = 'error' WHERE id = ? AND status = 'pending' "
                           "AND NOT EXISTS (SELECT 1 FROM chunks WHERE file_id = ?)",
                           (task["file_id"], task["file_id"]))


def ensure_task_publishable(task_id, cursor):
    """与取消共用 mutation_lock，提交前最后一次检查不能留下竞态窗口。"""
    cursor.execute("SELECT status, stage FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    if row and row["stage"] == "cancelling":
        raise BusinessError("CANCELLED", "用户取消任务")
    if not row or row["status"] != "processing":
        raise BusinessError("TASK_STATE", "任务已不在处理中，拒绝发布")


def get_latest_file_tasks(file_ids):
    if not file_ids:
        return {}
    with get_db_cursor() as cursor:
        cursor.execute(f"SELECT MAX(id) FROM tasks WHERE file_id IN ({','.join('?' for _ in file_ids)}) GROUP BY file_id", file_ids)
        ids = [row[0] for row in cursor.fetchall()]
    return {task["file_id"]: task for task in get_tasks_by_ids(ids)}


def retry_task(task_id):
    with mutation_lock:
        return _task_repo.retry(task_id)
