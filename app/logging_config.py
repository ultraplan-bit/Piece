"""
统一日志配置模块

提供控制台、滚动文件和内存缓冲的日志处理器，支持前端实时查看日志。
索引编排与 HTTP 调用留在服务进程，各线程共用这些处理器。
"""

import logging
import logging.handlers
import os
import sys
from collections import deque
from typing import Dict, List, Optional
from datetime import datetime

from app.platform import get_default_data_dir


# 全局日志缓冲区（最多保存 500 条）
_log_buffer: deque = deque(maxlen=500)

# 缓冲区锁（线程安全）
import threading
_buffer_lock = threading.Lock()

# 单个日志文件上限与保留份数
_LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
_LOG_FILE_BACKUP_COUNT = 3

# 第三方库降噪：这些 logger 只保留 WARNING 及以上
_NOISY_LOGGERS = (
    "aiohttp",
    "asyncio",
    "azure",
    "fastmcp",
    "httpcore",
    "httpx",
    "markdown_it",
    "mcp",
    "PIL",
    "python_multipart",
    "urllib3",
    "uvicorn.access",
    "watchfiles",
)


class BufferedHandler(logging.Handler):
    """内存缓冲日志处理器，将日志保存到 deque 中供 API 读取"""

    def emit(self, record: logging.LogRecord):
        """处理日志记录"""
        try:
            # 格式化日志记录
            log_entry = {
                "timestamp": datetime.fromtimestamp(record.created).isoformat(),
                "level": record.levelname,
                "module": record.name,
                "message": self.format(record),
                "line": record.lineno,
                "function": record.funcName,
            }

            # 线程安全地添加到缓冲区
            with _buffer_lock:
                _log_buffer.append(log_entry)
        except Exception:
            # 避免日志系统自身出错影响主程序
            self.handleError(record)


def _resolve_level(level: Optional[str]) -> int:
    """解析日志级别：显式参数 > PIECE_LOG_LEVEL 环境变量 > INFO"""
    name = (level or os.getenv("PIECE_LOG_LEVEL") or "INFO").upper()
    return getattr(logging, name, logging.INFO)


def _build_console_handler() -> Optional[logging.Handler]:
    """
    构建控制台 Handler

    PyInstaller windowed 模式（console=False）下 sys.stderr 为 None，
    此时不挂控制台 Handler，避免每条日志都触发写入异常。
    """
    if sys.stderr is None or getattr(sys.stderr, "closed", False):
        return None

    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    return handler


def _build_file_handler(log_file: str) -> Optional[logging.Handler]:
    """构建滚动文件 Handler（data/logs/<log_file>）"""
    try:
        log_dir = get_default_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        handler = logging.handlers.RotatingFileHandler(
            log_dir / log_file,
            maxBytes=_LOG_FILE_MAX_BYTES,
            backupCount=_LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(name)s:%(lineno)d - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        return handler
    except Exception:
        # 磁盘不可写时降级为无文件日志，不影响应用启动
        return None


def _build_buffer_handler() -> logging.Handler:
    """构建内存缓冲 Handler"""
    handler = BufferedHandler()
    handler.setLevel(logging.DEBUG)
    # 内存缓冲只保存 message，其他信息在 emit 中提取
    handler.setFormatter(logging.Formatter(fmt="%(message)s"))
    return handler


def setup_logging(
    level: Optional[str] = None,
    log_file: str = "piece.log",
):
    """配置服务进程的控制台、滚动文件和界面日志缓冲。"""
    root_logger = logging.getLogger()
    root_logger.setLevel(_resolve_level(level))

    # 清除已有的 handlers（避免重复配置）
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    console_handler = _build_console_handler()
    if console_handler is not None:
        root_logger.addHandler(console_handler)

    file_handler = _build_file_handler(log_file)
    if file_handler is not None:
        root_logger.addHandler(file_handler)

    root_logger.addHandler(_build_buffer_handler())

    # 第三方库降噪
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    root_logger.info("日志系统初始化成功")


def get_log_buffer(level: Optional[str] = None, limit: int = 500) -> List[Dict]:
    """
    获取日志缓冲区内容

    Args:
        level: 过滤级别（可选，如 "ERROR", "WARNING"）
        limit: 返回的最大日志数量

    Returns:
        日志列表（时间倒序）
    """
    with _buffer_lock:
        logs = list(_log_buffer)

    # 按级别过滤
    if level:
        logs = [log for log in logs if log["level"] == level.upper()]

    # 倒序（最新的在前）
    logs.reverse()

    # 限制数量
    return logs[:limit]


def count_log_buffer(level: Optional[str] = None) -> int:
    """
    统计缓冲区中符合过滤条件的日志总数（不受 limit 影响）

    Args:
        level: 过滤级别（可选）

    Returns:
        日志条数
    """
    with _buffer_lock:
        if not level:
            return len(_log_buffer)
        target = level.upper()
        return sum(1 for log in _log_buffer if log["level"] == target)


def clear_log_buffer():
    """清空日志缓冲区"""
    with _buffer_lock:
        _log_buffer.clear()

    # 记录清空操作
    logger = logging.getLogger(__name__)
    logger.info("日志缓冲区已清空")
