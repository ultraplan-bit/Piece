"""
集合服务模块

职责:
- 集合的增删改查（跨领域归类，一个文件可属于多个集合）
- 文件与集合的关联维护
- 把集合名解析为文件 ID，供检索侧缩小召回范围

集合解决的是"所有文件堆在一起、检索时相互干扰"的问题：
按集合过滤后，BM25/向量/标题三路检索都只在该领域内召回。
"""

import logging
import sqlite3
from typing import Any, Dict, List, Optional

from ..repositories import CollectionRepository
from ..database import get_db_cursor
from .task_service import serialized_mutation
from .errors import BusinessError

logger = logging.getLogger(__name__)

_collection_repo = CollectionRepository()


def list_collections() -> List[Dict[str, Any]]:
    """列出所有集合（含各自文件数）"""
    return _collection_repo.find_all_with_counts()


def create_collection(name: str, description: Optional[str] = None) -> Dict[str, Any]:
    """
    创建集合

    Args:
        name: 集合名（唯一，去除首尾空白）
        description: 集合说明

    Returns:
        {"success": bool, "message": str, "collection_id": int | None}
    """
    name = (name or "").strip()
    if not name:
        return {"success": False, "message": "集合名不能为空", "collection_id": None}

    try:
        collection_id = _collection_repo.insert(name, description)
    except sqlite3.IntegrityError:
        return {"success": False, "message": f"集合已存在: {name}", "collection_id": None}

    logger.info("[Collection] 创建集合: %s (id=%s)", name, collection_id)
    return {"success": True, "message": "集合创建成功", "collection_id": collection_id}


def rename_collection(collection_id: int, name: str) -> Dict[str, Any]:
    """重命名集合"""
    name = (name or "").strip()
    if not name:
        return {"success": False, "message": "集合名不能为空"}

    try:
        updated = _collection_repo.rename(collection_id, name)
    except sqlite3.IntegrityError:
        return {"success": False, "message": f"集合已存在: {name}"}

    if not updated:
        return {"success": False, "message": f"集合不存在 (ID: {collection_id})"}
    return {"success": True, "message": "集合重命名成功"}


def delete_collection(collection_id: int) -> bool:
    """删除集合（关联关系随外键级联清理，文件本身不受影响）"""
    return _collection_repo.delete_by_id(collection_id)


def get_file_collections(file_id: int) -> List[Dict[str, Any]]:
    """获取文件所属的集合列表"""
    return _collection_repo.find_by_file_id(file_id)


def get_collections_by_file() -> Dict[int, List[str]]:
    """获取"文件 ID -> 集合名列表"映射，供文件列表一次性渲染"""
    return _collection_repo.map_file_collections()


def set_file_collections(file_id: int, collection_ids: List[int]) -> None:
    """覆盖式设置文件所属集合"""
    _collection_repo.set_file_collections(file_id, collection_ids)


def get_file_ids(collection_ids: List[int]) -> List[int]:
    """获取属于指定集合（任意一个）的文件 ID 列表"""
    return _collection_repo.find_file_ids(collection_ids)


@serialized_mutation
def resolve_collection_ids(names, *, create=False):
    """写入按完整集合名定位，避免把近似名称误作归类目标。"""
    ids = []
    for raw in names or []:
        name = raw.strip()
        if not name or len(name) > 200:
            raise BusinessError("INVALID_INPUT", "集合名必须为 1–200 字符")
        item = _collection_repo.find_by_name(name)
        if not item:
            if not create:
                raise BusinessError("NOT_FOUND", f"集合不存在：{name}；请先创建集合")
            result = create_collection(name)
            if not result["success"]:
                raise BusinessError("COLLECTION_FAILED", result["message"])
            ids.append(result["collection_id"])
        else:
            ids.append(item["id"])
    return list(dict.fromkeys(ids))


@serialized_mutation
def assign_collections(file_ids, names):
    """一次事务覆盖归类，并保留 MCP 的按名称自动创建集合行为。"""
    names = list(dict.fromkeys(name.strip() for name in names if name.strip()))
    if any(len(name) > 200 for name in names):
        raise BusinessError("INVALID_INPUT", "集合名不能超过 200 字符")
    with get_db_cursor(write=True) as cursor:
        files = []
        for file_id in dict.fromkeys(file_ids):
            cursor.execute("SELECT filename FROM files WHERE id = ?", (file_id,))
            row = cursor.fetchone()
            if not row:
                raise BusinessError("NOT_FOUND", f"文件不存在：{file_id}")
            files.append({"file_id": file_id, "filename": row[0], "collections": names})
        ids = []
        for name in names:
            cursor.execute("INSERT OR IGNORE INTO collections (name) VALUES (?)", (name,))
            cursor.execute("SELECT id FROM collections WHERE name = ?", (name,))
            ids.append(cursor.fetchone()[0])
        for file in files:
            cursor.execute("DELETE FROM file_collections WHERE file_id = ?", (file["file_id"],))
            cursor.executemany("INSERT INTO file_collections VALUES (?, ?)", [(file["file_id"], cid) for cid in ids])
    return {"files": files, "file_ids": [file["file_id"] for file in files], "collections": names}


def resolve_names_to_file_ids(names: Optional[List[str]]) -> Optional[List[int]]:
    """
    将集合名解析为文件 ID 列表（不区分大小写的模糊匹配）

    供 MCP 检索工具使用：模型给出的集合名未必与库中完全一致，
    这里按包含关系匹配，匹配不到时返回 None 表示回退到全局检索。

    Args:
        names: 集合名列表

    Returns:
        文件 ID 列表；无匹配则返回 None
    """
    if not names:
        return None

    collections = _collection_repo.find_all_with_counts()
    matched_ids = []
    for name in names:
        keyword = (name or "").strip().lower()
        if not keyword:
            continue
        for collection in collections:
            collection_name = collection["name"].lower()
            if keyword == collection_name or keyword in collection_name:
                matched_ids.append(collection["id"])

    if not matched_ids:
        return []

    return _collection_repo.find_file_ids(list(set(matched_ids)))
