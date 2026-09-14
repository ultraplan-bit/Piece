"""
分块策略模块

采用工厂模式设计，根据文件类型选择不同的分块策略。
"""

from .base import BaseChunker
from .heading_chunker import HeadingChunker
from .slide_chunker import SlideChunker
from .sheet_chunker import SheetChunker
from .page_chunker import PageChunker
from .factory import ChunkerFactory

__all__ = [
    "BaseChunker",
    "HeadingChunker",
    "SlideChunker",
    "SheetChunker",
    "PageChunker",
    "ChunkerFactory",
]
