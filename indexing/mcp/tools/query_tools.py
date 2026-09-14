"""
查询工具 (Phase 2 - 查询和统计)

提供文件和切片的查询、统计功能。
"""
import logging
from typing import Dict, Any, Optional

from indexing.services.file_service import (
    get_files_list_paginated,
    get_file_by_id as _get_file_by_id,
    get_storage_stats as _get_storage_stats,
)
from indexing.services.chunk_service import (
    get_chunk_by_id as _get_chunk_by_id,
    get_chunks_count_by_file_id,
)

logger = logging.getLogger(__name__)


def list_files(
    limit: int = 20,
    offset: int = 0,
    status: Optional[str] = None
) -> Dict[str, Any]:
    """
    列出所有文件（支持分页和状态过滤）

    Args:
        limit: 每页数量（默认 20，最大 100）
        offset: 偏移量（默认 0）
        status: 可选，按状态筛选 ('pending', 'indexed', 'error', 'empty')

    Returns:
        {
            "success": bool,
            "message": str,
            "data": {
                "files": [
                    {
                        "id": int,
                        "filename": str,
                        "file_path": str,
                        "file_size": int,
                        "status": str,
                        "original_file_type": str | None,
                        "created_at": str,
                        "updated_at": str
                    },
                    ...
                ],
                "total": int,  # 总文件数
                "limit": int,  # 每页数量
                "offset": int  # 偏移量
            }
        }

    Example:
        >>> list_files(limit=10, offset=0, status="indexed")
        {
            "success": True,
            "message": "查询成功",
            "data": {
                "files": [...],
                "total": 25,
                "limit": 10,
                "offset": 0
            }
        }
    """
    try:
        # 参数验证
        if limit <= 0 or limit > 100:
            return {
                "success": False,
                "message": "limit 必须在 1-100 之间",
                "data": None
            }

        if offset < 0:
            return {
                "success": False,
                "message": "offset 不能为负数",
                "data": None
            }

        if status and status not in ["pending", "indexed", "error", "empty"]:
            return {
                "success": False,
                "message": "status 必须是 'pending', 'indexed', 'error', 'empty' 之一",
                "data": None
            }

        # 调用业务逻辑层
        result = get_files_list_paginated(limit=limit, offset=offset, status=status)

        logger.info(
            f"[MCP] 查询文件列表: limit={limit}, offset={offset}, "
            f"status={status}, total={result['total']}"
        )

        return {
            "success": True,
            "message": "查询成功",
            "data": result
        }

    except Exception as e:
        logger.error(f"[MCP] 查询文件列表异常: {str(e)}", exc_info=True)
        return {
            "success": False,
            "message": f"查询文件列表失败: {str(e)}",
            "data": None
        }


def get_file_info(file_id: int) -> Dict[str, Any]:
    """
    获取文件详情（包含切片数量等统计信息）

    Args:
        file_id: 文件 ID

    Returns:
        {
            "success": bool,
            "message": str,
            "data": {
                "id": int,
                "filename": str,
                "file_path": str,
                "file_size": int,
                "file_hash": str,
                "status": str,
                "original_file_type": str | None,
                "original_file_path": str | None,
                "chunks_count": int,  # 切片数量
                "created_at": str,
                "updated_at": str
            }
        }

    Example:
        >>> get_file_info(123)
        {
            "success": True,
            "message": "查询成功",
            "data": {
                "id": 123,
                "filename": "论文.md",
                "file_size": 102400,
                "status": "indexed",
                "chunks_count": 15,
                ...
            }
        }
    """
    try:
        # 获取文件信息
        file_info = _get_file_by_id(file_id)

        if not file_info:
            logger.warning(f"[MCP] 获取文件详情失败: 文件不存在 (ID: {file_id})")
            return {
                "success": False,
                "message": f"文件不存在 (ID: {file_id})",
                "data": None
            }

        # 获取切片数量
        chunks_count = get_chunks_count_by_file_id(file_id)

        # 组装返回数据（补上集合归类与 frontmatter 属性，便于 AI 判断文件用途）
        from indexing.services.collection_service import get_file_collections
        from indexing.services.metadata_service import decode_metadata

        data = {
            **file_info,
            "chunks_count": chunks_count,
            "collections": [item["name"] for item in get_file_collections(file_id)],
            "properties": decode_metadata(file_info.get("metadata")),
        }
        data.pop("metadata", None)

        logger.info(f"[MCP] 获取文件详情成功: {file_info['filename']} (ID: {file_id})")

        return {
            "success": True,
            "message": "查询成功",
            "data": data
        }

    except Exception as e:
        logger.error(f"[MCP] 获取文件详情异常: {str(e)}", exc_info=True)
        return {
            "success": False,
            "message": f"获取文件详情失败: {str(e)}",
            "data": None
        }


def get_chunk_info(chunk_id: int) -> Dict[str, Any]:
    """
    获取切片详情

    Args:
        chunk_id: 切片 ID

    Returns:
        {
            "success": bool,
            "message": str,
            "data": {
                "id": int,
                "file_id": int,
                "doc_title": str,
                "chunk_text": str
            }
        }

    Example:
        >>> get_chunk_info(456)
        {
            "success": True,
            "message": "查询成功",
            "data": {
                "id": 456,
                "file_id": 123,
                "doc_title": "论文_摘要",
                "chunk_text": "本文提出了一种新的方法..."
            }
        }
    """
    try:
        # 获取切片信息
        chunk_info = _get_chunk_by_id(chunk_id)

        if not chunk_info:
            logger.warning(f"[MCP] 获取切片详情失败: 切片不存在 (ID: {chunk_id})")
            return {
                "success": False,
                "message": f"切片不存在 (ID: {chunk_id})",
                "data": None
            }

        logger.info(
            f"[MCP] 获取切片详情成功: {chunk_info['doc_title']} (ID: {chunk_id})"
        )

        return {
            "success": True,
            "message": "查询成功",
            "data": chunk_info
        }

    except Exception as e:
        logger.error(f"[MCP] 获取切片详情异常: {str(e)}", exc_info=True)
        return {
            "success": False,
            "message": f"获取切片详情失败: {str(e)}",
            "data": None
        }


def get_storage_stats() -> Dict[str, Any]:
    """
    获取存储统计信息

    Returns:
        {
            "success": bool,
            "message": str,
            "data": {
                "total_files": int,      # 文件总数
                "indexed_files": int,    # 已索引文件数
                "total_chunks": int,     # 切片总数
                "total_size": int        # 总存储大小（字节）
            }
        }

    Example:
        >>> get_storage_stats()
        {
            "success": True,
            "message": "查询成功",
            "data": {
                "total_files": 50,
                "indexed_files": 45,
                "total_chunks": 1250,
                "total_size": 52428800
            }
        }
    """
    try:
        # 调用业务逻辑层
        stats = _get_storage_stats()

        logger.info(
            f"[MCP] 获取存储统计: files={stats['total_files']}, "
            f"chunks={stats['total_chunks']}, size={stats['total_size']}"
        )

        return {
            "success": True,
            "message": "查询成功",
            "data": stats
        }

    except Exception as e:
        logger.error(f"[MCP] 获取存储统计异常: {str(e)}", exc_info=True)
        return {
            "success": False,
            "message": f"获取存储统计失败: {str(e)}",
            "data": None
        }
