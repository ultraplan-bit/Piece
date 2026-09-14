"""
切片管理工具 (Phase 1 - 核心 CRUD)

提供切片的创建、修改内容和删除功能。
"""
import logging
from typing import Dict, Any, List

from pydantic import BaseModel, ConfigDict, Field

from indexing.services.chunk_service import (
    create_chunk_add_task,
    create_chunk_update_task,
    delete_chunk as _delete_chunk,
    get_chunk_by_id as _get_chunk_by_id,
    batch_delete_chunks as _batch_delete_chunks,
    create_chunks as _create_chunks,
)

logger = logging.getLogger(__name__)

MAX_BATCH_CHUNKS = 50
MAX_BATCH_TEXT_CHARS = 200_000


class ChunkInput(BaseModel):
    """批量新增中的一张卡片，空白内容由逐项受理校验处理。"""

    model_config = ConfigDict(extra="forbid")

    doc_title: str = Field(description="卡片标题，用于检索定位")
    chunk_text: str = Field(description="卡片正文，支持 Markdown")


def create_chunk(file_id: int, doc_title: str, chunk_text: str) -> Dict[str, Any]:
    """
    新增切片（异步生成向量）

    Args:
        file_id: 文件 ID
        doc_title: 文档标题（用于一阶段检索返回目标）
        chunk_text: 切片文本内容

    Returns:
        {
            "success": bool,
            "message": str,
            "data": {
                "task_id": int,  # 异步任务 ID（用于查询向量生成进度）
                "doc_title": str,
                "chunk_text": str
            }
        }

    Example:
        >>> create_chunk(
        ...     file_id=123,
        ...     doc_title="论文_摘要",
        ...     chunk_text="本文提出了一种新的方法..."
        ... )
        {
            "success": True,
            "message": "切片创建成功，正在生成向量",
            "data": {
                "task_id": 789,
                "doc_title": "论文_摘要",
                "chunk_text": "本文提出了一种新的方法..."
            }
        }
    """
    try:
        # 参数验证
        if not doc_title or not doc_title.strip():
            return {
                "success": False,
                "message": "doc_title 不能为空",
                "data": None
            }

        if not chunk_text or not chunk_text.strip():
            return {
                "success": False,
                "message": "chunk_text 不能为空",
                "data": None
            }

        # 调用业务逻辑层创建异步任务
        task_id = create_chunk_add_task(
            file_id=file_id,
            doc_title=doc_title.strip(),
            chunk_text=chunk_text.strip()
        )

        logger.info(
            f"[MCP] 创建切片任务成功: {doc_title} (task_id: {task_id})"
        )
        return {
            "success": True,
            "message": "切片创建成功，正在生成向量",
            "data": {
                "task_id": task_id,
                "doc_title": doc_title.strip(),
                "chunk_text": chunk_text.strip()
            }
        }

    except ValueError as e:
        # 文件不存在等业务错误
        logger.error(f"[MCP] 创建切片失败: {str(e)}")
        return {
            "success": False,
            "message": str(e),
            "data": None
        }
    except Exception as e:
        logger.error(f"[MCP] 创建切片异常: {str(e)}", exc_info=True)
        return {
            "success": False,
            "message": f"创建切片失败: {str(e)}",
            "data": None
        }


def create_chunks(file_id: int, chunks: List[ChunkInput]) -> Dict[str, Any]:
    """MCP 只转换输入输出；所有受理规则由共享业务处理。"""
    try:
        data = _create_chunks(file_id, [chunk.model_dump() for chunk in chunks])
    except ValueError as exc:
        return {"success": False, "message": str(exc), "data": None}
    accepted, rejected = data["accepted_count"], data["rejected_count"]
    return {
        "success": rejected == 0,
        "message": f"已受理 {accepted} 个任务，{rejected} 项受理失败；尚未完成索引，请查询任务状态，勿重复提交已受理项",
        "data": {key: data[key] for key in ("file_id", "accepted_count", "rejected_count", "items", "task_ids")},
    }


def update_chunk_content(chunk_id: int, new_content: str) -> Dict[str, Any]:
    """
    修改切片内容（异步重新生成向量）

    Args:
        chunk_id: 切片 ID
        new_content: 新的切片文本内容

    Returns:
        {
            "success": bool,
            "message": str,
            "data": {
                "chunk_id": int,
                "task_id": int,  # 异步任务 ID
                "new_content": str
            }
        }

    Example:
        >>> update_chunk_content(456, "更详细的描述...")
        {
            "success": True,
            "message": "切片内容已更新，正在重新生成向量",
            "data": {
                "chunk_id": 456,
                "task_id": 890,
                "new_content": "更详细的描述..."
            }
        }
    """
    try:
        # 参数验证
        if not new_content or not new_content.strip():
            return {
                "success": False,
                "message": "新内容不能为空",
                "data": None
            }

        # 检查切片是否存在
        chunk_info = _get_chunk_by_id(chunk_id)
        if not chunk_info:
            logger.warning(f"[MCP] 修改切片内容失败: 切片不存在 (ID: {chunk_id})")
            return {
                "success": False,
                "message": f"切片不存在 (ID: {chunk_id})",
                "data": None
            }

        # 调用业务逻辑层创建异步任务
        task_id = create_chunk_update_task(chunk_id, new_content.strip())

        logger.info(
            f"[MCP] 修改切片内容任务成功: chunk_id={chunk_id}, task_id={task_id}"
        )
        return {
            "success": True,
            "message": "切片内容已更新，正在重新生成向量",
            "data": {
                "chunk_id": chunk_id,
                "task_id": task_id,
                "new_content": new_content.strip()
            }
        }

    except Exception as e:
        logger.error(f"[MCP] 修改切片内容异常: {str(e)}", exc_info=True)
        return {
            "success": False,
            "message": f"修改切片内容失败: {str(e)}",
            "data": None
        }


