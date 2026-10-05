"""阶段 A：新 schema、层级不变量、读取范围以及跨入口合同。"""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from indexing import database
from indexing.services import collection_service as collections, file_service as files, maintenance_service
from indexing.services.errors import BusinessError


def create(name, parent_id=None):
    result = collections.create_collection(name, parent_id=parent_id)
    assert result["success"], result
    return result["collection_id"]


def test_current_schema_new_database_and_no_repair(knowledge_base):
    path = knowledge_base.settings.get_db_path()
    with database.get_db_cursor() as cursor:
        assert cursor.execute("PRAGMA user_version").fetchone()[0] == database.SCHEMA_VERSION
        assert any(row[3] == "parent_id" and row[6] == "RESTRICT"
                   for row in cursor.execute("PRAGMA foreign_key_list(collections)"))
        assert any(row[1] == "idx_collections_parent" for row in cursor.execute("PRAGMA index_list(collections)"))
    database.init_database(path)
    database.close_connection_pool()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX idx_collections_parent")
    with pytest.raises(RuntimeError, match="拒绝自动修复"):
        database.init_database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_collections_parent'").fetchone() is None


@pytest.mark.parametrize("version", [0, 1, 2, database.SCHEMA_VERSION + 1])
def test_existing_unsupported_schema_is_not_migrated(tmp_path, version):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE collections (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO collections VALUES (1, '保留')")
        conn.execute(f"PRAGMA user_version={version}")
    with pytest.raises(RuntimeError, match="不会删除或迁移"):
        database.init_database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM collections").fetchall() == [(1, "保留")]
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert [row[1] for row in conn.execute("PRAGMA table_info(collections)")] == ["id", "name"]


def test_create_rename_move_paths_and_invalid_parents(knowledge_base):
    root = create(" 人工智能 ")
    child = create("检索增强", root)
    leaf = create("知识图谱", child)
    slash = create("真实/名称")
    assert not collections.create_collection("人工智能", parent_id=child)["success"]
    assert not collections.create_collection(" ")["success"]
    with pytest.raises(BusinessError, match="父集合不存在"):
        create("无效子集合", 999999)
    with pytest.raises(BusinessError) as exc:
        collections.move_collection(root, leaf)
    assert exc.value.code == "COLLECTION_CYCLE"
    with pytest.raises(BusinessError, match="自身或后代"):
        collections.move_collection(child, child)
    with pytest.raises(BusinessError, match="父集合不存在"):
        collections.move_collection(child, 999999)
    assert collections.rename_collection(root, "AI")["success"]
    data = {item["id"]: item for item in collections.list_collections()}
    assert data[leaf]["path"] == [{"id": root, "name": "AI"}, {"id": child, "name": "检索增强"}, {"id": leaf, "name": "知识图谱"}]
    assert data[slash]["parent_id"] is None and len(data[slash]["path"]) == 1
    assert data[root]["child_count"] == 1
    collections.move_collection(child, None)
    data = {item["id"]: item for item in collections.list_collections()}
    assert data[child]["parent_id"] is None and data[root]["child_count"] == 0
    assert [item["id"] for item in data[leaf]["path"]] == [child, leaf]
    tree = collections.collection_tree()
    assert tree["total"] == 4
    assert next(item for item in tree["collections"] if item["id"] == child)["children"][0]["id"] == leaf


def test_concurrent_moves_cannot_form_cycle(knowledge_base):
    first, second = create("一"), create("二")
    def move(pair):
        try:
            collections.move_collection(*pair)
            return True
        except BusinessError as exc:
            assert exc.code == "COLLECTION_CYCLE"
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(move, [(first, second), (second, first)])) == [False, True]
    assert len(collections.collection_tree()["collections"]) == 1


