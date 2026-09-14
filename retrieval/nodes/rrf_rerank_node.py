"""
RRF重排序节点：使用RRF算法融合三路检索结果
"""
from typing import List, Dict, Any
from .state import State, SearchResult
from ..config import config


def rrf_fusion_three_way(
    exact_results: List[SearchResult],
    bm25_results: List[SearchResult],
    vector_results: List[SearchResult],
    k: int = 60,
    exact_weight: float = 0.4,
    bm25_weight: float = 0.3,
    vector_weight: float = 0.3,
) -> List[Dict[str, Any]]:
    """
    RRF (Reciprocal Rank Fusion) 算法融合三路检索结果

    Args:
        exact_results: 精确匹配结果（doc_title LIKE）
        bm25_results: BM25检索结果（chunk_text词频）
        vector_results: 向量检索结果（embedding余弦相似度）
        k: RRF常数，控制排名影响的平滑程度
        exact_weight: 精确匹配权重
        bm25_weight: BM25检索权重
        vector_weight: 向量检索权重

    Returns:
        融合后的结果列表，按rrf_score降序排序
    """
    # 构建排名字典
    exact_ranks = {
        item["doc_title"]: (rank + 1) for rank, item in enumerate(exact_results)
    }
    bm25_ranks = {
        item["doc_title"]: (rank + 1) for rank, item in enumerate(bm25_results)
    }
    vector_ranks = {
        item["doc_title"]: (rank + 1) for rank, item in enumerate(vector_results)
    }

    # 构建原始分数字典
    exact_scores = {item["doc_title"]: item["score"] for item in exact_results}
    bm25_scores = {item["doc_title"]: item["score"] for item in bm25_results}
    vector_scores = {item["doc_title"]: item["score"] for item in vector_results}

    # 获取所有唯一的doc_title
    all_doc_titles = set(exact_ranks.keys()) | set(bm25_ranks.keys()) | set(vector_ranks.keys())

    # 计算RRF分数
    fused_results = []
    for doc_title in all_doc_titles:
        # RRF公式: 1/(k + rank)
        exact_rrf = (
            (1.0 / (k + exact_ranks[doc_title])) * exact_weight
            if doc_title in exact_ranks
            else 0
        )
        bm25_rrf = (
            (1.0 / (k + bm25_ranks[doc_title])) * bm25_weight
            if doc_title in bm25_ranks
            else 0
        )
        vector_rrf = (
            (1.0 / (k + vector_ranks[doc_title])) * vector_weight
            if doc_title in vector_ranks
            else 0
        )

        rrf_score = exact_rrf + bm25_rrf + vector_rrf

        fused_results.append(
            {
                "doc_title": doc_title,
                "rrf_score": rrf_score,
                "exact_rank": exact_ranks.get(doc_title, None),
                "bm25_rank": bm25_ranks.get(doc_title, None),
                "vector_rank": vector_ranks.get(doc_title, None),
                "exact_score": exact_scores.get(doc_title, None),
                "bm25_score": bm25_scores.get(doc_title, None),
                "vector_score": vector_scores.get(doc_title, None),
            }
        )

    # 按rrf_score降序排序
    fused_results.sort(key=lambda x: x["rrf_score"], reverse=True)

    return fused_results


def rrf_rerank_node(state: State) -> State:
    """
    RRF重排序节点（三路融合）

    功能：
    1. 使用RRF算法融合三路检索结果：
       - exact_results（doc_title 精确匹配）
       - bm25_results（chunk_text BM25）
       - vector_results（embedding余弦相似度）
    2. 去重（通过doc_title）
    3. 按RRF分数降序排序

    Args:
        state: 当前状态

    Returns:
        更新后的状态，包含fused_results
    """
    # 检查是否有错误
    if state.get("error"):
        return {}

    exact_results = state.get("exact_results", [])
    bm25_results = state.get("bm25_results", [])
    vector_results = state.get("vector_results", [])

    # 确保至少有一路检索结果
    if not exact_results and not bm25_results and not vector_results:
        return {"fused_results": []}

    # 使用RRF算法融合三路结果（缺失的路自动按 0 分计入，单路命中时等价于该路的加权 RRF）
    fused_results = rrf_fusion_three_way(
        exact_results,
        bm25_results,
        vector_results,
        k=config.rrf_k,
        exact_weight=config.exact_weight,
        bm25_weight=config.bm25_weight,
        vector_weight=config.vector_weight,
    )

    return {"fused_results": fused_results}
