"""独立 Wiki 后端：全部文件/SQLite 操作限定 knowledge_base 临时目录。"""

import copy
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from indexing.database import get_db_cursor
from indexing.services import knowledge_service as graph
from indexing.services import wiki_service as wiki
from indexing.services.errors import BusinessError


def batch(**parts):
    return {"request_key": uuid4().hex, "reason": "测试 Wiki", **parts}


def create(title="页面", body="正文", **fields):
    payload = batch(pages=[{"ref": "p", "kind": "topic", "title": title, "body": body, **fields}])
    result = wiki.apply(payload)
    assert result["committed"] and not result["partial"], result
    page_id = result["refs"]["p"]["id"]
    actual = wiki.get_record(kind="page", id=page_id)["record"]
    assert result["pages"][0]["content_hash"] == result["refs"]["p"]["content_hash"] == actual["content_hash"]
    return page_id


def read(page_id):
    return wiki.get_record(kind="page", id=page_id)["record"]


def update(page_id, **fields):
    page = read(page_id)
    return {"id": page_id, "expected_revision": page["revision"], "expected_content_hash": page["content_hash"], **fields}


def path(page_id):
    return wiki.wiki_path() / read(page_id)["path"]


def delete(page_id):
    page = read(page_id)
    data = {"kind": "page", "id": page_id, "expected_revision": page["revision"], "expected_content_hash": page["content_hash"]}
    preview = wiki.delete(data)
    return wiki.delete({**data, "request_key": uuid4().hex, "dry_run": False, "confirmed": True, "impact_token": preview["impact_token"]})


def scalar(sql, args=()):
    with get_db_cursor() as cursor:
        return cursor.execute(sql, args).fetchone()[0]


def seed(kb, text="逐字引文。\n第二行"):
    with get_db_cursor(write=True) as cursor:
        cursor.execute("INSERT INTO files(file_hash,filename,file_path) VALUES (?,?,?)", (uuid4().hex, "资料.md", str(kb.path / "资料.md")))
        file_id = cursor.lastrowid
        cursor.execute("INSERT INTO chunks(file_id,doc_title,chunk_text,chunk_index,heading_path) VALUES (?,?,?,?,?)",
                       (file_id, "资料", text, 0, "文档 / 第3页 / 正文"))
        return file_id, cursor.lastrowid


def test_wiki_is_markdown_authority_and_graph_is_independent(knowledge_base):
    page_id = create(title="同名", body="长篇正文")
    node = graph.apply(batch(objects=[{"ref": "n", "kind": "entity", "title": "同名"}]))["refs"]["n"]["id"]
    assert page_id != node
    assert path(page_id).parent == knowledge_base.path / "wiki"
    assert path(page_id).read_text(encoding="utf-8").endswith("长篇正文")
    assert "body" not in graph.get_record(kind="object", id=node)["record"]
    assert scalar("SELECT COUNT(*) FROM knowledge_objects") == 1
    assert scalar("SELECT COUNT(*) FROM wiki_pages") == 1
    assert "body" not in {row[1] for row in _rows("PRAGMA table_info(wiki_pages)")}
    outcome = delete(page_id)
    assert outcome["committed"] and outcome["retains_history"]
    assert graph.get_record(kind="object", id=node)["record"]["title"] == "同名"
    assert wiki.history(kind="page", id=page_id)["total"] == 2


def _rows(sql):
    with get_db_cursor() as cursor:
        return cursor.execute(sql).fetchall()


def test_graph_delete_does_not_touch_wiki_sources_or_history(knowledge_base):
    page_id = create()
    node = graph.apply(batch(objects=[{"ref": "n", "kind": "concept", "title": "图实体"}]))["refs"]["n"]["id"]
    before = path(page_id).read_bytes()
    data = {"kind": "object", "id": node, "expected_revision": 1}
    preview = graph.delete(data)
    graph.delete({**data, "dry_run": False, "confirmed": True, "request_key": "delete-node", "impact_token": preview["impact_token"]})
    assert path(page_id).read_bytes() == before
    assert wiki.history(id=page_id)["total"] == 1


def test_dry_run_does_not_create_directory_or_consume_key(knowledge_base):
    payload = batch(pages=[{"ref": "p", "kind": "topic", "title": "页面"}])
    result = wiki.apply({**payload, "dry_run": True})
    assert result["dry_run"] and not result["committed"] and not result["partial"]
    assert not wiki.wiki_path().exists()
    assert scalar("SELECT COUNT(*) FROM wiki_operations") == 0
    with pytest.raises(BusinessError):
        wiki.request_result(payload["request_key"])
    assert wiki.apply(payload)["committed"]


