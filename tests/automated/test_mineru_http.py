"""MinerU API v4 的 HTTP 契约：请求形状、认证头、状态机与错误码。

这层是和外部服务对齐的部分，字段名或请求头写错不会在本机报错，
只会在用户第一次解析时失败，所以按官方文档逐项钉住。
"""

import json

import httpx
import pytest

from indexing.services import mineru_client

BATCH_ID = "2bb2f0ec-0000-0000-0000-000000000000"


@pytest.fixture
def http(monkeypatch):
    """把客户端的 httpx 调用接到 MockTransport 上，并记录每个出站请求。"""
    seen: list[httpx.Request] = []
    real = httpx.Client
    # 轮询退避在测试里没有意义，去掉等待让状态机用例秒级跑完
    monkeypatch.setattr(mineru_client, "POLL_INTERVAL_INITIAL", 0.0)

    def factory(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            mineru_client.httpx, "Client", lambda **kwargs: real(transport=transport, **kwargs)
        )
        monkeypatch.setattr(
            mineru_client.httpx, "put",
            lambda url, **kwargs: real(transport=transport).put(url, **kwargs),
        )
        monkeypatch.setattr(
            mineru_client.httpx, "stream",
            lambda method, url, **kwargs: real(transport=transport).stream(method, url, **kwargs),
        )
        return transport

    return seen, factory


def _json_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(payload).encode("utf-8"))


def test_request_upload_uses_bearer_auth_and_returns_first_url(http):
    seen, factory = http
    factory(lambda request: (seen.append(request), _json_response({
        "code": 0, "data": {"batch_id": BATCH_ID, "file_urls": ["https://upload.invalid/1"]},
    }))[1])
    client = mineru_client.MineruClient("  secret-token  ")
    try:
        batch_id, upload_url = client.request_upload(
            "part-1-200.pdf", model_version="vlm", is_ocr=True, language="japan",
        )
    finally:
        client.close()

    assert (batch_id, upload_url) == (BATCH_ID, "https://upload.invalid/1")
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://mineru.net/api/v4/file-urls/batch"
    # Token 两端空白必须去掉，但 Bearer 前缀要保留（去掉会被判 A0202）
    assert request.headers["Authorization"] == "Bearer secret-token"
    # model_version / language 是整批一致的顶层参数，is_ocr 属于单个文件
    assert json.loads(request.content) == {
        "files": [{"name": "part-1-200.pdf", "is_ocr": True}],
        "model_version": "vlm",
        "language": "japan",
    }


def test_business_error_code_carries_actionable_hint(http):
    seen, factory = http
    factory(lambda request: _json_response({"code": "A0202", "msg": "token error", "data": None}))
    client = mineru_client.MineruClient("bad")
    try:
        with pytest.raises(mineru_client.MineruError) as excinfo:
            client.request_upload("a.pdf", model_version="vlm")
    finally:
        client.close()

    assert "A0202" in str(excinfo.value) and "Token" in str(excinfo.value)


def test_quota_error_is_reported_with_hint(http):
    _, factory = http
    factory(lambda request: _json_response({"code": -60018, "msg": "limit", "data": None}))
    client = mineru_client.MineruClient("token")
    try:
        with pytest.raises(mineru_client.MineruError, match="额度"):
            client.request_upload("a.pdf", model_version="vlm")
    finally:
        client.close()


def test_missing_upload_url_is_rejected(http):
    _, factory = http
    factory(lambda request: _json_response({"code": 0, "data": {"batch_id": BATCH_ID, "file_urls": []}}))
    client = mineru_client.MineruClient("token")
    try:
        with pytest.raises(mineru_client.MineruError, match="上传地址"):
            client.request_upload("a.pdf", model_version="vlm")
    finally:
        client.close()


def test_upload_sends_raw_body_without_extra_headers(http, tmp_path):
    """预签名链接对请求头敏感：多带 Authorization/Content-Type 会导致签名失败。"""
    seen, factory = http
    factory(lambda request: (seen.append(request), httpx.Response(200))[1])
    payload = b"%PDF-1.4 payload"
    source = tmp_path / "part.pdf"
    source.write_bytes(payload)

    mineru_client.MineruClient("token").upload("https://upload.invalid/1", source)

    request = seen[0]
    assert request.method == "PUT"
    assert request.content == payload
    # 流式上传必须显式给出长度，否则会退化成 chunked
    assert request.headers["Content-Length"] == str(len(payload))
    assert "authorization" not in {key.lower() for key in request.headers}
    assert "content-type" not in {key.lower() for key in request.headers}


