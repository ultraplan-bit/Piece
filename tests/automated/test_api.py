"""真实 ASGI/SQLite 的本地 API 合同；不调用外部模型。"""

import base64
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import create_api
from app.runtime import BootstrapTokens


def test_identity_permissions_and_origin(api, knowledge_base):
    handshake = api.get("/api/v1/handshake").json()["data"]
    assert handshake["application"] == "Piece" and handshake["api_version"] == 1
    assert "key" not in str(handshake)
    assert api.get("/api/v1/status", headers={"Authorization": ""}).status_code == 401
    assert api.get("/api/v1/status", headers={"X-Piece-Target": "another-library"}).json()["error"]["code"] == "TARGET_MISMATCH"
    read = {"Authorization": "Bearer read-test-key"}
    write = {"Authorization": "Bearer write-test-key"}
    assert api.post("/api/v1/file/list", json={}, headers=read).status_code == 200
    assert api.post("/api/v1/file/create", json={"filename": "拒绝"}, headers=read).status_code == 403
    assert api.post("/api/v1/config/show", json={}, headers=write).status_code == 403
    assert api.post("/api/v1/file/import", json={"path": "anything.md"}, headers=write).status_code == 403
    assert api.get("/api/v1/status", headers={"Origin": "http://evil.invalid"}).status_code == 403
    assert api.get("/api/v1/handshake", headers={"Host": "evil.invalid:8689"}).status_code == 403
    assert api.get("/working/anything.md").status_code == 404
    shown = api.post("/api/v1/config/show", json={}).json()
    assert shown["success"] and shown["data"]["config"]["api"]["admin_key"] == "***"
    assert knowledge_base.settings.api.admin_key not in str(shown)


def test_api_write_read_mcp_shared_result(api, knowledge_base):
    created = api.post("/api/v1/file/create", json={"filename": "跨入口"}).json()["data"]
    file_id = created["file_id"]
    result = api.post("/api/v1/chunk/batch-add", json={"file_id": file_id, "request_key": "api-batch", "chunks": [
        {"doc_title": "研究结论", "chunk_text": "必要正文"}, {"doc_title": "", "chunk_text": "无标题"}]}).json()
    assert not result["success"] and result["error"]["code"] == "PARTIAL_FAILURE"
    task_ids = result["data"]["task_ids"]
    assert len(task_ids) == 1
    pending = api.post("/api/v1/task/query", json={"task_ids": task_ids}).json()["data"]
    assert not pending["all_done"]
    assert "input" not in pending["tasks"][0]
    knowledge_base.drain()
    complete = api.post("/api/v1/task/get", json={"task_id": task_ids[0]}).json()["data"]
    chunk_id = complete["result"]["chunk_id"]
    from indexing.mcp.tools.query_tools import get_chunk_info
    assert get_chunk_info(chunk_id)["data"]["chunk_text"] == "必要正文"
    from indexing.mcp.tools.chunk_tools import update_chunk_content
    changed = update_chunk_content(chunk_id, "从 MCP 更新")
    assert changed["success"]
    knowledge_base.drain()
    assert api.post("/api/v1/chunk/get", json={"chunk_id": chunk_id}).json()["data"]["chunk_text"] == "从 MCP 更新"
    from indexing.mcp.tools.task_tools import get_task_status
    assert get_task_status(task_ids[0])["data"]["chunk_id"] == chunk_id
    candidates = api.post("/api/v1/search", json={"query": "研究结论"}).json()["data"]["candidates"]
    assert candidates[0]["chunk_id"] == chunk_id and "chunk_text" not in candidates[0]
    preview = api.post("/api/v1/file/delete", json={"file_ids": [file_id], "dry_run": True}).json()
    assert preview["data"]["chunks_count"] == 1
    assert api.post("/api/v1/file/delete", json={"file_ids": [file_id]}).json()["error"]["code"] == "CONFIRMATION_REQUIRED"


@pytest.mark.parametrize("payload", [{"file_id": True}, {"file_id": -1}, {"file_id": "1"}, {"file_id": 1, "surprise": 2}])
def test_strict_arguments(api, payload):
    result = api.post("/api/v1/file/get", json=payload)
    assert result.status_code == 400
    assert result.json()["error"]["code"] == "INVALID_INPUT"