def delete_chunk(chunk_id: int) -> Dict[str, Any]:
    """
    删除切片（同步删除向量）

    注意：删除文件的最后一个切片时，会自动删除文件。

    Args:
        chunk_id: 切片 ID

    Returns:
        {
            "success": bool,
            "message": str,
            "data": {
                "chunk_id": int,
                "doc_title": str,
                "file_deleted": bool  # 是否因删除最后一个切片而删除了文件
            }
        }

    Example:
        >>> delete_chunk(456)
        {
            "success": True,
            "message": "切片删除成功",
            "data": {
                "chunk_id": 456,
                "doc_title": "论文_摘要",
                "file_deleted": False
            }
        }
    """
    try:
        # 先获取切片信息（用于日志）
        chunk_info = _get_chunk_by_id(chunk_id)
        if not chunk_info:
            logger.warning(f"[MCP] 删除切片失败: 切片不存在 (ID: {chunk_id})")
            return {
                "success": False,
                "message": f"切片不存在 (ID: {chunk_id})",
                "data": None
            }

        doc_title = chunk_info["doc_title"]

        # 调用业务逻辑层（返回 {"success": bool, "file_id": int, "file_deleted": bool}）
        result = _delete_chunk(chunk_id)

        if result["success"]:
            file_deleted = result.get("file_deleted", False)
            logger.info(
                f"[MCP] 删除切片成功: {doc_title} (chunk_id: {chunk_id})"
                + (", 文件已自动删除" if file_deleted else "")
            )
            return {
                "success": True,
                "message": "切片删除成功" + (", 文件已自动删除" if file_deleted else ""),
                "data": {
                    "chunk_id": chunk_id,
                    "doc_title": doc_title,
                    "file_deleted": file_deleted
                }
            }
        else:
            error_msg = result.get("error", "未知错误")
            logger.error(f"[MCP] 删除切片失败: {error_msg}")
            return {
                "success": False,
                "message": error_msg,
                "data": None
            }

    except Exception as e:
        logger.error(f"[MCP] 删除切片异常: {str(e)}", exc_info=True)
        return {
            "success": False,
            "message": f"删除切片失败: {str(e)}",
            "data": None
        }


def batch_delete_chunks(chunk_ids: list) -> Dict[str, Any]:
    """
    批量删除切片（同步删除向量）

    注意：如果删除的切片数量等于文件的总切片数，会自动删除整个文件。

    Args:
        chunk_ids: 切片 ID 列表

    Returns:
        {
            "success": bool,
            "message": str,
            "data": {
                "deleted_count": int,      # 成功删除的切片数
                "failed_count": int,       # 失败的切片数
                "deleted_files": List[int], # 被删除的文件 ID 列表
                "errors": List[str]        # 错误信息列表
            }
        }

    Example:
        >>> batch_delete_chunks([456, 457, 458])
        {
            "success": True,
            "message": "批量删除成功",
            "data": {
                "deleted_count": 3,
                "failed_count": 0,
                "deleted_files": [],
                "errors": []
            }
        }
    """
    try:
        # 参数验证
        if not chunk_ids:
            return {
                "success": False,
                "message": "切片 ID 列表不能为空",
                "data": None
            }

        if not isinstance(chunk_ids, list):
            return {
                "success": False,
                "message": "chunk_ids 必须是列表",
                "data": None
            }

        # 调用业务逻辑层
        result = _batch_delete_chunks(chunk_ids)

        logger.info(
            f"[MCP] 批量删除切片: deleted={result['deleted_count']}, "
            f"failed={result['failed_count']}, deleted_files={result['deleted_files']}"
        )

        message = "批量删除成功" if result["success"] else "批量删除部分失败"

        return {
            "success": result["success"],
            "message": message,
            "data": {
                "deleted_count": result["deleted_count"],
                "failed_count": result["failed_count"],
                "deleted_files": result["deleted_files"],
                "errors": result["errors"]
            }
        }

    except Exception as e:
        logger.error(f"[MCP] 批量删除切片异常: {str(e)}", exc_info=True)
        return {
            "success": False,
            "message": f"批量删除切片失败: {str(e)}",
            "data": None
        }

