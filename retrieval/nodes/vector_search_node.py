"""
向量检索节点：使用sqlite-vec对embedding字段进行余弦相似度匹配
"""
from typing import List
from .state import State, SearchResult
from indexing.database import get_db_cursor
from ..config import config

# 使用统一的 embedding 客户端（单例模式，复用连接）
from indexing.services import get_embeddings_model
from indexing.utils import serialize_float32


def vector_search_node(state: State) -> dict:
    """
    向量检索节点（sqlite-vec，使用连接池）

    功能：
    1. 将查询文本向量化
    2. 使用sqlite-vec进行余弦相似度检索
    3. 支持按 file_ids 过滤（可选）
    4. 返回Top-K结果

    Args:
        state: 当前状态

    Returns:
        更新后的状态
    """
    # 检查是否有错误
    if state.get("error"):
        return {}

    cleaned_query = state.get("cleaned_query")
    if not cleaned_query:
        return {
            "error": "缺少清洗后的查询文本",
        }

    # 获取文件ID过滤条件（可选）
    file_ids = state.get("file_ids")
    if file_ids == []:
        return {"vector_results": []}

    try:
        # 获取 embedding 实例（单例，复用连接）
        embeddings = get_embeddings_model()

        # 向量化查询
        query_embedding = embeddings.embed_query(cleaned_query)
        query_blob = serialize_float32(query_embedding)

        # 使用连接池
        with get_db_cursor() as cursor:
            # 构建SQL查询
            # vec_chunks 的 chunk_id 对应 chunks 表的 id
            # vec_distance_cosine 返回余弦距离（0-2），转换为相似度（1 - distance/2）
            if file_ids:
                # 有文件过滤条件
                placeholders = ",".join("?" * len(file_ids))
                query_sql = f"""
                    SELECT
                        c.doc_title,
                        1 - (vec_distance_cosine(v.embedding, ?) / 2) AS similarity
                    FROM vec_chunks v
                    JOIN chunks c ON v.chunk_id = c.id
                    WHERE c.file_id IN ({placeholders})
                    ORDER BY vec_distance_cosine(v.embedding, ?)
                    LIMIT ?
                """
                params = [query_blob] + file_ids + [query_blob, config.vector_top_k]
            else:
                # 无文件过滤，全局检索
                query_sql = """
                    SELECT
                        c.doc_title,
                        1 - (vec_distance_cosine(v.embedding, ?) / 2) AS similarity
                    FROM vec_chunks v
                    JOIN chunks c ON v.chunk_id = c.id
                    ORDER BY vec_distance_cosine(v.embedding, ?)
                    LIMIT ?
                """
                params = [query_blob, query_blob, config.vector_top_k]

            cursor.execute(query_sql, params)
            rows = cursor.fetchall()

            # 转换为SearchResult列表
            vector_results: List[SearchResult] = [
                {"doc_title": row[0], "score": float(row[1])}
                for row in rows
            ]

            return {
                "query_embedding": query_embedding,
                "vector_results": vector_results,
            }

    except Exception as e:
        return {
            "error": f"向量检索失败: {str(e)}",
        }

