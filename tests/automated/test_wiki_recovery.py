"""Wiki 实际文件捕获、编辑竞态与持久审计恢复；仅使用临时知识库。"""

import os
from pathlib import Path
import sqlite3

import pytest

from indexing import database
from indexing.services import wiki_service as wiki
from test_wiki_service import batch, create, path, read, scalar, update


def delete_payload(page_id, key):
    page = read(page_id)
    data = {"kind": "page", "id": page_id, "expected_revision": page["revision"], "expected_content_hash": page["content_hash"]}
    preview = wiki.delete(data)
    return {**data, "request_key": key, "dry_run": False, "confirmed": True, "impact_token": preview["impact_token"]}


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_edit_between_hash_check_and_capture_is_preserved(knowledge_base, monkeypatch, operation):
    page_id = create(body="旧正文")
    file = path(page_id)
    external = file.read_bytes().replace("旧正文".encode(), "编辑器刚保存的正文".encode())
    original_rename = os.rename
    def save_then_capture(source, target):
        if Path(source) == file:
            file.write_bytes(external)  # 精确发生在最后预检读取与真正移动之间。
        return original_rename(source, target)
    payload = batch(pages=[update(page_id, body="API新正文")]) if operation == "update" else delete_payload(page_id, "capture-delete")
    monkeypatch.setattr(wiki.os, "rename", save_then_capture)
    result = wiki.apply(payload) if operation == "update" else wiki.delete(payload)
    assert not result["committed"] and result["partial"]
    assert result["errors"][0]["code"] == "CONTENT_CONFLICT"
    assert file.read_bytes() == external
    assert Path(result["pages"][0]["recovery_path"]).read_bytes() == external
    assert wiki.history(id=page_id)["total"] == 1


def test_editor_recreates_path_between_capture_and_publish_is_not_overwritten(knowledge_base, monkeypatch):
    page_id = create(body="旧正文")
    file = path(page_id)
    old = file.read_bytes()
    external = old.replace("旧正文".encode(), "窗口内的外部保存".encode())
    payload = batch(pages=[update(page_id, body="API更新")])
    original_link = os.link
    def competing_save(source, target):
        if Path(target) == file:
            file.write_bytes(external)
        return original_link(source, target)
    monkeypatch.setattr(wiki.os, "link", competing_save)
    result = wiki.apply(payload)
    assert not result["committed"] and result["errors"][0]["code"] == "FILE_EXISTS"
    assert file.read_bytes() == external
    assert Path(result["pages"][0]["recovery_path"]).read_bytes() == old


def test_late_write_to_captured_inode_is_retained_and_reported(knowledge_base, monkeypatch):
    page_id = create(body="旧正文")
    file = path(page_id)
    external = file.read_bytes().replace("旧正文".encode(), "旧句柄晚到的保存".encode())
    payload = batch(pages=[update(page_id, body="API新正文")])
    original_link = os.link
    def late_save(source, target):
        original_link(source, target)
        if Path(target) == file:
            recovery = next(file.parent.glob(".wiki-recovery-*.bak"))
            recovery.write_bytes(external)  # 模拟编辑器仍持有被移动文件的旧 inode/句柄。
    monkeypatch.setattr(wiki.os, "link", late_save)
    result = wiki.apply(payload)
    assert result["committed"] and result["partial"]
    assert result["errors"][0]["code"] == "CONTENT_CONFLICT"
    assert read(page_id)["body"] == "API新正文"
    assert Path(result["pages"][0]["recovery_path"]).read_bytes() == external
    assert wiki.history(id=page_id)["history"][0]["recovery_retained"] is True


def test_capture_crash_has_recovery_receipt_and_explicit_retry(knowledge_base, monkeypatch):
    page_id = create(body="崩溃前的实际内容")
    file = path(page_id)
    before = file.read_bytes()
    payload = batch(pages=[update(page_id, body="原键恢复后的更新")])
    original_link = os.link
    def interrupted_publication(source, target):
        if Path(target) == file:
            raise KeyboardInterrupt("模拟捕获后、发布前进程中断")
        return original_link(source, target)
    monkeypatch.setattr(wiki.os, "link", interrupted_publication)
    with pytest.raises(KeyboardInterrupt):
        wiki.apply(payload)
    assert not file.exists()
    monkeypatch.setattr(wiki.os, "link", original_link)
    database.close_connection_pool()
    database.init_connection_pool()
    receipt = wiki.request_result(payload["request_key"])
    assert not receipt["committed"] and receipt["errors"][0]["code"] == "RECOVERY_REQUIRED"
    recovery = Path(receipt["pages"][0]["recovery_path"])
    assert recovery.read_bytes() == before
    rebuilt = wiki.rebuild_index()
    assert rebuilt["partial"] and not rebuilt["rewrote_files"] and not file.exists()
    assert any(issue["code"] == "RECOVERY_REQUIRED" for issue in wiki.lint()["issues"])
    result = wiki.apply(payload)
    assert result["committed"] and not result["partial"]
    assert read(page_id)["body"] == "原键恢复后的更新"
    assert recovery.read_bytes() == before
    assert wiki.history(id=page_id)["total"] == 2


