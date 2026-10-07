"""三路召回身份、FTS 字面量及新库中文索引；全程临时库/假嵌入。"""

import asyncio
import sqlite3
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from indexing import database
from indexing.fts import quote_fts_term
from indexing.services import file_service, chunk_service, task_service
from indexing.utils import serialize_float32
from retrieval.nodes.bm25_search_node import bm25_search_node
from retrieval.nodes.exact_match_node import exact_match_node
from retrieval.nodes.preprocess_node import tokenize_query
from retrieval.nodes.rrf_rerank_node import rrf_fusion_three_way
from retrieval.service import search, resolve_database_keywords
from retrieval.tools.get_docs import get_docs


def _chunk(file_id, title, text, embedding=None):
    with database.get_db_cursor(write=True) as cursor:
        cursor.execute("INSERT INTO chunks(file_id,doc_title,chunk_text) VALUES (?,?,?)", (file_id, title, text))
        chunk_id = cursor.lastrowid
        if embedding is not None:
            cursor.execute("INSERT INTO vec_chunks(chunk_id,embedding) VALUES (?,?)", (chunk_id, serialize_float32(embedding)))
        return chunk_id


def _matches(query):
    result = bm25_search_node({"tokens": tokenize_query(query)})
    assert "error" not in result, result
    return {item["chunk_id"] for item in result["bm25_results"]}


def _same_titles(monkeypatch):
    from retrieval.config import config

    monkeypatch.setattr(config, "vector_top_k", 1)
    first_file = file_service.create_empty_file("来源一")['file_id']
    second_file = file_service.create_empty_file("来源二")['file_id']
    wrong = _chunk(first_file, "共同标题", "unrelated body", [0.0, 1.0])
    first = _chunk(first_file, "共同标题", "needle first source", [1.0, 0.0])
    second = _chunk(second_file, "共同标题", "needle second source", [0.0, 1.0])
    return first_file, wrong, first, second


def test_same_title_identity_across_routes_files_and_scopes(knowledge_base, monkeypatch):
    file_id, wrong, first, second = _same_titles(monkeypatch)
    result = asyncio.run(search("needle", limit=2, diagnostics=True))
    assert [item["chunk_id"] for item in result["candidates"]] == [first, second]
    assert result["candidates"][0]["score"] > result["candidates"][1]["score"]
    routes = {item["chunk_id"]: item for item in result["diagnostics"]["fused_top_k"]}
    assert routes[first]["vector_rank"] == 1 and routes[second]["vector_rank"] is None
    assert [item["chunk_id"] for item in asyncio.run(search("needle", file_ids=[file_id]))["candidates"]] == [first]
    assert {item["chunk_id"] for item in exact_match_node({"tokens": tokenize_query("共同标题")})["exact_results"]} == {wrong, first, second}
    legacy = asyncio.run(resolve_database_keywords("needle"))
    assert legacy["keywords"] == ["共同标题"]
    assert legacy["confidence_scores"]["共同标题"] == legacy["candidates"][0]["score"]
    assert [item["chunk_id"] for item in legacy["candidates"]] == [first, second]

    hit = {"chunk_id": first, "doc_title": "共同标题", "score": 1.0}
    other = {"chunk_id": second, "doc_title": "共同标题", "score": 0.5}
    fused = rrf_fusion_three_way([hit, hit], [other], [hit])
    assert len(fused) == 2
    assert fused[0]["chunk_id"] == first
    assert fused[0]["rrf_score"] == pytest.approx(0.7 / 61)
    assert fused[1]["exact_rank"] is None


@pytest.mark.parametrize("word", ["AND", "OR", "NOT"])
def test_fts_reserved_words_are_literals_in_both_routes(knowledge_base, word):
    file_id = file_service.create_empty_file("语法")['file_id']
    chunk_id = _chunk(file_id, f"operator {word}", f"literal {word}")
    assert _matches(word) == {chunk_id}
    exact = exact_match_node({"tokens": [word]})
    assert "error" not in exact
    assert [item["chunk_id"] for item in exact["exact_results"]] == [chunk_id]
    assert [item["chunk_id"] for item in asyncio.run(search(word))["candidates"]] == [chunk_id]
    # 即使调用者直接传入带引号/括号的词项，MATCH 也只能当作字面量解析。
    with database.get_db_cursor() as cursor:
        cursor.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", (quote_fts_term('operator"' + word),))
        assert [row[0] for row in cursor.fetchall()] == [chunk_id]
    assert [item["chunk_id"] for item in exact_match_node({"tokens": ['operator"' + word]})["exact_results"]] == [chunk_id]


