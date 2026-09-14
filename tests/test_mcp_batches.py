"""批量 MCP 工作流：临时 SQLite、本机带鉴权 HTTP 和模拟嵌入，不访问真实知识库。"""

import asyncio
import importlib
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEST_INDEX_KEY = "piece-batch-test-index-key"


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    from indexing import database, settings

    monkeypatch.setattr(settings, "DEFAULT_DATA_PATH", tmp_path)
    monkeypatch.setattr(settings, "_settings", settings.AppSettings(
        data_path=str(tmp_path),
        embedding=settings.EmbeddingSettings(api_key="test-only", vector_dim=2),
        mcp=settings.McpSettings(
            api_key="piece-batch-test-retrieval-key", index_api_key=TEST_INDEX_KEY, auth_enabled=True,
        ),
        performance=settings.PerformanceSettings(worker_concurrency=3),
    ))
    from indexing.mcp import server
    from indexing.mcp.tools import chunk_tools, task_tools
    from indexing.services import chunk_service, file_service, processor, task_service

    # 模块可能被其他测试导入过；用本轮临时密钥重新构造真实 MCP 服务。
    server = importlib.reload(server)

    async def embed(texts):
        return [[1.0, 0.0] for _ in texts]

    async def acquire(_tokens):
        pass

    model = SimpleNamespace(aembed_documents=embed)
    monkeypatch.setattr(chunk_service, "get_embeddings_model", lambda: model)
    monkeypatch.setattr(chunk_service, "get_rate_limiter", lambda: SimpleNamespace(acquire=acquire))
    monkeypatch.setattr(processor, "IDLE_POLL_SECONDS", 0.001)
    database.close_connection_pool()
    database.init_database(tmp_path / "kb.db")
    database.init_connection_pool(tmp_path / "kb.db", pool_size=4)
    file_service.ensure_files_dir()
    try:
        yield SimpleNamespace(
            root=tmp_path, database=database, server=server, model=model,
            chunk_tools=chunk_tools, task_tools=task_tools, chunk_service=chunk_service,
            file_service=file_service, task_service=task_service, processor=processor,
        )
    finally:
        database.close_connection_pool()


@asynccontextmanager
async def _client(workspace, token=TEST_INDEX_KEY):
    from app.mcp_servers import MCPServerManager

    manager = MCPServerManager()
    try:
        await manager.start([(workspace.server.mcp, 0)], host="127.0.0.1")
        port = manager._servers[0].servers[0].sockets[0].getsockname()[1]
        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/mcp", headers={"Authorization": f"Bearer {token}"},
        )
        async with Client(transport) as client:
            yield client
    finally:
        await manager.stop()


def _call(workspace, name, arguments):
    async def invoke():
        async with _client(workspace) as client:
            return await client.call_tool(name, arguments, raise_on_error=False)

    return asyncio.run(asyncio.wait_for(invoke(), timeout=10))


def _file(workspace, name="notes"):
    return workspace.file_service.create_empty_file(name)


def _task_count(workspace):
    with workspace.database.get_db_cursor() as cursor:
        return cursor.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def test_registered_tools_and_input_schemas(workspace):
    async def check():
        async with _client(workspace) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
        assert {"add_chunk", "check_task_status", "batch_add_chunks", "check_tasks_status"} <= tools.keys()
        chunks_schema = json.dumps(tools["batch_add_chunks"].inputSchema)
        assert '"doc_title"' in chunks_schema and '"chunk_text"' in chunks_schema
        assert '"maxItems": 50' in chunks_schema
        assert '"maxItems": 100' in json.dumps(tools["check_tasks_status"].inputSchema)

    asyncio.run(check())


@pytest.mark.parametrize("token", ["wrong-key", "piece-batch-test-retrieval-key"])
def test_batch_tools_reject_invalid_or_read_only_keys(workspace, token):
    file_id = _file(workspace)["file_id"]

    async def check():
        async with _client(workspace, token) as client:
            for name, arguments in [
                ("batch_add_chunks", {"file_id": file_id, "chunks": [{"doc_title": "x", "chunk_text": "y"}]}),
                ("check_tasks_status", {"task_ids": [1]}),
            ]:
                result = await client.call_tool(name, arguments, raise_on_error=False)
                assert result.is_error
                assert any(block.type == "text" and "Unauthorized" in block.text for block in result.content)

    asyncio.run(check())
    assert _task_count(workspace) == 0


@pytest.mark.parametrize("as_json", [False, True])
def test_batch_round_trip_and_single_tool_compatibility(workspace, as_json):
    file = _file(workspace)
    chunks = [
        {"doc_title": "笔记_定义 | 示例", "chunk_text": "第一段\n\n包含 |、引号 \" 和中文。"},
        {"doc_title": "笔记_应用", "chunk_text": "第二段内容"},
    ]
    submitted = _call(workspace, "batch_add_chunks", {
        "file_id": file["file_id"], "chunks": json.dumps(chunks) if as_json else chunks,
    }).data
    assert submitted["success"]
    data = submitted["data"]
    assert data["accepted_count"] == 2 and data["rejected_count"] == 0
    assert [item["index"] for item in data["items"]] == [0, 1]
    assert [item["task_id"] for item in data["items"]] == data["task_ids"]
    assert "chunk_text" not in json.dumps(data)
    assert not workspace.file_service.get_chunks_by_file_id(file["file_id"])

    async def process():
        for task_id in data["task_ids"]:
            assert workspace.task_service.claim_next_pending_task()["id"] == task_id
            await workspace.chunk_service.process_chunk_add_task(task_id)

    asyncio.run(process())
    ids = json.dumps(data["task_ids"]) if as_json else data["task_ids"]
    status = _call(workspace, "check_tasks_status", {"task_ids": ids}).data["data"]
    assert status["all_done"] and status["all_succeeded"]
    assert status["unfinished_task_ids"] == [] and status["poll_after_ms"] == 0
    stored = workspace.file_service.get_chunks_by_file_id(file["file_id"])
    assert [row["doc_title"] for row in stored] == [item["doc_title"] for item in chunks]
    assert [row["chunk_text"] for row in stored] == [item["chunk_text"] for item in chunks]
    assert [row["chunk_id"] for row in status["tasks"]] == [row["id"] for row in stored]

    single = _call(workspace, "check_task_status", {"task_id": data["task_ids"][0]}).data["data"]
    assert single["chunk_id"] == stored[0]["id"]
    assert {"current_page", "total_pages", "processed_chunks", "original_filename", "created_at", "updated_at"} <= single.keys()
    added = _call(workspace, "add_chunk", {
        "file_id": file["file_id"], "doc_title": "单条", "chunk_text": "仍然兼容",
    }).data
    assert added["success"] and added["data"]["chunk_text"] == "仍然兼容"


