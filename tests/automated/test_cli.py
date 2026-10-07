"""命令/标准流/HTTP 客户端合同：短命本机替身，不启动业务服务或调用外网。"""

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from app import cli
from app.client import ClientError, PieceClient, _content_disposition_filename, make_envelope, target_id


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    monkeypatch.delenv("PIECE_API_KEY", raising=False)
    config = tmp_path / "配置"
    config.mkdir()
    (config / "config.json").write_text(json.dumps({"api": {"admin_key": "a" * 32}, "data_path": str(config)}), encoding="utf-8")
    state = SimpleNamespace(config=config, calls=[], routes={})
    state.identity = {"application": "Piece", "api_version": 1, "ready": True,
                      "target_id": target_id(config, config / "kb.db")}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            payload = json.loads(raw) if raw else None
            state.calls.append({"path": self.path, "payload": payload, "headers": dict(self.headers)})
            try:
                if self.path in state.routes:
                    value = state.routes[self.path](self, payload)
                elif self.path == "/api/v1/handshake":
                    value = make_envelope(True, "identity", state.identity)
                else:
                    value = make_envelope(True, "ok", {})
                if value is None:
                    return
                raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.port = server.server_port
    state.client = lambda: PieceClient(port=state.port, config_dir=config, db_path=config / "kb.db", api_key="a" * 32)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def invoke(endpoint, capsys, *args):
    try:
        code = cli.main([*args, "--data-dir", str(endpoint.config), "--port", str(endpoint.port), "--json"])
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def failure(code, data=None):
    return make_envelope(False, code, data, {"code": code, "message": code})


def complete(ids):
    return make_envelope(True, "done", {"tasks": [{"id": i, "status": "completed"} for i in ids],
        "all_done": True, "all_succeeded": True, "not_found": [], "poll_after_ms": 0})


def test_file_name_and_stats_commands(endpoint, capsys):
    code, result, _ = invoke(endpoint, capsys, "file", "list", "--name", "100%_中文", "--collection", "论文")
    assert code == 0 and result["success"]
    assert endpoint.calls[-1]["payload"]["name"] == "100%_中文"
    assert endpoint.calls[-1]["payload"]["collections"] == ["论文"]
    code, result, _ = invoke(endpoint, capsys, "stats")
    assert code == 0 and endpoint.calls[-1]["path"] == "/api/v1/storage/stats"


def test_collection_tree_parent_move_and_direct_scope_commands(endpoint, capsys):
    code, _, _ = invoke(endpoint, capsys, "collection", "create", "子集合", "--parent-id", "3")
    assert code == 0 and endpoint.calls[-1]["payload"] == {"name": "子集合", "parent_id": 3}
    code, _, _ = invoke(endpoint, capsys, "collection", "tree")
    assert code == 0 and endpoint.calls[-1]["path"] == "/api/v1/collection/tree"
    code, _, _ = invoke(endpoint, capsys, "collection", "move", "4", "--root")
    assert code == 0 and endpoint.calls[-1]["payload"] == {"collection_id": 4, "parent_id": None}
    code, _, _ = invoke(endpoint, capsys, "collection", "move", "4", "--parent-id", "5")
    assert code == 0 and endpoint.calls[-1]["payload"] == {"collection_id": 4, "parent_id": 5}
    for command in (("file", "list"), ("search", "方法")):
        code, _, _ = invoke(endpoint, capsys, *command, "--collection", "父集合")
        assert code == 0 and endpoint.calls[-1]["payload"]["include_descendants"] is True
        code, _, _ = invoke(endpoint, capsys, *command, "--collection", "父集合", "--direct-only")
        assert code == 0 and endpoint.calls[-1]["payload"]["include_descendants"] is False
    code, _, _ = invoke(endpoint, capsys, "file", "list", "--uncategorized", "--sort-by", "filename", "--ascending")
    assert code == 0
    payload = endpoint.calls[-1]["payload"]
    assert payload["uncategorized"] and payload["sort_by"] == "filename" and not payload["descending"]
    for args in (("collection", "move", "4"), ("collection", "move", "4", "--root", "--parent-id", "5")):
        code, _, _ = invoke(endpoint, capsys, *args)
        assert code == 2


