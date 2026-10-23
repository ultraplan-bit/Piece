"""阶段 B 跨入口验收：临时新库、真实本机 HTTP/CLI 与 MCP 协议，不调用外部模型。"""

import asyncio
import io
import json
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from indexing import knowledge_models as km
from indexing.services import knowledge_service as knowledge
from indexing.services import knowledge_common
from indexing.services.errors import BusinessError
from indexing import wiki_models as wm
from indexing.services import wiki_service as wiki


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
    from indexing.mcp import graph_server, server as index, wiki_server
    from retrieval import server as retrieval

    # 服务是模块级单例；测试不依赖之前测试在首次导入时固定的鉴权凭据。
    for module in (index, retrieval, graph_server, wiki_server):
        monkeypatch.setattr(module.mcp, "middleware", [])
    return index, retrieval, graph_server, wiki_server


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
        "list": {}, "search": {"query": "概念"}, "relation-search": {"query": "概念"}, "get": {"kind": "object", "id": oid},
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
              "delete": {"kind": "object", "id": oid, "expected_revision": 1, "dry_run": True},
              "rebuild-index": {}}
    for name, payload in writes.items():
        path = "/api/v1/knowledge/" + name
        assert api.post(path, json=payload, headers=read_key).status_code == 403
        assert api.post(path, json=payload, headers={"Authorization": ""}).status_code == 401
        granted = api.post(path, json=payload, headers=write_key).json()
        assert granted["success"], name
        if name != "rebuild-index":
            assert not granted["data"]["committed"]


def test_api_wiki_read_write_permissions(api):
    batch = _batch(pages=[{"ref": "p", "kind": "concept", "title": "页面", "body": "正文"}])
    page = _post(api, "wiki/apply", **batch)
    pid = page["refs"]["p"]["id"]
    record = _post(api, "wiki/get", kind="page", id=pid)["record"]
    assert record["body"] == "正文" and record["content_hash"] and "index_status" in record
    library_id = _post(api, "wiki/list")["library_id"]
    reads = {
        "list": {}, "search": {"query": "页面"}, "get": {"kind": "page", "id": pid},
        "references": {"source_library_id": library_id, "source_file_id": 1},
        "lint": {"page_ids": [pid]}, "history": {"kind": "page", "id": pid},
        "request": {"request_key": batch["request_key"]},
    }
    read_key, write_key = {"Authorization": "Bearer read-test-key"}, {"Authorization": "Bearer write-test-key"}
    for name, payload in reads.items():
        path = "/api/v1/wiki/" + name
        assert api.post(path, json=payload, headers=read_key).status_code == 200, name
        assert api.post(path, json=payload, headers=write_key).status_code == 200, name
        assert api.post(path, json=payload, headers={"Authorization": ""}).status_code == 401, name
    writes = {
        "apply": {"reason": "预检", "dry_run": True, "pages": [{"ref": "x", "kind": "concept", "title": "x"}]},
        "delete": {"kind": "page", "id": pid, "expected_revision": record["revision"],
                   "expected_content_hash": record["content_hash"], "dry_run": True},
        "rebuild-index": {},
    }
    for name, payload in writes.items():
        path = "/api/v1/wiki/" + name
        # 索引重建会写入派生数据，读凭据不得触发。
        assert api.post(path, json=payload, headers=read_key).status_code == 403, name
        assert api.post(path, json=payload, headers={"Authorization": ""}).status_code == 401, name
        granted = api.post(path, json=payload, headers=write_key).json()
        assert granted["success"], name


