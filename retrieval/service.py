"""共享三路检索业务；MCP、GUI 和本地 API 仅适配输入输出。"""

from time import perf_counter
from langgraph.graph import StateGraph, END

from indexing.database import get_db_cursor
from indexing.services.errors import BusinessError
from indexing.services.page_render import page_number_from_heading
from indexing.utils import run_sync
from .nodes import (State, preprocess_node, exact_match_node, vector_search_node,
                    bm25_search_node, rrf_rerank_node, output_node)


def build_graph():
    workflow = StateGraph(State)
    for name, node in (("preprocess", preprocess_node), ("exact_match", exact_match_node),
                       ("bm25_search", bm25_search_node), ("vector_search", vector_search_node),
                       ("rrf_rerank", rrf_rerank_node), ("output", output_node)):
        workflow.add_node(name, node)
    workflow.set_entry_point("preprocess")
    for name in ("exact_match", "bm25_search", "vector_search"):
        workflow.add_edge("preprocess", name)
        workflow.add_edge(name, "rrf_rerank")
    workflow.add_edge("rrf_rerank", "output")
    workflow.add_edge("output", END)
    return workflow.compile()


_GRAPH = build_graph()


def _has_chunks(file_ids):
    with get_db_cursor() as cursor:
        if file_ids is None:
            cursor.execute("SELECT 1 FROM chunks LIMIT 1")
        elif not file_ids:
            return False
        else:
            cursor.execute(f"SELECT 1 FROM chunks WHERE file_id IN ({','.join('?' for _ in file_ids)}) LIMIT 1", file_ids)
        return cursor.fetchone() is not None


async def _resolve(query, filenames=None, collections=None, file_ids=None, limit=20):
    if not isinstance(query, str) or not query.strip() or len(query) > 10000:
        raise BusinessError("INVALID_INPUT", "查询必须为 1–10000 字符的非空文本")
    state = {"query": query, "filenames": filenames, "collections": collections,
             "file_ids": file_ids, "limit": limit, "error": None}
    scope = await run_sync(preprocess_node, state)
    if scope.get("error"):
        raise BusinessError("INVALID_INPUT", scope["error"])
    state.update(scope)
    if not await run_sync(_has_chunks, state["file_ids"]):
        return {**state, "final_keywords": [], "confidence_scores": {}, "fused_results": [],
                "stats": {"total_fused_results": 0, "final_top_k": 0},
                "debug_stats": {"tokens": state["tokens"], "file_ids_filter": state["file_ids"]}}
    # scope 已确定；图中的预处理会保留显式 ID 范围。
    result = await _GRAPH.ainvoke(state)
    if result.get("error"):
        raise BusinessError("SEARCH_FAILED", result["error"])
    return result


async def resolve_database_keywords(query, filenames=None, collections=None):
    result = await _resolve(query, filenames, collections)
    return {"keywords": result.get("final_keywords", []), "confidence_scores": result.get("confidence_scores", {}),
            "stats": result.get("stats", {}), "debug_stats": result.get("debug_stats", {})}


def _candidates(result, limit):
    candidates = []
    scope = result.get("file_ids")
    with get_db_cursor() as cursor:
        for title in result.get("final_keywords", []):
            params = [title]
            where = "c.doc_title = ?"
            if scope is not None:
                if not scope:
                    break
                where += f" AND c.file_id IN ({','.join('?' for _ in scope)})"
                params.extend(scope)
            cursor.execute(
                f"SELECT c.id AS chunk_id,c.file_id,c.doc_title,c.heading_path,f.filename FROM chunks c "
                f"JOIN files f ON f.id=c.file_id WHERE {where} ORDER BY c.id LIMIT ?",
                (*params, limit - len(candidates)),
            )
            for row in cursor.fetchall():
                item = dict(row)
                item["score"] = result["confidence_scores"].get(title, 0)
                item["page_number"] = page_number_from_heading(item["heading_path"])
                candidates.append(item)
            if len(candidates) >= limit:
                break
    return candidates


async def search(query, file_ids=None, collections=None, limit=20, diagnostics=False):
    started = perf_counter()
    result = await _resolve(query, collections=collections, file_ids=file_ids, limit=limit)
    data = {"candidates": await run_sync(_candidates, result, limit), "stats": result["stats"],
            "file_ids_filter": result.get("file_ids")}
    if diagnostics:
        data["diagnostics"] = {**result.get("debug_stats", {}), "elapsed_ms": round((perf_counter() - started) * 1000, 2)}
    return data
