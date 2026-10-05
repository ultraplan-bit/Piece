"""
文件管理工具 (Phase 1 - 核心 CRUD)

提供文件的创建、整篇导入和删除功能。
"""

import logging
from typing import Any, Dict, List, Optional

from indexing.services.collection_service import resolve_collection_ids
from indexing.services.file_service import (
    MAX_MARKDOWN_CHARS,
    create_empty_file as _create_empty_file,
    import_markdown as _import_markdown,
)

logger = logging.getLogger(__name__)

# MCP 入参校验与业务层同一上限，超限请求在建 schema 阶段就被拒绝
MAX_IMPORT_CHARS = MAX_MARKDOWN_CHARS


def import_markdown(
    filename: str,
    content: str,
    properties: Optional[Dict[str, Any]] = None,
    collections: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """MCP 只做输入输出转换：切片、查重、属性合并和入队都由共享业务完成。

    集合按名称定位，不存在时自动创建，与 set_file_collections 的行为一致。
    """
    try:
        collection_ids = resolve_collection_ids(collections, create=True) if collections else None
        result = _import_markdown(filename, content, collection_ids, properties)
    except ValueError as exc:
        logger.error("[MCP] 导入 Markdown 失败: %s", exc)
        return {"success": False, "message": str(exc), "data": None}
    except Exception as exc:
        logger.error("[MCP] 导入 Markdown 异常: %s", exc, exc_info=True)
        return {"success": False, "message": f"导入失败: {exc}", "data": None}

    duplicate = bool(result.get("duplicate"))
    logger.info("[MCP] 导入 Markdown: file_id=%s, duplicate=%s", result["file_id"], duplicate)
    return {
        "success": True,
        "message": (
            "相同内容已存在，返回已有文件，未创建新任务"
            if duplicate
            else "已受理，尚未完成索引；请用 check_task_status 查询 task_id"
        ),
        "data": {
            "file_id": result["file_id"],
            "filename": result["filename"],
            "task_id": result.get("task_id"),
            "task_ids": result.get("task_ids", []),
            "duplicate": duplicate,
            "status": result.get("status"),
        },
    }


def create_empty_file(filename: str) -> Dict[str, Any]:
    """
    创建空白 Markdown 文件

    Args:
        filename: 文件名（自动补充 .md 后缀，处理重名冲突）

    Returns:
        {
            "success": bool,
            "message": str,
            "data": {
                "file_id": int,
                "filename": str,
                "file_path": str,
                "status": "empty"
            }
        }

    Example:
        >>> create_empty_file("学术论文_2024研究")
        {
            "success": True,
            "message": "文件创建成功",
            "data": {
                "file_id": 123,
                "filename": "学术论文_2024研究.md",
                "file_path": "data/files/学术论文_2024研究.md",
                "status": "empty"
            }
        }
    """
    try:
        # 调用业务逻辑层（返回 {"file_id": ..., "filename": ..., "file_path": ...}）
        result = _create_empty_file(filename)

        logger.info(f"[MCP] 创建空文件成功: {result['filename']}")
        return {
            "success": True,
            "message": "文件创建成功",
            "data": {
                "file_id": result["file_id"],
                "filename": result["filename"],
                "file_path": result["file_path"],
                "status": "empty",
            },
        }

    except ValueError as e:
        # 业务逻辑错误（文件名为空等）
        logger.error(f"[MCP] 创建空文件失败: {str(e)}")
        return {"success": False, "message": str(e), "data": None}
    except Exception as e:
        logger.error(f"[MCP] 创建空文件异常: {str(e)}", exc_info=True)
        return {"success": False, "message": f"创建文件失败: {str(e)}", "data": None}


def delete_file(file_id: int, dry_run: bool = False, confirmed: bool = False) -> Dict[str, Any]:
    """文件删除统一走维护服务，预览包含知识证据引用数与快照保留提示。"""
    from indexing.services.maintenance_service import delete_files
    from indexing.services.errors import BusinessError
    try:
        impact = delete_files([file_id], dry_run=dry_run, confirmed=confirmed)
        success = not impact.get("failed_count")
        return {
            "success": success,
            "message": "删除预览，未执行" if dry_run else ("文件删除成功" if success else "文件删除失败"),
            "data": {**impact, "file_id": file_id, "filename": impact["filenames"][0],
                     "deleted_chunks": impact["chunks_count"] if not dry_run and success else 0},
        }
    except BusinessError as exc:
        return {"success": False, "message": str(exc), "data": exc.data,
                "error": {"code": exc.code, "message": str(exc)}}
