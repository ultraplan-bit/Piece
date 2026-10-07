"""独立 Wiki CLI：路由、文件正文、并发哈希与失败恢复。"""
import json

import pytest

from app.client import make_envelope
from test_cli import endpoint, failure, invoke  # noqa: F401
from test_wiki_cli import EVIDENCE, LIBRARY_UUID, OBJECT_UUID, _write_json

HASH = "b" * 64


def _receipt(handler, payload):
    page = payload["pages"][0]
    return make_envelope(True, "已保存", {
        "committed": True, "dry_run": payload.get("dry_run", False),
        "library_id": LIBRARY_UUID, "index_status": "current", "partial": False,
        "pages": [{"id": OBJECT_UUID, "revision": page.get("expected_revision", 0) + 1,
                   "content_hash": HASH}], "evidence": [],
    })


def _page(handler, payload):
    return make_envelope(True, "", {"library_id": LIBRARY_UUID, "record": {
        "id": OBJECT_UUID, "revision": 1, "content_hash": HASH, "body": "正文"},
        "evidence": {"total": 0}, "has_evidence": False})


def test_wiki_page_add_saves_body_and_evidence_in_page_request(endpoint, capsys, tmp_path):
    endpoint.routes["/api/v1/wiki/apply"] = _receipt
    endpoint.routes["/api/v1/wiki/get"] = _page
    body = tmp_path / "中文正文.md"
    body.write_bytes("正文\n\\frac{1}{2}".encode())
    evidence = _write_json(tmp_path / "evidence.json", EVIDENCE)
    request = tmp_path / "request.json"
    code, result, _ = invoke(endpoint, capsys, "wiki", "page", "add", "--kind", "topic",
                             "--title", "标题", "--body-file", str(body), "--evidence-file", str(evidence),
                             "--request-file", str(request))
    assert code == 0 and result["data"]["read_back"]["complete"]
    payload = json.loads(request.read_text(encoding="utf-8"))["request"]
    assert payload["pages"][0]["body"] == "正文\n\\frac{1}{2}"
    assert payload["evidence"][0]["page"] == {"ref": "item"}
    assert not any("knowledge/" in call["path"] for call in endpoint.calls)


def test_wiki_update_transmits_both_concurrency_guards(endpoint, capsys, tmp_path):
    request = tmp_path / "update.json"
    invoke(endpoint, capsys, "wiki", "page", "update", OBJECT_UUID,
           "--expected-revision", "3", "--expected-content-hash", HASH, "--title", "新标题",
           "--request-file", str(request), "--dry-run")
    payload = json.loads(request.read_text(encoding="utf-8"))["request"]
    assert payload["pages"] == [{"id": OBJECT_UUID, "expected_revision": 3,
                                  "expected_content_hash": HASH, "title": "新标题"}]
    assert endpoint.calls[-1]["path"] == "/api/v1/wiki/apply"


@pytest.mark.parametrize("namespace,command", [("wiki", "list"), ("wiki", "search"), ("graph", "list")])
def test_reads_use_separate_namespaces(endpoint, capsys, namespace, command):
    args = [namespace, command] + (["检索词"] if command == "search" else [])
    invoke(endpoint, capsys, *args)
    path = "wiki" if namespace == "wiki" else "knowledge"
    assert endpoint.calls[-1]["path"] == f"/api/v1/{path}/{command}"


def test_wiki_rebuild_index_is_explicit_and_has_no_knowledge_payload(endpoint, capsys):
    invoke(endpoint, capsys, "wiki", "rebuild-index")
    assert endpoint.calls[-1]["path"] == "/api/v1/wiki/rebuild-index"
    assert endpoint.calls[-1]["payload"] == {}


@pytest.mark.parametrize("namespace,args", [
    ("wiki", ["relation", "add"]), ("wiki", ["graph", OBJECT_UUID]),
    ("graph", ["page", "add"]),
    ("graph", ["object", "add", "--kind", "entity", "--title", "实体", "--body-file", "unused"]),
])
def test_coupled_commands_are_not_exposed(endpoint, capsys, namespace, args):
    code, _, _ = invoke(endpoint, capsys, namespace, *args)
    assert code == 2 and not endpoint.calls


def test_wiki_delete_sends_hash_even_for_evidence(endpoint, capsys):
    invoke(endpoint, capsys, "wiki", "delete", "evidence", OBJECT_UUID, "--expected-content-hash", HASH)
    assert endpoint.calls[-1]["payload"] == {
        "kind": "evidence", "id": OBJECT_UUID, "expected_content_hash": HASH, "dry_run": True}


def test_wiki_readback_detects_external_body_change_with_same_revision(endpoint, capsys, tmp_path):
    endpoint.routes["/api/v1/wiki/apply"] = _receipt
    def changed(handler, payload):
        result = _page(handler, payload)
        result["data"]["record"]["content_hash"] = "c" * 64
        return result
    endpoint.routes["/api/v1/wiki/get"] = changed
    code, result, _ = invoke(endpoint, capsys, "wiki", "page", "add", "--kind", "topic", "--title", "页",
                             "--request-file", str(tmp_path / "request.json"))
    assert code == 1 and result["error"]["code"] == "READ_BACK_INCOMPLETE"
    assert result["data"]["committed"] is True
    assert "wiki" in result["data"]["next_argv"]
    assert sum(call["path"].endswith("/apply") for call in endpoint.calls) == 1


def test_wiki_version_conflict_recovers_in_wiki_namespace(endpoint, capsys, tmp_path):
    endpoint.routes["/api/v1/wiki/apply"] = lambda *args: failure(
        "VERSION_CONFLICT", {"section": "pages", "id": OBJECT_UUID})
    code, result, _ = invoke(endpoint, capsys, "wiki", "page", "update", OBJECT_UUID,
                             "--expected-revision", "1", "--expected-content-hash", HASH, "--title", "新页",
                             "--request-file", str(tmp_path / "request.json"))
    assert code == 1
    assert result["data"]["next_argv"][-5:-1] == ["wiki", "get", "page", OBJECT_UUID]


@pytest.mark.parametrize("error_code,committed,next_words", [
    ("CONTENT_CONFLICT", False, ["wiki", "get", "page", OBJECT_UUID]),
    ("INDEX_FAILED", True, ["wiki", "rebuild-index"]),
])
def test_wiki_partial_receipt_preserves_outcome_and_offers_safe_recovery(
        endpoint, capsys, tmp_path, error_code, committed, next_words):
    endpoint.routes["/api/v1/wiki/apply"] = lambda *args: failure("WIKI_PARTIAL", {
        "committed": committed, "partial": True, "index_status": "stale",
        "pages": [{"id": OBJECT_UUID, "committed": committed}],
        "errors": [{"code": error_code, "section": "pages", "index": 0, "page_id": OBJECT_UUID}],
    })
    code, result, _ = invoke(endpoint, capsys, "wiki", "page", "add", "--kind", "topic", "--title", "页",
                             "--request-file", str(tmp_path / "request.json"))
    assert code == 1 and result["data"]["committed"] is committed
    assert result["data"]["partial"] is True
    assert result["data"]["next_argv"][-len(next_words)-1:-1] == next_words
    assert not any(call["path"].endswith("/rebuild-index") for call in endpoint.calls)
