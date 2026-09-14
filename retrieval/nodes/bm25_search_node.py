"""
BM25检索节点：使用SQLite FTS5对chunk_text字段进行全文检索
专门用于长文本内容的词频检索
"""
from typing import List
from .state import State, SearchResult
from indexing.database import get_db_cursor
from ..config import config


def bm25_search_node(state: State) -> dict:
    """
    BM25检索节点（SQLite FTS5，使用连接池）

    功能：
    1. 使用预处理节点生成的tokens
    2. 使用SQLite FTS5对chunk_text字段进行全文检索
    3. 利用FTS5内置的BM25算法计算相关性分数
    4. 支持按 file_ids 过滤（可选）
    5. 返回按BM25分数排序的Top-K结果

    适用场景：长文本内容的词频检索

    Args:
        state: 当前状态

    Returns:
        更新后的状态，包含bm25_results
    """
    # 检查是否有错误
    if state.get("error"):
        return {}

    tokens = state.get("tokens")
    if not tokens:
        return {
            "error": "缺少分词结果",
        }

    # 获取文件ID过滤条件（可选）
    file_ids = state.get("file_ids")
    if file_ids == []:
        return {"bm25_results": []}

    try:
        # 使用连接池
        with get_db_cursor() as cursor:
            # 构建FTS5查询语句
            # 使用OR连接多个词，实现宽松匹配
            query_str = " OR ".join(tokens)

            # 构建SQL查询
            # 使用FTS5的bm25()函数获取相关性分数
            # bm25()返回负值，值越小表示相关性越高，所以取负数变为正值
            if file_ids:
                # 有文件过滤条件
                placeholders = ",".join("?" * len(file_ids))
                query_sql = f"""
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
                params = [query_str] + file_ids + [config.bm25_top_k]
            else:
                # 无文件过滤，全局检索
                query_sql = """
                    SELECT DISTINCT
                        c.doc_title,
                        -bm25(chunks_fts) AS bm25_score
                    FROM chunks_fts
                    JOIN chunks c ON chunks_fts.rowid = c.id
                    WHERE chunks_fts MATCH ?
                    ORDER BY bm25_score DESC
                    LIMIT ?
                """
                params = [query_str, config.bm25_top_k]

            # 执行查询
            cursor.execute(query_sql, params)
            rows = cursor.fetchall()

            # 转换为SearchResult列表
            bm25_results: List[SearchResult] = [
                {"doc_title": row[0], "score": float(row[1])}
                for row in rows
            ]

            return {
                "bm25_results": bm25_results,
            }

    except Exception as e:
        return {
            "error": f"BM25检索失败: {str(e)}",
        }

