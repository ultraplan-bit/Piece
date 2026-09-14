"""
按幻灯片切分策略

适用于: PowerPoint (.pptx)
识别 markitdown 转换后的 Slide 分隔符
"""

import re
from typing import List, Dict
from .base import BaseChunker
from .heading_chunker import HeadingChunker
from .utils import recursive_split, build_chunk


class SlideChunker(BaseChunker):
    """按幻灯片切分文档"""

    def __init__(self):
        """初始化分块器"""
        super().__init__()

    def chunk(self, content: str, base_name: str) -> List[Dict[str, str]]:
        """
        按 Slide 切分文档

        markitdown 转换格式:
        <!-- Slide number: 1 -->
        内容...
        ### Notes:
        备注内容（可选）

        Args:
            content: Markdown 文档内容
            base_name: 文档基础名称（用于生成 doc_title）

        Returns:
            切片列表，每个切片包含 doc_title 和 chunk_text
        """
        # 匹配 Slide 分隔符：<!-- Slide number: N -->
        slide_pattern = r"<!--\s*Slide number:\s*(\d+)\s*-->"
        slide_matches = list(re.finditer(slide_pattern, content))

        # 未找到 Slide 分隔符，回退到标题切分
        if not slide_matches:
            heading_chunker = HeadingChunker()
            return heading_chunker.chunk(content, base_name)

        chunks = []

        for i, match in enumerate(slide_matches):
            slide_num = match.group(1)  # 提取幻灯片编号
            start = match.end()  # Slide 分隔符之后的内容
            end = (
                slide_matches[i + 1].start()
                if i + 1 < len(slide_matches)
                else len(content)
            )
            slide_content = content[start:end].strip()

            if not slide_content:
                continue

            # 移除末尾的 ### Notes: 标记及其后续内容
            slide_content = self._remove_notes_section(slide_content)

            if slide_content:
                # 检查单页大小是否超过限制
                if len(slide_content) > self.max_chunk_size:
                    # 超大页面：递归切分为多个部分
                    sub_chunks = recursive_split(slide_content, chunk_size=self.max_chunk_size)
                    for j, sub_chunk in enumerate(sub_chunks, start=1):
                        if sub_chunk.strip():
                            chunks.append(
                                build_chunk(
                                    [base_name, f"第{slide_num}页", f"第{j}部分"],
                                    sub_chunk.strip(),
                                )
                            )
                else:
                    # 正常大小：一页一个切片
                    chunks.append(
                        build_chunk([base_name, f"第{slide_num}页"], slide_content)
                    )

        return chunks

    def _remove_notes_section(self, content: str) -> str:
        """
        移除幻灯片末尾的 ### Notes: 标记及其后续内容

        Args:
            content: 幻灯片内容

        Returns:
            清理后的内容
        """
        # 查找 ### Notes: 的位置（不区分大小写）
        notes_pattern = r"###\s*Notes:\s*"
        match = re.search(notes_pattern, content, re.IGNORECASE)

        if match:
            # 截取 Notes 标记之前的内容
            return content[: match.start()].strip()

        return content.strip()
