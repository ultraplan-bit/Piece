"""
Collection Repository

职责:
- 集合数据访问层
- 封装 collections 表和 file_collections 关联表的数据库操作
"""

from typing import Optional, List, Dict, Any
from datetime import datetime

from .base_repository import BaseRepository
from ..database import get_db_cursor


class CollectionRepository(BaseRepository):
    """
    Collection Repository

    管理 collections 表（集合）与 file_collections 表（文件-集合多对多关联）
    """

    @property
    def table_name(self) -> str:
        return "collections"

    @property
    def allowed_fields(self) -> List[str]:
        """返回 collections 表的所有合法字段名"""
        return ["id", "name", "description", "created_at"]

    # ========== 集合本身 ==========

    def find_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """按名称精确查询集合"""
        with get_db_cursor() as cursor:
            cursor.execute("SELECT * FROM collections WHERE name = ?", (name,))
            row = cursor.fetchone()
            return self._row_to_dict(row) if row else None

    def find_all_with_counts(self) -> List[Dict[str, Any]]:
        """
        列出所有集合，附带各自的文件数

        Returns:
            [{"id": 1, "name": "论文", "description": None, "file_count": 12}, ...]
        """
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                SELECT c.id, c.name, c.description, c.created_at,
                       COUNT(fc.file_id) AS file_count
                FROM collections c
                LEFT JOIN file_collections fc ON fc.collection_id = c.id
                GROUP BY c.id
                ORDER BY c.name
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def insert(self, name: str, description: Optional[str] = None) -> int:
        """
        创建集合

        Args:
            name: 集合名（唯一）
            description: 集合说明

        Returns:
            新集合 ID

        Raises:
            sqlite3.IntegrityError: 同名集合已存在
        """
        with get_db_cursor(write=True) as cursor:
            cursor.execute(
                "INSERT INTO collections (name, description, created_at) VALUES (?, ?, ?)",
                (name, description, datetime.now().isoformat()),
            )
            return cursor.lastrowid

    def rename(self, collection_id: int, name: str) -> bool:
        """重命名集合"""
        return self.update_by_id(collection_id, name=name)

    # ========== 文件与集合的关联 ==========

    def find_by_file_id(self, file_id: int) -> List[Dict[str, Any]]:
        """获取某个文件所属的集合列表"""
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                SELECT c.id, c.name
                FROM collections c
                JOIN file_collections fc ON fc.collection_id = c.id
                WHERE fc.file_id = ?
                ORDER BY c.name
                """,
                (file_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def map_file_collections(self) -> Dict[int, List[str]]:
        """
        一次性取出所有文件的集合名，供文件列表渲染使用

        Returns:
            {file_id: ["论文", "工作"], ...}
        """
        mapping: Dict[int, List[str]] = {}
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                SELECT fc.file_id, c.name
                FROM file_collections fc
                JOIN collections c ON c.id = fc.collection_id
                ORDER BY c.name
                """
            )
            for row in cursor.fetchall():
                mapping.setdefault(row["file_id"], []).append(row["name"])
        return mapping

    def find_file_ids(self, collection_ids: List[int]) -> List[int]:
        """获取属于指定集合（任意一个）的文件 ID 列表"""
        if not collection_ids:
            return []

        placeholders = ",".join("?" * len(collection_ids))
        with get_db_cursor() as cursor:
            cursor.execute(
                f"SELECT DISTINCT file_id FROM file_collections "
                f"WHERE collection_id IN ({placeholders})",
                collection_ids,
            )
            return [row["file_id"] for row in cursor.fetchall()]

    def set_file_collections(self, file_id: int, collection_ids: List[int]) -> None:
        """
        覆盖式设置文件所属集合（先清空再写入）

        Args:
            file_id: 文件 ID
            collection_ids: 目标集合 ID 列表，空列表表示取消所有归类
        """
        with get_db_cursor(write=True) as cursor:
            cursor.execute("DELETE FROM file_collections WHERE file_id = ?", (file_id,))
            if collection_ids:
                cursor.executemany(
                    "INSERT INTO file_collections (file_id, collection_id) VALUES (?, ?)",
                    [(file_id, cid) for cid in dict.fromkeys(collection_ids)],
                )
