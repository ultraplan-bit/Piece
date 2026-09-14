"""真实 Streamable HTTP 双端口生命周期、鉴权和非阻塞工具回归。"""

import asyncio
import json
import signal
import socket
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from mcp.shared.exceptions import McpError

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.mcp_servers import MCPServerManager
from indexing import settings
from indexing.mcp.auth import apply_bearer_auth


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "_settings", settings.AppSettings(data_path=str(tmp_path)))


def _service(name, events):
    @asynccontextmanager
    async def lifespan(_):
        events.append((name, "start", id(asyncio.get_running_loop())))
        try:
            yield {}
        finally:
            events.append((name, "stop", id(asyncio.get_running_loop())))

    mcp = FastMCP(name, lifespan=lifespan)

    @mcp.tool()
    async def identity() -> dict:
        return {"name": name, "loop": id(asyncio.get_running_loop()), "thread": threading.get_ident()}

    return mcp


def _url(manager, index):
    listener = manager._servers[index].servers[0]
    port = listener.sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}/mcp"


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=20))


@pytest.mark.parametrize("iteration", range(3))
def test_both_servers_share_loop_and_close_lifespans(iteration):
    async def scenario():
        events = []
        manager = MCPServerManager()
        loop_id = id(asyncio.get_running_loop())
        thread_id = threading.get_ident()
        streams = sys.stdout, sys.stderr
        signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        services = [(_service("piece-kb", events), 0), (_service("piece-index", events), 0)]
        try:
            await manager.start(services, host="127.0.0.1")
            urls = [_url(manager, i) for i in range(2)]
            assert urls[0] != urls[1]
            assert (sys.stdout, sys.stderr) == streams
            assert {sig: signal.getsignal(sig) for sig in signals} == signals

            async def check(url, name):
                async with Client(url) as client:
                    assert [tool.name for tool in await client.list_tools()] == ["identity"]
                    result = await client.call_tool("identity")
                    assert result.data == {"name": name, "loop": loop_id, "thread": thread_id}

            await asyncio.gather(check(urls[0], "piece-kb"), check(urls[1], "piece-index"))
            original_tasks = list(manager._tasks)
            await manager.start(services, host="127.0.0.1")
            assert manager._tasks == original_tasks, "重复启动不应再绑定端口"
        finally:
            await manager.stop()
        assert len(events) == 4
        assert all(event[2] == loop_id for event in events)
        assert sorted((name, state) for name, state, _ in events) == [
            ("piece-index", "start"), ("piece-index", "stop"),
            ("piece-kb", "start"), ("piece-kb", "stop"),
        ]
        assert not manager._tasks and not manager._servers
        await manager.stop()  # 关闭幂等
        for url in urls:
            port = int(url.split(":")[2].split("/")[0])
            with socket.socket() as probe:
                probe.settimeout(0.2)
                assert probe.connect_ex(("127.0.0.1", port)) != 0

    _run(scenario())


def test_occupied_port_does_not_exit_loop_or_break_other_server(caplog):
    async def scenario():
        events = []
        manager = MCPServerManager()
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            port = occupied.getsockname()[1]
            try:
                await manager.start([
                    (_service("occupied", events), port),
                    (_service("healthy", events), 0),
                ], host="127.0.0.1")
                assert manager._tasks[0].done()
                assert not manager._servers[0].started
                assert manager._servers[1].started
                async with Client(_url(manager, 1)) as client:
                    assert (await client.call_tool("identity")).data["name"] == "healthy"
            finally:
                await manager.stop()
        assert {(name, state) for name, state, _ in events} == {
            ("occupied", "start"), ("occupied", "stop"),
            ("healthy", "start"), ("healthy", "stop"),
        }

    _run(scenario())
    assert "[MCP occupied] 服务启动或运行失败" in caplog.text


def test_lifespan_startup_failure_is_isolated(caplog):
    async def scenario():
        @asynccontextmanager
        async def failing_lifespan(_):
            raise RuntimeError("expected startup failure")
            yield {}

        manager = MCPServerManager()
        try:
            await manager.start([
                (FastMCP("failed", lifespan=failing_lifespan), 0),
                (_service("healthy", []), 0),
            ], host="127.0.0.1")
            assert manager._tasks[0].done()
            async with Client(_url(manager, 1)) as client:
                assert await client.ping()
        finally:
            await manager.stop()

    _run(scenario())
    assert "[MCP failed] 服务启动或运行失败" in caplog.text


