"""持久化任务队列；输入与结果独立保存，同文件按受理顺序执行。"""

import json
from datetime import datetime

from .base_repository import BaseRepository
from ..database import get_db_cursor
from ..services.errors import BusinessError


class TaskRepository(BaseRepository):
    @property
    def table_name(self):
        return "tasks"

    @property
    def allowed_fields(self):
        return [
            "id", "file_id", "original_filename", "task_type", "input_json",
            "result_json", "request_key", "status", "progress", "current_page",
            "total_pages", "processed_chunks", "stage", "error_code",
            "error_message", "created_at", "updated_at",
        ]

    def _row_to_dict(self, row):
        data = dict(row)
        data["input"] = json.loads(data.pop("input_json"))
        result = data.pop("result_json")
        data["result"] = json.loads(result) if result else None
        return data

    def create(self, original_filename, file_id=None, *, task_type="file_index",
               input_data=None, request_key=None, cursor=None):
        if cursor is None:
            with get_db_cursor(write=True) as transaction:
                return self.create(original_filename, file_id, task_type=task_type,
                                   input_data=input_data, request_key=request_key, cursor=transaction)
        payload = json.dumps(input_data or {}, ensure_ascii=False, sort_keys=True)
        if request_key:
            cursor.execute("SELECT * FROM tasks WHERE request_key = ?", (request_key,))
            previous = cursor.fetchone()
            if previous:
                if (previous["file_id"], previous["task_type"], previous["input_json"]) != (file_id, task_type, payload):
                    raise BusinessError("REQUEST_CONFLICT", "请求键已用于不同的操作或输入")
                return previous["id"]
        cursor.execute("SELECT id FROM files WHERE id = ?", (file_id,))
        if not cursor.fetchone():
            raise BusinessError("NOT_FOUND", "文件不存在")
        cursor.execute(
            "SELECT task_type FROM tasks WHERE file_id = ? AND status IN ('pending', 'processing')",
            (file_id,),
        )
        active = cursor.fetchall()
        if active and (task_type == "file_index" or any(t[0] == "file_index" for t in active)):
            raise BusinessError("FILE_BUSY", "文件有在途任务，请等待完成后重新索引或修改")
        now = datetime.now().isoformat()
        cursor.execute(
            "INSERT INTO tasks (file_id, original_filename, task_type, input_json, request_key, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (file_id, original_filename, task_type, payload, request_key, now, now),
        )
        return cursor.lastrowid

    def claim_next_pending(self):
        with get_db_cursor(write=True) as cursor:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("""
                SELECT t.id FROM tasks t WHERE t.status = 'pending'
                AND NOT EXISTS (SELECT 1 FROM tasks active WHERE active.file_id = t.file_id
                    AND active.status = 'processing')
                ORDER BY t.id LIMIT 1
            """)
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute(
                "UPDATE tasks SET status = 'processing', updated_at = ? WHERE id = ? AND status = 'pending'",
                (datetime.now().isoformat(), row["id"]),
            )
            cursor.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],))
            return self._row_to_dict(cursor.fetchone())

    def _query(self, where, params=(), *, limit=None, offset=0):
        with get_db_cursor() as cursor:
            sql = f"SELECT * FROM tasks WHERE {where} ORDER BY id"
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                params = (*params, limit, offset)
            cursor.execute(sql, params)
            return [self._row_to_dict(row) for row in cursor.fetchall()]

    def find_processing(self):
        return self._query("status = 'processing'")

    def find_active(self):
        return self._query("status IN ('pending', 'processing')")

    def find_active_or_updated_since(self, marker):
        return self._query("updated_at >= ? OR status IN ('pending', 'processing')", (marker,))

    def find_by_ids(self, task_ids):
        if not task_ids:
            return []
        return self._query(f"id IN ({','.join('?' for _ in task_ids)})", task_ids)

    def list_tasks(self, limit=20, offset=0, status=None, request_key=None):
        where, params = ("status = ?", (status,)) if status else ("1", ())
        if request_key is not None:
            prefix = request_key + ":"
            where += " AND (request_key = ? OR substr(request_key, 1, ?) = ?)"
            params = (*params, request_key, len(prefix), prefix)
        with get_db_cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM tasks WHERE {where}", params)
            total = cursor.fetchone()[0]
        return {"tasks": self._query(where, params, limit=limit, offset=offset),
                "total": total, "limit": limit, "offset": offset}

    def mark_processing_failed(self, error_message):
        with get_db_cursor(write=True) as cursor:
            cursor.execute(
                "UPDATE tasks SET status = 'failed', error_code = 'INTERRUPTED', error_message = ?, updated_at = ? "
                "WHERE status = 'processing'",
                (error_message, datetime.now().isoformat()),
            )
            count = cursor.rowcount
            cursor.execute("DELETE FROM staged_chunks WHERE task_id IN (SELECT id FROM tasks WHERE status = 'failed')")
            return count

    def update_page_progress(self, task_id, current_page, total_pages, processed_chunks, progress, stage=None):
        with get_db_cursor(write=True) as cursor:
            cursor.execute(
                "UPDATE tasks SET status = 'processing', progress = ?, current_page = ?, total_pages = ?, "
                "processed_chunks = ?, stage = COALESCE(?, stage), updated_at = ? "
                "WHERE id = ? AND status IN ('pending', 'processing')",
                (progress, current_page, total_pages, processed_chunks, stage, datetime.now().isoformat(), task_id),
            )
            return cursor.rowcount > 0

    def update_status(self, task_id, status, progress=None, error_message=None, file_id=None,
                      *, result=None, error_code=None, cursor=None):
        if cursor is None:
            with get_db_cursor(write=True) as transaction:
                return self.update_status(task_id, status, progress, error_message, file_id,
                                          result=result, error_code=error_code, cursor=transaction)
        cursor.execute(
            "UPDATE tasks SET status = ?, progress = COALESCE(?, progress), file_id = COALESCE(?, file_id), "
            "error_message = ?, error_code = ?, result_json = COALESCE(?, result_json), updated_at = ? "
            "WHERE id = ? AND status IN ('pending', 'processing')",
            (status, progress, file_id, error_message, error_code,
             json.dumps(result, ensure_ascii=False) if result is not None else None,
             datetime.now().isoformat(), task_id),
        )
        return cursor.rowcount > 0

    def cancel_pending(self, task_id):
        with get_db_cursor(write=True) as cursor:
            cursor.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                raise BusinessError("NOT_FOUND", "任务不存在")
            if row[0] != "pending":
                raise BusinessError("TASK_NOT_CANCELLABLE", "仅允许取消未领取任务；运行中任务必须等安全完成")
            return self.update_status(task_id, "cancelled", error_message="用户取消未领取任务",
                                      error_code="CANCELLED", cursor=cursor)

    def retry(self, task_id):
        with get_db_cursor(write=True) as cursor:
            cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                raise BusinessError("NOT_FOUND", "任务不存在")
            task = self._row_to_dict(row)
            if task["status"] not in ("failed", "cancelled") or task["result"] is not None:
                raise BusinessError("TASK_NOT_RETRYABLE", "只能重试尚无已发布结果的失败或取消任务")
            return self.create(task["original_filename"], task["file_id"], task_type=task["task_type"],
                               input_data=task["input"], request_key=f"retry:{task_id}", cursor=cursor)
