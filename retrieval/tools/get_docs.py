"""
文档获取工具：根据doc_title列表批量获取完整文档内容及元数据
"""
import json
from typing import List, Dict, Optional, Any
from indexing.database import get_db_cursor


def _load_collections(cursor, file_ids: List[int]) -> Dict[int, List[str]]:
    """批量查询文件所属集合名，供结果标注来源领域。"""
    if not file_ids:
        return {}

    placeholders = ",".join("?" for _ in file_ids)
    cursor.execute(
        f"""
        SELECT fc.file_id, c.name
        FROM file_collections fc
        JOIN collections c ON c.id = fc.collection_id
        WHERE fc.file_id IN ({placeholders})
        ORDER BY c.name
        """,
        file_ids,
    )
    mapping: Dict[int, List[str]] = {}
    for row in cursor.fetchall():
        mapping.setdefault(row[0], []).append(row[1])
    return mapping


def get_docs(doc_titles: Optional[List[str]] = None, *, chunk_ids: Optional[List[int]] = None) -> Dict[str, Optional[Dict[str, Any]]]:
    """
    根据doc_title列表获取文档内容及元数据（使用连接池）

    Args:
        doc_titles: 兼容旧标题查询；同名时返回 ambiguous/candidates，不选择任意一张。
        chunk_ids: 精确卡片 ID，与 doc_titles 二选一；返回字典以字符串 ID 为键。

    Returns:
        doc_title到文档详情的字典映射
        {
            "项目手册_快速开始": {
                "chunk_id": 123,
                "file_id": 45,
                "filename": "项目手册.md",
                "file_path": "…/working/项目手册.md",
                "doc_title": "项目手册_快速开始",
                "heading_path": "项目手册 / 快速开始",
                "chunk_text": "文档内容...",
                "total_chunks_in_file": 5,
                "chunk_index_in_file": 2,
                "collections": ["技术文档"],
                "properties": {"author": "some-author"}
            },
            "接口文档_鉴权": {...},
            ...
        }

        未找到的doc_title会被设置为None

        heading_path 给出切片在文档中的层级位置，collections/properties 为
        可选的出处信息（集合归类、frontmatter 属性），为空时不返回以节省 token

        file_path 供服务端解析正文中的相对图片引用，original_file_path 供
        PDF 原页渲染回退，两者都是本机路径，返回给 MCP 客户端前由调用方剔除
    """
    if chunk_ids is not None and doc_titles:
        raise ValueError("doc_titles 与 chunk_ids 只能提供一项")
    selectors = chunk_ids if chunk_ids is not None else doc_titles
    if not selectors:
        return {}
    if chunk_ids is not None and any(type(chunk_id) is not int or chunk_id < 1 for chunk_id in chunk_ids):
        raise ValueError("chunk_ids 必须为正整数列表")

    with get_db_cursor() as cursor:
        column = "c.id" if chunk_ids is not None else "c.doc_title"
        placeholders = ",".join("?" for _ in selectors)

        # 批量查询：统计切片在所属文件中的真实位置
        # （窗口函数只能统计结果集内的行，命中多个切片时会给出错误的总数和序号）
        query = f"""
            SELECT
                c.id AS chunk_id,
                c.file_id,
                c.doc_title,
                c.chunk_text,
                f.filename,
                f.file_path,
                (
                    SELECT COUNT(*) FROM chunks t WHERE t.file_id = c.file_id
                ) AS total_chunks_in_file,
                (
                    SELECT COUNT(*) FROM chunks t
                    WHERE t.file_id = c.file_id
                      AND (t.chunk_index, t.id) <= (c.chunk_index, c.id)
                ) AS chunk_index_in_file,
                c.heading_path,
                f.metadata,
                f.original_file_path
            FROM chunks c
            JOIN files f ON c.file_id = f.id
            WHERE {column} IN ({placeholders})
            ORDER BY c.id
        """

        cursor.execute(query, selectors)
        rows = cursor.fetchall()

        collections_by_file = _load_collections(
            cursor, list({row[1] for row in rows})
        )

        # 同名结果先分组，禁止字典赋值静默覆盖另一张卡片。
        matches = {}
        for row in rows:
            doc: Dict[str, Any] = {
                "chunk_id": row[0],
                "file_id": row[1],
                "filename": row[4],
                "file_path": row[5],
                "doc_title": row[2],
                "chunk_text": row[3],
                "total_chunks_in_file": row[6],
                "chunk_index_in_file": row[7],
                "original_file_path": row[10],
            }

            if row[8]:
                doc["heading_path"] = row[8]

            collections = collections_by_file.get(row[1])
            if collections:
                doc["collections"] = collections

            if row[9]:
                try:
                    properties = json.loads(row[9])
                except (TypeError, ValueError):
                    properties = None
                if isinstance(properties, dict) and properties:
                    doc["properties"] = properties

            key = str(row[0]) if chunk_ids is not None else row[2]
            matches.setdefault(key, []).append(doc)

        result = {}
        for selector in selectors:
            key = str(selector)
            docs = matches.get(key, [])
            if len(docs) <= 1:
                result[key] = docs[0] if docs else None
            else:
                result[key] = {
                    "doc_title": key, "ambiguous": True,
                    "candidates": [{field: doc.get(field) for field in
                                    ("chunk_id", "file_id", "filename", "doc_title", "heading_path")}
                                   for doc in docs],
                }
        return result