def test_corrupt_cycles_are_detected_and_scope_traversal_terminates(knowledge_base):
    first, second = create("一"), create("二")
    with database.get_db_cursor(write=True) as cursor:
        cursor.execute("UPDATE collections SET parent_id=? WHERE id=?", (second, first))
        cursor.execute("UPDATE collections SET parent_id=? WHERE id=?", (first, second))
    assert collections.get_file_ids([first]) == []
    with pytest.raises(ValueError, match="循环"):
        collections.collection_tree()


def test_direct_assignment_subtree_union_counts_and_pagination(knowledge_base):
    root = create("人工智能")
    first, second = create("检索增强", root), create("知识图谱", root)
    shared = files.create_empty_file("共享", [first, second])["file_id"]
    direct = files.create_empty_file("直接", [root])["file_id"]
    other = files.create_empty_file("未归类")["file_id"]
    empty = create("空集合")
    assert {item["id"] for item in collections.get_file_collections(shared)} == {first, second}
    assert collections.get_file_ids([root, first, second]) == [shared, direct]
    assert collections.get_file_ids([root], False) == [direct]
    assert collections.get_file_ids(None) is None
    assert collections.get_file_ids([]) == []
    assert collections.get_file_ids([999999]) == []
    data = {item["id"]: item for item in collections.list_collections()}
    assert (data[root]["direct_file_count"], data[root]["subtree_file_count"], data[root]["file_count"]) == (1, 2, 2)
    assert data[first]["direct_file_count"] == data[first]["subtree_file_count"] == 1
    assert data[empty]["subtree_file_count"] == 0
    page = files.get_files_list_paginated(limit=1, collection_ids=[root], sort_by="id", descending=False)
    assert page["total"] == 2 and page["files"][0]["id"] == shared
    assert files.get_files_list_paginated(limit=1, offset=1, collection_ids=[root], sort_by="id", descending=False)["files"][0]["id"] == direct
    assert files.get_files_list_paginated(collection_ids=[root], include_descendants=False)["total"] == 1
    assert files.get_files_list_paginated(collection_ids=[])["total"] == 0
    assert files.get_files_list_paginated(collection_ids=[empty])["total"] == 0
    assert files.get_files_list_paginated(collection_ids=[999999])["total"] == 0
    assert files.get_files_list_paginated()["total"] == 3
    assert [item["id"] for item in files.get_files_list_paginated(uncategorized=True)["files"]] == [other]
    assert files.get_files_list_paginated(collection_ids=[root], uncategorized=True)["total"] == 0
    assert collections.resolve_names_to_file_ids(None) is None
    assert collections.resolve_names_to_file_ids([]) == []
    assert collections.resolve_names_to_file_ids(["不存在"]) == []
    assert collections.resolve_names_to_file_ids(["人工"]) == [shared, direct]
    assert collections.resolve_names_to_file_ids(["人工"], False) == [direct]
    assert collections.get_collections_by_file([shared]) == {shared: ["检索增强", "知识图谱"]}
    assert collections.get_collections_by_file([]) == {}
    collections.set_file_collections(shared, [second, second])
    assert [item["id"] for item in collections.get_file_collections(shared)] == [second]
    with pytest.raises(BusinessError):
        collections.set_file_collections(shared, [first, 999999])
    assert [item["id"] for item in collections.get_file_collections(shared)] == [second]
    collections.move_collection(second, None)
    assert collections.get_file_ids([root]) == [direct]
    assert files.get_file_by_id(shared)


