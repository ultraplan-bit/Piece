"""模块间隔离的本机 OpenAI 兼容嵌入替身；不访问外部服务。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest


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
    # 每次 fixture 实例拥有独立类，calls/fail 及测试打补丁的事件均不会跨模块共享。
    class Handler(_EmbeddingStub):
        calls = 0
        fail = False

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}/v1", handler=Handler)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
