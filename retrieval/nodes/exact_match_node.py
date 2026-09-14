"""
标题检索节点：对 doc_title 字段进行多条件 LIKE 匹配 + BM25 搜索
结合两种策略，LIKE 匹配结果优先
"""
from typing import List, Set
from .state import State, SearchResult
from indexing.database import get_db_cursor
from ..config import config


def exact_match_node(state: State) -> dict:
    """
    标题检索节点（LIKE + BM25 双策略）

    功能：
    1. 使用分词结果对 doc_title 字段进行多条件 LIKE 匹配（AND 连接）
    2. 使用分词结果对 doc_title 字段进行 BM25 搜索（AND 连接）
    3. 合并去重，LIKE 结果优先
    4. 支持按 file_ids 过滤（可选）
    5. 返回 Top-K 结果

    Args:
        state: 当前状态

    Returns:
        更新后的状态，包含 exact_results
    """
    # 检查是否有错误
    if state.get("error"):
        return {}

    tokens = state.get("tokens", [])
    if not tokens:
        return {
            "error": "缺少分词结果",
        }

    file_ids = state.get("file_ids")
    if file_ids == []:
        return {"exact_results": []}

    try:
        with get_db_cursor() as cursor:
            exact_results: List[SearchResult] = []
            seen_titles: Set[str] = set()

            # === 策略1: 多条件 LIKE 匹配（AND 连接）===
            # 构建多个 LIKE 条件，用 AND 连接
            like_conditions = " AND ".join([f"doc_title LIKE ?" for _ in tokens])
            like_values = [f"%{token}%" for token in tokens]

            if file_ids:
                placeholders = ",".join("?" * len(file_ids))
                like_sql = f"""
                    SELECT DISTINCT
                        doc_title,
                        1.0 AS match_score
                    FROM chunks
                    WHERE {like_conditions}
                      AND file_id IN ({placeholders})
                    LIMIT ?
                """
                like_params = like_values + file_ids + [config.exact_top_k]
            else:
                like_sql = f"""
                    SELECT DISTINCT
                        doc_title,
                        1.0 AS match_score
                    FROM chunks
                    WHERE {like_conditions}
                    LIMIT ?
                """
                like_params = like_values + [config.exact_top_k]

            cursor.execute(like_sql, like_params)
            like_rows = cursor.fetchall()

            # LIKE 结果优先加入
            for row in like_rows:
                doc_title = row[0]
                if doc_title not in seen_titles:
                    exact_results.append({"doc_title": doc_title, "score": float(row[1])})
                    seen_titles.add(doc_title)

            # === 策略2: BM25 标题搜索（AND 连接）===
            # 只有当还需要更多结果时才执行
            if len(exact_results) < config.exact_top_k:
                # 构建 FTS5 查询，限定在 doc_title 字段，使用 AND 连接
                query_terms = " AND ".join([f"doc_title:{token}" for token in tokens])

                remaining = config.exact_top_k - len(exact_results)

                if file_ids:
                    placeholders = ",".join("?" * len(file_ids))
                    bm25_sql = f"""
                        SELECT DISTINCT
                            c.doc_title,
                            -bm25(chunks_fts) AS bm25_score
                        FROM chunks_fts
                        JOIN chunks c ON chunks_fts.rowid = c.id
                        WHERE chunks_fts MATCH ?
                          AND c.file_id IN ({placeholders})
                        ORDER BY bm25_score DESC
                        LIMIT ?
                    """
                    bm25_params = [query_terms] + file_ids + [remaining + len(seen_titles)]
                else:
                    bm25_sql = """
                        SELECT DISTINCT
                            c.doc_title,
                            -bm25(chunks_fts) AS bm25_score
                        FROM chunks_fts
                        JOIN chunks c ON chunks_fts.rowid = c.id
                        WHERE chunks_fts MATCH ?
                        ORDER BY bm25_score DESC
                        LIMIT ?
                    """
                    bm25_params = [query_terms, remaining + len(seen_titles)]

                try:
                    cursor.execute(bm25_sql, bm25_params)
                    bm25_rows = cursor.fetchall()

                    # BM25 结果去重后加入
                    for row in bm25_rows:
                        doc_title = row[0]
                        if doc_title not in seen_titles:
                            # BM25 分数归一化到 0-1 范围（假设最大分数为 10）
                            normalized_score = min(float(row[1]) / 10.0, 1.0)
                            exact_results.append({"doc_title": doc_title, "score": normalized_score})
                            seen_titles.add(doc_title)
                            if len(exact_results) >= config.exact_top_k:
                                break
                except Exception:
                    # BM25 搜索失败时静默忽略，保留 LIKE 结果
                    pass

            return {
                "exact_results": exact_results,
            }

    except Exception as e:
        return {
            "error": f"标题检索失败: {str(e)}",
        }