def test_runtime_failure_closes_listener_and_lifespan(monkeypatch, caplog):
    from app.mcp_servers import _EmbeddedServer

    ports = []

    async def fail_after_binding(server):
        ports.append(server.servers[0].sockets[0].getsockname()[1])
        raise RuntimeError("expected runtime failure")

    monkeypatch.setattr(_EmbeddedServer, "main_loop", fail_after_binding)

    async def scenario():
        events = []
        manager = MCPServerManager()
        try:
            await manager.start([(_service("failed", events), 0)], host="127.0.0.1")
            await asyncio.gather(*manager._tasks)
            assert [state for _, state, _ in events] == ["start", "stop"]
            with socket.socket() as probe:
                probe.settimeout(0.2)
                assert probe.connect_ex(("127.0.0.1", ports[0])) != 0
        finally:
            await manager.stop()

    _run(scenario())
    assert "[MCP failed] 服务启动或运行失败" in caplog.text


def test_shutdown_waits_for_inflight_request_before_lifespan_cleanup():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        events = []
        mcp = _service("drain", events)

        @mcp.tool()
        async def slow() -> str:
            entered.set()
            await release.wait()
            return "done"

        manager = MCPServerManager()
        await manager.start([(mcp, 0)], host="127.0.0.1")

        async def request():
            async with Client(_url(manager, 0)) as client:
                # 客户端需先缓存输出 schema，否则拿到结果后还会补发 tools/list。
                await client.list_tools()
                return await client.call_tool("slow")

        request_task = asyncio.create_task(request())
        stop_task = None
        try:
            await entered.wait()
            stop_task = asyncio.create_task(manager.stop())
            await asyncio.sleep(0.2)
            assert not stop_task.done()
            assert not any(state == "stop" for _, state, _ in events)
            release.set()
            assert (await request_task).data == "done"
            await stop_task
            assert events[-1][1] == "stop"
        finally:
            release.set()
            await asyncio.gather(request_task, return_exceptions=True)
            if stop_task is not None:
                await stop_task
            else:
                await manager.stop()

    _run(scenario())


def test_split_keys_are_enforced_over_real_http():
    async def scenario():
        settings.get_settings().mcp = settings.McpSettings(api_key="read-key", index_api_key="write-key")
        read = _service("piece-kb", [])
        write = _service("piece-index", [])
        apply_bearer_auth(read, "retrieval")
        apply_bearer_auth(write, "index")
        manager = MCPServerManager()
        try:
            await manager.start([(read, 0), (write, 0)], host="127.0.0.1")
            for index, name, correct, wrong in (
                (0, "piece-kb", "read-key", "write-key"),
                (1, "piece-index", "write-key", "read-key"),
            ):
                for token in (correct, wrong, ""):
                    headers = {"Authorization": f"Bearer {token}"} if token else {}
                    transport = StreamableHttpTransport(_url(manager, index), headers=headers)
                    async with Client(transport) as client:
                        if token == correct:
                            assert len(await client.list_tools()) == 1
                            assert (await client.call_tool("identity")).data["name"] == name
                        else:
                            with pytest.raises(McpError, match="Unauthorized"):
                                await client.list_tools()
                            result = await client.call_tool("identity", raise_on_error=False)
                            assert result.is_error
        finally:
            await manager.stop()

    _run(scenario())


@pytest.mark.parametrize("tool_name, backend_name, args", [
    ("create_file", "create_empty_file", ["a.md"]),
    ("remove_file", "delete_file", [1]),
    ("query_files", "list_files", []),
    ("query_file_info", "get_file_info", [1]),
    ("add_chunk", "create_chunk", [1, "title", "body"]),
    ("modify_chunk_content", "update_chunk_content", [1, "body"]),
    ("remove_chunk", "delete_chunk", [1]),
    ("batch_remove_chunks", "batch_delete_chunks", [[1]]),
    ("query_chunk_info", "get_chunk_info", [1]),
    ("check_task_status", "get_task_status", [1]),
    ("query_storage_stats", "get_storage_stats", []),
    ("query_collections", "list_collections", []),
    ("create_collection_tool", "create_collection", ["name"]),
    ("set_file_collections", "assign_file_collections", [1, ["name"]]),
])
def test_index_tools_offload_synchronous_io(monkeypatch, tool_name, backend_name, args):
    from indexing.mcp import server

    def backend(*args, **kwargs):
        return {"thread": threading.get_ident()}

    monkeypatch.setattr(server, backend_name, backend)
    main_thread = threading.get_ident()
    result = _run(getattr(server, tool_name).fn(*args))
    assert result["thread"] != main_thread


