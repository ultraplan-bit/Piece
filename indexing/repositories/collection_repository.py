"""集合及直接文件归属的数据访问；读取范围可包含后代，写入不展开。"""

from typing import Optional, List, Dict, Any
from datetime import datetime

from .base_repository import BaseRepository
from ..database import get_db_cursor


def collection_scope_cte(collection_ids=None, include_descendants=True):
    """有限的祖先/后代配对；UNION 同时去重并防止异常循环无限遍历。"""
    params = list(dict.fromkeys(collection_ids)) if collection_ids is not None else []
    where = "1" if collection_ids is None else (
        f"id IN ({','.join('?' for _ in params)})" if params else "0"
    )
    descendants = """
        UNION
        SELECT s.root_id, c.id FROM collections c
        JOIN collection_scope s ON c.parent_id = s.id
    """ if include_descendants else ""
    return f"""WITH RECURSIVE collection_scope(root_id, id) AS (
        SELECT id, id FROM collections WHERE {where}
        {descendants}
    )""", params


def file_ids_query(collection_ids, include_descendants=True):
    cte, params = collection_scope_cte(collection_ids, include_descendants)
    return cte + " SELECT DISTINCT fc.file_id FROM file_collections fc JOIN collection_scope s ON s.id = fc.collection_id", params


class CollectionRepository(BaseRepository):
    @property
    def table_name(self) -> str:
        return "collections"

    @property
    def allowed_fields(self) -> List[str]:
        return ["id", "name", "description", "parent_id", "created_at"]

    def find_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT * FROM collections WHERE name = ?", (name,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def find_all_with_counts(self) -> List[Dict[str, Any]]:
        """一次查询返回直接计数和子树唯一文件计数；path 以 ID/名称数组展示。"""
        cte, params = collection_scope_cte()
        with get_db_cursor() as cursor:
            cursor.execute(cte + """
                SELECT c.*,
                    (SELECT COUNT(*) FROM file_collections WHERE collection_id = c.id) AS direct_file_count,
                    (SELECT COUNT(*) FROM collections WHERE parent_id = c.id) AS child_count,
                    (SELECT COUNT(DISTINCT fc.file_id) FROM collection_scope s
                     JOIN file_collections fc ON fc.collection_id = s.id
                     WHERE s.root_id = c.id) AS subtree_file_count
                FROM collections c ORDER BY c.name, c.id
            """, params)
            items = [dict(row) for row in cursor.fetchall()]
        by_id = {item["id"]: item for item in items}
        for item in items:
            path, visited = [], set()
            current = item
            while current is not None:
                if current["id"] in visited:
                    raise ValueError("集合结构存在循环，请检查父子关系")
                visited.add(current["id"])
                path.append({"id": current["id"], "name": current["name"]})
                parent_id = current["parent_id"]
                if parent_id is not None and parent_id not in by_id:
                    raise ValueError("集合父节点不存在，请检查父子关系")
                current = by_id.get(parent_id)
            item["path"] = list(reversed(path))
            item["file_count"] = item["subtree_file_count"]
        return items

    def insert(self, name: str, description: Optional[str] = None, parent_id: Optional[int] = None) -> int:
        with get_db_cursor(write=True) as cursor:
            cursor.execute(
                "INSERT INTO collections (name, description, parent_id, created_at) VALUES (?, ?, ?, ?)",
                (name, description, parent_id, datetime.now().isoformat()),
            )
            return cursor.lastrowid

    def rename(self, collection_id: int, name: str) -> bool:
        return self.update_by_id(collection_id, name=name)

    def find_by_file_id(self, file_id: int) -> List[Dict[str, Any]]:
        """只返回直接归属，不把祖先当作实际关联。"""
        with get_db_cursor() as cursor:
            cursor.execute("SELECT collection_id FROM file_collections WHERE file_id = ?", (file_id,))
            ids = {row[0] for row in cursor.fetchall()}
        return [item for item in self.find_all_with_counts() if item["id"] in ids]

    def map_file_collections(self, file_ids=None) -> Dict[int, List[str]]:
        mapping: Dict[int, List[str]] = {}
        if file_ids is not None and not file_ids:
            return mapping
        params = list(dict.fromkeys(file_ids)) if file_ids is not None else []
        where = f"WHERE fc.file_id IN ({','.join('?' for _ in params)})" if file_ids is not None else ""
        with get_db_cursor() as cursor:
            cursor.execute(f"""
                SELECT fc.file_id, c.name FROM file_collections fc
                JOIN collections c ON c.id = fc.collection_id
                {where} ORDER BY c.name
            """, params)
            for row in cursor.fetchall():
                mapping.setdefault(row["file_id"], []).append(row["name"])
        return mapping

    def find_file_ids(self, collection_ids: List[int], include_descendants=True) -> List[int]:
        sql, params = file_ids_query(collection_ids, include_descendants)
        with get_db_cursor() as cursor:
            cursor.execute(sql + " ORDER BY fc.file_id", params)
            return [row["file_id"] for row in cursor.fetchall()]
