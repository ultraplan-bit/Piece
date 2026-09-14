"""
按标题切分策略

适用于: PDF、Markdown、Word、TXT
"""

import re
from typing import List, Dict
from .base import BaseChunker
from .utils import recursive_split, clean_heading, build_chunk, normalize_tables


class HeadingChunker(BaseChunker):
    """按标题层级切分文档"""

    def __init__(self):
        """初始化分块器"""
        super().__init__()

    def chunk(self, content: str, base_name: str) -> List[Dict[str, str]]:
        """
        按二级标题切分文档

        Args:
            content: Markdown 文档内容
            base_name: 文档基础名称（用于生成 doc_title）

        Returns:
            切片列表，每个切片包含 doc_title 和 chunk_text
        """
        # 先做表格归一化：去掉 style 噪声后不少章节能压回一个切片，
        # 长度判断必须基于归一化后的文本，否则白白多切一刀
        content = normalize_tables(content, self.max_chunk_size)

        h2_pattern = r"^##\s+(.+)$"
        h2_matches = list(re.finditer(h2_pattern, content, re.MULTILINE))

        # 无标题文档，按长度切分
        if not h2_matches:
            return self._split_by_length(content, base_name)

        chunks = []

        # 第一个 ## 之前的内容作为概述
        if h2_matches[0].start() > 0:
            intro_content = content[: h2_matches[0].start()].strip()
            if intro_content:
                chunks.append(build_chunk([base_name, "概述"], intro_content))

        # 按二级标题切分
        for i, match in enumerate(h2_matches):
            h2_name = clean_heading(match.group(1))
            start = match.start()
            end = h2_matches[i + 1].start() if i + 1 < len(h2_matches) else len(content)
            chunk_content = content[start:end].strip()

            if not chunk_content:
                continue

            # 超过最大分块大小，按三级标题切分
            if len(chunk_content) > self.max_chunk_size:
                sub_chunks = self._split_large_chunk(chunk_content, base_name, h2_name)
                chunks.extend(sub_chunks)
            else:
                chunks.append(build_chunk([base_name, h2_name], chunk_content))

        return chunks

    def _split_by_length(self, content: str, base_name: str) -> List[Dict]:
        """按递归字符切分文档（用于无标题文档）"""
        text_chunks = recursive_split(content, chunk_size=self.max_chunk_size)

        chunks = []
        for chunk_text in text_chunks:
            if chunk_text.strip():
                # 取文本前10个字符作为子标题
                sub_title = chunk_text.strip()[:10].replace("\n", " ")
                chunks.append(build_chunk([base_name, sub_title], chunk_text.strip()))
        return chunks

    def _split_large_chunk(self, chunk_content: str, base_name: str, h2_name: str) -> List[Dict]:
        """处理超过最大分块大小的大分块，按三级标题切分，仍过长则递归切分"""
        h3_pattern = r"^###\s+(.+)$"
        h3_matches = list(re.finditer(h3_pattern, chunk_content, re.MULTILINE))

        if not h3_matches:
            # 无三级标题，使用递归切分
            text_chunks = recursive_split(chunk_content, chunk_size=self.max_chunk_size)
            chunks = []
            for chunk_text in text_chunks:
                sub_title = chunk_text.strip()[:10].replace("\n", " ")
                chunks.append(
                    build_chunk([base_name, h2_name, sub_title], chunk_text.strip())
                )
            return chunks

        chunks = []

        # 三级标题前的引言部分
        if h3_matches[0].start() > 0:
            intro_content = chunk_content[: h3_matches[0].start()].strip()
            if intro_content:
                # 引言部分过长也需要递归切分
                if len(intro_content) > self.max_chunk_size:
                    text_chunks = recursive_split(intro_content, chunk_size=self.max_chunk_size)
                    for chunk_text in text_chunks:
                        sub_title = chunk_text.strip()[:10].replace("\n", " ")
                        chunks.append(
                            build_chunk(
                                [base_name, h2_name, sub_title], chunk_text.strip()
                            )
                        )
                else:
                    chunks.append(build_chunk([base_name, h2_name], intro_content))

        # 按三级标题切分
        for i, match in enumerate(h3_matches):
            h3_name = clean_heading(match.group(1))
            start = match.start()
            end = (
                h3_matches[i + 1].start() if i + 1 < len(h3_matches) else len(chunk_content)
            )
            h3_content = chunk_content[start:end].strip()

            if h3_content:
                # 三级标题内容过长，递归切分
                if len(h3_content) > self.max_chunk_size:
                    text_chunks = recursive_split(h3_content, chunk_size=self.max_chunk_size)
                    for j, chunk_text in enumerate(text_chunks):
                        if j == 0:
                            # 第一个切片保留完整标题
                            chunks.append(
                                build_chunk(
                                    [base_name, h2_name, h3_name], chunk_text.strip()
                                )
                            )
                        else:
                            # 后续切片用内容前缀区分
                            sub_title = chunk_text.strip()[:10].replace("\n", " ")
                            chunks.append(
                                build_chunk(
                                    [base_name, h2_name, h3_name, sub_title],
                                    chunk_text.strip(),
                                )
                            )
                else:
                    chunks.append(
                        build_chunk([base_name, h2_name, h3_name], h3_content)
                    )

        return chunks