def test_missing_tasks_and_gui_disabled(api):
    result = api.post("/api/v1/task/query", json={"task_ids": [932]}).json()
    assert not result["success"] and result["data"]["not_found"] == [932]
    assert not result["data"]["all_done"]
    assert api.post("/api/v1/window/open", json={}).json()["error"]["code"] == "GUI_DISABLED"


def test_browser_authentication_and_static_boundary(knowledge_base):
    runtime = SimpleNamespace(port=8689, ready=True, with_gui=True, status=lambda: {}, open_window=lambda: None,
                              database_path=knowledge_base.settings.get_db_path().resolve(),
                              bootstrap_tokens=BootstrapTokens())
    app = create_api(runtime)
    @app.get("/working/sample.md")
    def document():
        return {"document": "private"}
    with TestClient(app, base_url="http://127.0.0.1:8689") as client:
        denied = client.get("/working/sample.md")
        assert denied.status_code == 401 and "Basic" in denied.headers["www-authenticate"]
        credentials = base64.b64encode(f"piece:{knowledge_base.settings.api.admin_key}".encode()).decode()
        granted = client.get("/working/sample.md", headers={"Authorization": f"Basic {credentials}"})
        assert granted.status_code == 200
        assert "HttpOnly" in granted.headers["set-cookie"] and "SameSite=Strict" in granted.headers["set-cookie"]
        assert client.get("/working/sample.md").status_code == 200
        assert client.get("/working/sample.md", headers={"Origin": "http://evil.invalid"}).status_code == 403
        assert client.post("/anything").status_code == 403
        # GUI cookie 不能用于管理 API，更不能通过跨站表单调用 API。
        assert client.post("/api/v1/file/create", json={"filename": "不应创建"}).status_code == 401


def test_bootstrap_token_login_and_replay_rejected(knowledge_base):
    """一次性引导令牌：首次进入发放会话 cookie，重放与无效令牌回退 Basic。"""
    runtime = SimpleNamespace(port=8689, ready=True, with_gui=True, status=lambda: {}, open_window=lambda: None,
                              database_path=knowledge_base.settings.get_db_path().resolve(),
                              bootstrap_tokens=BootstrapTokens())
    app = create_api(runtime)

    @app.get("/working/sample.md")
    def document():
        return {"document": "private"}

    with TestClient(app, base_url="http://127.0.0.1:8689") as client:
        token = runtime.bootstrap_tokens.issue()
        granted = client.get(f"/bootstrap?token={token}", follow_redirects=False)
        assert granted.status_code == 303 and granted.headers["location"] == "/"
        cookie = granted.headers["set-cookie"]
        assert "piece_session=" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
        # 会话 cookie 可继续访问 GUI 静态路径，且 GUI cookie 不能调用管理 API。
        assert client.get("/working/sample.md", headers={"Cookie": cookie.split(";")[0]}).status_code == 200
        assert client.post("/api/v1/file/create", json={"filename": "不应创建"},
                           headers={"Cookie": cookie.split(";")[0]}).status_code == 401
        # 令牌单次消费：重放与未知令牌均回落 Basic 登录。
        replay = client.get(f"/bootstrap?token={token}", follow_redirects=False)
        assert replay.status_code == 401 and replay.headers["www-authenticate"].startswith("Basic")
        unknown = client.get("/bootstrap?token=not-a-real-token", follow_redirects=False)
        assert unknown.status_code == 401


def test_bootstrap_token_expiry_and_cross_origin(knowledge_base):
    """过期令牌拒绝；引导路径同样受同源与 Host 检查约束。"""
    runtime = SimpleNamespace(port=8689, ready=True, with_gui=True, status=lambda: {}, open_window=lambda: None,
                              database_path=knowledge_base.settings.get_db_path().resolve(),
                              bootstrap_tokens=BootstrapTokens(ttl=0.0))
    app = create_api(runtime)
    with TestClient(app, base_url="http://127.0.0.1:8689") as client:
        expired = client.get(f"/bootstrap?token={runtime.bootstrap_tokens.issue()}", follow_redirects=False)
        assert expired.status_code == 401
        cross = client.get(f"/bootstrap?token={runtime.bootstrap_tokens.issue()}",
                           headers={"Origin": "http://evil.invalid"}, follow_redirects=False)
        assert cross.status_code == 403 and cross.json()["error"]["code"] == "INVALID_ORIGIN"
