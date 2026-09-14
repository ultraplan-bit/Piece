"""
File Repository

职责:
- 文件数据访问层
- 封装 files 表的数据库操作
"""

from typing import Optional, List, Dict, Any
from datetime import datetime

from .base_repository import BaseRepository
from ..database import get_db_cursor


class FileRepository(BaseRepository):
    """
    File Repository

    管理 files 表的数据访问
    """

    @property
    def table_name(self) -> str:
        return "files"

    @property
    def allowed_fields(self) -> List[str]:
        """返回 files 表的所有合法字段名"""
        return [
            "id", "file_hash", "filename", "file_path", "file_size",
            "original_file_type", "original_file_path", "metadata", "status",
            "created_at", "updated_at"
        ]

    # ========== 文件特定操作 ==========

    def find_by_hash(self, file_hash: str) -> Optional[Dict[str, Any]]:
        """
        根据文件哈希查询文件

        Args:
            file_hash: 文件 SHA256 哈希值

        Returns:
            文件记录，不存在则返回 None
        """
        with get_db_cursor() as cursor:
            cursor.execute("SELECT * FROM files WHERE file_hash = ?", (file_hash,))
            row = cursor.fetchone()
            return self._row_to_dict(row) if row else None

    def find_by_status(self, status: str) -> List[Dict[str, Any]]:
        """
        根据状态查询文件列表

        Args:
            status: 文件状态（pending/indexed/error/empty）

        Returns:
            文件列表
        """
        return self.find_by(status=status)

    def find_all_ordered(self, order_by: str = "created_at", desc: bool = True) -> List[Dict[str, Any]]:
        """
        查询所有文件并排序

        Args:
            order_by: 排序字段
            desc: 是否降序

        Returns:
            文件列表
        """
        self._validate_field_name(order_by)
        with get_db_cursor() as cursor:
            order = "DESC" if desc else "ASC"
            sql = f"SELECT * FROM files ORDER BY {order_by} {order}"
            cursor.execute(sql)
            return [self._row_to_dict(row) for row in cursor.fetchall()]

    def insert(
        self,
        file_hash: str,
        filename: str,
        file_path: str,
        file_size: int,
        status: str = "pending",
        original_file_type: Optional[str] = None,
        original_file_path: Optional[str] = None
    ) -> int:
        """
        插入文件记录

        Args:
            file_hash: 文件哈希
            filename: 工作文件名
            file_path: 工作文件路径
            file_size: 原始文件大小
            status: 文件状态
            original_file_type: 原始文件类型
            original_file_path: 原始文件路径

        Returns:
            新插入的 file_id
        """
        with get_db_cursor(write=True) as cursor:
            now = datetime.now().isoformat()
            cursor.execute(
                """
                INSERT INTO files (
                    file_hash, filename, file_path, file_size,
                    original_file_type, original_file_path,
                    status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (file_hash, filename, file_path, file_size,
                 original_file_type, original_file_path,
                 status, now, now),
            )
            return cursor.lastrowid

    def update_status(self, file_id: int, status: str) -> bool:
        """
        更新文件状态

        Args:
            file_id: 文件 ID
            status: 新状态

        Returns:
            是否更新成功
        """
        now = datetime.now().isoformat()
        return self.update_by_id(file_id, status=status, updated_at=now)

    def update_metadata(self, file_id: int, metadata_json: Optional[str]) -> bool:
        """
        更新文件自定义属性（JSON 字符串，来源于 frontmatter 或手动编辑）

        Args:
            file_id: 文件 ID
            metadata_json: 序列化后的属性，None 表示清空

        Returns:
            是否更新成功
        """
        now = datetime.now().isoformat()
        return self.update_by_id(file_id, metadata=metadata_json, updated_at=now)

    def find_notes(self) -> List[Dict[str, Any]]:
        """
        查询应用内新建的笔记文件（无原始文件来源）

        知识卡片就存放在这类文件里，便于同步和导出。

        Returns:
            笔记文件列表（按创建时间倒序）
        """
        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT * FROM files WHERE original_file_path IS NULL "
                "ORDER BY created_at DESC"
            )
            return [self._row_to_dict(row) for row in cursor.fetchall()]

    def find_tracked_original_paths(self) -> set:
        """
        获取所有已跟踪的原始文件路径

        Returns:
            原始文件路径集合
        """
        with get_db_cursor() as cursor:
            cursor.execute("SELECT original_file_path FROM files WHERE original_file_path IS NOT NULL")
            return {row["original_file_path"] for row in cursor.fetchall()}

    def get_storage_stats(self) -> Dict[str, Any]:
        """
        获取存储统计信息

        Returns:
            {
                "total_files": 文件总数,
                "indexed_files": 已索引文件数,
                "total_size": 总存储大小（字节）
            }
        """
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT
                    COUNT(*) as total_files,
                    SUM(CASE WHEN status = 'indexed' THEN 1 ELSE 0 END) as indexed_files,
                    COALESCE(SUM(file_size), 0) as total_size
                FROM files
            """)
            stats = cursor.fetchone()

            return {
                "total_files": stats["total_files"] or 0,
                "indexed_files": stats["indexed_files"] or 0,
                "total_size": stats["total_size"] or 0,
            }

    def delete_with_chunks(self, file_id: int) -> bool:
        """
        删除文件及其关联的切片（级联删除）

        Args:
            file_id: 文件 ID

        Returns:
            是否删除成功
        """
        with get_db_cursor(write=True) as cursor:
            # 删除 vec_chunks 中的相关记录（需要手动删除，因为没有外键关联）
            cursor.execute(
                """
                DELETE FROM vec_chunks WHERE chunk_id IN (
                    SELECT id FROM chunks WHERE file_id = ?
                )
                """,
                (file_id,)
            )

            # 删除数据库记录（chunks 会级联删除）
            cursor.execute("DELETE FROM files WHERE id = ?", (file_id,))

            return cursor.rowcount > 0
