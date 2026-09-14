"""
Repository 基类

职责:
- 定义通用的数据访问接口
- 提供基础的 CRUD 操作模板
- 封装数据库连接细节
"""

from typing import Optional, List, Dict, Any, Generic, TypeVar
from abc import ABC, abstractmethod
from ..database import get_db_cursor


T = TypeVar('T')


class BaseRepository(ABC, Generic[T]):
    """
    Repository 基类，提供通用的数据访问接口

    子类需要实现:
    - table_name: 表名
    - allowed_fields: 允许的字段名白名单（用于动态 SQL 的字段名校验）
    - _row_to_dict: 可选，默认按 sqlite3.Row 全字段转换
    """

    @property
    @abstractmethod
    def table_name(self) -> str:
        """返回表名"""
        pass

    @property
    def allowed_fields(self) -> List[str]:
        """
        返回允许的字段名白名单（用于动态 SQL 验证）

        子类应该重写此方法返回表的所有合法字段名
        默认返回空列表表示不进行字段名验证
        """
        return []

    def _validate_field_name(self, field_name: str) -> None:
        """
        验证字段名是否在白名单中

        Raises:
            ValueError: 字段名不在白名单中
        """
        allowed = self.allowed_fields
        if allowed and field_name not in allowed:
            raise ValueError(f"Invalid field name: {field_name}. Allowed fields: {allowed}")

    def _row_to_dict(self, row) -> Dict[str, Any]:
        """将 sqlite3.Row 转换为字典（子类可重写以剔除字段）"""
        return dict(row)

    # ========== 基础 CRUD 操作 ==========

    def find_by_id(self, id: int) -> Optional[Dict[str, Any]]:
        """根据 ID 查询单条记录，不存在则返回 None"""
        with get_db_cursor() as cursor:
            cursor.execute(f"SELECT * FROM {self.table_name} WHERE id = ?", (id,))
            row = cursor.fetchone()
            return self._row_to_dict(row) if row else None

    def find_by(self, **conditions) -> List[Dict[str, Any]]:
        """
        按字段等值条件查询记录（AND 连接）

        Example:
            repo.find_by(status='indexed')
        """
        if not conditions:
            raise ValueError("find_by 必须指定条件")

        for key in conditions:
            self._validate_field_name(key)

        where_sql = " AND ".join(f"{key} = ?" for key in conditions)
        sql = f"SELECT * FROM {self.table_name} WHERE {where_sql}"

        with get_db_cursor() as cursor:
            cursor.execute(sql, tuple(conditions.values()))
            return [self._row_to_dict(row) for row in cursor.fetchall()]

    def count(self) -> int:
        """统计记录总数"""
        with get_db_cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) as count FROM {self.table_name}")
            return cursor.fetchone()["count"]

    def exists(self, id: int) -> bool:
        """检查记录是否存在"""
        with get_db_cursor() as cursor:
            cursor.execute(f"SELECT 1 FROM {self.table_name} WHERE id = ? LIMIT 1", (id,))
            return cursor.fetchone() is not None

    def delete_by_id(self, id: int) -> bool:
        """根据 ID 删除记录，返回是否删除成功"""
        with get_db_cursor(write=True) as cursor:
            cursor.execute(f"DELETE FROM {self.table_name} WHERE id = ?", (id,))
            return cursor.rowcount > 0

    def update_by_id(self, id: int, **updates) -> bool:
        """根据 ID 更新记录，返回是否更新成功"""
        if not updates:
            return False

        for key in updates:
            self._validate_field_name(key)

        set_sql = ", ".join(f"{key} = ?" for key in updates)
        sql = f"UPDATE {self.table_name} SET {set_sql} WHERE id = ?"

        with get_db_cursor(write=True) as cursor:
            cursor.execute(sql, (*updates.values(), id))
            return cursor.rowcount > 0
