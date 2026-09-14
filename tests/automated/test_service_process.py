"""真实短命 Piece 服务集成：源码入口、临时数据目录、本地嵌入替身，不接触默认知识库。

本文件启动一个真实 `piece serve --no-gui --no-mcp --no-tray` 子进程，因此需要：
- 环境变量 PIECE_TEST_EMBEDDING_PORT 之外的本机空闲端口（自动分配）；
- 嵌入服务替身：由本测试进程内的 HTTP 服务器提供 OpenAI 兼容的 /embeddings。
所有 fixture 都在 finally 中停止服务；服务未能安全退出时测试失败并报告 PID。
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _EmbeddingStub(BaseHTTPRequestHandler):
    calls = 0
    fail = False

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        type(self).calls += 1
        if type(self).fail:
            self.send_response(500)
            self.end_headers()
            return
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        data = [{"object": "embedding", "index": i, "embedding": [1.0, 0.0]} for i, _ in enumerate(inputs)]
        raw = json.dumps({"object": "list", "data": data, "model": body.get("model", "stub"),
                          "usage": {"prompt_tokens": 1, "total_tokens": 1}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture(scope="module")
def embedding_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}/v1", handler=_EmbeddingStub)
    finally:
        server.shutdown()
        server.server_close()


def _cli(data_dir, port, *args, stdin=None, timeout=60):
    env = {**os.environ, "PYTHONUTF8": "1"}
    command = [sys.executable, "-m", "app.cli", *args, "--data-dir", str(data_dir), "--port", str(port), "--json"]
    if stdin is None:
        # 不继承终端 stdin：缺确认参数的命令必须直接失败，而不是等待输入。
        result = subprocess.run(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    else:
        result = subprocess.run(command, cwd=ROOT, env=env, input=stdin, capture_output=True, timeout=timeout)
    stdout = result.stdout.decode("utf-8")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        payload = None
    return SimpleNamespace(code=result.returncode, data=payload, stdout=stdout, stderr=result.stderr.decode("utf-8"))


@pytest.fixture
def service(tmp_path, embedding_stub):
    data_dir = tmp_path / "知识库 目录"
    data_dir.mkdir()
    port = _free_port()
    init = _cli(data_dir, port, "config", "init")
    assert init.code == 0, init.stderr
    assert not (data_dir / "kb.db").exists()
    patch = {"embedding": {"base_url": embedding_stub.url, "api_key": "stub-key", "vector_dim": 2},
             "mcp": {"port": _free_port()}}
    updated = _cli(data_dir, port, "config", "update", "--offline", "--input", "-", stdin=json.dumps(patch).encode("utf-8"))
    assert updated.code == 0, updated.stderr
    log = open(tmp_path / "serve.log", "wb")
    process = subprocess.Popen([sys.executable, "-m", "app.cli", "serve", "--no-gui", "--no-mcp", "--no-tray",
                                "--data-dir", str(data_dir), "--port", str(port)],
                               cwd=ROOT, env={**os.environ, "PYTHONUTF8": "1"}, stdout=log, stderr=subprocess.STDOUT,
                               creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    handle = SimpleNamespace(data_dir=data_dir, port=port, process=process, log=tmp_path / "serve.log",
                             cli=lambda *args, **kw: _cli(data_dir, port, *args, **kw))
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = handle.cli("status")
            if status.code == 0:
                break
            assert process.poll() is None, handle.log.read_text(encoding="utf-8", errors="replace")
            time.sleep(0.3)
        else:
            pytest.fail("服务未在 60 秒内就绪：" + handle.log.read_text(encoding="utf-8", errors="replace"))
        yield handle
    finally:
        if process.poll() is None:
            stop = _cli(data_dir, port, "stop")
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                log.close()
                pytest.fail(f"服务未在 60 秒内安全停止（stop={stop.data}）：" + handle.log.read_text(encoding="utf-8", errors="replace"))
        log.close()
        assert process.returncode == 0, handle.log.read_text(encoding="utf-8", errors="replace")


def test_import_wait_search_and_export_roundtrip(service, tmp_path):
    source = tmp_path / "资料 论文.md"
    source.write_text("# 方法\n\n研究方法正文，包含限制说明。\n\n## 结论\n\n结论正文。", encoding="utf-8")
    created = service.cli("collection", "create", "论文")
    assert created.code == 0
    imported = service.cli("file", "import", str(source), "--collection", "论文", "--wait", "--timeout", "60")
    assert imported.code == 0, imported.stdout
    file_id = imported.data["data"]["items"][0]["response"]["data"]["file_id"]
    listed = service.cli("file", "list")
    assert listed.code == 0 and listed.data["data"]["total"] == 1
    found = service.cli("search", "研究方法", "--collection", "论文")
    assert found.code == 0 and found.data["data"]["candidates"][0]["file_id"] == file_id
    chunk_id = found.data["data"]["candidates"][0]["chunk_id"]
    chunk = service.cli("chunk", "get", str(chunk_id))
    assert chunk.code == 0 and "研究方法" in chunk.data["data"]["chunk_text"]
    duplicate = service.cli("file", "import", str(source))
    assert duplicate.code == 0 and duplicate.data["data"]["duplicate_count"] == 1
    output = tmp_path / "导出"
    output.mkdir()
    exported = service.cli("file", "export", str(file_id), "--format", "markdown", "--output", str(output))
    assert exported.code == 0 and Path(exported.data["data"]["path"]).read_text(encoding="utf-8").startswith("#")
    again = service.cli("file", "export", str(file_id), "--format", "markdown", "--output", str(output))
    assert again.code == 1 and again.data["error"]["code"] == "ALREADY_EXISTS"
    assert source.is_file()


def test_note_batch_cards_partial_failure_and_readback(service, tmp_path):
    note = service.cli("file", "create", "项目 笔记")
    assert note.code == 0
    file_id = note.data["data"]["file_id"]
    cards = tmp_path / "cards.json"
    cards.write_text(json.dumps([{"doc_title": "研究结论", "chunk_text": "必要正文"},
                                 {"doc_title": " ", "chunk_text": "无标题"}], ensure_ascii=False), encoding="utf-8")
    batch = service.cli("chunk", "batch-add", str(file_id), "--input", str(cards), "--request-id", "batch-1", "--wait", "--timeout", "60")
    assert batch.code == 1 and batch.data["error"]["code"] == "PARTIAL_FAILURE"
    task_ids = batch.data["data"]["task_ids"]
    assert len(task_ids) == 1
    waited = service.cli("task", "wait", str(task_ids[0]), "--timeout", "60")
    assert waited.code == 0
    chunk_id = waited.data["data"]["last_result"]["data"]["tasks"][0]["result"]["chunk_id"]
    replay = service.cli("chunk", "batch-add", str(file_id), "--input", str(cards), "--request-id", "batch-1")
    assert replay.data["data"]["task_ids"] == task_ids
    lookup = service.cli("task", "list", "--request-id", "batch-1")
    assert lookup.code == 0 and [t["id"] for t in lookup.data["data"]["tasks"]] == task_ids
    readback = service.cli("chunk", "get", str(chunk_id))
    assert readback.code == 0 and readback.data["data"]["doc_title"] == "研究结论"
    missing = service.cli("task", "wait", "999999", "--timeout", "5")
    assert missing.code == 1 and missing.data["error"]["code"] == "NOT_FOUND"
    denied = service.cli("file", "delete", str(file_id))
    assert denied.code == 1 and denied.data["error"]["code"] == "CONFIRMATION_REQUIRED"
    preview = service.cli("file", "delete", str(file_id), "--dry-run")
    assert preview.code == 0 and preview.data["data"]["chunks_count"] == 1


def test_wrong_credentials_target_and_second_instance(service, tmp_path):
    env_key = {**os.environ, "PIECE_API_KEY": "wrong-key", "PYTHONUTF8": "1"}
    result = subprocess.run([sys.executable, "-m", "app.cli", "file", "list", "--data-dir", str(service.data_dir),
                             "--port", str(service.port), "--json"], cwd=ROOT, env=env_key, capture_output=True, timeout=60)
    assert result.returncode == 1 and json.loads(result.stdout)["error"]["code"] == "UNAUTHORIZED"
    other = tmp_path / "另一个库"
    other.mkdir()
    assert _cli(other, service.port, "config", "init").code == 0
    mismatch = _cli(other, service.port, "file", "list")
    assert mismatch.code == 1 and mismatch.data["error"]["code"] == "TARGET_MISMATCH"
    assert not (other / "kb.db").exists()
    second = subprocess.run([sys.executable, "-m", "app.cli", "serve", "--no-gui", "--no-mcp", "--no-tray",
                             "--data-dir", str(service.data_dir), "--port", str(_free_port()), "--json"],
                            cwd=ROOT, env={**os.environ, "PYTHONUTF8": "1"}, capture_output=True, timeout=120)
    assert second.returncode == 1
    assert "INSTANCE_BUSY" in second.stdout.decode("utf-8") or "独占" in second.stdout.decode("utf-8")
    offline = service.cli("config", "update", "--offline", "--input", "-", stdin=b'{"appearance": {"theme": "dark"}}')
    assert offline.code == 1 and offline.data["error"]["code"] == "INSTANCE_BUSY"


def test_shutdown_waits_for_inflight_index_task(service, tmp_path):
    source = tmp_path / "大文档.md"
    source.write_text("\n\n".join(f"# 段落 {i}\n\n内容 {i}" for i in range(40)), encoding="utf-8")
    imported = service.cli("file", "import", str(source))
    assert imported.code == 0
    task_id = imported.data["data"]["task_ids"][0]
    stop = service.cli("stop")
    assert stop.code == 0
    service.process.wait(timeout=60)
    assert service.process.returncode == 0
    import sqlite3
    connection = sqlite3.connect(service.data_dir / "kb.db")
    try:
        status = connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
        assert status in {"completed", "failed", "pending"}
        assert connection.execute("SELECT COUNT(*) FROM tasks WHERE status = 'processing'").fetchone()[0] == 0
    finally:
        connection.close()
    assert not (service.data_dir / "files" / ".staging").exists() or not list((service.data_dir / "files" / ".staging").iterdir())