def test_api_partial_update_retry_conflicts_and_clear(api):
    oid = _object(api, summary="原摘要", aliases=["别名"], status="disputed")
    update = _batch(objects=[{"id": oid, "expected_revision": 1, "title": "修改标题"}])
    first = _post(api, "knowledge/apply", **update)
    assert _post(api, "knowledge/apply", **update) == first
    assert _post(api, "knowledge/request", request_key=update["request_key"]) == first
    record = _post(api, "knowledge/get", kind="object", id=oid)["record"]
    assert (record["title"], record["summary"], record["aliases"], record["status"], record["revision"]) == (
        "修改标题", "原摘要", ["别名"], "disputed", 2)
    stale = api.post("/api/v1/knowledge/apply", json={**update, "request_key": "stale"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "VERSION_CONFLICT"
    reused = api.post("/api/v1/knowledge/apply", json={**update, "reason": "改了输入不能复用键"})
    assert reused.status_code == 409 and reused.json()["error"]["code"] == "REQUEST_CONFLICT"
    _post(api, "knowledge/apply", **_batch(objects=[{"id": oid, "expected_revision": 2, "summary": "", "aliases": []}]))
    record = _post(api, "knowledge/get", kind="object", id=oid)["record"]
    assert record["summary"] == "" and record["aliases"] == []
    assert _post(api, "knowledge/history", kind="object", id=oid)["total"] == 3


def test_api_unknown_fields_types_and_raw_size_limit(api):
    oid = _object(api)
    bad = [
        ("list", {"unexpected": "reject"}),
        ("get", {"kind": "object", "id": 1}),
        ("graph", {"root_id": oid, "depth": 3}),
        ("list", {"limit": "1"}),
        ("apply", _batch(objects=[{"id": oid, "expected_revision": 1, "summary": None}])),
        ("apply", _batch(objects=[{"id": oid, "expected_revision": 1, "unexpected": "reject"}])),
        ("apply", _batch(objects=[{"ref": "x", "kind": "concept", "title": "拒绝", "extra": 1}])),
    ]
    for name, payload in bad:
        result = api.post("/api/v1/knowledge/" + name, json=payload)
        assert result.status_code == 400 and result.json()["error"]["code"] == "INVALID_INPUT"
    body = json.dumps({"reason": "体积边界", "dry_run": True,
                       "objects": [{"ref": "a", "kind": "entity", "title": "预检"}]}).encode()
    boundary = body + b" " * (knowledge_common.MAX_REQUEST_BYTES - len(body))
    assert api.post("/api/v1/knowledge/apply", content=boundary,
                    headers={"Content-Type": "application/json"}).json()["success"]
    too_large = api.post("/api/v1/knowledge/apply", content=boundary + b" ",
                         headers={"Content-Type": "application/json"}).json()
    assert too_large["error"]["code"] == "INPUT_TOO_LARGE"
    # Wiki 批次与图谱共用 512 KiB 上限；单页 8 MiB 只是文件存储上限，不放宽批次大小。
    wiki_body = json.dumps({"reason": "体积边界", "dry_run": True,
                            "pages": [{"ref": "p", "kind": "entity", "title": "预检"}]}).encode()
    wiki_boundary = wiki_body + b" " * (knowledge_common.MAX_REQUEST_BYTES - len(wiki_body))
    assert api.post("/api/v1/wiki/apply", content=wiki_boundary,
                    headers={"Content-Type": "application/json"}).json()["success"]
    wiki_too_large = api.post("/api/v1/wiki/apply", content=wiki_boundary + b" ",
                              headers={"Content-Type": "application/json"}).json()
    assert wiki_too_large["error"]["code"] == "INPUT_TOO_LARGE"
    assert _post(api, "knowledge/list")["total"] == 1


def test_mcp_protocol_schema_validation_and_partial_updates(api, mcp_servers):
    from fastmcp import Client
    index, retrieval, graph, wiki_server = mcp_servers

    async def scenario():
        async with Client(graph.mcp) as graph_client, Client(wiki_server.mcp) as wiki_client:
            graph_tools = {tool.name: tool for tool in await graph_client.list_tools()}
            assert set(graph_tools) == {"knowledge-" + name for name in (
                "list", "search", "relation-search", "get", "graph", "references", "lint", "history", "request",
                "apply", "delete", "rebuild-index", "object-add", "object-update", "relation-add", "relation-update")}
            reads = {name: tool for name, tool in graph_tools.items() if tool.annotations.readOnlyHint}
            assert set(reads) == {"knowledge-" + name for name in (
                "list", "search", "relation-search", "get", "graph", "references", "lint", "history", "request")}
            for tool in reads.values():
                assert tool.annotations.idempotentHint and not tool.annotations.openWorldHint and not tool.annotations.destructiveHint
                assert "params" in tool.inputSchema["properties"]
            for name in ("knowledge-apply", "knowledge-delete"):
                assert "params" in graph_tools[name].inputSchema["properties"]
            assert "params" not in graph_tools["knowledge-rebuild-index"].inputSchema["properties"]
            intents = {"knowledge-object-add", "knowledge-object-update", "knowledge-relation-add", "knowledge-relation-update"}
            for name in intents:
                assert not graph_tools[name].annotations.readOnlyHint and not graph_tools[name].annotations.openWorldHint
                # 意图工具每次自动生成新 request_key，不能宣称幂等。
                assert graph_tools[name].annotations.idempotentHint is False
            for name in ("knowledge-apply", "knowledge-delete", "knowledge-rebuild-index"):
                assert graph_tools[name].annotations.idempotentHint is True
            for name in ("knowledge-apply", "knowledge-delete", "knowledge-object-update", "knowledge-relation-update"):
                assert graph_tools[name].annotations.destructiveHint
            for name in ("knowledge-rebuild-index", "knowledge-object-add", "knowledge-relation-add"):
                assert not graph_tools[name].annotations.destructiveHint
            # 意图工具参数扁平：没有 params 包装，证据是字符串数组，schema 直接列出字段。
            flat = graph_tools["knowledge-object-add"].inputSchema
            assert "params" not in flat["properties"] and {"title", "kind", "evidence", "stance"} <= set(flat["properties"])
            assert flat["required"] == ["title"]
            relation_update = graph_tools["knowledge-relation-update"].inputSchema
            assert "params" not in relation_update["properties"]
            assert set(relation_update["required"]) == {"id", "expected_revision"}
            assert {"source", "predicate", "target", "description", "basis", "qualifier", "status", "evidence"} <= set(relation_update["properties"])
            # knowledge-get 的 kind 可省略：schema 不再把它列为必填。
            get_schema = json.dumps(reads["knowledge-get"].inputSchema)
            assert '"required": ["id"]' in get_schema and '"required": ["kind", "id"]' not in get_schema
            wiki_tools = {tool.name: tool for tool in await wiki_client.list_tools()}
            assert set(wiki_tools) == {"wiki-" + name for name in (
                "list", "search", "get", "references", "lint", "history", "request", "apply", "delete",
                "rebuild-index", "list-recovery", "purge-recovery", "page-add", "page-update")}
            for name in ("wiki-list", "wiki-search", "wiki-get", "wiki-references", "wiki-lint",
                         "wiki-history", "wiki-request", "wiki-list-recovery"):
                assert wiki_tools[name].annotations.readOnlyHint and wiki_tools[name].annotations.idempotentHint
            for name in ("wiki-apply", "wiki-delete", "wiki-page-update", "wiki-purge-recovery"):
                assert wiki_tools[name].annotations.destructiveHint
            for name in ("wiki-list", "wiki-search", "wiki-get", "wiki-references", "wiki-lint",
                         "wiki-history", "wiki-request", "wiki-apply", "wiki-delete"):
                assert "params" in wiki_tools[name].inputSchema["properties"]
            for name in ("wiki-list-recovery", "wiki-purge-recovery", "wiki-page-add", "wiki-page-update"):
                assert "params" not in wiki_tools[name].inputSchema["properties"]
            assert "params" not in wiki_tools["wiki-rebuild-index"].inputSchema["properties"]
            apply_schema = json.dumps(graph_tools["knowledge-apply"].inputSchema)
            assert '"maxItems": 20' in apply_schema and '"expected_revision"' in apply_schema
            assert "reason" in graph_tools["knowledge-apply"].inputSchema["$defs"]["ApplyInput"]["required"]
            assert (await graph_client.call_tool("knowledge-apply", {"params": {}}, raise_on_error=False)).is_error
            create = _batch(objects=[{"ref": "a", "kind": "concept", "title": "MCP标题",
                                      "summary": "保留摘要", "aliases": ["MCP别名"]}])
            made = (await graph_client.call_tool("knowledge-apply", {"params": create})).data
            assert made["success"], made
            oid = made["data"]["refs"]["a"]["id"]
            update = _batch(objects=[{"id": oid, "expected_revision": 1, "title": "MCP修订"}])
            first = (await graph_client.call_tool("knowledge-apply", {"params": update})).data
            assert first["success"], first
            assert first["data"]["objects"][0]["submitted_fields"] == ["title"]
            assert (await graph_client.call_tool("knowledge-apply", {"params": update})).data == first
            record = (await graph_client.call_tool("knowledge-get", {"params": {"kind": "object", "id": oid}})).data["record"]
            assert record["summary"] == "保留摘要" and record["aliases"] == ["MCP别名"]
            assert record["revision"] == 2 and record["title"] == "MCP修订" and "body" not in record
            stale = (await graph_client.call_tool("knowledge-apply", {"params": {**update, "request_key": "mcp-stale"}})).data
            assert stale["error"]["code"] == "VERSION_CONFLICT" and stale["data"]["current_revision"] == 2
            for payload in ({"limit": "1"}, {"unknown": 1}):
                assert (await graph_client.call_tool("knowledge-list", {"params": payload}, raise_on_error=False)).is_error
            bad = _batch(objects=[{"id": oid, "expected_revision": 2, "summary": None}])
            assert (await graph_client.call_tool("knowledge-apply", {"params": bad}, raise_on_error=False)).is_error
            assert (await graph_client.call_tool("knowledge-apply", {"params": {**create, "unknown": 1}}, raise_on_error=False)).is_error
            large = _batch(pages=[{"ref": "big", "kind": "entity", "title": "大页面"}],
                           evidence=[{"page": {"ref": "big"}, "source_kind": "user",
                                      "quote": "x" * 100000} for _ in range(6)])
            rejected = (await wiki_client.call_tool("wiki-apply", {"params": large})).data
            assert rejected["error"]["code"] == "INPUT_TOO_LARGE"
            assert (await graph_client.call_tool("knowledge-list", {"params": {}})).data["total"] == 1
            return oid

    oid = asyncio.run(scenario())
    assert _post(api, "knowledge/get", kind="object", id=oid)["record"]["summary"] == "保留摘要"


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
    ("knowledge_rebuild_index", "rebuild_index", None, None),
    ("wiki_list", "list_pages", wm.ListInput, {}),
    ("wiki_search", "search_pages", wm.SearchInput, {"query": "x"}),
    ("wiki_get", "get_record", wm.GetInput, {"kind": "page", "id": str(uuid4())}),
    ("wiki_references", "references", wm.ReferencesInput, {"source_library_id": str(uuid4()), "source_file_id": 1}),
    ("wiki_lint", "lint", wm.LintInput, {}),
    ("wiki_history", "history", wm.HistoryInput, {"kind": "page", "id": str(uuid4())}),
    ("wiki_request", "request_result", wm.RequestInput, {"request_key": "x"}),
    ("wiki_apply", "apply", wm.ApplyInput, {"reason": "预检", "dry_run": True, "pages": [{"ref": "x", "kind": "entity", "title": "x"}]}),
    ("wiki_delete", "delete", wm.DeleteInput, {"kind": "evidence", "id": str(uuid4()), "expected_content_hash": "0" * 64}),
    ("wiki_rebuild_index", "rebuild_index", None, None),
    ("wiki_list_recovery", "list_recovery", "kwargs", {}),
    ("wiki_purge_recovery", "purge_recovery", "kwargs", {}),
])
def test_knowledge_mcp_offloads_database_io(mcp_servers, monkeypatch, tool_name, backend, model, payload):
    index, retrieval, graph, wiki_server = mcp_servers
    main_thread = threading.get_ident()
    calls = []
    is_wiki = tool_name.startswith("wiki")
    owner = wiki if is_wiki else knowledge
    server = wiki_server if is_wiki else graph

    def service(*args, **kwargs):
        calls.append((threading.get_ident(), kwargs))
        return {"committed": False}

    monkeypatch.setattr(owner, backend, service)
    target = getattr(server, tool_name)
    if model is None:
        asyncio.run(target.fn())
    elif model == "kwargs":
        asyncio.run(target.fn(**payload))
    else:
        asyncio.run(target.fn(model.model_validate(payload)))
    assert len(calls) == 1 and calls[0][0] != main_thread
    if backend in {"apply", "delete"}:
        assert calls[0][1]["actor"] == ("mcp:wiki" if is_wiki else "mcp:graph")