def test_update_checks_revision_and_raw_file_hash(knowledge_base):
    page_id = create()
    stale = update(page_id, body="API更新")
    file = path(page_id)
    file.write_bytes(file.read_bytes().replace("正文".encode(), "外部改动".encode()))
    assert read(page_id)["body"] == "外部改动"
    result = wiki.apply(batch(pages=[stale]))
    assert not result["committed"] and result["partial"]
    assert result["errors"][0]["code"] == "CONTENT_CONFLICT"
    assert file.read_text(encoding="utf-8").endswith("外部改动")
    current = update(page_id, body="合并后")
    assert wiki.apply(batch(pages=[current]))["committed"]
    history = wiki.history(id=page_id)["history"]
    assert history[0]["before"]["body"] == "外部改动"
    assert history[0]["after"]["body"] == "合并后"
    assert wiki.apply(batch(pages=[stale]))["errors"][0]["code"] == "VERSION_CONFLICT"


def test_crlf_changes_file_hash_even_when_body_unchanged(knowledge_base):
    page_id = create(body="a\nb")
    old = update(page_id, title="更新")
    file = path(page_id)
    file.write_bytes(file.read_bytes().replace(b"\n", b"\r\n"))
    assert read(page_id)["content_hash"] != old["expected_content_hash"]
    assert wiki.apply(batch(pages=[old]))["errors"][0]["code"] == "CONTENT_CONFLICT"


def test_idempotent_retry_after_newer_revision(knowledge_base):
    page_id = create()
    payload = batch(pages=[update(page_id, title="第二版")])
    first = wiki.apply(payload)
    wiki.apply(batch(pages=[update(page_id, title="第三版")]))
    assert wiki.apply(copy.deepcopy(payload)) == first
    assert read(page_id)["title"] == "第三版"
    assert wiki.request_result(payload["request_key"])["pages"] == first["pages"]
    assert wiki.history(id=page_id)["total"] == 3
    with pytest.raises(BusinessError) as exc:
        wiki.apply({**payload, "reason": "不同请求"})
    assert exc.value.code == "REQUEST_CONFLICT"


def test_missing_and_invalid_files_never_return_index_body(knowledge_base):
    page_id = create()
    file = path(page_id)
    file.write_text("没有元数据的人工正文", encoding="utf-8")
    with pytest.raises(BusinessError) as exc:
        read(page_id)
    assert exc.value.code == "FILE_INVALID"
    assert wiki.list_pages()["pages"] == []
    assert wiki.lint()["issues"]
    file.unlink()
    with pytest.raises(BusinessError) as exc:
        read(page_id)
    assert exc.value.code == "FILE_MISSING"
    assert wiki.search_pages(query="正文")["total"] == 0


def test_rebuild_after_cache_clear_and_external_unicode_rename(knowledge_base):
    target = create("目标")
    source = create("中文资料", body=f"[导航](piece://wiki/{target.upper()})", aliases=["别名"])
    file = path(source)
    renamed = file.with_name("中文重命名页面.md")
    file.rename(renamed)
    raw = renamed.read_bytes()
    with get_db_cursor(write=True) as cursor:
        cursor.execute("DELETE FROM wiki_pages")
    result = wiki.rebuild_index()
    assert result["index_status"] == "current" and result["indexed_pages"] == 2
    assert renamed.read_bytes() == raw
    assert read(source)["id"] == source and read(source)["path"] == renamed.name
    assert wiki.search_pages(query="别名")["pages"][0]["id"] == source
    assert wiki.get_record(kind="page", id=target)["backlinks"]["items"][0]["source_id"] == source
    assert wiki.get_record(kind="page", id=source)["links"]["items"][0]["target_id"] == target
    assert scalar("SELECT COUNT(*) FROM knowledge_relations") == 0
    assert scalar("SELECT COUNT(*) FROM wiki_fts") == 2
    assert wiki.history(id=source)["total"] == 1


def test_duplicate_ids_are_not_silently_chosen(knowledge_base):
    page_id = create()
    file = path(page_id)
    file.with_name("复制.md").write_bytes(file.read_bytes())
    with pytest.raises(BusinessError) as exc:
        read(page_id)
    assert exc.value.code == "DUPLICATE_PAGE_ID"
    result = wiki.rebuild_index()
    assert result["partial"] and result["indexed_pages"] == 0
    assert any(error["code"] == "DUPLICATE_PAGE_ID" for error in result["errors"])