def test_chinese_fts_write_update_delete_rebuild_and_connection_registration(knowledge_base):
    # 每条池连接都必须注册，不能恰好借到初始化时的那条连接才成功。
    pool = database._connection_pool
    connections = [pool.get_connection() for _ in range(pool.pool_size)]
    try:
        for connection in connections:
            assert "量子" in connection.execute("SELECT piece_tokenize('量子力学研究')").fetchone()[0].split()
    finally:
        for connection in connections:
            pool.return_connection(connection)

    file_id = file_service.create_empty_file("中文")['file_id']
    task_id = chunk_service.create_chunk_add_task(file_id, "量子力学研究", "人工智能推动机器学习技术发展")
    knowledge_base.drain()
    chunk_id = task_service.get_task(task_id)["result"]["chunk_id"]
    assert _matches("机器学习") == _matches("量子") == {chunk_id}
    chunk = chunk_service.get_chunk_by_id(chunk_id)
    assert chunk["chunk_text"] == "人工智能推动机器学习技术发展"
    assert chunk["doc_title"] == "量子力学研究"

    task_id = chunk_service.create_chunk_update_task(chunk_id, "深度学习推动计算机视觉进展", "天体物理研究")
    knowledge_base.drain()
    assert task_service.get_task(task_id)["status"] == "completed"
    assert _matches("量子") == _matches("机器") == set()
    assert _matches("计算机视觉") == {chunk_id}

    # 原生 rebuild 从 FTS 自己保存的分词内容恢复，独立 SQLite 读/重建无需 UDF。
    with sqlite3.connect(knowledge_base.settings.get_db_path()) as connection:
        connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
        connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('integrity-check')")
    assert _matches("计算机视觉") == {chunk_id}
    # 第三方直接写入可显式复用注册入口；所有写路径都仍由同一触发器维护。
    with sqlite3.connect(knowledge_base.settings.get_db_path()) as connection:
        database.register_sqlite_functions(connection)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("UPDATE chunks SET chunk_text='自然语言处理用于智能问答' WHERE id=?", (chunk_id,))
    assert _matches("计算机视觉") == set()
    assert _matches("自然语言") == {chunk_id}
    with database.get_db_cursor(write=True) as cursor:
        cursor.execute("DELETE FROM files WHERE id=?", (file_id,))
    assert _matches("自然语言") == set()
    with database.get_db_cursor() as cursor:
        assert cursor.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 0
        cursor.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('integrity-check')")


def test_current_schema_reopen_preserves_chinese_fts_without_rebuilding(knowledge_base, monkeypatch):
    path = knowledge_base.settings.get_db_path()
    file_id = file_service.create_empty_file("新库")['file_id']
    chunk_id = _chunk(file_id, "量子力学研究", "人工智能推动机器学习技术发展", [1.0, 0.0])
    before = chunk_service.get_chunk_by_id(chunk_id)
    database.close_connection_pool()

    def fail(_):
        raise AssertionError("重新打开当前库不应重建全文索引")

    with monkeypatch.context() as patch:
        patch.setattr(database, "tokenize_fts_text", fail)
        database.init_database(path)
    database.init_connection_pool(path)
    assert chunk_service.get_chunk_by_id(chunk_id) == before
    assert _matches("机器学习") == _matches("量子") == {chunk_id}