def test_delete_preview_token_matches_api_mcp_and_cli(api, http_cli, mcp_servers):
    from fastmcp import Client
    index, retrieval, graph, wiki_server = mcp_servers
    oid = _object(api, summary="删除后在线历史不保留")
    payload = {"kind": "object", "id": oid, "expected_revision": 1}
    preview = _post(api, "knowledge/delete", **payload)
    code, cli_preview = http_cli("graph", "delete", "object", oid, "--expected-revision", 1)
    assert code == 0 and cli_preview["data"] == preview

    async def check_and_add_dependency():
        async with Client(graph.mcp) as client:
            mcp_preview = (await client.call_tool("knowledge-delete", {"params": payload})).data
            assert mcp_preview["data"] == preview
            added = (await client.call_tool("knowledge-apply", {"params": _batch(evidence=[
                {"object": {"id": oid}, "source_kind": "user", "quote": "预览后新增引用"}])})).data
            assert added["success"]

    asyncio.run(check_and_add_dependency())
    code, stale = http_cli("graph", "delete", "object", oid, "--expected-revision", 1,
                           "--impact-token", preview["impact_token"], "--request-id", "stale-delete", "--yes")
    assert code == 1 and stale["error"]["code"] == "IMPACT_CONFLICT"
    fresh = _post(api, "knowledge/delete", **payload)
    assert fresh["counts"]["evidence"] == 1 and fresh["impact_token"] != preview["impact_token"]

    async def finish():
        async with Client(graph.mcp) as client:
            request = {**payload, "dry_run": False, "request_key": "delete-final", "impact_token": fresh["impact_token"]}
            denied = (await client.call_tool("knowledge-delete", {"params": request})).data
            assert denied["error"]["code"] == "CONFIRMATION_REQUIRED"
            result = (await client.call_tool("knowledge-delete", {"params": {**request, "confirmed": True}})).data
            assert result["success"] and result["data"]["committed"]
            assert (await client.call_tool("knowledge-request", {"params": {"request_key": "delete-final"}})).data == result["data"]
            assert not result["data"]["deletes_files"] and not result["data"]["backups_affected"]

    asyncio.run(finish())
    assert api.post("/api/v1/knowledge/get", json={"kind": "object", "id": oid}).status_code == 404
    assert api.post("/api/v1/knowledge/history", json={"kind": "object", "id": oid}).status_code == 404


