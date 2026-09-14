"""新 schema、共享业务及安全发布的可运行合同。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from indexing import database
from indexing.services import file_service as files, chunk_service as chunks, task_service as tasks
from indexing.services import config_service, maintenance_service
from indexing.services.errors import BusinessError


def test_import_deduplication_and_atomic_acceptance(knowledge_base):
    source = knowledge_base.path / "中文 空格.md"
    source.write_text("# 方法\n\n相同内容只导入一次", encoding="utf-8")
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: files.import_file(source), range(4)))
    assert sum(not r["duplicate"] for r in results) == 1
    assert len({r["file_id"] for r in results}) == 1
    task = tasks.get_task(next(r for r in results if not r["duplicate"])["task_id"])
    assert task["task_type"] == "file_index" and task["input"] == {"source": "original"}
    assert task["error_message"] is None
    assert len(tasks.get_active_tasks()) == 1
    knowledge_base.drain()
    completed = tasks.get_task(task["id"])
    assert completed["status"] == "completed"
    assert completed["result"]["chunk_ids"]
    assert source.is_file()


def test_import_compensates_and_keeps_source(knowledge_base, monkeypatch):
    source = knowledge_base.path / "保留原件.md"
    source.write_text("合法内容", encoding="utf-8")
    monkeypatch.setattr(tasks, "create_task", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("queue unavailable")))
    with pytest.raises(RuntimeError):
        files.import_file(source)
    assert source.read_text(encoding="utf-8") == "合法内容"
    assert files.get_files_list() == []
    assert list(files.get_originals_dir().iterdir()) == []
    assert list(files.get_working_dir().iterdir()) == []


def test_batch_partial_and_idempotency(knowledge_base):
    file_id = files.create_empty_file("笔记")["file_id"]
    result = chunks.create_chunks(file_id, [{"doc_title": "正常|标题", "chunk_text": "正文|不编码进错误字段"},
                                             {"doc_title": " ", "chunk_text": "坏输入"}], request_key="batch-a")
    assert result["accepted_count"] == 1 and result["rejected_count"] == 1
    again = chunks.create_chunk_add_task(file_id, "正常|标题", "正文|不编码进错误字段", request_key="batch-a:0")
    assert result["task_ids"] == [again]
    with pytest.raises(BusinessError, match="请求键"):
        chunks.create_chunk_add_task(file_id, "不同内容", "正文", request_key="batch-a:0")
    knowledge_base.drain()
    task = tasks.get_task(again)
    assert task["status"] == "completed" and task["error_message"] is None
    assert chunks.get_chunk_by_id(task["result"]["chunk_id"])["doc_title"] == "正常|标题"
    assert len(files.get_chunks_by_file_id(file_id)) == 1
    assert tasks.task_summary([again, 98765])["not_found"] == [98765]
    assert not tasks.task_summary([again, 98765])["all_succeeded"]


def test_reindex_failure_preserves_available_index(knowledge_base, monkeypatch):
    source = knowledge_base.path / "原始文档.md"
    source.write_text("# 标题\n\n已经索引的正文", encoding="utf-8")
    accepted = files.import_file(source)
    knowledge_base.drain()
    file_id = accepted["file_id"]
    before = files.get_chunks_by_file_id(file_id)
    work_before = files.export_file(file_id).read_bytes()
    retry = files.reindex_file(file_id)
    async def fail(texts):
        raise RuntimeError("embedding offline")
    monkeypatch.setattr(knowledge_base.model, "aembed_documents", fail)
    knowledge_base.drain()
    assert tasks.get_task(retry["task_id"])["status"] == "failed"
    assert files.get_chunks_by_file_id(file_id) == before
    assert files.export_file(file_id).read_bytes() == work_before
    assert files.get_file_by_id(file_id)["status"] == "indexed"
    with database.get_db_cursor() as cursor:
        assert cursor.execute("SELECT COUNT(*) FROM staged_chunks").fetchone()[0] == 0


def test_conflicts_cancel_retry_and_restart(knowledge_base):
    file_id = files.create_empty_file("任务")["file_id"]
    first = chunks.create_chunk_add_task(file_id, "第一张", "正文")
    second = chunks.create_chunk_add_task(file_id, "第二张", "正文")
    with pytest.raises(BusinessError, match="在途"):
        files.reindex_file(file_id)
    with pytest.raises(BusinessError, match="在途"):
        files.delete_file(file_id)
    claimed = tasks.claim_next_pending_task()
    assert claimed["id"] == first
    assert tasks.claim_next_pending_task() is None
    with pytest.raises(BusinessError, match="未领取"):
        tasks.cancel_task(first)
    database.close_connection_pool()
    database.init_database()
    database.init_connection_pool()
    tasks.mark_processing_tasks_failed("应用重启")
    assert tasks.get_task(first)["error_code"] == "INTERRUPTED"
    assert tasks.get_task(second)["status"] == "pending"
    tasks.cancel_task(second)
    retry_id = tasks.retry_task(second)
    assert tasks.retry_task(second) == retry_id
    knowledge_base.drain()
    assert tasks.get_task(retry_id)["status"] == "completed"
    with pytest.raises(BusinessError, match="重试"):
        tasks.retry_task(retry_id)


def test_title_content_update_and_last_chunk_deletion(knowledge_base):
    file_id = files.create_empty_file("页码")["file_id"]
    added = chunks.create_chunk_add_task(file_id, "旧标题", "旧正文")
    knowledge_base.drain()
    chunk_id = tasks.get_task(added)["result"]["chunk_id"]
    with database.get_db_cursor(write=True) as cursor:
        cursor.execute("UPDATE files SET original_file_type='pdf' WHERE id=?", (file_id,))
        cursor.execute("UPDATE chunks SET heading_path='文档 / 第3页', heading_level=2 WHERE id=?", (chunk_id,))
    updated = chunks.create_chunk_update_task(chunk_id, "新正文", "新标题")
    knowledge_base.drain()
    chunk = chunks.get_chunk_by_id(chunk_id)
    assert chunk["doc_title"] == "新标题" and chunk["chunk_text"] == "新正文"
    assert chunk["heading_path"] == "文档 / 第3页"
    assert tasks.get_task(updated)["result"]["chunk_id"] == chunk_id
    preview = maintenance_service.delete_chunks([chunk_id, chunk_id], dry_run=True)
    assert preview["deletes_file_ids"] == [file_id]
    with pytest.raises(BusinessError, match="确认"):
        maintenance_service.delete_chunks([chunk_id])
    deleted = maintenance_service.delete_chunks([chunk_id, chunk_id], confirmed=True)
    assert deleted["deleted_count"] == 1
    assert files.get_file_by_id(file_id) is None


def test_config_redaction_and_live_path_pinning(knowledge_base):
    before = knowledge_base.settings
    result = config_service.update_config({"data_path": str(knowledge_base.path / "next"),
                                          "ocr": {"api_key": "do-not-print-this"}})
    assert result["requires_restart"] == ["data_path"]
    assert result["config"]["ocr"]["api_key"] == "***"
    from indexing.settings import get_settings
    assert get_settings().get_db_path() == before.get_db_path()
    assert not (knowledge_base.path / "next").exists()
    assert "do-not-print-this" not in str(config_service.show_config())


def test_offline_config_rejects_live_config_lock(knowledge_base):
    from app.platform import database_lock
    from indexing.settings import _get_config_file_path
    with database_lock(_get_config_file_path()):
        with pytest.raises(RuntimeError, match="独占"):
            config_service.update_config({"appearance": {"theme": "dark"}}, offline=True)


def test_empty_library_dimension_change_and_model_guard(knowledge_base):
    config_service.update_config({"embedding": {"vector_dim": 3}})
    with database.get_db_cursor() as cursor:
        schema = cursor.execute("SELECT sql FROM sqlite_master WHERE name='vec_chunks'").fetchone()[0]
    assert "float[3]" in schema
    config_service.update_config({"embedding": {"vector_dim": 2}})
    file_id = files.create_empty_file("已索引")["file_id"]
    chunks.create_chunk_add_task(file_id, "标题", "正文")
    knowledge_base.drain()
    with pytest.raises(BusinessError, match="已有向量"):
        config_service.update_config({"embedding": {"model": "different-model"}})


def test_scope_mismatch_never_searches_whole_library(knowledge_base):
    from retrieval.service import search
    file_id = files.create_empty_file("可检索")["file_id"]
    chunks.create_chunk_add_task(file_id, "研究方法", "研究方法正文")
    knowledge_base.drain()
    found = asyncio.run(search("研究方法"))
    assert found["candidates"][0]["file_id"] == file_id
    assert "chunk_text" not in found["candidates"][0]
    assert asyncio.run(search("研究方法", collections=["不存在的集合"]))["candidates"] == []