@pytest.mark.parametrize("args,path,payload", [
    (("list", "--kind", "entity", "--status", "disputed", "--limit", "2", "--offset", "3"), "list",
     {"kind": "entity", "status": "disputed", "limit": 2, "offset": 3}),
    (("search", "中文别名"), "search", {"query": "中文别名", "limit": 50, "offset": 0}),
    (("get", "object", "uuid"), "get", {"kind": "object", "id": "uuid", "limit": 50, "offset": 0}),
    (("graph", "uuid", "--depth", "2", "--predicate", "supports", "--status", "active"), "graph",
     {"root_id": "uuid", "depth": 2, "predicates": ["supports"], "statuses": ["active"], "max_nodes": 100, "max_edges": 300}),
    (("references", "library", "7"), "references", {"source_library_id": "library", "source_file_id": 7, "limit": 50, "offset": 0}),
    (("lint", "--object-id", "a", "--object-id", "b"), "lint", {"object_ids": ["a", "b"], "limit": 50, "offset": 0}),
    (("history", "relation", "uuid"), "history", {"kind": "relation", "id": "uuid", "limit": 50, "offset": 0}),
    (("request", "stable-key"), "request", {"request_key": "stable-key"}),
])
def test_graph_read_commands_map_to_http(endpoint, capsys, args, path, payload):
    code, result, _ = invoke(endpoint, capsys, "graph", *args)
    assert code == 0 and result["success"]
    assert endpoint.calls[-1]["path"] == f"/api/v1/knowledge/{path}"
    assert endpoint.calls[-1]["payload"] == payload


@pytest.mark.parametrize("args,payload", [
    (("extract", "7", "--lines", "12-14"), {"chunk_id": 7, "lines": "12-14", "max_matches": 20}),
    (("extract", "7", "--grep", "关键句", "--context", "2", "--regex"),
     {"chunk_id": 7, "grep": "关键句", "regex": True, "context": 2, "max_matches": 20}),
])
def test_chunk_extract_maps_to_http(endpoint, capsys, args, payload):
    code, result, _ = invoke(endpoint, capsys, "chunk", *args)
    assert code == 0 and result["success"]
    assert endpoint.calls[-1]["path"] == "/api/v1/chunk/extract"
    assert endpoint.calls[-1]["payload"] == payload


def test_chunk_extract_requires_one_locator(endpoint, capsys):
    for args in (("extract", "7"), ("extract", "7", "--lines", "1", "--grep", "x")):
        code, _, _ = invoke(endpoint, capsys, "chunk", *args)
        assert code == 2 and endpoint.calls == []


def test_graph_apply_preserves_input_and_rejects_unsafe_arguments(endpoint, capsys, monkeypatch, tmp_path):
    payload = {"reason": "增量", "objects": [{"id": "uuid", "expected_revision": 1, "summary": "", "aliases": []}]}
    source = tmp_path / "batch.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    code, _, _ = invoke(endpoint, capsys, "graph", "apply", "--input", str(source), "--dry-run")
    assert code == 0 and endpoint.calls[-1]["payload"] == {**payload, "dry_run": True}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code, _, _ = invoke(endpoint, capsys, "graph", "apply", "--input", "-", "--request-id", "stable")
    assert code == 0 and endpoint.calls[-1]["payload"] == {**payload, "request_key": "stable"}
    endpoint.calls.clear()
    code, _, _ = invoke(endpoint, capsys, "graph", "apply", "--input", str(source))
    assert code == 2
    source.write_text(json.dumps({**payload, "request_key": "original"}), encoding="utf-8")
    code, _, _ = invoke(endpoint, capsys, "graph", "apply", "--input", str(source), "--request-id", "different")
    assert code == 2
    code, _, _ = invoke(endpoint, capsys, "graph", "apply", "--input", str(source), "--wait")
    assert code == 2 and endpoint.calls == []