def test_cancelled_index_call_waits_for_synchronous_write(monkeypatch):
    from indexing.mcp import server

    entered = threading.Event()
    release = threading.Event()
    write_finished = threading.Event()

    def backend(_):
        entered.set()
        assert release.wait(timeout=5)
        write_finished.set()
        return {"success": True}

    monkeypatch.setattr(server, "create_empty_file", backend)

    async def scenario():
        scope = anyio.CancelScope()
        call_finished = asyncio.Event()

        async def invoke():
            with scope:
                await server.create_file.fn("a.md")
            call_finished.set()

        task = asyncio.create_task(invoke())
        try:
            while not entered.is_set():
                await asyncio.sleep(0.01)
            scope.cancel()
            await asyncio.sleep(0.05)
            assert not call_finished.is_set(), "不能遗弃仍在使用数据库的写线程"
        finally:
            release.set()
            await task
        assert write_finished.is_set()
        assert call_finished.is_set()

    _run(scenario())


def test_retrieval_offloads_database_and_image_work(monkeypatch):
    from retrieval import server

    threads = []

    def get_docs(_):
        threads.append(threading.get_ident())
        return {"title": {"chunk_text": "body"}}

    def collect_images(*args, **kwargs):
        threads.append(threading.get_ident())
        return [], 0

    monkeypatch.setattr(server, "get_docs", get_docs)
    monkeypatch.setattr(server, "collect_chunk_images", collect_images)
    ctx = SimpleNamespace(info=AsyncMock(), warning=AsyncMock(), error=AsyncMock())
    _run(server.get_docs_tool.fn(ctx, ["title"], include_images=True))
    assert len(threads) == 2
    assert all(thread != threading.get_ident() for thread in threads)


@pytest.fixture
def retrieval_image_documents(monkeypatch, tmp_path):
    from PIL import Image as PILImage
    from retrieval import server

    settings.get_settings().mcp.auth_enabled = False
    image_dir = tmp_path / "figures"
    image_dir.mkdir()
    refs = [f"figures/{index}.png" for index in range(13)]
    for index, ref in enumerate(refs):
        PILImage.new("RGB", (128, 128), (index, 0, 0)).save(tmp_path / ref)
    body = "\n".join(f"![图]({ref})" for ref in refs)
    md = tmp_path / "document.md"
    md.write_text(body, encoding="utf-8")
    document = {
        "chunk_id": 1,
        "chunk_text": body,
        "file_path": str(md),
        "original_file_path": str(tmp_path / "source.pdf"),
    }
    monkeypatch.setattr(
        server, "get_docs",
        lambda titles: {title: dict(document) for title in titles if title == "title"},
    )
    return server, refs


def test_get_docs_image_pagination_contract(retrieval_image_documents):
    server, refs = retrieval_image_documents

    async def scenario():
        async with Client(server.mcp) as client:
            tool = next(tool for tool in await client.list_tools() if tool.name == "get-docs")
            offset_schema = tool.inputSchema["properties"]["image_offset"]
            assert offset_schema["default"] == 0
            assert offset_schema["minimum"] == 0
            assert "image_offset" not in tool.inputSchema.get("required", [])

            arguments = {"doc_titles": ["title"], "include_images": True}
            seen_refs = []
            seen_data = []
            for count, remaining in ((6, 7), (6, 1), (1, 0)):
                response = await client.call_tool("get-docs", arguments)
                result = response.structured_content
                assert result is not None
                text_block = response.content[0]
                assert text_block.type == "text"
                assert json.loads(text_block.text) == result
                assert result["not_found"] == []
                assert "file_path" not in result["documents"]["title"]
                assert "original_file_path" not in result["documents"]["title"]
                assert len(result["images"]) == count
                assert all(image["doc_title"] == "title" for image in result["images"])
                assert result.get("images_truncated", 0) == remaining
                seen_refs.extend(image["ref"] for image in result["images"])

                image_blocks = [block for block in response.content if block.type == "image"]
                assert len(image_blocks) == count
                assert all(block.mimeType == "image/png" and block.data for block in image_blocks)
                seen_data.extend(block.data for block in image_blocks)
                next_offset = result["next_image_offset"]
                assert next_offset == (len(seen_refs) if remaining else None)
                if next_offset is not None:
                    # 保留模型把整数参数写成字符串时的兼容行为。
                    arguments["image_offset"] = str(next_offset)

            assert seen_refs == refs
            assert len(set(seen_data)) == len(refs)

            arguments["image_offset"] = 99
            exhausted = await client.call_tool("get-docs", arguments)
            result = exhausted.structured_content
            assert result is not None
            assert result["images"] == []
            assert result["next_image_offset"] is None
            assert "images_truncated" not in result
            assert all(block.type == "text" for block in exhausted.content)

    _run(scenario())