def test_cross_entry_compile_query_conflict_and_source_lifecycle(api, knowledge_base, http_cli, mcp_servers, tmp_path):
    from fastmcp import Client
    index, retrieval, graph, wiki_server = mcp_servers
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
                 {"ref": "graph", "kind": "concept", "title": "知识图谱"},
                 {"ref": "summary", "kind": "concept", "title": "资料与图谱分层", "summary": "原文和综合结论各有用途。"}],
        relations=[{"ref": "relation", "source": {"ref": "rag"}, "predicate": "related_to", "target": {"ref": "graph"},
                    "description": "二者都需要追溯到原始资料", "qualifier": "知识库场景", "basis": "synthesis"},
                   {"ref": "context", "source": {"ref": "summary"}, "predicate": "related_to", "target": {"ref": "rag"},
                    "description": "按资料与图谱分层组织", "qualifier": "知识库场景", "basis": "synthesis"}],
        evidence=[{"relation": {"ref": "relation"}, "source_kind": "piece", "source_library_id": library_id,
                   "source_file_id": source["file_id"], "source_chunk_id": source["chunk_id"], "quote": source["quote"]} for source in sources])
    input_path = tmp_path / "知识批次.json"
    input_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
    code, preview = http_cli("graph", "apply", "--input", input_path, "--dry-run")
    assert code == 0 and not preview["data"]["committed"]
    assert _post(api, "knowledge/list")["total"] == 0
    code, committed = http_cli("graph", "apply", "--input", input_path, "--request-id", batch["request_key"])
    assert code == 0 and committed["data"]["committed"] and "task_id" not in committed["data"]
    ids = {ref: value["id"] for ref, value in committed["data"]["refs"].items()}
    assert _post(api, "knowledge/request", request_key=batch["request_key"])["refs"] == committed["data"]["refs"]

    async def read_and_revise():
        async with Client(graph.mcp) as client:
            found = (await client.call_tool("knowledge-search", {"params": {"query": "RAG"}})).data
            assert found["objects"][0]["id"] == ids["rag"]
            neighborhood = (await client.call_tool("knowledge-graph", {"params": {"root_id": ids["summary"], "depth": 2}})).data
            assert {node["id"] for node in neighborhood["nodes"]} == {ids["rag"], ids["graph"], ids["summary"]}
            assert len(neighborhood["edges"]) == 2 and not neighborhood["truncated"]
            evidence = (await client.call_tool("knowledge-get", {"params": {"kind": "relation", "id": ids["relation"], "limit": 1}})).data
            assert evidence["evidence"]["total"] == 2 and len(evidence["evidence"]["items"]) == 1
            assert evidence["record"]["display"] in {"检索增强生成 —related_to— 知识图谱", "知识图谱 —related_to— 检索增强生成"}
            edge = next(edge for edge in neighborhood["edges"] if edge["id"] == ids["relation"])
            assert edge["display"] == evidence["record"]["display"]
            assert evidence["evidence"]["items"][0]["location_status"] == "current"
            # 检索策略由 MCP 服务自身说明提供，不依赖读取 Skill。
            assert graph.mcp.instructions is not None
            assert "knowledge-relation-search" in graph.mcp.instructions
            assert "端点标题" in graph.mcp.instructions
            assert "只读检索" in graph.mcp.instructions
            assert '"params":{"query":"关键词"}' in graph.mcp.instructions
            assert '"title":"实体"' in graph.mcp.instructions
            assert "knowledge-object-add/update" in graph.mcp.instructions
            assert '"params":{"reason":"补关系证据"' in graph.mcp.instructions
            assert "relation: {id: 关系UUID}" in graph.mcp.instructions
            update = _batch(objects=[{"id": ids["summary"], "expected_revision": 1, "title": "增量修订标题"}])
            assert (await client.call_tool("knowledge-apply", {"params": update})).data["success"]

    asyncio.run(read_and_revise())
    code, references = http_cli("graph", "references", library_id, sources[0]["file_id"])
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
    code, page = http_cli("graph", "get", "object", ids["summary"])
    assert code == 0 and page["data"]["record"]["revision"] == 3
    assert page["data"]["record"]["summary"] == "原文和综合结论各有用途。"
    _post(api, "file/reindex", file_id=sources[0]["file_id"], source="original", confirmed=True, request_key="reindex-source")
    knowledge_base.drain()
    code, missing = http_cli("graph", "get", "evidence", evidence_id)
    assert code == 0 and missing["data"]["record"]["location_status"] == "missing"
    assert missing["data"]["record"]["quote"] == sources[0]["quote"]
    lint = _post(api, "knowledge/lint", object_ids=[ids["rag"]])
    assert any(issue["code"] == "SOURCE_MISSING" and issue["evidence_id"] == evidence_id for issue in lint["issues"])
    preview = _post(api, "knowledge/delete", kind="evidence", id=evidence_id)
    code, deleted = http_cli("graph", "delete", "evidence", evidence_id, "--impact-token", preview["impact_token"],
                             "--request-id", "delete-source-evidence", "--yes")
    assert code == 0 and deleted["data"]["committed"]
    assert _post(api, "knowledge/get", kind="relation", id=ids["relation"])["evidence"]["total"] == 1
    assert _post(api, "knowledge/list")["total"] == 3
    assert api.post("/api/v1/knowledge/get", json={"kind": "evidence", "id": evidence_id}).status_code == 404


def test_file_deletion_exposes_reference_count_and_retains_knowledge(api, knowledge_base, http_cli, mcp_servers):
    from fastmcp import Client
    index, retrieval, graph, wiki_server = mcp_servers
    source = _post(api, "file/import-markdown", filename="被引用原件", content="# 来源\n\n删除后引用快照仍然保留。")
    knowledge_base.drain()
    file_id = source["file_id"]
    chunk = _post(api, "chunk/list", file_id=file_id)["chunks"][0]
    oid = _object(api, summary="图谱摘要不随原件删除")
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
        async with Client(index.mcp) as writer, Client(graph.mcp) as reader:
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
    assert _post(api, "knowledge/get", kind="object", id=oid)["record"]["summary"] == "图谱摘要不随原件删除"


def test_extract_quote_mcp_matches_api_and_feeds_apply(api, knowledge_base, mcp_servers):
    from fastmcp import Client
    index, retrieval, graph, wiki_server = mcp_servers
    imported = _post(api, "file/import-markdown", filename="公式手册",
                     content="# 公式手册\n\n第二行 $ V_{DD} $ 公式\n第三行 <td>表格</td>")
    knowledge_base.drain()
    cid = _post(api, "chunk/list", file_id=imported["file_id"])["chunks"][0]["id"]
    chunk_text = _post(api, "chunk/get", chunk_id=cid)["chunk_text"]
    target = next(line for line in chunk_text.split("\n") if "V_{DD}" in line)

    async def scenario():
        async with Client(retrieval.mcp) as reader, Client(graph.mcp) as writer:
            quoted = (await reader.call_tool("extract-quote", {"params": {"chunk_id": cid, "grep": target}})).data
            assert quoted["matches"][0]["quote"] == target
            evidence = quoted["matches"][0]["evidence"]
            assert evidence["source_chunk_id"] == cid and evidence["source_library_id"]
            created = (await writer.call_tool("knowledge-apply", {"params": _batch(
                objects=[{"ref": "a", "kind": "concept", "title": "公式概念"}],
                evidence=[{**evidence, "object": {"ref": "a"}}])})).data
            assert created["success"], created
            record = (await writer.call_tool("knowledge-get", {
                "params": {"kind": "evidence", "id": created["data"]["evidence"][0]["id"]}})).data["record"]
            assert record["location_status"] == "current"

    asyncio.run(scenario())


