"""
任务管理工具

提供异步任务状态查询功能。
"""
import logging
from typing import Dict, Any, List

from indexing.services.task_service import get_task, get_tasks_by_ids

logger = logging.getLogger(__name__)

MAX_TASKS_PER_QUERY = 100
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
POLL_AFTER_MS = 2000


def _task_status_data(task_info: Dict[str, Any]) -> Dict[str, Any]:
    """统一单条与批量状态输出，不向客户端泄露待执行的任务负载。"""
    status = task_info["status"]
    error_message = task_info.get("error_message") or ""
    chunk_id = (task_info.get("result") or {}).get("chunk_id")

    return {
        "task_id": task_info["id"],
        "status": status,
        "progress": task_info["progress"],
        "chunk_id": chunk_id,
        "error_message": (error_message or None) if status in ("failed", "cancelled") else None,
    }


def get_task_status(task_id: int) -> Dict[str, Any]:
    """
    查询任务状态（用于监控异步向量生成任务）

    Args:
        task_id: 任务 ID

    Returns:
        {
            "success": bool,
            "message": str,
            "data": {
                "task_id": int,
                "status": str,  # "pending" | "processing" | "completed" | "failed"
                "progress": int,  # 0-100
                "current_page": int,
                "total_pages": int,
                "processed_chunks": int,
                "original_filename": str,
                "error_message": str | None,
                "chunk_id": int | None,  # 任务完成后返回创建的切片ID
                "created_at": str,
                "updated_at": str
            }
        }

    Example:
        >>> get_task_status(789)
        {
            "success": True,
            "message": "任务查询成功",
            "data": {
                "task_id": 789,
                "status": "completed",
                "progress": 100,
                "original_filename": "学术论文_2024研究.md",
                "error_message": None,
                "chunk_id": 456,  # 任务完成后返回
                "created_at": "2025-12-12T10:30:00",
                "updated_at": "2025-12-12T10:30:15"
            }
        }
    """
    try:
        # 调用业务逻辑层
        task_info = get_task(task_id)

        if task_info:
            data = _task_status_data(task_info)
            data.update({
                "current_page": task_info.get("current_page", 0),
                "total_pages": task_info.get("total_pages", 0),
                "processed_chunks": task_info.get("processed_chunks", 0),
                "original_filename": task_info["original_filename"],
                "created_at": task_info["created_at"],
                "updated_at": task_info["updated_at"],
            })
            logger.info(
                f"[MCP] 查询任务状态: task_id={task_id}, "
                f"status={data['status']}, progress={data['progress']}%, "
                f"chunk_id={data['chunk_id']}"
            )
            return {
                "success": True,
                "message": "任务查询成功",
                "data": data,
            }
        else:
            logger.warning(f"[MCP] 查询任务状态失败: 任务不存在 (ID: {task_id})")
            return {
                "success": False,
                "message": f"任务不存在 (ID: {task_id})",
                "data": None
            }

    except Exception as e:
        logger.error(f"[MCP] 查询任务状态异常: {str(e)}", exc_info=True)
        return {
            "success": False,
            "message": f"查询任务状态失败: {str(e)}",
            "data": None
        }


def get_tasks_status(task_ids: List[int]) -> Dict[str, Any]:
    """一次读取多个任务，按请求顺序返回精简状态并显式报告缺失 ID。"""
    if not 1 <= len(task_ids) <= MAX_TASKS_PER_QUERY:
        return {
            "success": False,
            "message": f"任务 ID 数量必须在 1-{MAX_TASKS_PER_QUERY} 之间",
            "data": None,
        }

    task_ids = list(dict.fromkeys(task_ids))
    try:
        by_id = {task["id"]: task for task in get_tasks_by_ids(task_ids)}
        tasks = [_task_status_data(by_id[task_id]) for task_id in task_ids if task_id in by_id]
        not_found = [task_id for task_id in task_ids if task_id not in by_id]
        unfinished = [task["task_id"] for task in tasks if task["status"] not in TERMINAL_STATUSES]
        summary: Dict[str, int] = dict.fromkeys(("pending", "processing", *TERMINAL_STATUSES), 0)
        for task in tasks:
            status = task["status"]
            summary[status] = summary.get(status, 0) + 1
        summary["not_found"] = len(not_found)
        summary["total"] = len(task_ids)

        logger.info("[MCP] 批量查询任务状态: %s", summary)
        return {
            "success": not not_found,
            "message": (
                "任务查询成功" if not not_found
                else "部分任务不存在，请检查 not_found 中的 ID，不要把它们作为待完成任务轮询"
            ),
            "data": {
                "tasks": tasks,
                "summary": summary,
                "not_found": not_found,
                "unfinished_task_ids": unfinished,
                "all_done": not not_found and not unfinished,
                "all_succeeded": summary["completed"] == len(task_ids),
                "poll_after_ms": POLL_AFTER_MS if unfinished else 0,
            },
        }
    except Exception as e:
        logger.error("[MCP] 批量查询任务状态异常: %s", e, exc_info=True)
        return {
            "success": False,
            "message": f"批量查询任务状态失败: {str(e)}",
            "data": None,
        }