def test_delete_preview_final_recheck_and_originals_retained(knowledge_base):
    root, other = create("父"), create("另一个")
    leaf = create("叶", root)
    source = knowledge_base.path / "原件.md"
    source.write_text("# 正文\n\n保留文件", encoding="utf-8")
    file_id = files.import_file(source, collection_ids=[leaf])["file_id"]
    shared = files.create_empty_file("跨集合", [leaf, other])["file_id"]
    file = files.get_file_by_id(file_id)
    with pytest.raises(BusinessError) as exc:
        maintenance_service.delete_collection(root, dry_run=True)
    assert exc.value.code == "COLLECTION_NOT_EMPTY"
    with pytest.raises(BusinessError) as exc:
        maintenance_service.delete_collection(leaf)
    assert exc.value.code == "CONFIRMATION_REQUIRED"
    preview = maintenance_service.delete_collection(leaf, dry_run=True)
    assert preview["direct_file_count"] == 2 and preview["unclassified_file_count"] == 1
    child = create("预览之后新增的子集合", leaf)
    with pytest.raises(BusinessError) as exc:
        maintenance_service.delete_collection(leaf, confirmed=True)
    assert exc.value.code == "COLLECTION_NOT_EMPTY"
    collections.move_collection(child, None)
    result = maintenance_service.delete_collection(leaf, confirmed=True)
    assert not result["deletes_files"] and result["unclassified_file_count"] == 1
    assert files.get_file_by_id(file_id) and files.get_file_by_id(shared)
    assert Path(file["original_file_path"]).is_file() and Path(file["file_path"]).is_file() and source.is_file()
    assert collections.get_file_collections(file_id) == []
    assert [item["id"] for item in collections.get_file_collections(shared)] == [other]