def test_wait_batch_polls_until_done_and_reports_progress(http):
    seen, factory = http
    states = [
        {"file_name": "a.pdf", "state": "waiting-file", "err_msg": ""},
        {"file_name": "a.pdf", "state": "running", "err_msg": "",
         "extract_progress": {"extracted_pages": 3, "total_pages": 5}},
        {"file_name": "a.pdf", "state": "done", "err_msg": "",
         "full_zip_url": "https://cdn.invalid/result.zip"},
    ]
    calls = {"n": 0}

    def handler(request):
        seen.append(request)
        payload = states[min(calls["n"], len(states) - 1)]
        calls["n"] += 1
        return _json_response({"code": 0, "data": {"extract_result": [payload]}})

    factory(handler)
    progress = []
    client = mineru_client.MineruClient("token")
    try:
        zip_url = client.wait_batch(BATCH_ID, "a.pdf", progress_callback=lambda done, total: progress.append((done, total)))
    finally:
        client.close()

    assert zip_url == "https://cdn.invalid/result.zip"
    assert progress == [(3, 5)]
    # batch 级 code 恒为 0，必须按 file_name 在数组里找自己那一项
    assert len(seen) == 3
    assert str(seen[0].url).endswith(f"/extract-results/batch/{BATCH_ID}")


def test_wait_batch_raises_on_failed_item(http):
    _, factory = http
    factory(lambda request: _json_response({"code": 0, "data": {"extract_result": [
        {"file_name": "a.pdf", "state": "failed", "err_msg": "page limit exceeded"},
    ]}}))
    client = mineru_client.MineruClient("token")
    try:
        with pytest.raises(mineru_client.MineruError, match="page limit exceeded"):
            client.wait_batch(BATCH_ID, "a.pdf")
    finally:
        client.close()


def test_wait_batch_stops_on_cancel(http):
    _, factory = http
    factory(lambda request: _json_response({"code": 0, "data": {"extract_result": []}}))
    client = mineru_client.MineruClient("token")
    try:
        with pytest.raises(mineru_client.MineruError, match="取消"):
            client.wait_batch(BATCH_ID, "a.pdf", stop_check=lambda: True)
    finally:
        client.close()


def test_wait_batch_tolerates_transient_network_failure(http, monkeypatch):
    """单次网络抖动不应终止整份文档的解析。"""
    _, factory = http
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return _json_response({"code": 0, "data": {"extract_result": [
            {"file_name": "a.pdf", "state": "done", "full_zip_url": "https://cdn.invalid/r.zip"},
        ]}})

    factory(handler)
    monkeypatch.setattr(mineru_client, "POLL_INTERVAL_INITIAL", 0.0)
    client = mineru_client.MineruClient("token")
    try:
        assert client.wait_batch(BATCH_ID, "a.pdf") == "https://cdn.invalid/r.zip"
    finally:
        client.close()
    assert calls["n"] == 2


def test_download_zip_writes_file_and_cleans_up(http, tmp_path):
    _, factory = http
    factory(lambda request: httpx.Response(200, content=b"PK\x03\x04fake-zip"))
    dest = tmp_path / "result.zip"

    mineru_client.MineruClient("token").download_zip("https://cdn.invalid/r.zip", dest)

    assert dest.read_bytes() == b"PK\x03\x04fake-zip"
    assert not dest.with_suffix(".zip.tmp").exists()


def test_content_list_accepts_both_file_names(tmp_path):
    import zipfile

    for inner in ("doc_content_list.json", "content_list.json"):
        archive_path = tmp_path / f"{inner}.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(inner, json.dumps([{"type": "text", "text": "正文", "page_idx": 0}]))
        with zipfile.ZipFile(archive_path) as archive:
            blocks = mineru_client._read_content_list(archive)
        assert blocks == [{"type": "text", "text": "正文", "page_idx": 0}]


def test_content_list_missing_is_reported(tmp_path):
    import zipfile

    archive_path = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("full.md", "# 只有 Markdown")
    with zipfile.ZipFile(archive_path) as archive:
        with pytest.raises(mineru_client.MineruError, match="content_list"):
            mineru_client._read_content_list(archive)


def test_only_experimental_v2_points_at_pipeline(tmp_path):
    """只给 v2 的模型版本要直接说清怎么办，不能只丢一句"缺少 content_list"。"""
    import zipfile

    archive_path = tmp_path / "v2.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("doc_content_list_v2.json", "[]")
    with zipfile.ZipFile(archive_path) as archive:
        with pytest.raises(mineru_client.MineruError, match="pipeline"):
            mineru_client._read_content_list(archive)
