"""阶段 B 跨入口验收：临时新库、真实本机 HTTP/CLI 与 MCP 协议，不调用外部模型。"""

import asyncio
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest

from indexing import knowledge_models as km
from indexing.services import knowledge_service as knowledge


def _batch(**parts):
    return {"request_key": uuid4().hex, "reason": "跨入口验收", **parts}


def _post(api, operation, **payload):
    result = api.post("/api/v1/" + operation, json=payload).json()
    assert result["success"], result
    return result["data"]


def _object(api, **fields):
    result = _post(api, "knowledge/apply", **_batch(objects=[
        {"ref": "a", "kind": "concept", "title": "概念", **fields}]))
    return result["refs"]["a"]["id"]


@pytest.fixture
def mcp_servers(knowledge_base, monkeypatch):
    pytest.importorskip("fastmcp")
    from indexing.mcp import server as index
    from retrieval import server as retrieval

    # 服务是模块级单例；测试不依赖之前测试在首次导入时固定的鉴权凭据。
    monkeypatch.setattr(index.mcp, "middleware", [])
    monkeypatch.setattr(retrieval.mcp, "middleware", [])
    return index, retrieval


@pytest.fixture
def http_cli(knowledge_base, capsys):
    """真实临时端口，复用服务实现；没有 Worker，资料索引由 fixture 显式 drain。"""
    import uvicorn
    from app import cli
    from app.api import create_api
    from app.mcp_servers import _EmbeddedServer
    from app.runtime import BootstrapTokens

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    runtime = SimpleNamespace(port=port, ready=True, with_gui=False, with_mcp=False,
                              database_path=knowledge_base.settings.get_db_path().resolve(),
                              status=lambda: {}, bootstrap_tokens=BootstrapTokens(),
                              open_window=lambda url: None)
    server = _EmbeddedServer(uvicorn.Config(create_api(runtime), host="127.0.0.1", port=port,
                                          log_config=None, access_log=False, ws="none",
                                          timeout_graceful_shutdown=2))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()

    def invoke(*args):
        capsys.readouterr()
        try:
            code = cli.main([*map(str, args), "--data-dir", str(knowledge_base.path),
                             "--port", str(port), "--json"])
        except SystemExit as exc:
            code = exc.code
        output = capsys.readouterr()
        return code, json.loads(output.out)

    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "临时 HTTP 服务未启动"
        yield invoke
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive(), "测试 HTTP 线程必须排空后退出"


def test_api_knowledge_read_write_permissions(api):
    oid = _object(api)
    library_id = _post(api, "knowledge/list")["library_id"]
    committed = _batch(objects=[{"ref": "b", "kind": "entity", "title": "空正文实体"}])
    _post(api, "knowledge/apply", **committed)
    reads = {
        "list": {}, "search": {"query": "概念"}, "get": {"kind": "object", "id": oid},
        "graph": {"root_id": oid}, "references": {"source_library_id": library_id, "source_file_id": 1},
        "lint": {"object_ids": [oid]}, "history": {"kind": "object", "id": oid},
        "request": {"request_key": committed["request_key"]},
    }
    read_key, write_key = {"Authorization": "Bearer read-test-key"}, {"Authorization": "Bearer write-test-key"}
    for name, payload in reads.items():
        path = "/api/v1/knowledge/" + name
        assert api.post(path, json=payload, headers=read_key).status_code == 200, name
        assert api.post(path, json=payload, headers=write_key).status_code == 200, name
        assert api.post(path, json=payload, headers={"Authorization": ""}).status_code == 401, name
    writes = {"apply": {**committed, "dry_run": True},
              "delete": {"kind": "object", "id": oid, "expected_revision": 1, "dry_run": True}}
    for name, payload in writes.items():
        path = "/api/v1/knowledge/" + name
        assert api.post(path, json=payload, headers=read_key).status_code == 403
        assert api.post(path, json=payload, headers={"Authorization": ""}).status_code == 401
        granted = api.post(path, json=payload, headers=write_key).json()
        assert granted["success"] and not granted["data"]["committed"]