def test_api_hierarchy_scopes_and_validation(api):
    def post(path, **payload):
        return api.post("/api/v1/" + path, json=payload).json()
    root = post("collection/create", name="父")["data"]["collection_id"]
    child = post("collection/create", name="子", parent_id=root)["data"]["collection_id"]
    file_id = post("file/create", filename="文档", collections=["子"])["data"]["file_id"]
    assert post("collection/list", limit=1)["data"]["total"] == 2
    tree = post("collection/tree")["data"]
    assert tree["collections"][0]["children"][0]["id"] == child
    assert post("file/list", collections=["父"])["data"]["files"][0]["id"] == file_id
    assert post("file/list", collections=["父"], include_descendants=False)["data"]["total"] == 0
    assert post("file/list", collections=[])["data"]["total"] == 0
    assert post("file/list", collections=["不存在"])["error"]["code"] == "NOT_FOUND"
    assert post("file/list", uncategorized=True)["data"]["total"] == 0
    assert post("collection/move", collection_id=child)["error"]["code"] == "INVALID_INPUT"
    assert post("collection/move", collection_id=root, parent_id=child)["error"]["code"] == "COLLECTION_CYCLE"
    assert post("collection/delete", collection_id=root, confirmed=True)["error"]["code"] == "COLLECTION_NOT_EMPTY"
    assert post("collection/move", collection_id=child, parent_id=None)["success"]
    assert post("file/list", collections=["父"])["data"]["total"] == 0
    assert post("collection/delete", collection_id=child, dry_run=True)["data"]["unclassified_file_count"] == 1
    assert post("collection/delete", collection_id=child)["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert post("collection/delete", collection_id=child, confirmed=True)["success"]
    assert post("file/get", file_id=file_id)["success"]


def test_search_and_mcp_use_same_default_scope(knowledge_base, api, monkeypatch):
    # 此处直接测工具业务；真实 HTTP 鉴权另有测试，不把当前测试凭据固化到模块级服务。
    monkeypatch.setattr(knowledge_base.settings.mcp, "auth_enabled", False)
    from indexing.services import chunk_service
    from indexing.mcp import server as index
    from retrieval import server as retrieval
    from retrieval.service import search
    root, child = create("论文"), None
    child = create("研究", root)
    file_id = files.create_empty_file("研究方法", [child])["file_id"]
    chunk_service.create_chunk_add_task(file_id, "研究方法", "研究方法正文")
    knowledge_base.drain()
    assert asyncio.run(search("研究方法", collections=["论文"]))["candidates"][0]["file_id"] == file_id
    assert asyncio.run(search("研究方法", collections=["论文"], include_descendants=False))["candidates"] == []
    assert asyncio.run(search("研究方法", collections=[]))["candidates"] == []
    result = api.post("/api/v1/search", json={"query": "研究方法", "collections": ["论文"], "include_descendants": False}).json()
    assert result["data"]["candidates"] == []
    listed = asyncio.run(index.query_files.fn(collections=["论文"]))
    assert listed["data"]["total"] == 1
    assert asyncio.run(index.query_files.fn(collections=["论文"], include_descendants=False))["data"]["total"] == 0
    assert asyncio.run(index.query_files.fn(collections=[]))["data"]["total"] == 0
    assert not asyncio.run(index.query_files.fn(collections=["未知"]))["success"]
    ctx = SimpleNamespace(info=AsyncMock(), warning=AsyncMock(), error=AsyncMock())
    assert asyncio.run(retrieval.resolve_keywords_tool.fn(ctx, "研究方法", collections=["论文"]))["keywords"]
    assert asyncio.run(retrieval.resolve_keywords_tool.fn(ctx, "研究方法", collections=["论文"], include_descendants=False))["keywords"] == []
    assert asyncio.run(retrieval.resolve_keywords_tool.fn(ctx, "研究方法", collections="[]"))["keywords"] == []
    read = asyncio.run(retrieval.list_collections_tool.fn(ctx))["collections"]
    write = asyncio.run(index.query_collections.fn())["data"]["collections"]
    assert read == write == collections.list_collections()
    assert asyncio.run(index.move_collection_tool.fn(child, None))["success"]
    assert not asyncio.run(index.remove_collection.fn(child))["success"]
    assert asyncio.run(index.remove_collection.fn(child, dry_run=True))["data"]["direct_file_count"] == 1
    assert asyncio.run(index.remove_collection.fn(child, confirmed=True))["success"]
    assigned = asyncio.run(index.set_file_collections.fn(file_id, ["普通/名称"]))
    assert assigned["success"]
    item = collections.get_file_collections(file_id)[0]
    assert item["name"] == "普通/名称" and item["parent_id"] is None
    assert not asyncio.run(index.set_file_collections.fn(file_id, '{"bad": "input"}'))["success"]
    assert collections.get_file_collections(file_id)[0]["id"] == item["id"]


def test_mcp_hierarchy_protocol_schemas(knowledge_base, monkeypatch):
    monkeypatch.setattr(knowledge_base.settings.mcp, "auth_enabled", False)
    from fastmcp import Client
    from indexing.mcp import server as index
    from retrieval import server as retrieval

    async def scenario():
        async with Client(index.mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            move = tools["move_collection_tool"].inputSchema
            assert "parent_id" in move["required"]
            assert tools["query_files"].inputSchema["properties"]["include_descendants"]["default"] is True
            created = await client.call_tool("create_collection_tool", {"name": "父"})
            parent_id = created.data["data"]["collection_id"]
            created = await client.call_tool("create_collection_tool", {"name": "子", "parent_id": parent_id})
            child_id = created.data["data"]["collection_id"]
            file_id = files.create_empty_file("协议文档", [child_id])["file_id"]
            query = await client.call_tool("query_files", {"collections": '["父"]'})
            assert query.data["data"]["files"][0]["id"] == file_id
            query = await client.call_tool("query_files", {"collections": "[]"})
            assert query.data["data"]["total"] == 0
            missing = await client.call_tool("move_collection_tool", {"collection_id": child_id}, raise_on_error=False)
            assert missing.is_error
            rejected = await client.call_tool("remove_collection", {"collection_id": child_id})
            assert rejected.data["code"] == "CONFIRMATION_REQUIRED"
            moved = await client.call_tool("move_collection_tool", {"collection_id": child_id, "parent_id": None})
            assert moved.data["success"]
        async with Client(retrieval.mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert tools["resolve-keywords"].inputSchema["properties"]["include_descendants"]["default"] is True
            result = await client.call_tool("list-collections", {})
            assert result.data["collections"] == collections.list_collections()
    asyncio.run(scenario())