def test_failed_delete_then_invalid_external_rename_is_not_a_commit(knowledge_base, monkeypatch):
    page_id = create()
    file = path(page_id)
    payload = delete_payload(page_id, "failed-delete-invalid-rename")
    original_delete = wiki._atomic_delete
    monkeypatch.setattr(wiki, "_atomic_delete", lambda *args: (_ for _ in ()).throw(OSError("模拟捕获失败")))
    assert not wiki.delete(payload)["committed"]
    renamed = file.with_name("外部改名且格式损坏.md")
    file.rename(renamed)
    renamed.write_text("人工内容，没有frontmatter", encoding="utf-8")
    monkeypatch.setattr(wiki, "_atomic_delete", original_delete)
    assert not wiki.delete(payload)["committed"]
    receipt = wiki.request_result(payload["request_key"])
    assert not receipt["committed"] and receipt["scan_errors"]
    assert wiki.rebuild_index()["recovered_operations"] == 0
    assert wiki.history(id=page_id)["total"] == 1
    assert scalar("SELECT state FROM wiki_operations WHERE request_key=?", (payload["request_key"],)) == "pending"
    assert renamed.read_text(encoding="utf-8") == "人工内容，没有frontmatter"


def test_pending_delete_retry_rechecks_impact_token(knowledge_base, monkeypatch):
    page_id = create()
    payload = delete_payload(page_id, "pending-impact-change")
    original_delete = wiki._atomic_delete
    monkeypatch.setattr(wiki, "_atomic_delete", lambda *args: (_ for _ in ()).throw(OSError("模拟捕获失败")))
    assert not wiki.delete(payload)["committed"]
    create(body=f"[新入链](piece://wiki/{page_id})")
    monkeypatch.setattr(wiki, "_atomic_delete", original_delete)
    retry = wiki.delete(payload)
    assert not retry["committed"] and retry["errors"][0]["code"] == "IMPACT_CONFLICT"
    assert path(page_id).exists()
    assert not retry.get("recovery_retained")


def test_pending_delete_retry_without_impact_change_can_finish(knowledge_base, monkeypatch):
    page_id = create()
    payload = delete_payload(page_id, "pending-unchanged")
    original_delete = wiki._atomic_delete
    monkeypatch.setattr(wiki, "_atomic_delete", lambda *args: (_ for _ in ()).throw(OSError("模拟捕获失败")))
    assert not wiki.delete(payload)["committed"]
    monkeypatch.setattr(wiki, "_atomic_delete", original_delete)
    result = wiki.delete(payload)
    assert result["committed"] and not result["partial"]
    assert Path(result["recovery_path"]).exists()


def test_wiki_intent_is_committed_with_full_durability_before_capture(knowledge_base, monkeypatch):
    for connection in database._connection_pool._all_connections:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    page_id = create()
    payload = batch(pages=[update(page_id, body="需要持久意向的更新")])
    original_capture = wiki._capture_original
    observed = []
    def durable_capture(file, expected_hash, operation_id):
        with sqlite3.connect(knowledge_base.settings.get_db_path()) as independent:
            row = independent.execute("SELECT state FROM wiki_operations WHERE id=?", (operation_id,)).fetchone()
            assert row is not None and row[0] == "pending"
            assert independent.execute("SELECT request_key FROM wiki_requests WHERE request_key=?", (payload["request_key"],)).fetchone()
        observed.append(operation_id)
        return original_capture(file, expected_hash, operation_id)
    monkeypatch.setattr(wiki, "_capture_original", durable_capture)
    assert wiki.apply(payload)["committed"]
    assert len(observed) == 1


@pytest.mark.parametrize("body", ["第一行\r\n第二行\r\n", "第一行\n第二行\n", "第一行\r第二行\r"])
def test_receipt_hash_matches_actual_bytes_with_all_newline_styles(knowledge_base, body):
    import hashlib
    payload = batch(pages=[{"ref": "p", "kind": "topic", "title": "换行与字段排序", "body": body}])
    first = wiki.apply(payload)
    page_id = first["refs"]["p"]["id"]
    def verify(result):
        actual = read(page_id)
        digest = hashlib.sha256(path(page_id).read_bytes()).hexdigest()
        assert actual["body"] == body
        assert result["pages"][0]["content_hash"] == actual["content_hash"] == digest
        if result["refs"]:
            assert result["refs"]["p"]["content_hash"] == digest
        operation = scalar("SELECT after_hash FROM wiki_operations WHERE page_id=? ORDER BY rowid DESC LIMIT 1", (page_id,))
        assert operation == digest
    assert first["committed"] and not first["partial"]
    verify(first)
    assert wiki.apply(payload) == first
    revised = wiki.apply(batch(pages=[update(page_id, title="标题更新不改换行")]))
    verify(revised)


def test_receipt_hash_is_stable_after_index_failure_and_request_replay(knowledge_base, monkeypatch):
    import hashlib
    original_index = wiki._index_page
    monkeypatch.setattr(wiki, "_index_page", lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("索引失败")))
    payload = batch(pages=[{"body": "正文\r\n下一行\r\n", "title": "字段顺序不同", "kind": "topic", "ref": "p"}])
    first = wiki.apply(payload)
    assert first["committed"] and first["partial"]
    page_id = first["refs"]["p"]["id"]
    raw = path(page_id).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == first["pages"][0]["content_hash"] == read(page_id)["content_hash"]
    monkeypatch.setattr(wiki, "_index_page", original_index)
    replayed = wiki.apply(payload)
    assert not replayed["partial"] and path(page_id).read_bytes() == raw
    assert replayed["pages"][0]["content_hash"] == first["pages"][0]["content_hash"]