def test_knowledge_mcp_real_http_keys_are_separate(mcp_servers, monkeypatch):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from mcp.shared.exceptions import McpError
    from app.mcp_servers import MCPServerManager
    from indexing.mcp.auth import BearerAuthMiddleware
    index, retrieval, graph, wiki_server = mcp_servers
    monkeypatch.setattr(index.mcp, "middleware", [BearerAuthMiddleware("write-test-key")])
    monkeypatch.setattr(graph.mcp, "middleware", [BearerAuthMiddleware("graph-test-key")])
    monkeypatch.setattr(retrieval.mcp, "middleware", [BearerAuthMiddleware("read-test-key")])

    async def scenario():
        manager = MCPServerManager()
        try:
            await manager.start([(retrieval.mcp, 0), (graph.mcp, 0)], host="127.0.0.1")
            for position, good_key, bad_key, tool, payload in (
                (0, "read-test-key", "write-test-key", "list-collections", {}),
                (1, "graph-test-key", "read-test-key", "knowledge-apply", {
                    "params": {"reason": "仅预检", "dry_run": True,
                               "objects": [{"ref": "x", "kind": "entity", "title": "未提交"}]}}),
            ):
                port = manager.servers[position].servers[0].sockets[0].getsockname()[1]
                url = f"http://127.0.0.1:{port}/mcp"
                async with Client(StreamableHttpTransport(url, headers={"Authorization": f"Bearer {good_key}"})) as client:
                    result = await client.call_tool(tool, payload)
                    assert not result.is_error
                async with Client(StreamableHttpTransport(url, headers={"Authorization": f"Bearer {bad_key}"})) as client:
                    with pytest.raises(McpError, match="Unauthorized"):
                        await client.list_tools()
                    assert (await client.call_tool(tool, payload, raise_on_error=False)).is_error
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
    code, added = http_cli("graph", "object", "add", "--kind", "concept", "--title", "公式概念",
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


def test_cli_inline_evidence_single_command_writes_and_reads_back(api, knowledge_base, http_cli, tmp_path):
    """零文件写入：--evidence CHUNK_ID:LOCATOR 由服务切引文，不传请求文件，CLI 自生成键。"""
    imported = _post(api, "file/import-markdown", filename="内联证据来源",
                     content="# 来源\n\n第一行正文\n增量维护依赖稳定身份。\n第三行反方观点")
    knowledge_base.drain()
    chunk = _post(api, "chunk/list", file_id=imported["file_id"])["chunks"][0]
    before = sorted(p.name for p in tmp_path.iterdir())

    code, added = http_cli("graph", "object", "add", "--kind", "concept", "--title", "稳定身份",
                           "--evidence", f"{chunk['id']}:grep=稳定身份",
                           "--evidence", f"{chunk['id']}:5",
                           "--stance", "supports", "--stance", "contradicts", "--reason", "内联证据端到端")
    assert code == 0 and added["success"] and added["data"]["committed"] is True, added
    assert added["data"]["read_back"]["complete"] is True
    assert "request_file" not in added["data"]
    assert sorted(p.name for p in tmp_path.iterdir()) == before, "不得在数据目录留下请求或证据文件"

    oid = next(r for r in added["data"]["read_back"]["records"] if r["kind"] == "object")["record"]["id"]
    stored = _post(api, "knowledge/get", kind="object", id=oid)
    quotes = {item["quote"]: item["stance"] for item in stored["evidence"]["items"]}
    assert quotes == {"增量维护依赖稳定身份。": "supports", "第三行反方观点": "contradicts"}
    assert all(item["location_status"] == "current" for item in stored["evidence"]["items"])
    # 自生成键可直接用于只读查询回执。
    code, recovered = http_cli("graph", "request", added["data"]["request_key"], "--read-back")
    assert code == 0 and recovered["data"]["read_back"]["records"][0]["record"]["id"] == oid


def test_cli_inline_evidence_ambiguous_match_refuses_to_write(api, knowledge_base, http_cli):
    imported = _post(api, "file/import-markdown", filename="歧义来源",
                     content="# 来源\n\n关键词出现一次\n关键词出现两次")
    knowledge_base.drain()
    chunk = _post(api, "chunk/list", file_id=imported["file_id"])["chunks"][0]
    code, result = http_cli("graph", "object", "add", "--kind", "concept", "--title", "不该写入",
                            "--evidence", f"{chunk['id']}:grep=关键词")
    assert code != 0 and result["error"]["code"] == "EVIDENCE_SELECTION_REQUIRED"
    assert result["data"]["match_count"] == 2
    assert result["data"]["next_argv"][-4:] == ["chunk", "get", str(chunk["id"]), "--json"]
    assert _post(api, "knowledge/list")["total"] == 0


def test_mcp_intent_tools_flat_params_auto_key_and_shared_builder(api, knowledge_base, mcp_servers):
    """图谱 MCP 意图工具：扁平参数、内联证据、自动 request_key，与 CLI 共用组装逻辑。"""
    from fastmcp import Client
    index, retrieval, graph, wiki_server = mcp_servers
    imported = _post(api, "file/import-markdown", filename="意图来源",
                     content="# 来源\n\n第一行正文\n增量维护依赖稳定身份。\n第三行反方观点")
    knowledge_base.drain()
    chunk = _post(api, "chunk/list", file_id=imported["file_id"])["chunks"][0]
    library_id = _post(api, "knowledge/list")["library_id"]
    locator = f"{chunk['id']}:grep=稳定身份"

    async def scenario():
        async with Client(graph.mcp) as writer, Client(wiki_server.mcp) as wiki_writer, \
                   Client(index.mcp) as index_client, Client(graph.mcp) as reader:
            # dry_run 不消耗键、不写入。
            preview = (await writer.call_tool("knowledge-object-add", {
                "title": "稳定身份", "evidence": [locator], "dry_run": True})).data
            assert preview["success"] and preview["data"]["dry_run"] and "request_key" not in preview["data"]
            assert (await reader.call_tool("knowledge-list", {"params": {}})).data["total"] == 0

            # 正式提交：键自动生成并回传；证据经 extract 切出精确子串；stance 逐条对应；JSON 字符串数组也接受。
            added = (await writer.call_tool("knowledge-object-add", {
                "title": "稳定身份", "kind": "concept", "aliases": '["身份稳定"]',
                "evidence": [locator, f"{chunk['id']}:5"], "stance": ["supports", "contradicts"],
                "reason": "意图工具端到端"})).data
            assert added["success"] and added["data"]["committed"], added
            key = added["data"]["request_key"]
            oid = added["data"]["refs"]["item"]["id"]
            assert (await reader.call_tool("knowledge-request", {"params": {"request_key": key}})).data["refs"]["item"]["id"] == oid
            record = (await reader.call_tool("knowledge-get", {"params": {"kind": "object", "id": oid}})).data
            assert record["record"]["aliases"] == ["身份稳定"]
            quotes = {item["quote"]: item["stance"] for item in record["evidence"]["items"]}
            assert quotes == {"增量维护依赖稳定身份。": "supports", "第三行反方观点": "contradicts"}
            assert all(item["location_status"] == "current" for item in record["evidence"]["items"])

            # 同参数再提交是新键新记录，不复用旧回执（意图工具不幂等）。
            again = (await writer.call_tool("knowledge-object-add", {"title": "稳定身份", "kind": "entity"})).data
            assert again["success"] and again["data"]["request_key"] != key
            other = again["data"]["refs"]["item"]["id"]

            # update：只改一个字段，其他保留；版本错给 VERSION_CONFLICT。
            renamed = (await writer.call_tool("knowledge-object-update", {
                "id": oid, "expected_revision": 1, "title": "稳定身份（修订）"})).data
            assert renamed["success"] and renamed["data"]["objects"][0]["submitted_fields"] == ["title"]
            stale = (await writer.call_tool("knowledge-object-update", {
                "id": oid, "expected_revision": 1, "summary": "过期"})).data
            assert not stale["success"] and stale["error"]["code"] == "VERSION_CONFLICT"
            record = (await reader.call_tool("knowledge-get", {"params": {"kind": "object", "id": oid}})).data["record"]
            assert record["title"] == "稳定身份（修订）" and record["aliases"] == ["身份稳定"] and record["revision"] == 2

            # 图谱 Update 要求至少一个待改字段：只补证据被拒（与模型契约一致）。
            only_evidence = (await writer.call_tool("knowledge-object-update", {
                "id": oid, "expected_revision": 2, "evidence": [f"{chunk['id']}:3"]})).data
            assert not only_evidence["success"] and only_evidence["error"]["code"] == "INVALID_ARGUMENT"

            # 先给实体挂证据；这不能替代关系本身的证据。
            object_evidence = _batch(evidence=[{
                "object": {"id": oid}, "source_kind": "piece", "stance": "supports",
                "source_library_id": library_id, "source_file_id": imported["file_id"],
                "source_chunk_id": chunk["id"], "quote": "增量维护依赖稳定身份。",
            }])
            object_attached = (await writer.call_tool("knowledge-apply", {"params": object_evidence})).data
            assert object_attached["success"]

            # 实体证据不会自动算作关系证据；lint 应明确报告缺少关系自身的证据。
            related = (await writer.call_tool("knowledge-relation-add", {
                "source": oid, "predicate": "related_to", "target": other,
                "description": "同名不同类型，需要区分", "basis": "user_statement"})).data
            assert related["success"] and related["data"]["refs"]["item"]["kind"] == "relation"
            rid = related["data"]["refs"]["item"]["id"]
            initial_lint = (await reader.call_tool("knowledge-lint", {"params": {}})).data
            assert any(issue["code"] == "RELATION_WITHOUT_EVIDENCE" and issue["relation_id"] == rid
                       for issue in initial_lint["issues"])
            # 高层更新只接受有字段改动；纯补关系证据走 apply，owner 必须明确是 relation。
            only_relation_evidence = (await writer.call_tool("knowledge-relation-update", {
                "id": rid, "expected_revision": 1, "evidence": [f"{chunk['id']}:3"]})).data
            assert not only_relation_evidence["success"] and only_relation_evidence["error"]["code"] == "INVALID_ARGUMENT"
            relation_evidence = _batch(evidence=[{
                "relation": {"id": rid}, "source_kind": "piece", "stance": "context",
                "source_library_id": library_id, "source_file_id": imported["file_id"],
                "source_chunk_id": chunk["id"], "quote": "增量维护依赖稳定身份。",
            }])
            attached = (await writer.call_tool("knowledge-apply", {"params": relation_evidence})).data
            assert attached["success"] and attached["data"]["evidence"][0]["action"] == "created"
            attached_lint = (await reader.call_tool("knowledge-lint", {"params": {}})).data
            assert not any(issue["code"] == "RELATION_WITHOUT_EVIDENCE" and issue["relation_id"] == rid
                           for issue in attached_lint["issues"])

            # 相同端点/谓词/qualifier 但内容不同 → KNOWLEDGE_CONFLICT，回执带已有关系摘要和可直接调用的修订入口。
            conflict = (await writer.call_tool("knowledge-relation-add", {
                "source": oid, "predicate": "related_to", "target": other,
                "description": "另一种说法", "basis": "inference"})).data
            assert not conflict["success"] and conflict["error"]["code"] == "KNOWLEDGE_CONFLICT"
            assert conflict["data"]["id"] == rid
            existing = conflict["data"]["existing"]
            assert existing["id"] == rid and existing["revision"] == 1 and existing["predicate"] == "related_to"
            assert existing["description"] == "同名不同类型，需要区分" and existing["basis"] == "user_statement"
            assert "related_to" in existing["display"] and "稳定身份（修订）" in existing["display"]
            assert "related_to" in conflict["message"] and "revision 1" in conflict["message"]
            assert conflict["data"]["next_tool"]["name"] == "knowledge-relation-update"
            assert conflict["data"]["next_tool"]["arguments"] == {"id": rid, "expected_revision": 1}
            # 内容完全相同 → reused，不报冲突。
            same = (await writer.call_tool("knowledge-relation-add", {
                "source": oid, "predicate": "related_to", "target": other,
                "description": "同名不同类型，需要区分", "basis": "user_statement"})).data
            assert same["success"] and same["data"]["relations"][0]["action"] == "reused"

            # relation update：按冲突回执给的 id/expected_revision 修订；只改一个字段，端点与谓词保留。
            revised = (await writer.call_tool("knowledge-relation-update", {
                **conflict["data"]["next_tool"]["arguments"], "description": "另一种说法", "basis": "inference",
                "evidence": [f"{chunk['id']}:3"], "stance": "context"})).data
            assert revised["success"] and revised["data"]["committed"], revised
            assert sorted(revised["data"]["relations"][0]["submitted_fields"]) == ["basis", "description"]
            relation = (await reader.call_tool("knowledge-get", {"params": {"kind": "relation", "id": rid}})).data
            assert relation["record"]["description"] == "另一种说法" and relation["record"]["basis"] == "inference"
            assert relation["record"]["predicate"] == "related_to" and relation["record"]["revision"] == 2
            assert {relation["record"]["source_id"], relation["record"]["target_id"]} == {oid, other}
            assert relation["evidence"]["total"] == 2
            assert {item["stance"] for item in relation["evidence"]["items"]} == {"context"}
            # 过期版本拒绝；空更新拒绝。
            stale_relation = (await writer.call_tool("knowledge-relation-update", {
                "id": rid, "expected_revision": 1, "status": "disputed"})).data
            assert not stale_relation["success"] and stale_relation["error"]["code"] == "VERSION_CONFLICT"
            empty_relation = (await writer.call_tool("knowledge-relation-update", {"id": rid, "expected_revision": 2})).data
            assert not empty_relation["success"] and empty_relation["error"]["code"] == "INVALID_ARGUMENT"

            # knowledge-get 省略 kind：按 UUID 自动探测，响应报告实际类型。
            by_uuid = (await reader.call_tool("knowledge-get", {"params": {"id": rid}})).data
            assert by_uuid["kind"] == "relation" and by_uuid["record"]["id"] == rid
            assert (await reader.call_tool("knowledge-get", {"params": {"id": oid}})).data["kind"] == "object"
            evidence_id = relation["evidence"]["items"][0]["id"]
            assert (await reader.call_tool("knowledge-get", {"params": {"id": evidence_id}})).data["kind"] == "evidence"
            unknown = await reader.call_tool("knowledge-get", {"params": {"id": str(uuid4())}}, raise_on_error=False)
            assert unknown.is_error and "NOT_FOUND" in unknown.content[0].text

            # 歧义证据：拒绝写入，并给出可直接调用的下一步工具。
            ambiguous = (await writer.call_tool("knowledge-object-add", {
                "title": "不该写入", "evidence": [f"{chunk['id']}:grep=行"]})).data
            assert not ambiguous["success"] and ambiguous["error"]["code"] == "EVIDENCE_SELECTION_REQUIRED"
            assert ambiguous["data"]["match_count"] >= 2
            assert ambiguous["data"]["next_tool"] == {
                "name": "query_chunk_info", "arguments": {"chunk_id": chunk["id"]},
                "hint": ambiguous["data"]["next_tool"]["hint"]}
            # 非法定位在任何写入前拒绝。
            bad = (await writer.call_tool("knowledge-object-add", {"title": "x", "evidence": ["abc:1"]})).data
            assert not bad["success"] and bad["error"]["code"] == "INVALID_ARGUMENT"
            # 空更新被拒。
            empty = (await writer.call_tool("knowledge-object-update", {"id": oid, "expected_revision": 3})).data
            assert not empty["success"] and empty["error"]["code"] == "INVALID_ARGUMENT"
            assert (await reader.call_tool("knowledge-list", {"params": {}})).data["total"] == 2

            # wiki：同一套组装逻辑，页面与图实体彼此独立；链接需明确使用稳定 UUID URI。
            page = (await wiki_writer.call_tool("wiki-page-add", {
                "title": "稳定身份页", "kind": "topic", "body": "# 稳定身份\n\n见 [其他页](piece://wiki/00000000-0000-0000-0000-000000000001)。",
                "evidence": locator})).data
            assert page["success"] and page["data"]["committed"], page
            pid = page["data"]["pages"][0]["id"]
            got = (await wiki_writer.call_tool("wiki-get", {"params": {"kind": "page", "id": pid}})).data
            assert got["record"]["body"].startswith("# 稳定身份") and got["evidence"]["total"] == 1
            # wiki-get 省略 kind：页面与证据都按 UUID 探测。
            assert (await wiki_writer.call_tool("wiki-get", {"params": {"id": pid}})).data["kind"] == "page"
            page_evidence_id = got["evidence"]["items"][0]["id"]
            by_evidence = (await wiki_writer.call_tool("wiki-get", {"params": {"id": page_evidence_id}})).data
            assert by_evidence["kind"] == "evidence" and by_evidence["record"]["page_id"] == pid
            missing_page = await wiki_writer.call_tool("wiki-get", {"params": {"id": str(uuid4())}}, raise_on_error=False)
            assert missing_page.is_error and "NOT_FOUND" in missing_page.content[0].text
            updated = (await wiki_writer.call_tool("wiki-page-update", {
                "id": pid, "expected_revision": got["record"]["revision"],
                "expected_content_hash": got["record"]["content_hash"], "summary": "补摘要"})).data
            assert updated["success"] and updated["data"]["committed"], updated
            stale_page = (await wiki_writer.call_tool("wiki-page-update", {
                "id": pid, "expected_revision": got["record"]["revision"],
                "expected_content_hash": got["record"]["content_hash"], "summary": "过期"})).data
            assert not stale_page["success"]
            assert (await reader.call_tool("knowledge-list", {"params": {}})).data["total"] == 2, "写页不建图"

            # query_files 与 query_file_info 同形：文件属性以解码后的 properties 返回，不暴露原始 metadata JSON 串。
            listed = (await index_client.call_tool("query_files", {"name": "意图来源"})).data
            assert listed["success"] and listed["data"]["total"] == 1
            [entry] = listed["data"]["files"]
            assert "metadata" not in entry and isinstance(entry["properties"], dict)
            detail = (await index_client.call_tool("query_file_info", {"file_id": entry["id"]})).data["data"]
            assert "metadata" not in detail and detail["properties"] == entry["properties"]
            return oid

    oid = asyncio.run(scenario())
    assert _post(api, "knowledge/get", kind="object", id=oid)["record"]["title"] == "稳定身份（修订）"


def test_index_mcp_lists_file_chunks_outline_and_text(api, knowledge_base, mcp_servers):
    """索引 MCP 的 list_file_chunks：默认只给大纲，include_text 才带正文；供分批读文档。"""
    from fastmcp import Client
    index, retrieval, graph, wiki_server = mcp_servers
    imported = _post(api, "file/import-markdown", filename="分批来源",
                     content="# 分批来源\n\n## 甲\n\n甲段正文内容\n\n## 乙\n\n乙段正文内容")
    knowledge_base.drain()
    file_id = imported["file_id"]

    async def scenario():
        async with Client(index.mcp) as index_client:
            outline = (await index_client.call_tool("list_file_chunks", {"file_id": file_id})).data
            assert outline["success"], outline
            data = outline["data"]
            assert data["file_id"] == file_id and data["total"] >= 2
            assert [c["chunk_index"] for c in data["chunks"]] == sorted(c["chunk_index"] for c in data["chunks"])
            assert all("chunk_text" not in c for c in data["chunks"])
            assert {"id", "chunk_index", "doc_title", "heading_path", "heading_level"} <= set(data["chunks"][0])
            # include_text 才带正文
            with_text = (await index_client.call_tool("list_file_chunks", {"file_id": file_id, "include_text": True})).data
            assert all("chunk_text" in c and c["chunk_text"] for c in with_text["data"]["chunks"])
            # 分页：page_size=1 时 total_pages 覆盖全部
            paged = (await index_client.call_tool("list_file_chunks", {"file_id": file_id, "page": 1, "page_size": 1})).data
            assert len(paged["data"]["chunks"]) == 1 and paged["data"]["total_pages"] == data["total"]
            # 不存在的文件如实失败
            missing = (await index_client.call_tool("list_file_chunks", {"file_id": 999999})).data
            assert not missing["success"]

    asyncio.run(scenario())


def test_mcp_intent_and_cli_build_identical_payloads(api, knowledge_base):
    """两条入口共用 knowledge_intent：同样的扁平输入必须组装出相同的 apply 批次。"""
    from indexing.services import knowledge_intent as intent
    imported = _post(api, "file/import-markdown", filename="同构来源", content="# 来源\n\n唯一引文")
    knowledge_base.drain()
    chunk = _post(api, "chunk/list", file_id=imported["file_id"])["chunks"][0]
    evidence = intent.resolve_evidence([f"{chunk['id']}:grep=唯一引文"])
    assert len(evidence) == 1 and evidence[0]["quote"] == "唯一引文" and evidence[0]["source_kind"] == "piece"
    built = intent.build_intent("object", "add", {"kind": "concept", "title": "T", "summary": None},
                                evidence, None, reason="r")
    assert built == {"reason": "r", "objects": [{"kind": "concept", "title": "T", "ref": "item"}],
                     "evidence": [{**evidence[0], "object": {"ref": "item"}, "stance": "supports"}]}
    relation = intent.build_intent("relation", "update", {"predicate": "supports"}, [], ["context"],
                                   record_id="00000000-0000-0000-0000-0000000000b2", expected_revision=2)
    assert relation["relations"] == [{"predicate": "supports", "id": "00000000-0000-0000-0000-0000000000b2",
                                      "expected_revision": 2}]
    assert relation["reason"] == "intent relation update"
    with pytest.raises(BusinessError):
        intent.build_intent("object", "add", {"title": "T"}, evidence, ["supports", "context"])


def test_cli_save_request_lands_in_data_dir_requests(api, http_cli, knowledge_base):
    code, added = http_cli("graph", "object", "add", "--kind", "entity", "--title", "留档实体", "--save-request")
    assert code == 0 and added["success"]
    saved = Path(added["data"]["request_file"])
    assert saved.parent == (Path(knowledge_base.settings.data_path) / "requests").resolve()
    assert saved.name.startswith("graph-object-add-") and saved.suffix == ".json"
    wrapper = json.loads(saved.read_text(encoding="utf-8"))
    assert wrapper["request"]["request_key"] == added["data"]["request_key"]
    code, replay = http_cli("graph", "request", "--input", saved, "--read-back")
    assert code == 0 and replay["data"]["read_back"]["complete"]


def test_cli_apply_stdin_without_request_id_generates_key(api, http_cli, monkeypatch):
    batch = {"reason": "stdin 批次", "objects": [{"ref": "a", "kind": "concept", "title": "stdin 概念"}]}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(batch, ensure_ascii=False)))
    code, result = http_cli("graph", "apply", "--input", "-", "--read-back")
    assert code == 0 and result["success"] and result["data"]["committed"] is True
    key = result["data"]["request_key"]
    assert key and _post(api, "knowledge/request", request_key=key)["refs"]["a"]["id"]
    assert _post(api, "knowledge/list")["total"] == 1


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
    code, result = http_cli("graph", "apply", "--input", batch_path, "--request-id", batch["request_key"])
    assert code == 1 and not result["success"]
    assert result["error"]["code"] == "SOURCE_CHANGED"
    # 整批回滚：对象不能半写，也不缓存请求结果。
    assert _post(api, "knowledge/list")["total"] == 0
    assert api.post("/api/v1/knowledge/request", json={"request_key": batch["request_key"]}).status_code == 404