def test_atomic_failure_keeps_old_file_and_retry_commits_once(knowledge_base, monkeypatch):
    page_id = create()
    file = path(page_id)
    original = file.read_bytes()
    payload = batch(pages=[update(page_id, body="新正文")])
    original_rename = os.rename
    def fail_capture(source, target):
        if Path(target).suffix == ".bak":
            raise OSError("simulated disk error")
        return original_rename(source, target)
    monkeypatch.setattr(wiki.os, "rename", fail_capture)
    failed = wiki.apply(payload)
    assert not failed["committed"] and failed["errors"][0]["code"] == "FILE_WRITE_FAILED"
    assert file.read_bytes() == original
    assert not list(file.parent.glob(".wiki-*"))
    assert wiki.history(id=page_id)["total"] == 1
    monkeypatch.setattr(wiki.os, "rename", original_rename)
    retried = wiki.apply(payload)
    assert retried["committed"] and not retried["partial"]
    assert read(page_id)["revision"] == 2
    assert wiki.history(id=page_id)["total"] == 2


def test_index_failure_keeps_committed_file_and_recovers_audit(knowledge_base, monkeypatch):
    original = wiki._index_page
    def broken(*args):
        raise sqlite3.OperationalError("simulated index failure")
    monkeypatch.setattr(wiki, "_index_page", broken)
    payload = batch(pages=[{"ref": "p", "kind": "synthesis", "title": "已写MD", "body": "成功正文"}])
    result = wiki.apply(payload)
    page_id = result["refs"]["p"]["id"]
    assert result["committed"] and result["partial"] and result["index_status"] == "stale"
    assert read(page_id)["body"] == "成功正文"
    assert wiki.history(id=page_id)["total"] == 1
    recovered = wiki.request_result(payload["request_key"])
    assert recovered["committed"] and recovered["partial"]
    monkeypatch.setattr(wiki, "_index_page", original)
    rebuilt = wiki.rebuild_index()
    assert rebuilt["recovered_operations"] == 1
    assert wiki.request_result(payload["request_key"])["partial"] is False
    assert not wiki.apply(payload)["partial"]
    assert read(page_id)["revision"] == 1
    assert wiki.history(id=page_id)["total"] == 1


def test_partial_batch_retries_failed_page_without_rewriting_success(knowledge_base, monkeypatch):
    original = wiki._atomic_write
    calls = []
    def fail_second(file, data, expected):
        calls.append(file.name)
        if len(calls) == 2:
            raise BusinessError("FILE_WRITE_FAILED", "第二页失败")
        original(file, data, expected)
    monkeypatch.setattr(wiki, "_atomic_write", fail_second)
    payload = batch(pages=[{"ref": "a", "kind": "topic", "title": "甲"}, {"ref": "b", "kind": "topic", "title": "乙"}])
    result = wiki.apply(payload)
    assert result["committed"] and result["partial"]
    assert [item["committed"] for item in result["pages"]] == [True, False]
    aid = result["refs"]["a"]["id"]
    before = path(aid).read_bytes()
    monkeypatch.setattr(wiki, "_atomic_write", original)
    retried = wiki.apply(payload)
    assert not retried["partial"] and retried["counts"]["created"] == 2
    assert path(aid).read_bytes() == before
    assert wiki.list_pages()["total"] == 2
    assert wiki.history(id=aid)["total"] == 1


def test_pending_write_never_overwrites_new_external_content(knowledge_base, monkeypatch):
    page_id = create()
    payload = batch(pages=[update(page_id, body="待提交")])
    original = wiki._atomic_write
    monkeypatch.setattr(wiki, "_atomic_write", lambda *a: (_ for _ in ()).throw(BusinessError("FILE_WRITE_FAILED", "失败")))
    assert wiki.apply(payload)["partial"]
    file = path(page_id)
    file.write_bytes(file.read_bytes().replace("正文".encode(), "人工改动".encode()))
    monkeypatch.setattr(wiki, "_atomic_write", original)
    retry = wiki.apply(payload)
    assert retry["errors"][0]["code"] == "CONTENT_CONFLICT"
    assert read(page_id)["body"] == "人工改动"