@pytest.mark.parametrize("default_images, include_images, max_images, expected_count", [
    (False, None, 6, 0),
    (True, False, 6, 0),
    (True, None, 2, 2),
    (False, True, 2, 2),
])
def test_get_docs_image_pagination_respects_settings(
    retrieval_image_documents, default_images, include_images, max_images, expected_count
):
    server, refs = retrieval_image_documents
    settings.get_settings().mcp.include_images_default = default_images
    settings.get_settings().mcp.max_images_per_call = max_images

    async def scenario():
        async with Client(server.mcp) as client:
            arguments = {"doc_titles": ["title"]}
            if include_images is not None:
                arguments["include_images"] = include_images
            response = await client.call_tool("get-docs", arguments)
            result = response.structured_content
            assert result is not None
            assert sum(block.type == "image" for block in response.content) == expected_count
            if expected_count:
                assert [image["ref"] for image in result["images"]] == refs[:expected_count]
                assert result["next_image_offset"] == expected_count
                assert result["images_truncated"] == len(refs) - expected_count
            else:
                assert "images" not in result
                assert "next_image_offset" not in result

    _run(scenario())


@pytest.mark.parametrize("image_offset", [-1, 1.5, "invalid"])
def test_get_docs_rejects_invalid_image_offset(retrieval_image_documents, image_offset):
    server, _ = retrieval_image_documents

    async def scenario():
        async with Client(server.mcp) as client:
            response = await client.call_tool(
                "get-docs",
                {"doc_titles": ["title"], "include_images": True, "image_offset": image_offset},
                raise_on_error=False,
            )
            assert response.is_error
            assert any("image_offset" in block.text for block in response.content if block.type == "text")

    _run(scenario())


@pytest.fixture
def source_page_document(monkeypatch):
    import pymupdf
    from indexing.services import file_service, maintenance_service
    from indexing.services.errors import BusinessError
    from retrieval import server

    settings.get_settings().mcp.auth_enabled = False
    originals = file_service.get_originals_dir()
    originals.mkdir(parents=True, exist_ok=True)
    source = originals / "document.pdf"
    pdf = pymupdf.open()
    for number in (1, 2):
        page = pdf.new_page()
        page.insert_text((72, 72), f"Source page {number}")
    pdf.save(source)
    pdf.close()
    document = {
        "id": 7,
        "filename": "document.md",
        "original_file_path": str(source),
        "original_file_type": "pdf",
    }

    def file_info(file_id):
        if file_id != 7:
            raise BusinessError("NOT_FOUND", "文件不存在")
        return dict(document)

    monkeypatch.setattr(maintenance_service, "file_info", file_info)
    monkeypatch.setattr(server, "get_docs", lambda _: pytest.fail("查看原页不应依赖卡片查询"))
    return server, document, source


