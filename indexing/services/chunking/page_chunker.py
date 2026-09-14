"""
按页面切分策略

适用于: PDF (.pdf)
识别 PyMuPDF 转换后的页面分隔符
"""

from typing import Dict, Iterator, List
from .base import BaseChunker
from .utils import recursive_split, build_chunk, normalize_tables


class PageChunker(BaseChunker):
    """按页面切分文档"""

    def chunk_page(
        self, page_text: str, base_name: str, page_number: int
    ) -> List[Dict[str, str]]:
        """将单页文本切分为一个或多个切片。"""
        return list(self._page_chunks(page_text, base_name, str(page_number)))

    def _page_chunks(
        self, page_content: str, base_name: str, page_num: str
    ) -> Iterator[Dict[str, str]]:
        # 先做表格归一化：去掉 style 噪声后不少整页表格能压回一个切片，
        # 长度判断必须基于归一化后的文本，否则白白多切一刀
        page_content = normalize_tables(page_content.strip(), self.max_chunk_size)
        if not page_content:
            return

        if len(page_content) > self.max_chunk_size:
            for index, sub_chunk in enumerate(
                recursive_split(page_content, chunk_size=self.max_chunk_size),
                start=1,
            ):
                if sub_chunk.strip():
                    yield build_chunk(
                        [base_name, f"第{page_num}页", f"第{index}部分"],
                        sub_chunk.strip(),
                    )
        else:
            yield build_chunk([base_name, f"第{page_num}页"], page_content)

    def chunk(self, content: str, base_name: str) -> List[Dict[str, str]]:
        """PDF 走 processor 的逐页流式路径（chunk_page），不整体切分。"""
        raise NotImplementedError("PDF 请使用 chunk_page 逐页切分")