def test_cli_apply_identical_retry_reuses_ids_and_counts(api, http_cli, tmp_path):
    batch = _batch(objects=[{"ref": "item", "kind": "concept", "title": "幂等概念"}])
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
    code, first = http_cli("graph", "apply", "--input", batch_path, "--request-id", batch["request_key"])
    code2, second = http_cli("graph", "apply", "--input", batch_path, "--request-id", batch["request_key"])
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
    code, result = http_cli("graph", "apply", "--input", batch_path, "--request-id", batch["request_key"])
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
    code, made = http_cli("graph", "apply", "--input", source, "--read-back")
    assert code == 0 and made["data"]["read_back"]["total"] == 1
    record = made["data"]["read_back"]["records"][0]
    assert record["submitted_fields"] == ["basis", "description", "predicate", "qualifier", "source", "status", "target"]
    assert record["record"]["display"] == "增量维护 —part_of→ 稳定身份"
    rid = record["record"]["id"]

    request_file = tmp_path / "description-update.json"
    code, changed = http_cli("graph", "relation", "update", rid, "--expected-revision", 1,
                            "--description", "增量维护依赖稳定身份", "--request-file", request_file)
    assert code == 0 and changed["data"]["read_back"]["complete"]
    read = changed["data"]["read_back"]["records"][0]
    assert read["submitted_fields"] == ["description"]
    assert read["record"]["display"] == "增量维护 —part_of→ 稳定身份"
    assert read["record"]["source_id"] == a and read["record"]["target_id"] == b
    assert read["record"]["predicate"] == "part_of" and read["record"]["revision"] == 2
    code, recovered = http_cli("graph", "request", "--input", request_file, "--read-back")
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
    code, result = http_cli("graph", "object", "add", "--kind", "concept", "--title", "应回滚",
                            "--evidence-file", evidence, "--request-file", request_file)
    assert code == 1 and result["error"]["code"] == "SOURCE_CHANGED"
    assert _post(api, "knowledge/list")["total"] == 0
    assert result["data"]["next_argv"][-4:] == ["chunk", "get", str(chunk_id), "--json"]
    saved = json.loads(request_file.read_text(encoding="utf-8"))["request"]
    assert api.post("/api/v1/knowledge/request", json={"request_key": saved["request_key"]}).status_code == 404