def test_sources_hash_quote_ownership_and_lifecycle(knowledge_base):
    file_id, chunk_id = seed(knowledge_base)
    payload = batch(pages=[{"ref": "p", "kind": "source_summary", "title": "来源摘要"}], evidence=[
        {"page": {"ref": "p"}, "source_kind": "piece", "source_library_id": wiki.library_id(),
         "source_file_id": file_id, "source_chunk_id": chunk_id, "quote": "逐字引文"}])
    result = wiki.apply(payload)
    assert not result["partial"], result
    page_id, evidence_id = result["refs"]["p"]["id"], result["evidence"][0]["id"]
    evidence = wiki.get_record(kind="evidence", id=evidence_id)["record"]
    assert evidence["location_status"] == "current" and evidence["page_number"] == 3
    assert evidence["page_content_hash"] == read(page_id)["content_hash"]
    assert graph.file_reference_count(file_id) == 0 and wiki.file_reference_count(file_id) == 1
    assert wiki.references(source_library_id=wiki.library_id(), source_file_id=file_id)["total"] == 1
    with get_db_cursor(write=True) as cursor:
        cursor.execute("UPDATE chunks SET chunk_text='修改后' WHERE id=?", (chunk_id,))
    assert wiki.get_record(kind="evidence", id=evidence_id)["record"]["location_status"] == "changed"
    assert wiki.lint()["issues"][0]["code"] == "SOURCE_CHANGED"
    with get_db_cursor(write=True) as cursor:
        cursor.execute("DELETE FROM files WHERE id=?", (file_id,))
    assert wiki.get_record(kind="evidence", id=evidence_id)["record"]["location_status"] == "missing"
    assert wiki.apply(payload)["committed"]  # 已提交请求重试不重新检查已变化来源。


@pytest.mark.parametrize("change,code", [({"quote": "不存在"}, "QUOTE_MISMATCH"),
                                        ({"expected_content_hash": "0" * 64}, "SOURCE_CHANGED"),
                                        ({"source_file_id": 9999}, "INVALID_SOURCE")])
def test_invalid_source_does_not_create_page(knowledge_base, change, code):
    file_id, chunk_id = seed(knowledge_base)
    result = wiki.apply(batch(pages=[{"ref": "p", "kind": "topic", "title": "不应保存"}], evidence=[
        {"page": {"ref": "p"}, "source_kind": "piece", "source_library_id": wiki.library_id(),
         "source_file_id": file_id, "source_chunk_id": chunk_id, "quote": "逐字引文", **change}]))
    assert not result["committed"] and result["errors"][0]["code"] == code
    assert wiki.list_pages()["total"] == 0


def test_append_evidence_requires_page_version_and_deduplicates(knowledge_base):
    page_id = create()
    with pytest.raises(BusinessError):
        wiki.apply(batch(evidence=[{"page": {"id": page_id}, "source_kind": "user", "quote": "用户意见"}]))
    data = batch(pages=[update(page_id)], evidence=[{"page": {"id": page_id}, "source_kind": "user", "quote": "用户意见", "stance": "contradicts"}])
    first = wiki.apply(data)
    assert not first["partial"] and first["pages"][0]["revision"] == 2
    evidence_id = first["evidence"][0]["id"]
    second = wiki.apply(batch(pages=[update(page_id)], evidence=data["evidence"]))
    assert second["evidence"][0]["action"] == "reused" and second["evidence"][0]["id"] == evidence_id
    assert wiki.get_record(kind="page", id=page_id)["evidence"]["total"] == 1
    assert wiki.get_record(kind="evidence", id=evidence_id)["record"]["location_status"] == "unverified"


def test_delete_evidence_is_versioned_md_change_and_preserves_audit(knowledge_base):
    result = wiki.apply(batch(pages=[{"ref": "p", "kind": "topic", "title": "页面"}],
                             evidence=[{"page": {"ref": "p"}, "source_kind": "user", "quote": "陈述"}]))
    page_id = result["refs"]["p"]["id"]
    evidence_id = result["evidence"][0]["id"]
    data = {"kind": "evidence", "id": evidence_id, "expected_content_hash": read(page_id)["content_hash"]}
    preview = wiki.delete(data)
    assert not preview["deletes_files"] and preview["retains_history"]
    payload = {**data, "dry_run": False, "confirmed": True, "request_key": "del-evidence", "impact_token": preview["impact_token"]}
    done = wiki.delete(payload)
    assert done["committed"] and not done["partial"]
    assert wiki.delete(payload) == done
    assert read(page_id)["revision"] == 2 and wiki.history(id=page_id)["total"] == 2
    assert wiki.get_record(kind="page", id=page_id)["evidence"]["total"] == 0


