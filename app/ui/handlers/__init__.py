"""
Handlers 包初始化

提供页面业务逻辑处理器
"""

from app.ui.handlers.file_handlers import FileHandlers
from app.ui.handlers.chunk_handlers import ChunkHandlers
from app.ui.handlers.task_handlers import TaskHandlers
from app.ui.handlers.settings_handlers import SettingsHandlers
from app.ui.handlers.sync_handlers import SyncHandlers

__all__ = [
    "FileHandlers",
    "ChunkHandlers",
    "TaskHandlers",
    "SettingsHandlers",
    "SyncHandlers",
]