def test_cli_new_request_can_recreate_deleted_object_without_reusing_old_receipt(api, http_cli, tmp_path):
    args = ("graph", "object", "add", "--kind", "concept", "--title", "同样内容")
    first_file = tmp_path / "first.json"
    code, first = http_cli(*args, "--request-file", first_file)
    assert code == 0
    first_id = first["data"]["objects"][0]["id"]
    preview = _post(api, "knowledge/delete", kind="object", id=first_id, expected_revision=1, dry_run=True)
    _post(api, "knowledge/delete", kind="object", id=first_id, expected_revision=1,
          impact_token=preview["impact_token"], request_key="delete-first", dry_run=False, confirmed=True)
    code, deleted = http_cli("graph", "request", "delete-first", "--read-back")
    assert code == 0 and deleted["data"]["read_back"]["records"] == [
        {"kind": "object", "id": first_id, "deleted": True}]

    code, second = http_cli(*args, "--request-file", tmp_path / "second.json")
    assert code == 0 and second["data"]["objects"][0]["id"] != first_id
    assert second["data"]["request_key"] != first["data"]["request_key"]
    assert second["data"]["read_back"]["records"][0]["has_evidence"] is False
    assert _post(api, "knowledge/list")["total"] == 1
    code, replay = http_cli("graph", "apply", "--input", first_file, "--read-back")
    assert code == 1 and replay["error"]["code"] == "READ_BACK_INCOMPLETE"
    assert replay["data"]["committed"] is True and _post(api, "knowledge/list")["total"] == 1


