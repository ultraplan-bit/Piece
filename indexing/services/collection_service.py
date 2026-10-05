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
    """层级、路径及计数；file_count 是含后代的唯一文件数。"""
    try:
        return _collection_repo.find_all_with_counts()
    except ValueError as exc:
        raise BusinessError("INVALID_COLLECTION_TREE", str(exc)) from exc


def collection_tree():
    """返回仅含集合结构的树，不附带文件或正文。"""
    items = list_collections()  # 同时验证父节点和循环
    nodes = {item["id"]: {**item, "children": []} for item in items}
    roots = []
    for item in items:
        node = nodes[item["id"]]
        if item["parent_id"] is None:
            roots.append(node)
        else:
            nodes[item["parent_id"]]["children"].append(node)
    return {"collections": roots, "total": len(items)}


@serialized_mutation
def create_collection(name: str, description: Optional[str] = None, parent_id: Optional[int] = None) -> Dict[str, Any]:
    """
    创建集合

    Args:
        name: 集合名（唯一，去除首尾空白）
        description: 集合说明

    Returns:
        {"success": bool, "message": str, "collection_id": int | None}
    """
    name = (name or "").strip()
    if not name or len(name) > 200:
        return {"success": False, "message": "集合名必须为 1–200 字符", "collection_id": None}
    if parent_id is not None and not _collection_repo.exists(parent_id):
        raise BusinessError("NOT_FOUND", f"父集合不存在：{parent_id}")

    try:
        collection_id = _collection_repo.insert(name, description, parent_id)
    except sqlite3.IntegrityError:
        return {"success": False, "message": f"集合已存在: {name}", "collection_id": None}

    logger.info("[Collection] 创建集合: %s (id=%s)", name, collection_id)
    return {"success": True, "message": "集合创建成功", "collection_id": collection_id}


@serialized_mutation
def rename_collection(collection_id: int, name: str) -> Dict[str, Any]:
    """重命名集合；身份和层级不变。"""
    name = (name or "").strip()
    if not name or len(name) > 200:
        return {"success": False, "message": "集合名必须为 1–200 字符"}

    try:
        updated = _collection_repo.rename(collection_id, name)
    except sqlite3.IntegrityError:
        return {"success": False, "message": f"集合已存在: {name}"}

    if not updated:
        return {"success": False, "message": f"集合不存在 (ID: {collection_id})"}
    return {"success": True, "message": "集合重命名成功"}


@serialized_mutation
def move_collection(collection_id: int, parent_id: Optional[int]) -> Dict[str, Any]:
    """显式指定新父节点（None 为根层）；检查与更新在同一写事务内。"""
    with get_db_cursor(write=True) as cursor:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT id FROM collections WHERE id = ?", (collection_id,))
        if cursor.fetchone() is None:
            raise BusinessError("NOT_FOUND", "集合不存在")
        current, visited = parent_id, {collection_id}
        while current is not None:
            if current in visited:
                raise BusinessError("COLLECTION_CYCLE", "不能移到自身或后代集合；父子关系不得形成循环")
            visited.add(current)
            cursor.execute("SELECT parent_id FROM collections WHERE id = ?", (current,))
            row = cursor.fetchone()
            if row is None:
                raise BusinessError("NOT_FOUND", f"父集合不存在：{current}")
            current = row[0]
        cursor.execute("UPDATE collections SET parent_id = ? WHERE id = ?", (parent_id, collection_id))
    return {"success": True, "message": "集合移动成功", "collection_id": collection_id, "parent_id": parent_id}


def _require_leaf(cursor, collection_id):
    cursor.execute("SELECT * FROM collections WHERE id = ?", (collection_id,))
    collection = cursor.fetchone()
    if collection is None:
        raise BusinessError("NOT_FOUND", "集合不存在")
    cursor.execute("SELECT 1 FROM collections WHERE parent_id = ? LIMIT 1", (collection_id,))
    if cursor.fetchone():
        raise BusinessError("COLLECTION_NOT_EMPTY", "集合含有子集合，请先移动或删除子集合")
    return dict(collection)


@serialized_mutation
def collection_deletion_impact(collection_id):
    with get_db_cursor() as cursor:
        collection = _require_leaf(cursor, collection_id)
        cursor.execute("SELECT COUNT(*) FROM file_collections WHERE collection_id = ?", (collection_id,))
        count = cursor.fetchone()[0]
        cursor.execute("""
            SELECT COUNT(*) FROM file_collections fc WHERE collection_id = ?
            AND NOT EXISTS (SELECT 1 FROM file_collections other
                            WHERE other.file_id = fc.file_id AND other.collection_id != fc.collection_id)
        """, (collection_id,))
        unclassified = cursor.fetchone()[0]
    collection = next(item for item in list_collections() if item["id"] == collection["id"])
    return {"collection": collection, "deletes_files": False,
            "direct_file_count": count, "unclassified_file_count": unclassified}


@serialized_mutation
def delete_collection(collection_id: int) -> bool:
    """只删除叶子集合的分类记录；最终执行时重新验证，不依赖预览。"""
    with get_db_cursor(write=True) as cursor:
        cursor.execute("BEGIN IMMEDIATE")
        _require_leaf(cursor, collection_id)
        cursor.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
        return cursor.rowcount > 0


def get_file_collections(file_id: int) -> List[Dict[str, Any]]:
    """获取文件的直接所属集合（含层级字段），不隐式补齐祖先。"""
    return _collection_repo.find_by_file_id(file_id)


def get_collections_by_file(file_ids=None) -> Dict[int, List[str]]:
    """文件 ID → 直接集合名；可只查询当前文件页。"""
    return _collection_repo.map_file_collections(file_ids)


@serialized_mutation
def set_file_collections(file_id: int, collection_ids: List[int]) -> None:
    """覆盖式设置直接归属；不会展开父子范围。"""
    with get_db_cursor(write=True) as cursor:
        cursor.execute("SELECT 1 FROM files WHERE id = ?", (file_id,))
        if cursor.fetchone() is None:
            raise BusinessError("NOT_FOUND", f"文件不存在：{file_id}")
        ids = list(dict.fromkeys(collection_ids))
        for cid in ids:
            cursor.execute("SELECT 1 FROM collections WHERE id = ?", (cid,))
            if cursor.fetchone() is None:
                raise BusinessError("NOT_FOUND", f"集合不存在：{cid}")
        cursor.execute("DELETE FROM file_collections WHERE file_id = ?", (file_id,))
        cursor.executemany("INSERT INTO file_collections VALUES (?, ?)", [(file_id, cid) for cid in ids])


def get_file_ids(collection_ids: Optional[List[int]], include_descendants=True) -> Optional[List[int]]:
    """指定集合的并集，默认含后代；None 不限范围，显式空范围返回空列表。"""
    if collection_ids is None:
        return None
    return _collection_repo.find_file_ids(collection_ids, include_descendants)


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


def resolve_names_to_file_ids(names: Optional[List[str]], include_descendants=True) -> Optional[List[int]]:
    """按既有大小写不敏感的子串规则读集合；None 不限范围，[] 或无匹配为空。"""
    if names is None:
        return None
    if not names:
        return []

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

    return _collection_repo.find_file_ids(list(set(matched_ids)), include_descendants)