def test_mcp_keywords_and_get_docs_keep_legacy_fields_without_title_overwrite(knowledge_base, monkeypatch):
    monkeypatch.setattr(knowledge_base.settings.mcp, "auth_enabled", False)
    from fastmcp import Client
    from retrieval import server

    _, wrong, first, second = _same_titles(monkeypatch)
    mapping = get_docs(["共同标题", "不存在"])
    assert mapping["不存在"] is None
    assert mapping["共同标题"]["ambiguous"] is True
    assert {item["chunk_id"] for item in mapping["共同标题"]["candidates"]} == {wrong, first, second}
    assert "chunk_text" not in mapping["共同标题"]

    async def scenario():
        async with Client(server.mcp) as client:
            resolved = (await client.call_tool("resolve-keywords", {"query": "needle", "max_results": 1})).data
            assert {"keywords", "confidence_scores", "stats", "candidates"} <= resolved.keys()
            assert resolved["keywords"] == ["共同标题"]
            assert [item["chunk_id"] for item in resolved["candidates"]] == [first]
            assert resolved["stats"]["final_top_k"] == 1
            ambiguous = (await client.call_tool("get-docs", {"doc_titles": ["共同标题"], "include_images": False})).data
            assert ambiguous["documents"] == {} and ambiguous["not_found"] == []
            assert {item["chunk_id"] for item in ambiguous["ambiguous"]["共同标题"]} == {wrong, first, second}
            docs = (await client.call_tool("get-docs", {"chunk_ids": [second, first, 98765], "include_images": False, "include_metadata": True})).data
            assert list(docs["documents"]) == [str(second), str(first)]
            assert docs["documents"][str(first)]["chunk_text"] == "needle first source"
            assert docs["documents"][str(second)]["chunk_text"] == "needle second source"
            assert docs["not_found"] == ["98765"]
            assert docs["metadata"]["total_requested"] == 3
            assert all("file_path" not in doc and "original_file_path" not in doc for doc in docs["documents"].values())
            conflict = await client.call_tool("get-docs", {"doc_titles": ["共同标题"], "chunk_ids": [first]}, raise_on_error=False)
            assert conflict.is_error
    asyncio.run(scenario())

    with database.get_db_cursor(write=True) as cursor:
        cursor.execute("UPDATE chunks SET doc_title='唯一标题' WHERE id=?", (first,))
    ctx = SimpleNamespace(info=AsyncMock(), warning=AsyncMock(), error=AsyncMock())
    legacy = asyncio.run(server.get_docs_tool.fn(ctx, '["唯一标题"]', include_images=False))
    assert legacy["documents"]["唯一标题"]["chunk_id"] == first
    assert legacy["not_found"] == []


def test_recall_ui_keeps_same_title_bodies_and_routes(knowledge_base, monkeypatch):
    from app.ui.views import recall_test_view

    _, _, first, second = _same_titles(monkeypatch)
    state = {"query": "needle"}
    asyncio.run(recall_test_view._run_search(state, {}))
    assert state["error"] is None
    assert [item["chunk_id"] for item in state["results"]] == [first, second]
    assert [item["doc"]["chunk_text"] for item in state["results"]] == ["needle first source", "needle second source"]
    assert "vector_rank" in state["results"][0]["routes"]
    assert "vector_rank" not in state["results"][1]["routes"]


def test_file_reindex_replaces_chinese_fts_rows(knowledge_base):
    from pathlib import Path

    source = knowledge_base.path / "原始中文.md"
    source.write_text("# 初始标题\n\n人工智能推动机器学习技术发展", encoding="utf-8")
    file_id = file_service.import_file(source)["file_id"]
    knowledge_base.drain()
    before_ids = _matches("机器学习")
    assert before_ids
    working = Path(file_service.get_file_by_id(file_id)["file_path"])
    working.write_text("# 新标题\n\n自然语言处理用于智能问答", encoding="utf-8")
    task_id = file_service.reindex_file(file_id, source="working")["task_id"]
    knowledge_base.drain()
    assert task_service.get_task(task_id)["status"] == "completed"
    assert _matches("机器学习") == set()
    after_ids = _matches("自然语言")
    assert after_ids and not before_ids.intersection(after_ids)
    with database.get_db_cursor() as cursor:
        assert {row[0] for row in cursor.execute("SELECT rowid FROM chunks_fts")} == after_ids


def test_chunk_id_image_mapping_keeps_title_and_identity(tmp_path):
    from PIL import Image
    from retrieval.tools.chunk_images import collect_chunk_images

    Image.new("RGB", (128, 128)).save(tmp_path / "figure.png")
    documents = {str(chunk_id): {"chunk_id": chunk_id, "doc_title": "同名标题",
                                "file_path": str(tmp_path / "source.md"), "chunk_text": "![图](figure.png)"}
                 for chunk_id in (21, 22)}
    first, remaining = collect_chunk_images(documents, max_images=1)
    second, remaining_after = collect_chunk_images(documents, max_images=1, image_offset=1)
    assert remaining == 1 and remaining_after == 0
    assert [image["chunk_id"] for image in first + second] == [21, 22]
    assert all(image["doc_title"] == "同名标题" for image in first + second)