@pytest.mark.parametrize("chunks", [
    [],
    [{"doc_title": "x", "chunk_text": "y"}] * 51,
    json.dumps([{"doc_title": "x", "chunk_text": "y"}] * 51),
    "{",
    '{"doc_title": "not-an-array"}',
    [{"doc_title": "missing-content"}],
    [{"doc_title": "x", "chunk_text": 123}],
    [{"doc_title": "x", "chunk_text": "y", "unexpected": True}],
])
def test_invalid_batch_shape_is_rejected_before_enqueue(workspace, chunks):
    result = _call(workspace, "batch_add_chunks", {"file_id": _file(workspace)["file_id"], "chunks": chunks})
    assert result.is_error
    assert _task_count(workspace) == 0


@pytest.mark.parametrize("extra, accepted", [(0, True), (1, False)])
def test_total_character_limit_is_checked_before_enqueue(workspace, extra, accepted):
    limit = workspace.chunk_tools.MAX_BATCH_TEXT_CHARS
    result = _call(workspace, "batch_add_chunks", {
        "file_id": _file(workspace)["file_id"],
        "chunks": [{"doc_title": "x", "chunk_text": "文" * (limit - 1 + extra)}],
    }).data
    assert result["success"] is accepted
    assert _task_count(workspace) == int(accepted)


def test_missing_file_does_not_create_tasks(workspace):
    result = _call(workspace, "batch_add_chunks", {
        "file_id": 999999, "chunks": [{"doc_title": "x", "chunk_text": "y"}],
    }).data
    assert not result["success"] and result["data"]["rejected_count"] == 1
    assert result["data"]["task_ids"] == [] and _task_count(workspace) == 0


@pytest.mark.parametrize("task_ids", [[], list(range(1, 102)), [0], [-1], [True], [1.5], ["1"], "invalid-json"])
def test_invalid_task_ids_are_rejected(workspace, task_ids):
    assert _call(workspace, "check_tasks_status", {"task_ids": task_ids}).is_error


@pytest.mark.parametrize("file_id", [0, -1, True, 1.5, None])
def test_invalid_file_id_is_rejected_before_enqueue(workspace, file_id):
    _file(workspace)
    result = _call(workspace, "batch_add_chunks", {
        "file_id": file_id, "chunks": [{"doc_title": "x", "chunk_text": "y"}],
    })
    assert result.is_error and _task_count(workspace) == 0


def test_batch_count_limit_accepts_fifty_items(workspace):
    result = _call(workspace, "batch_add_chunks", {
        "file_id": _file(workspace)["file_id"],
        "chunks": [{"doc_title": f"item-{index}", "chunk_text": "正文"} for index in range(50)],
    }).data
    assert result["success"] and result["data"]["accepted_count"] == 50
    assert _task_count(workspace) == 50


def test_worker_preserves_batch_order_without_blocking_other_files(workspace):
    a, b = _file(workspace, "a"), _file(workspace, "b")
    tasks_a = _call(workspace, "batch_add_chunks", {
        "file_id": a["file_id"],
        "chunks": [{"doc_title": f"a_{index}", "chunk_text": text} for index, text in enumerate(("first", "second", "third"))],
    }).data["data"]["task_ids"]
    task_b = workspace.chunk_service.create_chunk_add_task(b["file_id"], "b", "other")

    async def check():
        entered, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        async def embed(texts):
            calls.append(texts[0])
            if texts[0] == "first":
                entered.set()
                await release.wait()
            return [[1.0, 0.0] for _ in texts]

        async def until_complete(task_ids):
            while any(task["status"] != "completed" for task in workspace.task_service.get_tasks_by_ids(task_ids)):
                await asyncio.sleep(0.005)

        workspace.model.aembed_documents = embed
        running = asyncio.create_task(workspace.processor.TaskProcessor().run(stop))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            await asyncio.wait_for(until_complete([task_b]), timeout=5)
            assert [workspace.task_service.get_task(task_id)["status"] for task_id in tasks_a] == ["processing", "pending", "pending"]
            release.set()
            await asyncio.wait_for(until_complete(tasks_a), timeout=5)
        finally:
            release.set()
            stop.set()
            await asyncio.wait_for(running, timeout=5)
        assert [text for text in calls if text != "other"] == ["first", "second", "third"]

    asyncio.run(check())
    chunks = workspace.file_service.get_chunks_by_file_id(a["file_id"])
    assert [chunk["chunk_index"] for chunk in chunks] == [0, 1, 2]
    assert [chunk["chunk_text"] for chunk in chunks] == ["first", "second", "third"]
    working = Path(a["file_path"]).read_text(encoding="utf-8")
    assert working.index("first") < working.index("second") < working.index("third")