def test_delete_detects_file_and_backlink_changes(knowledge_base):
    page_id = create()
    page = read(page_id)
    data = {"kind": "page", "id": page_id, "expected_revision": page["revision"], "expected_content_hash": page["content_hash"]}
    preview = wiki.delete(data)
    create(body=f"piece://wiki/{page_id}")
    with pytest.raises(BusinessError) as exc:
        wiki.delete({**data, "dry_run": False, "confirmed": True, "request_key": "delete-old", "impact_token": preview["impact_token"]})
    assert exc.value.code == "IMPACT_CONFLICT"
    assert delete(page_id)["committed"]
    assert any(issue["code"] == "BROKEN_BODY_LINK" for issue in wiki.lint()["issues"])


@pytest.mark.parametrize("name", ["../other.md", "..\\other.md", "C:\\other.md", "file.md:stream", "/etc/passwd", ".."])
def test_path_traversal_and_windows_streams_rejected(knowledge_base, name):
    with pytest.raises(BusinessError) as exc:
        wiki._path(name)
    assert exc.value.code == "UNSAFE_PATH"


def test_existing_target_is_never_overwritten(knowledge_base):
    root = wiki.wiki_path()
    root.mkdir()
    file = root / "人工.md"
    file.write_text("人工内容", encoding="utf-8")
    with pytest.raises(BusinessError) as exc:
        wiki._atomic_write(file, b"overwrite", None)
    assert exc.value.code == "FILE_EXISTS"
    assert file.read_text(encoding="utf-8") == "人工内容"


def test_schema_guard_rejects_previous_version_without_migration(knowledge_base, tmp_path):
    from indexing import database
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE user_data(value TEXT)")
        conn.execute("INSERT INTO user_data VALUES ('保留')")
        conn.execute("PRAGMA user_version=3")
    with pytest.raises(RuntimeError, match="仅支持新库"):
        database.init_database(old)
    with sqlite3.connect(old) as conn:
        assert conn.execute("SELECT value FROM user_data").fetchone()[0] == "保留"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_failed_intent_can_be_superseded_after_explicit_reread(knowledge_base, monkeypatch):
    page_id = create()
    old = batch(pages=[update(page_id, body="失败意向")])
    original = wiki._atomic_write
    monkeypatch.setattr(wiki, "_atomic_write", lambda *args: (_ for _ in ()).throw(BusinessError("FILE_WRITE_FAILED", "写失败")))
    assert wiki.apply(old)["partial"]
    file = path(page_id)
    file.write_bytes(file.read_bytes().replace("正文".encode(), "人工改动".encode()))
    monkeypatch.setattr(wiki, "_atomic_write", original)
    merged = wiki.apply(batch(pages=[update(page_id, body="重新读回后合并")]))
    assert merged["committed"] and not merged["partial"]
    assert wiki.apply(old)["errors"][0]["code"] == "REQUEST_SUPERSEDED"
    assert read(page_id)["body"] == "重新读回后合并"
    assert wiki.history(id=page_id)["total"] == 2


def test_index_transaction_commit_failure_is_recoverable(knowledge_base, monkeypatch):
    from contextlib import contextmanager
    original = wiki.get_db_cursor
    writes = 0
    @contextmanager
    def failing_commit(write=False):
        nonlocal writes
        if write:
            writes += 1
        call = writes
        with original(write=write) as cursor:
            yield cursor
            if write and call == 3:  # request、intent 已提交；索引事务在提交前回滚。
                raise sqlite3.OperationalError("模拟 commit 失败")
    monkeypatch.setattr(wiki, "get_db_cursor", failing_commit)
    result = wiki.apply(batch(pages=[{"ref": "p", "kind": "topic", "title": "文件已提交"}]))
    assert result["committed"] and result["index_status"] == "stale"
    assert scalar("SELECT COUNT(*) FROM wiki_pages") == 0
    assert scalar("SELECT COUNT(*) FROM wiki_operations WHERE state='pending'") == 1
    monkeypatch.setattr(wiki, "get_db_cursor", original)
    assert wiki.rebuild_index()["recovered_operations"] == 1


