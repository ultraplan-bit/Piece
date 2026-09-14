"""
Repository 层基础模块

职责:
- 提供数据访问抽象层
- 封装数据库操作细节
- 提供统一的 CRUD 接口
"""

from .base_repository import BaseRepository
from .file_repository import FileRepository
from .chunk_repository import ChunkRepository
from .task_repository import TaskRepository
from .collection_repository import CollectionRepository

__all__ = [
    "BaseRepository",
    "FileRepository",
    "ChunkRepository",
    "TaskRepository",
    "CollectionRepository",
]
