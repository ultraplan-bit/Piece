"""
按 Excel Sheet 切分策略

适用于: Excel (.xlsx)
识别 markitdown 转换后的 Sheet 分隔符
"""

import re
from typing import List, Dict
from .base import BaseChunker
from .heading_chunker import HeadingChunker
from .utils import recursive_split, build_chunk


class SheetChunker(BaseChunker):
    """按 Excel Sheet 切分文档"""

    def __init__(self):
        """初始化分块器，动态计算阈值"""
        super().__init__()
        # 大型 Sheet 阈值：2倍最大分块大小
        self.LARGE_SHEET_THRESHOLD = self.max_chunk_size * 2
        # 大型 Sheet 的分块大小：使用配置的最大分块大小
        self.LARGE_SHEET_CHUNK_SIZE = self.max_chunk_size
        # 大型 Sheet 的重叠窗口：10% 的分块大小
        self.LARGE_SHEET_OVERLAP = int(self.max_chunk_size * 0.1)

    def chunk(self, content: str, base_name: str) -> List[Dict[str, str]]:
        """
        按 Sheet 切分文档

        markitdown 转换格式:
        ## Sheet1
        | 列1 | 列2 |
        | --- | --- |
        | 数据1 | 数据2 |

        Args:
            content: Markdown 文档内容
            base_name: 文档基础名称（用于生成 doc_title）

        Returns:
            切片列表，每个切片包含 doc_title 和 chunk_text
        """
        # 匹配 Sheet 分隔符：## Sheet名称
        sheet_pattern = r"^##\s+(.+)$"
        sheet_matches = list(re.finditer(sheet_pattern, content, re.MULTILINE))

        # 未找到 Sheet 分隔符，回退到标题切分
        if not sheet_matches:
            heading_chunker = HeadingChunker()
            return heading_chunker.chunk(content, base_name)

        chunks = []

        for i, match in enumerate(sheet_matches):
            sheet_name = match.group(1).strip()
            start = match.end()
            end = (
                sheet_matches[i + 1].start()
                if i + 1 < len(sheet_matches)
                else len(content)
            )
            sheet_content = content[start:end].strip()

            if not sheet_content:
                continue

            # 大型 Sheet 处理：超过阈值时递归切分
            if len(sheet_content) > self.LARGE_SHEET_THRESHOLD:
                sub_chunks = self._split_large_sheet(
                    sheet_content, base_name, sheet_name
                )
                chunks.extend(sub_chunks)
            else:
                chunks.append(build_chunk([base_name, sheet_name], sheet_content))

        return chunks

    def _split_large_sheet(
        self, sheet_content: str, base_name: str, sheet_name: str
    ) -> List[Dict]:
        """
        处理大型 Sheet，递归切分为多个部分

        Args:
            sheet_content: Sheet 内容
            base_name: 文档基础名称
            sheet_name: Sheet 名称

        Returns:
            切片列表
        """
        text_chunks = recursive_split(
            sheet_content,
            chunk_size=self.LARGE_SHEET_CHUNK_SIZE,
            overlap=self.LARGE_SHEET_OVERLAP,
        )

        chunks = []
        for i, chunk_text in enumerate(text_chunks, start=1):
            if chunk_text.strip():
                chunks.append(
                    build_chunk(
                        [base_name, sheet_name, f"第{i}部分"], chunk_text.strip()
                    )
                )

        return chunks
