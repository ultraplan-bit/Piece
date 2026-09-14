"""
Indexing 模块 - 文件索引服务

职责:
- 数据库管理
- 文件存储和 CRUD
- 文档分块处理
- 向量生成和入库
- 异步任务管理

注意: 前端 UI 和 API 路由已迁移至 app/ 模块
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .database import init_database, init_connection_pool, close_connection_pool, get_db_cursor


__all__ = [
    "init_database",
    "init_connection_pool",
    "close_connection_pool",
    "get_db_cursor",
]


def __getattr__(name):
    # 解析 helper 也属于 indexing 包，但不能因导入包就加载数据库模块。
    if name in __all__:
        from . import database

        value = getattr(database, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