def test_request_receipt_recovers_evidence_when_result_cache_write_fails(knowledge_base, monkeypatch):
    original = wiki._store_result
    monkeypatch.setattr(wiki, "_store_result", lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("缓存提交失败")))
    payload = batch(pages=[{"ref": "p", "kind": "topic", "title": "正文"}],
                    evidence=[{"page": {"ref": "p"}, "source_kind": "user", "quote": "陈述"}])
    result = wiki.apply(payload)
    assert result["committed"] and result["partial"]
    recovered = wiki.request_result(payload["request_key"])
    assert recovered["evidence"] == result["evidence"]
    assert recovered["refs"] == result["refs"]
    assert recovered["counts"]["created"] == 2
    monkeypatch.setattr(wiki, "_store_result", original)
    assert wiki.apply(payload)["evidence"] == result["evidence"]


def test_delete_index_failure_and_receipt_counts(knowledge_base, monkeypatch):
    page_id = create()
    page = read(page_id)
    data = {"kind": "page", "id": page_id, "expected_revision": page["revision"], "expected_content_hash": page["content_hash"]}
    preview = wiki.delete(data)
    payload = {**data, "request_key": "delete-failure", "dry_run": False, "confirmed": True, "impact_token": preview["impact_token"]}
    original = wiki._finish_operation
    monkeypatch.setattr(wiki, "_finish_operation", lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("索引失败")))
    result = wiki.delete(payload)
    assert result["committed"] and result["partial"]
    assert not (wiki.wiki_path() / page["path"]).exists()
    assert wiki.request_result(payload["request_key"])["counts"] == result["counts"]
    monkeypatch.setattr(wiki, "_finish_operation", original)
    assert wiki.rebuild_index()["recovered_operations"] == 1
    assert not wiki.delete(payload)["partial"]
    assert wiki.history(id=page_id)["total"] == 2


def test_external_search_removes_ghosts_and_reads_current_body(knowledge_base):
    page_id = create(title="检索页", body="机器学习")
    assert wiki.search_pages(query="机器")["total"] == 1
    file = path(page_id)
    file.write_bytes(file.read_bytes().replace("机器学习".encode(), "卷积网络".encode()))
    assert wiki.search_pages(query="机器")["total"] == 0
    current = wiki.search_pages(query="卷积")
    assert current["total"] == 1 and current["index_status"] == "stale"
    assert wiki.rebuild_index()["index_status"] == "current"
    assert wiki.search_pages(query="卷积")["total"] == 1
    assert wiki.search_pages(query='" * () OR %_') is not None


def test_untrusted_frontmatter_cannot_forge_current_quote(knowledge_base):
    file_id, chunk_id = seed(knowledge_base)
    result = wiki.apply(batch(pages=[{"ref": "p", "kind": "topic", "title": "来源"}], evidence=[
        {"page": {"ref": "p"}, "source_kind": "piece", "source_library_id": wiki.library_id(),
         "source_file_id": file_id, "source_chunk_id": chunk_id, "quote": "逐字引文"}]))
    page_id = result["refs"]["p"]["id"]
    file = path(page_id)
    file.write_bytes(file.read_bytes().replace("逐字引文".encode(), "伪造引文".encode()))
    evidence = wiki.get_record(kind="evidence", id=result["evidence"][0]["id"])["record"]
    assert evidence["location_status"] != "current"


def test_symlink_targets_and_root_are_rejected(knowledge_base, tmp_path):
    root = wiki.wiki_path()
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("不可改变", encoding="utf-8")
    try:
        (root / "linked.md").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台不允许创建符号链接")
    with pytest.raises(BusinessError) as exc:
        wiki._atomic_write(root / "linked.md", b"bad", None)
    assert exc.value.code == "UNSAFE_PATH" and outside.read_text(encoding="utf-8") == "不可改变"
    (root / "linked.md").unlink()
    root.rmdir()
    root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(BusinessError) as exc:
        wiki.list_pages()
    assert exc.value.code == "UNSAFE_PATH"
    root.unlink()


def test_graph_rebuild_only_changes_derived_index(knowledge_base):
    result = graph.apply(batch(objects=[{"ref": "n", "kind": "entity", "title": "实体", "summary": "需要索引的卷积网络"}]))
    node_id = result["refs"]["n"]["id"]
    before = graph.get_record(kind="object", id=node_id)
    history = graph.history(kind="object", id=node_id)
    with get_db_cursor(write=True) as cursor:
        cursor.execute("DELETE FROM knowledge_fts")
    assert graph.search_objects(query="卷积")["total"] == 0
    assert graph.rebuild_index()["indexed_objects"] == 1
    assert graph.search_objects(query="卷积")["total"] == 1
    assert graph.get_record(kind="object", id=node_id) == before
    assert graph.history(kind="object", id=node_id) == history
    assert not wiki.wiki_path().exists()
