"""完整模式（GUI+MCP）真实服务测试：入口状态、GUI 认证边界、MCP 分服务认证与安全停止。

启动真实 `piece serve --no-tray` 子进程（GUI 与 MCP 均启用），全部使用临时数据目录
和动态端口。GUI 验证到 HTTP 层（Basic 登录、会话 cookie、来源与 Host 防护）；
浏览器内的 NiceGUI 交互（WebSocket、上传下载、关窗）仍属实机验收范围，不在本文件覆盖。
"""

import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
IGNORED_ENV = {"PIECE_DATA_DIR", "PIECE_API_KEY", "PIECE_INDEX_MCP_PORT",
               "NICEGUI_STORAGE_PATH", "PYTHONPATH", "VIRTUAL_ENV"}


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _child_env():
    env = {k: v for k, v in os.environ.items() if k.upper() not in IGNORED_ENV}
    env["PYTHONUTF8"] = "1"
    return env


def _http(port, path, headers=None, method="GET", body=None, host=None):
    """直接 HTTP 请求，返回 (状态码, 小写规范化响应头, 响应体)。"""
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    if host:
        request.add_header("Host", host)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()


def _cli(data_dir, port, *args, stdin=None, timeout=120):
    command = [sys.executable, "-m", "app.cli", *args,
               "--data-dir", str(data_dir), "--port", str(port), "--json"]
    if stdin is None:
        result = subprocess.run(command, cwd=ROOT, env=_child_env(),
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    else:
        result = subprocess.run(command, cwd=ROOT, env=_child_env(),
                                input=stdin, capture_output=True, timeout=timeout)
    payload = None
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except json.JSONDecodeError:
        pass
    return SimpleNamespace(code=result.returncode, data=payload,
                           stdout=result.stdout.decode("utf-8"), stderr=result.stderr.decode("utf-8"))


@pytest.fixture(scope="module")
def full_service(tmp_path_factory):
    """完整模式服务：GUI 与 MCP 启用，MCP 端口避开默认值。"""
    data_dir = tmp_path_factory.mktemp("fullmode") / "知识库 目录"
    data_dir.mkdir()
    port = _free_port()
    mcp_port = _free_port()
    assert _cli(data_dir, port, "config", "init").code == 0
    patch = {"mcp": {"port": mcp_port}}
    updated = _cli(data_dir, port, "config", "update", "--offline", "--input", "-",
                   stdin=json.dumps(patch).encode("utf-8"))
    assert updated.code == 0, updated.stderr
    config = json.loads((data_dir / "config.json").read_text("utf-8"))
    log = open(data_dir.parent / "serve.log", "wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "app.cli", "serve", "--no-tray",
         "--data-dir", str(data_dir), "--port", str(port)],
        cwd=ROOT, env=_child_env(), stdout=log, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    handle = SimpleNamespace(data_dir=data_dir, port=port, mcp_port=mcp_port,
                             process=process, log=data_dir.parent / "serve.log",
                             admin_key=config["api"]["admin_key"],
                             retrieval_key=config["mcp"]["api_key"],
                             index_key=config["mcp"]["index_api_key"],
                             cli=lambda *args, **kw: _cli(data_dir, port, *args, **kw))
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status = handle.cli("status")
            if status.code == 0:
                break
            assert process.poll() is None, handle.log.read_text(encoding="utf-8", errors="replace")
            time.sleep(0.5)
        else:
            pytest.fail("完整模式服务未在 90 秒内就绪：" + handle.log.read_text(encoding="utf-8", errors="replace"))
        yield handle
    finally:
        if process.poll() is None:
            handle.cli("stop")
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                log.close()
                pytest.fail("完整模式服务未安全停止：" + handle.log.read_text(encoding="utf-8", errors="replace"))
        log.close()
        assert process.returncode == 0, handle.log.read_text(encoding="utf-8", errors="replace")


def test_full_mode_status(full_service):
    status = full_service.cli("status")
    assert status.code == 0
    data = status.data["data"]
    assert data["ready"] is True
    assert data["with_gui"] is True and data["with_mcp"] is True


def test_gui_basic_auth_and_session_cookie(full_service):
    basic = "Basic " + base64.b64encode(f"piece:{full_service.admin_key}".encode()).decode()
    status, headers, _ = _http(full_service.port, "/")
    assert status == 401 and headers["www-authenticate"].startswith("Basic")
    status, _, _ = _http(full_service.port, "/", {"Authorization": "Basic " + base64.b64encode(b"piece:wrong").decode()})
    assert status == 401
    status, headers, _ = _http(full_service.port, "/", {"Authorization": basic})
    assert status == 200
    cookie = headers["set-cookie"]
    assert "piece_session=" in cookie and "httponly" in cookie.lower() and "samesite=strict" in cookie.lower()
    status, _, _ = _http(full_service.port, "/", {"Cookie": cookie.split(";")[0]})
    assert status == 200


def test_gui_bootstrap_token_flow(full_service):
    """piece open 一次性令牌：服务发窗成功，未知令牌回落 Basic，Basic 路径不受影响。"""
    opened = full_service.cli("open")
    assert opened.code == 0 and opened.data["success"] is True
    status, _, _ = _http(full_service.port, "/bootstrap")
    assert status == 401
    status, headers, _ = _http(full_service.port, "/bootstrap?token=forged-token")
    assert status == 401 and headers["www-authenticate"].startswith("Basic")
    # 令牌对客户端不可预测；真实开窗链路（生成 URL → 浏览器消费）在服务进程内，
    # 其合同由 test_api.py 的 BootstrapTokens 用例与这里的服务级 401 边界共同覆盖。
    basic = "Basic " + base64.b64encode(f"piece:{full_service.admin_key}".encode()).decode()
    status, _, _ = _http(full_service.port, "/", {"Authorization": basic})
    assert status == 200


def test_gui_rejects_cross_origin_and_wrong_host(full_service):
    basic = "Basic " + base64.b64encode(f"piece:{full_service.admin_key}".encode()).decode()
    status, _, body = _http(full_service.port, "/", {"Origin": "http://evil.example", "Authorization": basic})
    assert status == 403 and b"INVALID_ORIGIN" in body
    status, _, body = _http(full_service.port, "/", {"Authorization": basic}, host="evil.example:80")
    assert status == 403 and b"INVALID_HOST" in body


def _rpc(base_port, payload, token=None, session=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session:
        headers["Mcp-Session-Id"] = session
    return _http(base_port, "/mcp", headers, method="POST", body=json.dumps(payload).encode("utf-8"))


def test_mcp_auth_isolated_per_service(full_service):
    """MCP 工具级认证：无密钥、错误密钥、跨服务密钥一律拒绝；本服务密钥可用。

    initialize 属于协议握手不做认证（FastMCP 工具级中间件只覆盖 tools/list、tools/call），
    因此认证验证统一走 tools/list。
    """
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                             "clientInfo": {"name": "pytest", "version": "1.0"}}}
    tools_list = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    services = {"retrieval": (full_service.mcp_port, full_service.retrieval_key, full_service.index_key),
                "index": (full_service.mcp_port + 1, full_service.index_key, full_service.retrieval_key)}
    for label, (port, own_key, other_key) in services.items():
        status, headers, _ = _rpc(port, initialize, token=own_key)
        assert status == 200, f"{label} initialize 失败"
        session = headers["mcp-session-id"]
        assert session
        status, _, body = _rpc(port, tools_list, token=None, session=session)
        assert b"Unauthorized" in body, f"{label} 无密钥未被拒绝：{body[:200]!r}"
        status, _, body = _rpc(port, tools_list, token="wrong-key", session=session)
        assert b"Unauthorized" in body, f"{label} 错误密钥未被拒绝"
        status, _, body = _rpc(port, tools_list, token=other_key, session=session)
        assert b"Unauthorized" in body, f"{label} 跨服务密钥未被拒绝"
        status, _, body = _rpc(port, tools_list, token=own_key, session=session)
        assert status == 200 and b"tools" in body, f"{label} 本服务密钥不可用：{body[:200]!r}"
