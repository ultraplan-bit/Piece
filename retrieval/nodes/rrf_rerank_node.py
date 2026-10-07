"""RRF重排序节点：按 chunk_id 融合三路结果，标题不是身份。"""
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
    """每条路线按 rank 贡献 weight/(k+rank)，同一路的同一卡片只计一次。"""
    fused = {}
    for route, results, weight in (
        ("exact", exact_results, exact_weight),
        ("bm25", bm25_results, bm25_weight),
        ("vector", vector_results, vector_weight),
    ):
        seen = set()
        for rank, item in enumerate(results, 1):
            chunk_id = item["chunk_id"]
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            result = fused.setdefault(chunk_id, {
                "chunk_id": chunk_id, "doc_title": item["doc_title"], "rrf_score": 0.0,
                "exact_rank": None, "bm25_rank": None, "vector_rank": None,
                "exact_score": None, "bm25_score": None, "vector_score": None,
            })
            result["rrf_score"] += weight / (k + rank)
            result[f"{route}_rank"] = rank
            result[f"{route}_score"] = item["score"]
    return sorted(fused.values(), key=lambda result: (-result["rrf_score"], result["chunk_id"]))


def rrf_rerank_node(state: State) -> State:
    if state.get("error"):
        return {}
    return {"fused_results": rrf_fusion_three_way(
        state.get("exact_results") or [],
        state.get("bm25_results") or [],
        state.get("vector_results") or [],
        k=config.rrf_k,
        exact_weight=config.exact_weight,
        bm25_weight=config.bm25_weight,
        vector_weight=config.vector_weight,
    )}
