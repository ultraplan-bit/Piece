"""
文件管理工具 (Phase 1 - 核心 CRUD)

提供文件的创建和删除功能。
"""

import logging
from typing import Dict, Any

from indexing.services.file_service import (
    create_empty_file as _create_empty_file,
    delete_file as _delete_file,
    get_file_by_id as _get_file_by_id,
)

logger = logging.getLogger(__name__)


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