def test_graph_delete_defaults_preview_and_requires_original_token(endpoint, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    code, _, _ = invoke(endpoint, capsys, "graph", "delete", "evidence", "uuid")
    assert code == 0 and endpoint.calls[-1]["payload"] == {"kind": "evidence", "id": "uuid", "dry_run": True}
    endpoint.calls.clear()
    for args in (("--yes",), ("--impact-token", "a" * 64, "--yes"),
                 ("--impact-token", "a" * 64, "--request-id", "delete-key")):
        code, _, _ = invoke(endpoint, capsys, "graph", "delete", "evidence", "uuid", *args)
        assert code in (1, 2) and endpoint.calls == []
    code, _, _ = invoke(endpoint, capsys, "graph", "delete", "object", "uuid", "--expected-revision", "2",
                         "--impact-token", "a" * 64, "--request-id", "delete-key", "--yes")
    assert code == 0 and endpoint.calls[-1]["payload"] == {
        "kind": "object", "id": "uuid", "expected_revision": 2, "impact_token": "a" * 64,
        "request_key": "delete-key", "dry_run": False, "confirmed": True}


def test_graph_lost_response_uses_knowledge_request_not_tasks(endpoint, capsys, tmp_path):
    source = tmp_path / "batch.json"
    source.write_text(json.dumps({"reason": "保存", "objects": [{"ref": "a", "kind": "entity", "title": "x"}]}), encoding="utf-8")
    def lose_response(handler, payload):
        handler.connection.shutdown(socket.SHUT_RDWR)
        handler.connection.close()
    endpoint.routes["/api/v1/knowledge/apply"] = lose_response
    code, result, _ = invoke(endpoint, capsys, "graph", "apply", "--input", str(source), "--request-id", "lost-key")
    assert code == 3 and result["data"]["outcome_unknown"]
    assert result["data"]["request_key"] == "lost-key"
    assert result["data"]["recovery"] == {"command": "piece graph request", "request_id": "lost-key",
        "same_target_required": True, "do_not_resubmit": False, "retry_identical_only": True}
    assert len([call for call in endpoint.calls if call["path"].endswith("knowledge/apply")]) == 1


def test_scan_wait_preserves_partial_failure(endpoint, capsys):
    endpoint.routes["/api/v1/file/scan"] = lambda *a: failure("PARTIAL_FAILURE", {"task_ids": [7], "failed_count": 1})
    endpoint.routes["/api/v1/task/query"] = lambda *a: complete([7])
    code, result, _ = invoke(endpoint, capsys, "file", "scan", "--wait")
    assert code == 1 and result["error"]["code"] == "PARTIAL_FAILURE"
    assert result["data"]["task_ids"] == [7]
    assert endpoint.calls[-1]["path"] == "/api/v1/task/query"


def test_scan_preview_and_invalid_arguments(endpoint, capsys):
    code, _, _ = invoke(endpoint, capsys, "file", "scan", "--dry-run")
    assert code == 0 and endpoint.calls[-1]["payload"] == {"dry_run": True}
    endpoint.calls.clear()
    for args in (("file", "scan", "--dry-run", "--wait"), ("file", "list", "--name", "  "),
                 ("start", "--open", "--no-gui")):
        code, result, _ = invoke(endpoint, capsys, *args)
        assert code == 2 and result["error"]["code"] == "INVALID_ARGUMENT"
    assert endpoint.calls == []


def test_start_dispatch_and_defaults(endpoint, capsys, monkeypatch):
    from app import background
    calls = []
    def start(client, **kwargs):
        calls.append((client, kwargs))
        return make_envelope(True, "ready", {"pid": 123})
    monkeypatch.setattr(background, "start_service", start)
    code, result, _ = invoke(endpoint, capsys, "start")
    assert code == 0 and result["data"]["pid"] == 123
    assert calls[-1][0].config_dir == endpoint.config
    assert calls[-1][1] == {"timeout": 60, "with_gui": False, "with_mcp": False, "open_ui": False}
    code, _, _ = invoke(endpoint, capsys, "start", "--gui", "--mcp", "--timeout", "12")
    assert code == 0 and calls[-1][1]["with_gui"] and calls[-1][1]["with_mcp"]
    assert calls[-1][1]["timeout"] == 12


@pytest.mark.parametrize("error,expected", [("TARGET_MISMATCH", 1), ("PROTOCOL_ERROR", 1),
    ("NOT_PIECE", 1), ("VERSION_MISMATCH", 1), ("FORBIDDEN", 1), ("CONNECTION_FAILED", 1),
    ("SERVICE_UNAVAILABLE", 3), ("NOT_READY", 3), ("WAIT_TIMEOUT", 4), ("INVALID_INPUT", 2)])
def test_exit_codes_for_envelopes(endpoint, capsys, error, expected):
    endpoint.routes["/api/v1/status"] = lambda *a: failure(error)
    code, result, _ = invoke(endpoint, capsys, "status")
    assert code == expected and result["error"]["code"] == error


@pytest.mark.parametrize("field,value,error", [("application", "AnotherApp", "NOT_PIECE"),
    ("api_version", 999, "VERSION_MISMATCH"), ("target_id", "other-library", "TARGET_MISMATCH")])
def test_wrong_peer_never_receives_credentials(endpoint, capsys, field, value, error):
    endpoint.identity[field] = value
    code, result, _ = invoke(endpoint, capsys, "file", "list")
    assert code == 1 and result["error"]["code"] == error
    assert len(endpoint.calls) == 1
    assert "Authorization" not in endpoint.calls[0]["headers"]


def test_unconfigured_service_does_not_create_files(tmp_path, capsys):
    target = tmp_path / "not-created"
    code = cli.main(["status", "--data-dir", str(target), "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 3 and "piece serve" in result["message"]
    assert not target.exists()


@pytest.mark.parametrize("args", [["--help"], ["file", "--help"], ["--version", "--json"], ["doctor", "--json"], ["skill", "list", "--json"]])
def test_light_commands_utf8_and_no_initialization(tmp_path, args):
    target = tmp_path / "unused"
    env = {**os.environ, "PIECE_DATA_DIR": str(target), "PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"}
    result = subprocess.run([sys.executable, "-m", "app.cli", *args], env=env, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout.decode("utf-8")
    assert not target.exists()


def test_confirmation_prompt_stays_on_stderr(endpoint, capsys, monkeypatch):
    stdin = io.StringIO("yes\n")
    monkeypatch.setattr(stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdin", stdin)
    code, result, stderr = invoke(endpoint, capsys, "file", "delete", "7")
    assert code == 0 and result["success"]
    assert "yes" in stderr
    assert endpoint.calls[-1]["payload"]["confirmed"] is True


def test_noninteractive_confirmation_fails_without_request(endpoint, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    code, result, _ = invoke(endpoint, capsys, "file", "delete", "7")
    assert code == 1 and result["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert endpoint.calls == []


def test_wait_keeps_query_error_and_task_ids(endpoint, capsys):
    endpoint.routes["/api/v1/task/query"] = lambda *a: failure("NOT_FOUND", {"not_found": [99]})
    code, result, _ = invoke(endpoint, capsys, "task", "wait", "99", "--timeout", "1")
    assert code == 1 and result["error"]["code"] == "NOT_FOUND"
    assert result["data"]["task_ids"] == [99]
    assert len(endpoint.calls) == 2


def test_multi_batch_query_stops_and_keeps_earlier_results(endpoint, capsys):
    def query(handler, payload):
        return complete(payload["task_ids"]) if payload["task_ids"][0] == 1 else failure("FORBIDDEN")
    endpoint.routes["/api/v1/task/query"] = query
    code, result, _ = invoke(endpoint, capsys, "task", "wait", *map(str, range(1, 251)))
    assert code == 1 and result["error"]["code"] == "FORBIDDEN"
    assert result["data"]["task_ids"] == list(range(1, 251))
    assert result["data"]["last_result"]["data"]["results"][0]["data"]["all_succeeded"]
    assert len(endpoint.calls) == 3


def test_wait_timeout_includes_handshake_and_all_batches(endpoint):
    def delayed(handler, payload):
        time.sleep(0.18)
        return complete(payload["task_ids"])
    endpoint.routes["/api/v1/task/query"] = delayed
    initial = make_envelope(True, "accepted", {"task_ids": list(range(1, 202))})
    started = time.monotonic()
    result, code = cli._with_wait(endpoint.client(), initial, SimpleNamespace(wait=True, timeout=0.28))
    assert code == 4 and result["error"]["code"] == "WAIT_TIMEOUT"
    assert time.monotonic() - started < 0.9
    assert len(result["data"]["task_ids"]) == 201
    assert result["data"]["last_result"]["data"]["partial_query"]["partial_results"]


def test_deadline_interrupts_slow_response_headers(endpoint):
    def slow(handler, payload):
        handler.wfile.write(b"HTTP/1.0 200 OK\r\nX-Slow: ")
        for _ in range(12):
            handler.wfile.write(b"x")
            handler.wfile.flush()
            time.sleep(0.06)
    endpoint.routes["/api/v1/task/query"] = slow
    started = time.monotonic()
    result, code = cli._with_wait(endpoint.client(), make_envelope(True, "accepted", {"task_ids": [1]}),
                                  SimpleNamespace(wait=True, timeout=0.2))
    assert code == 4 and time.monotonic() - started < 0.7
    assert result["data"]["task_ids"] == [1]


def test_wait_interrupt_does_not_cancel_tasks(endpoint, monkeypatch):
    client = endpoint.client()
    monkeypatch.setattr(client, "post", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(ClientError) as caught:
        cli._with_wait(client, make_envelope(True, "accepted", {"task_ids": [5], "request_key": "save-me"}),
                       SimpleNamespace(wait=True, timeout=10))
    assert caught.value.code == "WAIT_INTERRUPTED"
    assert caught.value.data["task_ids"] == [5] and caught.value.data["request_key"] == "save-me"
    assert endpoint.calls == []


def test_lost_acceptance_preserves_request_key_without_replay(endpoint, capsys, tmp_path):
    body = tmp_path / "正文.txt"
    body.write_text("中文正文", encoding="utf-8")
    def disconnect(handler, payload):
        handler.connection.shutdown(socket.SHUT_RDWR)
        handler.connection.close()
    endpoint.routes["/api/v1/chunk/add"] = disconnect
    code, result, _ = invoke(endpoint, capsys, "chunk", "add", "3", "--title", "标题", "--input", str(body))
    assert code == 3 and result["data"]["outcome_unknown"]
    key = result["data"]["request_key"]
    assert key and result["data"]["recovery"]["request_id"] == key
    assert endpoint.calls[-1]["payload"]["request_key"] == key
    assert len([c for c in endpoint.calls if c["path"] == "/api/v1/chunk/add"]) == 1


def test_import_disconnect_distinguishes_unknown_and_unsubmitted(endpoint, capsys, tmp_path):
    sources = [tmp_path / f"资料 {i}.md" for i in range(3)]
    for source in sources:
        source.write_text("text", encoding="utf-8")
    def import_file(handler, payload):
        if payload["path"] == str(sources[0]):
            return make_envelope(True, "accepted", {"file_id": 1, "task_ids": [7]})
        handler.connection.shutdown(socket.SHUT_RDWR)
        handler.connection.close()
    endpoint.routes["/api/v1/file/import"] = import_file
    code, result, _ = invoke(endpoint, capsys, "file", "import", *map(str, sources))
    assert code == 3
    data = result["data"]
    assert data["task_ids"] == [7] and len(data["items"]) == 2
    assert data["unknown"] == [str(sources[1])]
    assert data["not_submitted"] == [str(sources[2])]


@pytest.mark.parametrize("header,expected", [
    ("attachment; filename*=utf-8''" + quote("中文 文件.md"), "中文 文件.md"),
    ("attachment; filename=plain.md; filename*=utf-8''" + quote("中文.md"), "中文.md"),
    ('attachment; filename="../escape.md"', None),
    ('attachment; filename="C:\\\\escape.md"', None),
    ('attachment; filename="CON"', None),
    ("attachment; filename*=utf-8''..%2Fescape", None),
])
def test_safe_content_disposition(header, expected):
    assert _content_disposition_filename(header) == expected


def test_download_chinese_directory_and_no_overwrite_race(endpoint, tmp_path, monkeypatch):
    output = tmp_path / "导出"
    output.mkdir()
    destination = output / "中文.md"
    def download(handler, payload):
        handler.send_response(200)
        handler.send_header("Content-Disposition", "attachment; filename*=utf-8''" + quote(destination.name))
        handler.send_header("Content-Length", "4")
        handler.end_headers()
        handler.wfile.write(b"data")
    endpoint.routes["/download"] = download
    real_link = os.link
    def race(source, dest):
        dest.write_text("other writer", encoding="utf-8")
        real_link(source, dest)
    monkeypatch.setattr(os, "link", race)
    with pytest.raises(ClientError, match="未覆盖"):
        endpoint.client().download("/download", output)
    assert destination.read_text() == "other writer"
    assert not list(output.glob("*.tmp"))
    monkeypatch.setattr(os, "link", real_link)
    result = endpoint.client().download("/download", output, overwrite=True)
    assert result["success"] and destination.read_bytes() == b"data"


def test_truncated_download_keeps_existing_output(endpoint, tmp_path):
    output = tmp_path / "output.md"
    output.write_text("keep me", encoding="utf-8")
    def truncated(handler, payload):
        handler.send_response(200)
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        handler.wfile.write(b"short")
        handler.wfile.flush()
        handler.close_connection = True
    endpoint.routes["/download"] = truncated
    with pytest.raises(ClientError):
        endpoint.client().download("/download", output, overwrite=True)
    assert output.read_text() == "keep me"
    assert not list(tmp_path.glob(".piece-download-*"))


def test_download_http_failure_is_not_success(endpoint, capsys, tmp_path):
    endpoint.routes["/api/v1/file/9/content?format=markdown"] = lambda *a: failure("NOT_FOUND")
    code, result, _ = invoke(endpoint, capsys, "file", "export", "9", "--output", str(tmp_path / "missing.md"))
    assert code == 1 and not result["success"]
    assert not (tmp_path / "missing.md").exists()


def test_cli_utf8_stdin_without_environment_override(endpoint):
    endpoint.routes["/api/v1/chunk/add"] = lambda handler, payload: make_envelope(True, "已受理", {"received": payload["chunk_text"]})
    env = {**os.environ, "PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"}
    result = subprocess.run([sys.executable, "-m", "app.cli", "chunk", "add", "1", "--title", "中文", "--input", "-",
        "--data-dir", str(endpoint.config), "--port", str(endpoint.port), "--json"],
        input="中文正文 🧩".encode("utf-8"), capture_output=True, env=env, timeout=10)
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert json.loads(result.stdout)["data"]["received"] == "中文正文 🧩"


def test_import_markdown_reads_stdin_and_waits(endpoint):
    endpoint.routes["/api/v1/file/import-markdown"] = lambda *a: make_envelope(True, "已受理", {"file_id": 3, "task_ids": [8]})
    endpoint.routes["/api/v1/task/query"] = lambda *a: complete([8])
    properties = endpoint.config / "props.json"
    properties.write_text(json.dumps({"source_url": "https://example.com/a"}), encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"}
    result = subprocess.run([sys.executable, "-m", "app.cli", "file", "import-markdown", "网页文章", "--input", "-",
        "--properties", str(properties), "--collection", "网页收藏", "--wait",
        "--data-dir", str(endpoint.config), "--port", str(endpoint.port), "--json"],
        input="# 标题\n\n正文 🧩".encode("utf-8"), capture_output=True, env=env, timeout=10)
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert json.loads(result.stdout)["data"]["task_ids"] == [8]
    call = next(c for c in endpoint.calls if c["path"] == "/api/v1/file/import-markdown")
    assert call["payload"] == {"filename": "网页文章", "content": "# 标题\n\n正文 🧩", "collections": ["网页收藏"],
                               "properties": {"source_url": "https://example.com/a"}}


def test_import_markdown_rejects_two_stdin_inputs(endpoint, capsys):
    code, result, _ = invoke(endpoint, capsys, "file", "import-markdown", "文章", "--input", "-", "--properties", "-")
    assert code == 2 and result["error"]["code"] == "INVALID_ARGUMENT"
    assert not [c for c in endpoint.calls if c["path"] == "/api/v1/file/import-markdown"]


def test_unsupported_sync_dry_run_is_rejected(endpoint, capsys):
    code, result, _ = invoke(endpoint, capsys, "sync", "run", "--dry-run")
    assert code == 2 and result["error"]["code"] == "INVALID_ARGUMENT"
    assert endpoint.calls == []


def test_import_checks_linked_ancestors_and_subdirectories(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "sub").mkdir()
    file = root / "sub" / "file.md"
    file.write_text("data", encoding="utf-8")
    alias = tmp_path / "alias"
    if sys.platform == "win32":
        result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(alias), str(root)], capture_output=True)
        assert result.returncode == 0
    else:
        alias.symlink_to(root, target_is_directory=True)
    try:
        candidates, skipped, _ = cli.import_candidates([alias / "sub" / "file.md", root], False)
        assert not candidates
        assert len(skipped) == 2
        candidates, skipped, _ = cli.import_candidates([root], True)
        assert candidates == [file] and not skipped
    finally:
        if sys.platform == "win32":
            alias.rmdir()
        else:
            alias.unlink()


def test_logs_mcp_config_and_search_diagnostics_contracts(endpoint, capsys):
    """召回诊断、日志与 MCP 配置生成：参数按合同传给服务端。"""
    code, result, _ = invoke(endpoint, capsys, "logs", "--level", "ERROR", "--limit", "5")
    assert code == 0 and result["success"] is True
    assert endpoint.calls[-1]["path"] == "/api/v1/logs"
    assert endpoint.calls[-1]["payload"] == {"level": "ERROR", "limit": 5}

    code, result, _ = invoke(endpoint, capsys, "zotero", "preview", "--zotero-collection", "ABCD1234", "--collection-mode", "top")
    assert code == 0 and endpoint.calls[-1]["path"] == "/api/v1/zotero/preview"
    assert endpoint.calls[-1]["payload"] == {"collection_keys": ["ABCD1234"], "collection_mode": "top", "base_url": None}

    endpoint.routes["/api/v1/zotero/import"] = lambda *a: make_envelope(True, "accepted", {"task_ids": [5], "accepted_count": 1})
    endpoint.routes["/api/v1/task/query"] = lambda *a: complete([5])
    code, result, _ = invoke(endpoint, capsys, "zotero", "import", "--collection", "论文", "--wait")
    assert code == 0 and result["success"] and result["data"]["task_ids"] == [5]
    import_call = next(c for c in endpoint.calls if c["path"] == "/api/v1/zotero/import")
    assert import_call["payload"] == {"collection_keys": None, "collection_mode": "path", "base_url": None, "collections": ["论文"]}

    body = endpoint.config / "props.json"
    body.write_text(json.dumps({"source": "obsidian"}), encoding="utf-8")
    note = endpoint.config / "笔记.md"
    note.write_text("正文", encoding="utf-8")
    endpoint.routes["/api/v1/file/import"] = lambda *a: make_envelope(True, "accepted", {"file_id": 1, "task_ids": [9]})
    code, result, _ = invoke(endpoint, capsys, "file", "import", str(note), "--properties", str(body))
    assert code == 0 and result["data"]["excluded"] == []
    assert endpoint.calls[-1]["payload"] == {"path": str(note), "collections": None, "properties": {"source": "obsidian"}}

    code, result, _ = invoke(endpoint, capsys, "mcp-config", "--service", "index")
    assert code == 0 and result["success"] is True
    assert endpoint.calls[-1]["path"] == "/api/v1/mcp-config"
    assert endpoint.calls[-1]["payload"] == {"service": "index", "include_secrets": False}

    code, result, _ = invoke(endpoint, capsys, "search", "查询 范围", "--diagnostics")
    assert code == 0 and result["success"] is True
    assert endpoint.calls[-1]["path"] == "/api/v1/search"
    assert endpoint.calls[-1]["payload"]["query"] == "查询 范围"
    assert endpoint.calls[-1]["payload"]["diagnostics"] is True


def test_task_cancel_and_retry_contracts(endpoint, capsys):
    """任务取消/重试：取消直接提交，重试带请求键保护并可等待。"""
    code, result, _ = invoke(endpoint, capsys, "task", "cancel", "7")
    assert code == 0 and result["success"] is True
    assert endpoint.calls[-1]["path"] == "/api/v1/task/cancel"
    assert endpoint.calls[-1]["payload"] == {"task_id": 7}

    code, result, _ = invoke(endpoint, capsys, "task", "retry", "9")
    assert code == 0 and result["success"] is True
    assert endpoint.calls[-1]["path"] == "/api/v1/task/retry"
    assert endpoint.calls[-1]["payload"] == {"task_id": 9}


def test_skill_commands_stay_local(endpoint, capsys, tmp_path):
    """piece skill 是纯本地命令：不请求服务端口，config.json 缺失只警告不阻断。"""
    missing_dir = tmp_path / "not-created"
    out_dir = tmp_path / "skills-out"

    # 列出：目标未配置 → 警告入 data.warnings，不创建目录
    code = cli.main(["skill", "list", "--data-dir", str(missing_dir), "--port", "1", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["success"] is True
    assert [s["id"] for s in result["data"]["skills"]] == ["piece-graph", "piece-index", "piece-search", "piece-wiki"]
    assert result["data"]["warnings"] and "未找到该目标的配置" in result["data"]["warnings"][0]
    assert not missing_dir.exists()

    # 已配置的目标不告警（endpoint 的数据目录带 config.json）
    code = cli.main(["skill", "list", "--data-dir", str(endpoint.config), "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["data"]["warnings"] == []

    # 导出全部（不传 ID）：头部注入数据目录与端口，绝对路径只写一次
    code = cli.main(["skill", "export", "--dir", str(out_dir),
                     "--data-dir", str(missing_dir), "--port", "8691", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["success"] is True
    assert result["data"]["exported"] == [
        str(out_dir / "piece-graph" / "SKILL.md"),
        str(out_dir / "piece-index" / "SKILL.md"),
        str(out_dir / "piece-search" / "SKILL.md"),
        str(out_dir / "piece-wiki" / "SKILL.md"),
    ]
    assert not missing_dir.exists()  # 数据目录仍未创建
    content = (out_dir / "piece-search" / "SKILL.md").read_text(encoding="utf-8")
    assert content.count(f'--data-dir "{missing_dir.resolve().as_posix()}"') == 1
    assert "--port 8691" in content

    # 全程未请求服务端口（端口 1 上没有任何监听）
    assert endpoint.calls == []

    # 再次导出默认拒绝；--overwrite 显式覆盖
    code = cli.main(["skill", "export", "--dir", str(out_dir),
                     "--data-dir", str(missing_dir), "--port", "8691", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["error"]["code"] == "TARGET_EXISTS"
    code = cli.main(["skill", "export", "--dir", str(out_dir),
                     "--data-dir", str(missing_dir), "--port", "8691", "--overwrite", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["success"] is True

    # 不存在的 ID 属参数错误
    code = cli.main(["skill", "export", "--dir", str(tmp_path / "other"), "ghost",
                     "--data-dir", str(missing_dir), "--port", "8691", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 2 and result["error"]["code"] == "SKILL_NOT_FOUND"


def test_skill_warning_goes_to_stderr_without_json(tmp_path, capsys):
    code = cli.main(["skill", "list", "--data-dir", str(tmp_path / "conf")])
    captured = capsys.readouterr()
    assert code == 0
    assert "未找到该目标的配置" in captured.err


def test_import_excludes_hidden_patterns_and_link_notes(tmp_path):
    """Obsidian vault 导入：隐藏目录默认跳过，--exclude 按段匹配，纯链接笔记可选跳过。"""
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    (vault / "templates").mkdir()
    (vault / "templates" / "daily.md").write_text("{{date}}", encoding="utf-8")
    (vault / "notes").mkdir()
    note = vault / "notes" / "想法.md"
    note.write_text("---\ntitle: 想法\n---\n# 想法\n\n这是一段真正的正文，讨论 [[另一篇]] 和 [[第三篇]] 与 [[第四篇]] 的关系。"
                    "原子笔记通常只有几百字，但已经足以承载一个完整的想法，导入后一篇对应一张卡片，"
                    "这正是知识库最理想的粒度，不应被当作索引页排除掉。", encoding="utf-8")
    moc = vault / "MOC.md"
    moc.write_text("# 索引\n\n- [[想法]]\n- [[另一篇]]\n- [[第三篇]]\n", encoding="utf-8")
    (vault / "notes" / "draft.md").write_text("草稿", encoding="utf-8")

    candidates, skipped, excluded = cli.import_candidates([vault], True)
    assert not skipped
    assert set(candidates) == {moc, vault / "notes" / "draft.md", note, vault / "templates" / "daily.md"}
    assert [e["reason"] for e in excluded] == ["hidden"]
    assert excluded[0]["path"] == str(vault / ".obsidian")

    candidates, _, excluded = cli.import_candidates(
        [vault], True, exclude=["templates", "notes/draft*"], skip_link_notes=True, include_hidden=True)
    assert set(candidates) == {note, vault / ".obsidian" / "app.json"}
    reasons = {e["path"]: e["reason"] for e in excluded}
    assert reasons[str(vault / "templates")] == "pattern: templates"
    assert reasons[str(vault / "notes" / "draft.md")] == "pattern: notes/draft*"
    assert reasons[str(moc)] == "link_note"
    assert str(vault / ".obsidian") not in reasons
    # 显式指定单个文件不受目录排除规则约束，但仍受链接笔记判定
    candidates, _, excluded = cli.import_candidates([vault / "templates" / "daily.md", moc], False,
                                                     exclude=["templates"], skip_link_notes=True)
    assert candidates == [vault / "templates" / "daily.md"] and [e["reason"] for e in excluded] == ["link_note"]
