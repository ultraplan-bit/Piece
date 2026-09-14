"""
分块策略基类

定义所有分块策略的统一接口。
"""

from abc import ABC, abstractmethod
from typing import List, Dict


class BaseChunker(ABC):
    """分块策略基类"""

    def __init__(self):
        """初始化分块器，从配置中读取最大分块大小"""
        from ...settings import get_chunking_config
        config = get_chunking_config()
        self.max_chunk_size = config["max_chunk_size"]

    @abstractmethod
    def chunk(self, content: str, base_name: str) -> List[Dict[str, str]]:
        """
        切分文档内容

        Args:
            content: Markdown 文档内容
            base_name: 文档基础名称（用于生成 doc_title）

        Returns:
            切片列表，每个切片包含 doc_title 和 chunk_text
            [
                {"doc_title": "文件名_章节名", "chunk_text": "内容"},
                ...
            ]
        """
        pass
