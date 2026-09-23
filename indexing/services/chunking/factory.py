"""
分块策略工厂

根据文件类型自动选择最优分块策略。
"""

from typing import Dict, Type
from .base import BaseChunker
from .heading_chunker import HeadingChunker
from .slide_chunker import SlideChunker
from .sheet_chunker import SheetChunker
from .page_chunker import PageChunker


class ChunkerFactory:
    """分块策略工厂"""

    # 文件扩展名 -> 分块器映射
    _chunker_map: Dict[str, Type[BaseChunker]] = {
        # PPT 使用 SlideChunker（按 Slide 切分）
        ".pptx": SlideChunker,
        ".ppt": SlideChunker,
        ".odp": SlideChunker,
        # Excel 使用 SheetChunker（按 Sheet 切分）
        ".xlsx": SheetChunker,
        # PDF 使用 PageChunker（按页切分）
        ".pdf": PageChunker,
        # Markdown、Word、TXT 使用 HeadingChunker（按标题切分）
        ".md": HeadingChunker,
        ".docx": HeadingChunker,
        ".doc": HeadingChunker,
        ".rtf": HeadingChunker,
        ".odt": HeadingChunker,
        ".txt": HeadingChunker,
        # 另存的网页和 EPUB 电子书先转成 Markdown，再按标题切分；
        # EPUB 每章转成一个 ## 段，正好按章成卡
        ".html": HeadingChunker,
        ".htm": HeadingChunker,
        ".epub": HeadingChunker,
    }

    @classmethod
    def get_chunker(cls, file_extension: str) -> BaseChunker:
        """
        根据文件扩展名获取对应的分块器实例

        Args:
            file_extension: 文件扩展名（如 ".pdf", ".pptx"）

        Returns:
            分块器实例

        Raises:
            ValueError: 不支持的文件类型
        """
        file_extension = file_extension.lower()

        chunker_class = cls._chunker_map.get(file_extension)
        if chunker_class is None:
            raise ValueError(f"不支持的文件类型: {file_extension}")

        return chunker_class()

    @classmethod
    def get_supported_extensions(cls) -> list:
        """获取所有支持的文件扩展名"""
        return list(cls._chunker_map.keys())
