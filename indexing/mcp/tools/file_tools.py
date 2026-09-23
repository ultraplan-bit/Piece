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
    delete_file as _delete_file,
    get_file_by_id as _get_file_by_id,
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


def delete_file(file_id: int) -> Dict[str, Any]:
    """
    删除文件（级联删除所有切片和物理文件）

    Args:
        file_id: 文件 ID

    Returns:
        {
            "success": bool,
            "message": str,
            "data": {
                "file_id": int,
                "filename": str,
                "deleted_chunks": int
            }
        }

    Example:
        >>> delete_file(123)
        {
            "success": True,
            "message": "文件删除成功",
            "data": {
                "file_id": 123,
                "filename": "学术论文_2024研究.md",
                "deleted_chunks": 15
            }
        }
    """
    try:
        # 先获取文件信息（用于日志和返回）
        file_info = _get_file_by_id(file_id)
        if not file_info:
            logger.warning(f"[MCP] 删除文件失败: 文件不存在 (ID: {file_id})")
            return {
                "success": False,
                "message": f"文件不存在 (ID: {file_id})",
                "data": None,
            }

        filename = file_info["filename"]

        # 获取切片数量（用于返回）
        from indexing.services.chunk_service import get_chunks_count_by_file_id
        chunks_count = get_chunks_count_by_file_id(file_id)

        # 调用业务逻辑层删除（返回 bool）
        success = _delete_file(file_id)

        if success:
            logger.info(f"[MCP] 删除文件成功: {filename} (ID: {file_id})")
            return {
                "success": True,
                "message": "文件删除成功",
                "data": {
                    "file_id": file_id,
                    "filename": filename,
                    "deleted_chunks": chunks_count,
                },
            }
        else:
            logger.error(f"[MCP] 删除文件失败: 未知错误")
            return {"success": False, "message": "删除文件失败", "data": None}

    except Exception as e:
        logger.error(f"[MCP] 删除文件异常: {str(e)}", exc_info=True)
        return {"success": False, "message": f"删除文件失败: {str(e)}", "data": None}