def test_api_partial_update_retry_conflicts_and_clear(api):
    oid = _object(api, summary="原摘要", body="人工正文", aliases=["别名"], status="disputed")
    update = _batch(objects=[{"id": oid, "expected_revision": 1, "title": "修改标题"}])
    first = _post(api, "knowledge/apply", **update)
    assert _post(api, "knowledge/apply", **update) == first
    assert _post(api, "knowledge/request", request_key=update["request_key"]) == first
    record = _post(api, "knowledge/get", kind="object", id=oid)["record"]
    assert (record["title"], record["body"], record["summary"], record["aliases"], record["status"], record["revision"]) == (
        "修改标题", "人工正文", "原摘要", ["别名"], "disputed", 2)
    stale = api.post("/api/v1/knowledge/apply", json={**update, "request_key": "stale"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "VERSION_CONFLICT"
    reused = api.post("/api/v1/knowledge/apply", json={**update, "reason": "改了输入不能复用键"})
    assert reused.status_code == 409 and reused.json()["error"]["code"] == "REQUEST_CONFLICT"
    _post(api, "knowledge/apply", **_batch(objects=[{"id": oid, "expected_revision": 2, "body": "", "aliases": []}]))
    record = _post(api, "knowledge/get", kind="object", id=oid)["record"]
    assert record["body"] == "" and record["aliases"] == [] and record["summary"] == "原摘要"
    assert _post(api, "knowledge/history", kind="object", id=oid)["total"] == 3


def test_api_unknown_fields_types_and_raw_size_limit(api):
    oid = _object(api)
    bad = [
        ("list", {"unexpected": "reject"}),
        ("get", {"kind": "object", "id": 1}),
        ("graph", {"root_id": oid, "depth": 3}),
        ("list", {"limit": "1"}),
        ("apply", _batch(objects=[{"id": oid, "expected_revision": 1, "body": None}])),
        ("apply", _batch(objects=[{"id": oid, "expected_revision": 1, "unexpected": "reject"}])),
        ("apply", _batch(objects=[{"ref": "x", "kind": "concept", "title": "拒绝", "extra": 1}])),
    ]
    for name, payload in bad:
        result = api.post("/api/v1/knowledge/" + name, json=payload)
        assert result.status_code == 400 and result.json()["error"]["code"] == "INVALID_INPUT"
    body = json.dumps({"reason": "体积边界", "dry_run": True,
                       "objects": [{"ref": "a", "kind": "entity", "title": "预检"}]}).encode()
    boundary = body + b" " * (knowledge.MAX_REQUEST_BYTES - len(body))
    assert api.post("/api/v1/knowledge/apply", content=boundary,
                    headers={"Content-Type": "application/json"}).json()["success"]
    too_large = api.post("/api/v1/knowledge/apply", content=boundary + b" ",
                         headers={"Content-Type": "application/json"}).json()
    assert too_large["error"]["code"] == "INPUT_TOO_LARGE"
    assert _post(api, "knowledge/list")["total"] == 1


def test_mcp_protocol_schema_validation_and_partial_updates(api, mcp_servers):
    from fastmcp import Client
    index, retrieval = mcp_servers

    async def scenario():
        async with Client(index.mcp) as writer, Client(retrieval.mcp) as reader:
            reads = {tool.name: tool for tool in await reader.list_tools() if tool.name.startswith("knowledge-")}
            writes = {tool.name: tool for tool in await writer.list_tools() if tool.name.startswith("knowledge_")}
            assert set(reads) == {"knowledge-" + name for name in (
                "list", "search", "get", "graph", "references", "lint", "history", "request")}
            assert set(writes) == {"knowledge_apply", "knowledge_delete"}
            for tool in reads.values():
                assert tool.annotations.readOnlyHint and tool.annotations.idempotentHint
                assert not tool.annotations.openWorldHint and not tool.annotations.destructiveHint
                assert "params" in tool.inputSchema["properties"]
            for tool in writes.values():
                assert not tool.annotations.readOnlyHint and tool.annotations.destructiveHint
                assert tool.annotations.idempotentHint and not tool.annotations.openWorldHint
            apply_schema = json.dumps(writes["knowledge_apply"].inputSchema)
            assert '"maxItems": 20' in apply_schema and '"expected_revision"' in apply_schema
            assert "reason" in writes["knowledge_apply"].inputSchema["$defs"]["ApplyInput"]["required"]
            assert (await reader.call_tool("knowledge_apply", {"params": {}}, raise_on_error=False)).is_error
            create = _batch(objects=[{"ref": "a", "kind": "concept", "title": "MCP标题",
                                      "body": "保留正文", "summary": "保留摘要", "aliases": ["MCP别名"]}])
            made = (await writer.call_tool("knowledge_apply", {"params": create})).data
            assert made["success"], made
            oid = made["data"]["refs"]["a"]["id"]
            update = _batch(objects=[{"id": oid, "expected_revision": 1, "title": "MCP修订"}])
            first = (await writer.call_tool("knowledge_apply", {"params": update})).data
            assert first["success"], first
            assert first["data"]["objects"][0]["submitted_fields"] == ["title"]
            assert (await writer.call_tool("knowledge_apply", {"params": update})).data == first
            record = (await reader.call_tool("knowledge-get", {"params": {"kind": "object", "id": oid}})).data["record"]
            assert record["body"] == "保留正文" and record["summary"] == "保留摘要" and record["aliases"] == ["MCP别名"]
            assert record["revision"] == 2 and record["title"] == "MCP修订"
            stale = (await writer.call_tool("knowledge_apply", {"params": {**update, "request_key": "mcp-stale"}})).data
            assert stale["error"]["code"] == "VERSION_CONFLICT" and stale["data"]["current_revision"] == 2
            for payload in ({"limit": "1"}, {"unknown": 1}):
                assert (await reader.call_tool("knowledge-list", {"params": payload}, raise_on_error=False)).is_error
            bad = _batch(objects=[{"id": oid, "expected_revision": 2, "body": None}])
            assert (await writer.call_tool("knowledge_apply", {"params": bad}, raise_on_error=False)).is_error
            assert (await writer.call_tool("knowledge_apply", {"params": {**create, "unknown": 1}}, raise_on_error=False)).is_error
            large = _batch(objects=[{"ref": str(i), "kind": "entity", "title": "大正文", "body": "x" * 200000} for i in range(3)])
            rejected = (await writer.call_tool("knowledge_apply", {"params": large})).data
            assert rejected["error"]["code"] == "INPUT_TOO_LARGE"
            assert (await reader.call_tool("knowledge-list", {"params": {}})).data["total"] == 1
            return oid

    oid = asyncio.run(scenario())
    assert _post(api, "knowledge/get", kind="object", id=oid)["record"]["body"] == "保留正文"


@pytest.mark.parametrize("tool_name,backend,model,payload", [
    ("knowledge_list", "list_objects", km.ListInput, {}),
    ("knowledge_search", "search_objects", km.SearchInput, {"query": "x"}),
    ("knowledge_get", "get_record", km.GetInput, {"kind": "object", "id": str(uuid4())}),
    ("knowledge_graph", "graph", km.GraphInput, {"root_id": str(uuid4())}),
    ("knowledge_references", "references", km.ReferencesInput, {"source_library_id": str(uuid4()), "source_file_id": 1}),
    ("knowledge_lint", "lint", km.LintInput, {}),
    ("knowledge_history", "history", km.HistoryInput, {"kind": "object", "id": str(uuid4())}),
    ("knowledge_request", "request_result", km.RequestInput, {"request_key": "x"}),
    ("knowledge_apply", "apply", km.ApplyInput, {"reason": "预检", "dry_run": True, "objects": [{"ref": "x", "kind": "entity", "title": "x"}]}),
    ("knowledge_delete", "delete", km.DeleteInput, {"kind": "evidence", "id": str(uuid4())}),
])
def test_knowledge_mcp_offloads_database_io(mcp_servers, monkeypatch, tool_name, backend, model, payload):
    index, retrieval = mcp_servers
    main_thread = threading.get_ident()
    calls = []

    def service(*args, **kwargs):
        calls.append((threading.get_ident(), kwargs))
        return {"committed": False}

    monkeypatch.setattr(knowledge, backend, service)
    server = index if backend in {"apply", "delete"} else retrieval
    asyncio.run(getattr(server, tool_name).fn(model.model_validate(payload)))
    assert len(calls) == 1 and calls[0][0] != main_thread
    if backend in {"apply", "delete"}:
        assert calls[0][1]["actor"] == "mcp:index"


def test_delete_preview_token_matches_api_mcp_and_cli(api, http_cli, mcp_servers):
    from fastmcp import Client
    index, retrieval = mcp_servers
    oid = _object(api, body="删除后在线历史不保留")
    payload = {"kind": "object", "id": oid, "expected_revision": 1}
    preview = _post(api, "knowledge/delete", **payload)
    code, cli_preview = http_cli("wiki", "delete", "object", oid, "--expected-revision", 1)
    assert code == 0 and cli_preview["data"] == preview

    async def check_and_add_dependency():
        async with Client(index.mcp) as client:
            mcp_preview = (await client.call_tool("knowledge_delete", {"params": payload})).data
            assert mcp_preview["data"] == preview
            added = (await client.call_tool("knowledge_apply", {"params": _batch(evidence=[
                {"object": {"id": oid}, "source_kind": "user", "quote": "预览后新增引用"}])})).data
            assert added["success"]

    asyncio.run(check_and_add_dependency())
    code, stale = http_cli("wiki", "delete", "object", oid, "--expected-revision", 1,
                           "--impact-token", preview["impact_token"], "--request-id", "stale-delete", "--yes")
    assert code == 1 and stale["error"]["code"] == "IMPACT_CONFLICT"
    fresh = _post(api, "knowledge/delete", **payload)
    assert fresh["counts"]["evidence"] == 1 and fresh["impact_token"] != preview["impact_token"]

    async def finish():
        async with Client(index.mcp) as writer, Client(retrieval.mcp) as reader:
            request = {**payload, "dry_run": False, "request_key": "delete-final", "impact_token": fresh["impact_token"]}
            denied = (await writer.call_tool("knowledge_delete", {"params": request})).data
            assert denied["error"]["code"] == "CONFIRMATION_REQUIRED"
            result = (await writer.call_tool("knowledge_delete", {"params": {**request, "confirmed": True}})).data
            assert result["success"] and result["data"]["committed"]
            assert (await reader.call_tool("knowledge-request", {"params": {"request_key": "delete-final"}})).data == result["data"]
            assert not result["data"]["deletes_files"] and not result["data"]["backups_affected"]

    asyncio.run(finish())
    assert api.post("/api/v1/knowledge/get", json={"kind": "object", "id": oid}).status_code == 404
    assert api.post("/api/v1/knowledge/history", json={"kind": "object", "id": oid}).status_code == 404


def test_cross_entry_compile_query_conflict_and_source_lifecycle(api, knowledge_base, http_cli, mcp_servers, tmp_path):
    from fastmcp import Client
    index, retrieval = mcp_servers
    sources = []
    for title, text in (("资料甲", "检索增强生成利用原始资料提供上下文。"), ("资料乙", "知识页保留综合结论与可追溯证据。")):
        imported = _post(api, "file/import-markdown", filename=title, content=f"# {title}\n\n{text}")
        sources.append({"file_id": imported["file_id"], "quote": text})
    knowledge_base.drain()
    for source in sources:
        chunks = _post(api, "chunk/list", file_id=source["file_id"])["chunks"]
        source["chunk_id"] = next(chunk["id"] for chunk in chunks if source["quote"] in chunk["chunk_text"])
    library_id = _post(api, "knowledge/list")["library_id"]
    batch = _batch(
        objects=[{"ref": "rag", "kind": "concept", "title": "检索增强生成", "aliases": ["RAG"]},
                 {"ref": "wiki", "kind": "concept", "title": "知识页"},
                 {"ref": "summary", "kind": "synthesis", "title": "资料与知识分层", "body": "原文和综合结论各有用途。"}],
        links=[{"source": {"ref": "summary"}, "target": {"ref": "rag"}},
               {"source": {"ref": "summary"}, "target": {"ref": "wiki"}}],
        relations=[{"ref": "relation", "source": {"ref": "rag"}, "predicate": "related_to", "target": {"ref": "wiki"},
                    "description": "二者都需要追溯到原始资料", "qualifier": "知识库场景", "basis": "synthesis"}],
        evidence=[{"relation": {"ref": "relation"}, "source_kind": "piece", "source_library_id": library_id,
                   "source_file_id": source["file_id"], "source_chunk_id": source["chunk_id"], "quote": source["quote"]} for source in sources])
    input_path = tmp_path / "知识批次.json"
    input_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
    code, preview = http_cli("wiki", "apply", "--input", input_path, "--dry-run")
    assert code == 0 and not preview["data"]["committed"]
    assert _post(api, "knowledge/list")["total"] == 0
    code, committed = http_cli("wiki", "apply", "--input", input_path, "--request-id", batch["request_key"])
    assert code == 0 and committed["data"]["committed"] and "task_id" not in committed["data"]
    ids = {ref: value["id"] for ref, value in committed["data"]["refs"].items()}
    assert _post(api, "knowledge/request", request_key=batch["request_key"])["refs"] == committed["data"]["refs"]

    async def read_and_revise():
        async with Client(retrieval.mcp) as reader, Client(index.mcp) as writer:
            found = (await reader.call_tool("knowledge-search", {"params": {"query": "RAG"}})).data
            assert found["objects"][0]["id"] == ids["rag"]
            graph = (await reader.call_tool("knowledge-graph", {"params": {"root_id": ids["summary"], "depth": 2}})).data
            assert {node["id"] for node in graph["nodes"]} == {ids["rag"], ids["wiki"], ids["summary"]}
            assert len(graph["edges"]) == 3 and not graph["truncated"]
            evidence = (await reader.call_tool("knowledge-get", {"params": {"kind": "relation", "id": ids["relation"], "limit": 1}})).data
            assert evidence["evidence"]["total"] == 2 and len(evidence["evidence"]["items"]) == 1
            assert evidence["record"]["display"] in {"检索增强生成 —related_to— 知识页", "知识页 —related_to— 检索增强生成"}
            edge = next(edge for edge in graph["edges"] if edge["kind"] == "relation")
            assert edge["display"] == evidence["record"]["display"]
            assert evidence["evidence"]["items"][0]["location_status"] == "current"
            update = _batch(objects=[{"id": ids["summary"], "expected_revision": 1, "summary": "增量修订摘要"}])
            assert (await writer.call_tool("knowledge_apply", {"params": update})).data["success"]

    asyncio.run(read_and_revise())
    code, references = http_cli("wiki", "references", library_id, sources[0]["file_id"])
    assert code == 0 and references["data"]["total"] == 1
    evidence_id = references["data"]["evidence"][0]["id"]
    assert references["data"]["evidence"][0]["owner_kind"] == "relation"
    assert "fact_verified" not in references["data"]["evidence"][0]
    barrier = threading.Barrier(2)

    def revise(title):
        barrier.wait(timeout=5)
        return api.post("/api/v1/knowledge/apply", json=_batch(objects=[
            {"id": ids["summary"], "expected_revision": 2, "title": title}])).json()

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(revise, ["调用者甲", "调用者乙"]))
    assert sorted(result["success"] for result in outcomes) == [False, True]
    assert next(result for result in outcomes if not result["success"])["error"]["code"] == "VERSION_CONFLICT"
    code, page = http_cli("wiki", "get", "object", ids["summary"])
    assert code == 0 and page["data"]["record"]["revision"] == 3
    assert page["data"]["record"]["body"] == "原文和综合结论各有用途。"
    _post(api, "file/reindex", file_id=sources[0]["file_id"], source="original", confirmed=True, request_key="reindex-source")
    knowledge_base.drain()
    code, missing = http_cli("wiki", "get", "evidence", evidence_id)
    assert code == 0 and missing["data"]["record"]["location_status"] == "missing"
    assert missing["data"]["record"]["quote"] == sources[0]["quote"]
    lint = _post(api, "knowledge/lint", object_ids=[ids["rag"]])
    assert any(issue["code"] == "SOURCE_MISSING" and issue["evidence_id"] == evidence_id for issue in lint["issues"])
    preview = _post(api, "knowledge/delete", kind="evidence", id=evidence_id)
    code, deleted = http_cli("wiki", "delete", "evidence", evidence_id, "--impact-token", preview["impact_token"],
                             "--request-id", "delete-source-evidence", "--yes")
    assert code == 0 and deleted["data"]["committed"]
    assert _post(api, "knowledge/get", kind="relation", id=ids["relation"])["evidence"]["total"] == 1
    assert _post(api, "knowledge/list")["total"] == 3
    assert api.post("/api/v1/knowledge/get", json={"kind": "evidence", "id": evidence_id}).status_code == 404


def test_file_deletion_exposes_reference_count_and_retains_knowledge(api, knowledge_base, http_cli, mcp_servers):
    from fastmcp import Client
    index, retrieval = mcp_servers
    source = _post(api, "file/import-markdown", filename="被引用原件", content="# 来源\n\n删除后引用快照仍然保留。")
    knowledge_base.drain()
    file_id = source["file_id"]
    chunk = _post(api, "chunk/list", file_id=file_id)["chunks"][0]
    oid = _object(api, body="知识正文不随原件删除")
    library_id = _post(api, "knowledge/list")["library_id"]
    result = _post(api, "knowledge/apply", **_batch(evidence=[
        {"object": {"id": oid}, "source_kind": "piece", "source_library_id": library_id,
         "source_file_id": file_id, "source_chunk_id": chunk["id"], "quote": "引用快照仍然保留"},
        {"object": {"id": oid}, "source_kind": "piece", "source_library_id": str(uuid4()),
         "source_file_id": file_id, "source_chunk_id": chunk["id"], "source_title": "别库同ID", "quote": "别库引文"}]))
    evidence_id = result["evidence"][0]["id"]
    preview = _post(api, "file/delete", file_ids=[file_id], dry_run=True)
    assert preview["knowledge_evidence_count"] == 1 and preview["retains_knowledge_snapshots"]
    assert preview["knowledge_warning"]
    code, cli_preview = http_cli("file", "delete", file_id, "--dry-run")
    assert code == 0 and cli_preview["data"] == preview

    async def scenario():
        async with Client(index.mcp) as writer, Client(retrieval.mcp) as reader:
            missing_confirmation = (await writer.call_tool("remove_file", {"file_id": file_id})).data
            assert missing_confirmation["error"]["code"] == "CONFIRMATION_REQUIRED"
            checked = (await writer.call_tool("remove_file", {"file_id": file_id, "dry_run": True})).data
            assert checked["success"] and checked["data"]["knowledge_evidence_count"] == 1
            deleted = (await writer.call_tool("remove_file", {"file_id": file_id, "confirmed": True})).data
            assert deleted["success"] and deleted["data"]["retains_knowledge_snapshots"]
            kept = (await reader.call_tool("knowledge-get", {"params": {"kind": "evidence", "id": evidence_id}})).data["record"]
            assert kept["quote"] == "引用快照仍然保留" and kept["location_status"] == "missing"

    asyncio.run(scenario())
    assert api.post("/api/v1/file/get", json={"file_id": file_id}).status_code == 404
    assert _post(api, "knowledge/get", kind="object", id=oid)["record"]["body"] == "知识正文不随原件删除"


def test_extract_quote_mcp_matches_api_and_feeds_apply(api, knowledge_base, mcp_servers):
    from fastmcp import Client
    index, retrieval = mcp_servers
    imported = _post(api, "file/import-markdown", filename="公式手册",
                     content="# 公式手册\n\n第二行 $ V_{DD} $ 公式\n第三行 <td>表格</td>")
    knowledge_base.drain()
    cid = _post(api, "chunk/list", file_id=imported["file_id"])["chunks"][0]["id"]
    chunk_text = _post(api, "chunk/get", chunk_id=cid)["chunk_text"]
    target = next(line for line in chunk_text.split("\n") if "V_{DD}" in line)

    async def scenario():
        async with Client(retrieval.mcp) as reader, Client(index.mcp) as writer:
            by_reader = (await reader.call_tool("extract-quote", {"params": {"chunk_id": cid, "grep": target}})).data
            by_writer = (await writer.call_tool("extract_quote", {"chunk_id": cid, "grep": target})).data
            assert by_reader["matches"][0]["quote"] == target == by_writer["matches"][0]["quote"]
            evidence = by_reader["matches"][0]["evidence"]
            assert evidence["source_chunk_id"] == cid and evidence["source_library_id"]
            created = (await writer.call_tool("knowledge_apply", {"params": _batch(
                objects=[{"ref": "a", "kind": "concept", "title": "公式概念"}],
                evidence=[{**evidence, "object": {"ref": "a"}}])})).data
            assert created["success"], created
            record = (await reader.call_tool("knowledge-get", {
                "params": {"kind": "evidence", "id": created["data"]["evidence"][0]["id"]}})).data["record"]
            assert record["location_status"] == "current"

    asyncio.run(scenario())


def test_knowledge_mcp_real_http_keys_are_separate(mcp_servers, monkeypatch):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from mcp.shared.exceptions import McpError
    from app.mcp_servers import MCPServerManager
    from indexing.mcp.auth import BearerAuthMiddleware
    index, retrieval = mcp_servers
    monkeypatch.setattr(index.mcp, "middleware", [BearerAuthMiddleware("write-test-key")])
    monkeypatch.setattr(retrieval.mcp, "middleware", [BearerAuthMiddleware("read-test-key")])

    async def scenario():
        manager = MCPServerManager()
        try:
            await manager.start([(retrieval.mcp, 0), (index.mcp, 0)], host="127.0.0.1")
            for position, good_key, bad_key, tool, payload in (
                (0, "read-test-key", "write-test-key", "knowledge-list", {}),
                (1, "write-test-key", "read-test-key", "knowledge_apply", {
                    "reason": "仅预检", "dry_run": True,
                    "objects": [{"ref": "x", "kind": "entity", "title": "未提交"}]}),
            ):
                port = manager.servers[position].servers[0].sockets[0].getsockname()[1]
                url = f"http://127.0.0.1:{port}/mcp"
                async with Client(StreamableHttpTransport(url, headers={"Authorization": f"Bearer {good_key}"})) as client:
                    result = await client.call_tool(tool, {"params": payload})
                    assert not result.is_error
                async with Client(StreamableHttpTransport(url, headers={"Authorization": f"Bearer {bad_key}"})) as client:
                    with pytest.raises(McpError, match="Unauthorized"):
                        await client.list_tools()
                    assert (await client.call_tool(tool, {"params": payload}, raise_on_error=False)).is_error
        finally:
            await manager.stop()
        assert not manager.tasks and not manager.servers

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))