def test_get_source_page_contract_and_offloads_io(source_page_document, monkeypatch):
    from indexing.services import maintenance_service

    server, _, source = source_page_document
    backend = maintenance_service.source_page
    calls = []

    def source_page(file_id, page_number):
        calls.append((file_id, page_number, threading.get_ident()))
        return backend(file_id, page_number)

    monkeypatch.setattr(maintenance_service, "source_page", source_page)

    async def scenario():
        async with Client(server.mcp) as client:
            # 独立取原页不受 get-docs 默认不附图的设置影响，也兼容字符串整数。
            assert settings.get_settings().mcp.include_images_default is False
            response = await client.call_tool(
                "get-source-page", {"file_id": "7", "page_number": "2"}
            )
            assert response.structured_content == {"file_id": 7, "page_number": 2}
            assert len(response.content) == 2
            text, image = response.content
            assert text.type == "text"
            assert json.loads(text.text) == response.structured_content
            assert str(source.parent) not in text.text
            assert image.type == "image" and image.mimeType == "image/png" and image.data

            first_page = await client.call_tool(
                "get-source-page", {"file_id": 7, "page_number": 1}
            )
            first_image = first_page.content[1]
            assert first_image.type == "image" and first_image.data != image.data

            tool = next(tool for tool in await client.list_tools() if tool.name == "get-source-page")
            assert set(tool.inputSchema["required"]) == {"file_id", "page_number"}
            for name in ("file_id", "page_number"):
                assert tool.inputSchema["properties"][name]["minimum"] == 1
            assert tool.annotations is not None and tool.annotations.readOnlyHint is True

    _run(scenario())
    assert [(file_id, page) for file_id, page, _ in calls] == [(7, 2), (7, 1)]
    assert all(thread != threading.get_ident() for _, _, thread in calls)


@pytest.mark.parametrize("extension", ["docx", "doc", "rtf", "odt", "pptx", "ppt", "odp"])
def test_get_source_page_reuses_office_conversion(source_page_document, monkeypatch, extension):
    from indexing.services import page_render

    server, document, pdf = source_page_document
    office = pdf.with_suffix(f".{extension}")
    office.write_bytes(b"office conversion is stubbed")
    document.update(original_file_path=str(office), original_file_type=extension)
    converted = []
    monkeypatch.setattr(page_render, "convert_to_pdf", lambda path: converted.append(path) or pdf)

    async def scenario():
        async with Client(server.mcp) as client:
            response = await client.call_tool("get-source-page", {"file_id": 7, "page_number": 2})
            assert response.structured_content == {"file_id": 7, "page_number": 2}
            assert sum(block.type == "image" for block in response.content) == 1

    _run(scenario())
    assert converted == [office]


@pytest.mark.parametrize("problem, code", [
    ("unknown_file", "NOT_FOUND"),
    ("no_original", "NOT_FOUND"),
    ("missing_original", "PAGE_UNAVAILABLE"),
    ("unsupported", "PAGE_UNAVAILABLE"),
    ("out_of_range", "PAGE_UNAVAILABLE"),
    ("outside_managed_storage", "INVALID_PATH"),
    ("converter_unavailable", "PAGE_UNAVAILABLE"),
])
def test_get_source_page_reports_unavailable(source_page_document, monkeypatch, problem, code):
    from indexing.services import page_render

    server, document, source = source_page_document
    arguments = {"file_id": 7, "page_number": 1}
    if problem == "unknown_file":
        arguments["file_id"] = 999
    elif problem == "no_original":
        document["original_file_path"] = None
    elif problem == "missing_original":
        document["original_file_path"] = str(source.with_name("missing.pdf"))
    elif problem == "unsupported":
        document["original_file_type"] = "md"
    elif problem == "out_of_range":
        arguments["page_number"] = 3
    elif problem == "outside_managed_storage":
        document["original_file_path"] = str(source.parent.parent / "outside.pdf")
    elif problem == "converter_unavailable":
        office = source.with_suffix(".docx")
        office.write_bytes(b"converter unavailable")
        document.update(original_file_path=str(office), original_file_type="docx")
        monkeypatch.setattr(page_render, "convert_to_pdf", lambda _: None)

    async def scenario():
        async with Client(server.mcp) as client:
            response = await client.call_tool("get-source-page", arguments, raise_on_error=False)
            assert response.is_error
            assert all(block.type == "text" for block in response.content)
            message = "\n".join(block.text for block in response.content if block.type == "text")
            assert code in message
            assert str(source.parent) not in message

    _run(scenario())


@pytest.mark.parametrize("arguments", [
    {"file_id": 0, "page_number": 1},
    {"file_id": 7, "page_number": 0},
    {"file_id": 7, "page_number": -1},
    {"file_id": 7, "page_number": 1.5},
    {"file_id": "invalid", "page_number": 1},
])
def test_get_source_page_rejects_invalid_arguments(source_page_document, arguments):
    server, _, _ = source_page_document

    async def scenario():
        async with Client(server.mcp) as client:
            response = await client.call_tool("get-source-page", arguments, raise_on_error=False)
            assert response.is_error
            assert any(
                "greater than or equal to 1" in block.text or "valid integer" in block.text
                for block in response.content if block.type == "text"
            )

    _run(scenario())
