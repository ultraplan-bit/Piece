"""
配置文件：检索参数配置

嵌入模型和数据库配置已统一迁移到 indexing/settings.py
本文件仅保留检索相关参数
"""

from pydantic import BaseModel


class SearchConfig(BaseModel):
    """检索参数配置（三路检索架构）"""

    # 精确匹配参数（doc_title LIKE 检索）
    exact_top_k: int = 10
    exact_weight: float = 0.4

    # BM25检索参数（chunk_text FTS5检索）
    bm25_top_k: int = 10
    bm25_weight: float = 0.3

    # 向量检索参数（embedding sqlite-vec余弦相似度）
    vector_top_k: int = 10
    vector_weight: float = 0.3

    # RRF重排序参数
    rrf_k: int = 60

    # 最终输出参数
    final_top_k: int = 20

    # 停用词
    stopwords: set = {
        "的",
        "是",
        "在",
        "了",
        "和",
        "与",
        "及",
        "或",
        "等",
        "个",
        "为",
        "有",
        "以",
        "将",
        "从",
        "把",
        "被",
        "让",
        "向",
        "到",
        "由",
        "给",
        "对",
        "而",
        "着",
        "之",
        "其",
        "中",
        "？",
        "！",
        "，",
        "。",
        "、",
        "；",
        "：",
        "（",
        "）",
        "【",
        "】",
        "什么",
        "怎么",
        "如何",
        "哪些",
        "哪个",
    }


# 全局检索参数实例
config = SearchConfig()
