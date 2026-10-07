"""标题检索：LIKE 优先，FTS5 补充；同名卡片按 chunk_id 分别保留。"""
from .state import State
from indexing.database import get_db_cursor
from indexing.fts import quote_fts_term
from ..config import config


def exact_match_node(state: State) -> dict:
    if state.get("error"):
        return {}
    tokens = state.get("tokens", [])
    if not tokens:
        return {"error": "缺少分词结果"}
    file_ids = state.get("file_ids")
    if file_ids == []:
        return {"exact_results": []}

    scope = f" AND c.file_id IN ({','.join('?' for _ in file_ids)})" if file_ids else ""
    scope_params = file_ids or []
    try:
        with get_db_cursor() as cursor:
            conditions = " AND ".join("c.doc_title LIKE ?" for _ in tokens)
            cursor.execute(
                f"SELECT c.id, c.doc_title FROM chunks c WHERE {conditions}{scope} ORDER BY c.id LIMIT ?",
                [f"%{token}%" for token in tokens] + scope_params + [config.exact_top_k],
            )
            results = [{"chunk_id": row[0], "doc_title": row[1], "score": 1.0} for row in cursor.fetchall()]
            seen_ids = {item["chunk_id"] for item in results}

            if len(results) < config.exact_top_k:
                query = " AND ".join(f"doc_title:{quote_fts_term(token)}" for token in tokens)
                cursor.execute(
                    "SELECT c.id, c.doc_title, -bm25(chunks_fts) AS score FROM chunks_fts "
                    "JOIN chunks c ON chunks_fts.rowid = c.id "
                    f"WHERE chunks_fts MATCH ?{scope} ORDER BY score DESC, c.id LIMIT ?",
                    [query] + scope_params + [config.exact_top_k],
                )
                for row in cursor.fetchall():
                    if row[0] not in seen_ids:
                        results.append({"chunk_id": row[0], "doc_title": row[1], "score": min(float(row[2]) / 10.0, 1.0)})
                        seen_ids.add(row[0])
                        if len(results) >= config.exact_top_k:
                            break
            return {"exact_results": results}
    except Exception as exc:
        return {"error": f"标题检索失败: {exc}"}