@pytest.mark.parametrize("kind", ["page", "evidence"])
def test_cli_wiki_delete_receipt_reads_back_deleted_actions(api, http_cli, kind):
    made = _post(api, "wiki/apply", request_key=str(uuid4()), reason="删除读回回归",
                 pages=[{"ref": "a", "kind": "topic", "title": "待删页", "body": "正文"}],
                 evidence=[{"page": {"ref": "a"}, "source_kind": "user", "quote": "待删证据"}])
    page_id = made["pages"][0]["id"]
    record_id = page_id if kind == "page" else made["evidence"][0]["id"]
    record = _post(api, "wiki/get", kind="page", id=page_id)["record"]
    payload = {"kind": kind, "id": record_id, "expected_content_hash": record["content_hash"]}
    if kind == "page":
        payload["expected_revision"] = record["revision"]
    preview = _post(api, "wiki/delete", **payload)
    key = str(uuid4())
    deleted = _post(api, "wiki/delete", **payload, request_key=key,
                    impact_token=preview["impact_token"], dry_run=False, confirmed=True)
    assert deleted["committed"]
    code, result = http_cli("wiki", "request", key, "--read-back")
    assert code == 0 and result["data"]["read_back"]["complete"]
    assert {"kind": kind, "id": record_id, "deleted": True} in result["data"]["read_back"]["records"]