# 高层命令端到端：精确证据、原子写入与读回。

def test_cli_extract_out_and_atomic_object_add_read_back(api, knowledge_base, http_cli, tmp_path):
    imported = _post(api, "file/import-markdown", filename="公式手册",
                     content="# 公式手册\n\n第一行 $ V_{DD} $ 公式\n第二行 <td>表格</td>")
    knowledge_base.drain()
    chunk = _post(api, "chunk/list", file_id=imported["file_id"])["chunks"][0]
    evidence_path = tmp_path / "证据.json"
    code, result = http_cli("chunk", "extract", chunk["id"], "--lines", "3-4", "--out", evidence_path)
    assert code == 0 and result["success"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["source_kind"] == "piece" and evidence["source_chunk_id"] == chunk["id"]
    assert "$ V_{DD} $" in evidence["quote"] and "<td>表格</td>" in evidence["quote"]

    request_path = tmp_path / "请求.json"
    code, added = http_cli("wiki", "object", "add", "--kind", "concept", "--title", "公式概念",
                           "--alias", "VD D", "--evidence-file", str(evidence_path),
                           "--request-file", str(request_path), "--reason", "保存公式概念")
    assert code == 0 and added["success"] and added["data"]["committed"] is True
    read_back = added["data"]["read_back"]
    assert read_back["complete"] is True and read_back["truncated"] is False
    object_entry = next(record for record in read_back["records"] if record["kind"] == "object")
    assert object_entry["revision_matches"] is True and object_entry["submitted_revision"] == 1
    oid = object_entry["record"]["id"]

    stored = _post(api, "knowledge/get", kind="object", id=oid)
    assert stored["has_evidence"] is True and stored["evidence"]["total"] == 1
    assert stored["evidence"]["items"][0]["quote"] == evidence["quote"]
    assert [obj for obj in _post(api, "knowledge/list")["objects"] if obj["id"] == oid]
    cached = _post(api, "knowledge/request", request_key=added["data"]["request_key"])
    assert "read_back" not in cached
    assert evidence["quote"] not in json.dumps(cached, ensure_ascii=False)
    assert "公式概念" not in json.dumps(cached, ensure_ascii=False)


def test_cli_apply_source_changed_rolls_back_whole_batch(api, knowledge_base, http_cli, tmp_path):
    imported = _post(api, "file/import-markdown", filename="来源", content="# 来源\n\n引文内容甲")
    knowledge_base.drain()
    chunk = _post(api, "chunk/list", file_id=imported["file_id"])["chunks"][0]
    library_id = _post(api, "knowledge/list")["library_id"]
    batch = _batch(
        objects=[{"ref": "item", "kind": "concept", "title": "应回滚概念"}],
        evidence=[{"object": {"ref": "item"}, "source_kind": "piece",
                   "source_library_id": library_id, "source_file_id": imported["file_id"],
                   "source_chunk_id": chunk["id"], "quote": "引文内容甲",
                   "expected_content_hash": "0" * 64}])
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
    code, result = http_cli("wiki", "apply", "--input", batch_path, "--request-id", batch["request_key"])
    assert code == 1 and not result["success"]
    assert result["error"]["code"] == "SOURCE_CHANGED"
    # 整批回滚：对象不能半写，也不缓存请求结果。
    assert _post(api, "knowledge/list")["total"] == 0
    assert api.post("/api/v1/knowledge/request", json={"request_key": batch["request_key"]}).status_code == 404


def test_cli_apply_identical_retry_reuses_ids_and_counts(api, http_cli, tmp_path):
    batch = _batch(objects=[{"ref": "item", "kind": "concept", "title": "幂等概念"}])
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
    code, first = http_cli("wiki", "apply", "--input", batch_path, "--request-id", batch["request_key"])
    code2, second = http_cli("wiki", "apply", "--input", batch_path, "--request-id", batch["request_key"])
    assert code == 0 and code2 == 0
    assert first["data"]["refs"] == second["data"]["refs"]
    assert first["data"]["counts"] == second["data"]["counts"]
    assert _post(api, "knowledge/request", request_key=batch["request_key"])["refs"] == first["data"]["refs"]
    assert _post(api, "knowledge/list")["total"] == 1


def test_cli_apply_same_title_creates_distinct_objects(api, http_cli, tmp_path):
    batch = _batch(objects=[{"ref": "a", "kind": "concept", "title": "同名概念"},
                            {"ref": "b", "kind": "concept", "title": "同名概念"}])
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
    code, result = http_cli("wiki", "apply", "--input", batch_path, "--request-id", batch["request_key"])
    assert code == 0 and result["success"]
    assert result["data"]["refs"]["a"]["id"] != result["data"]["refs"]["b"]["id"]
    assert _post(api, "knowledge/list")["total"] == 2


def test_cli_relation_receipt_exposes_submitted_fields_and_actual_structure(api, http_cli, tmp_path):
    a = _object(api, title="增量维护")
    b = _object(api, title="稳定身份")
    relation = {"source": {"id": a}, "predicate": "part_of", "target": {"id": b},
                "description": "原描述", "basis": "explicit"}
    batch = _batch(relations=[{**relation, "ref": "a", "status": "active"},
                              {**relation, "ref": "b", "qualifier": ""}])
    source = tmp_path / "relations.json"
    source.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
    code, made = http_cli("wiki", "apply", "--input", source, "--read-back")
    assert code == 0 and made["data"]["read_back"]["total"] == 1
    record = made["data"]["read_back"]["records"][0]
    assert record["submitted_fields"] == ["basis", "description", "predicate", "qualifier", "source", "status", "target"]
    assert record["record"]["display"] == "增量维护 —part_of→ 稳定身份"
    rid = record["record"]["id"]

    request_file = tmp_path / "description-update.json"
    code, changed = http_cli("wiki", "relation", "update", rid, "--expected-revision", 1,
                            "--description", "增量维护依赖稳定身份", "--request-file", request_file)
    assert code == 0 and changed["data"]["read_back"]["complete"]
    read = changed["data"]["read_back"]["records"][0]
    assert read["submitted_fields"] == ["description"]
    assert read["record"]["display"] == "增量维护 —part_of→ 稳定身份"
    assert read["record"]["source_id"] == a and read["record"]["target_id"] == b
    assert read["record"]["predicate"] == "part_of" and read["record"]["revision"] == 2
    code, recovered = http_cli("wiki", "request", "--input", request_file, "--read-back")
    assert code == 0 and recovered["data"]["read_back"]["records"][0]["submitted_fields"] == ["description"]
    cached = _post(api, "knowledge/request", request_key=changed["data"]["request_key"])
    assert "增量维护" not in json.dumps(cached, ensure_ascii=False) and "display" not in json.dumps(cached)


def test_api_status_exposes_library_id_when_ready(api):
    library_id = _post(api, "knowledge/list")["library_id"]
    status = api.get("/api/v1/status").json()
    assert status["success"] and status["data"]["ready"] is True
    assert status["data"]["library_id"] == library_id


def test_cli_evidence_file_rejects_source_change_without_partial_write(api, knowledge_base, http_cli, tmp_path):
    imported = _post(api, "file/import-markdown", filename="来源", content="# 来源\n\n原始引文")
    knowledge_base.drain()
    chunk_id = _post(api, "chunk/list", file_id=imported["file_id"])["chunks"][0]["id"]
    evidence = tmp_path / "evidence.json"
    assert http_cli("chunk", "extract", chunk_id, "--grep", "原始引文", "--out", evidence)[0] == 0
    original = _post(api, "chunk/get", chunk_id=chunk_id)["chunk_text"]
    _post(api, "chunk/update", chunk_id=chunk_id, chunk_text=original + "\n新增的内容", request_key="source-update")
    knowledge_base.drain()

    request_file = tmp_path / "request.json"
    code, result = http_cli("wiki", "object", "add", "--kind", "concept", "--title", "应回滚",
                            "--evidence-file", evidence, "--request-file", request_file)
    assert code == 1 and result["error"]["code"] == "SOURCE_CHANGED"
    assert _post(api, "knowledge/list")["total"] == 0
    assert result["data"]["next_argv"][-4:] == ["chunk", "get", str(chunk_id), "--json"]
    saved = json.loads(request_file.read_text(encoding="utf-8"))["request"]
    assert api.post("/api/v1/knowledge/request", json={"request_key": saved["request_key"]}).status_code == 404


def test_cli_new_request_can_recreate_deleted_object_without_reusing_old_receipt(api, http_cli, tmp_path):
    args = ("wiki", "object", "add", "--kind", "concept", "--title", "同样内容")
    first_file = tmp_path / "first.json"
    code, first = http_cli(*args, "--request-file", first_file)
    assert code == 0
    first_id = first["data"]["objects"][0]["id"]
    preview = _post(api, "knowledge/delete", kind="object", id=first_id, expected_revision=1, dry_run=True)
    _post(api, "knowledge/delete", kind="object", id=first_id, expected_revision=1,
          impact_token=preview["impact_token"], request_key="delete-first", dry_run=False, confirmed=True)
    code, deleted = http_cli("wiki", "request", "delete-first", "--read-back")
    assert code == 0 and deleted["data"]["read_back"]["records"] == [
        {"kind": "object", "id": first_id, "deleted": True}]

    code, second = http_cli(*args, "--request-file", tmp_path / "second.json")
    assert code == 0 and second["data"]["objects"][0]["id"] != first_id
    assert second["data"]["request_key"] != first["data"]["request_key"]
    assert second["data"]["read_back"]["records"][0]["has_evidence"] is False
    assert _post(api, "knowledge/list")["total"] == 1
    code, replay = http_cli("wiki", "apply", "--input", first_file, "--read-back")
    assert code == 1 and replay["error"]["code"] == "READ_BACK_INCOMPLETE"
    assert replay["data"]["committed"] is True and _post(api, "knowledge/list")["total"] == 1
