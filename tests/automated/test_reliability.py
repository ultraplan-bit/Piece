"""新模型的恢复、维护、GUI 适配和配置保护，不恢复旧任务负载兼容。"""

import asyncio
import json
import os
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest

from indexing import database, settings
from indexing.services import file_service as files, chunk_service as chunks, task_service as tasks
from indexing.services import config_service
from indexing.services.errors import BusinessError


def test_pool_close_waits_for_borrowed_connection(knowledge_base):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    def borrow():
        with database.get_db_cursor(write=True) as cursor:
            entered.set()
            assert release.wait(5)
            cursor.execute("SELECT 1")
    worker = threading.Thread(target=borrow)
    closer = threading.Thread(target=lambda: (database.close_connection_pool(), closed.set()))
    worker.start()
    assert entered.wait(5)
    closer.start()
    try:
        assert not closed.wait(0.15)
    finally:
        release.set()
        worker.join(5)
        closer.join(5)
    assert closed.is_set() and not worker.is_alive() and not closer.is_alive()
    with pytest.raises(RuntimeError, match="尚未初始化"):
        with database.get_db_cursor():
            pass


def test_offline_dimension_change_reconciles_only_empty_table(knowledge_base):
    database.close_connection_pool()
    config_service.update_config({"embedding": {"vector_dim": 3}}, offline=True)
    database.init_database()
    database.init_connection_pool()
    with database.get_db_cursor() as cursor:
        schema = cursor.execute("SELECT sql FROM sqlite_master WHERE name='vec_chunks'").fetchone()[0]
    assert "float[3]" in schema


def test_recovery_removes_only_owned_unreferenced_generations(knowledge_base):
    source = knowledge_base.path / "document.md"
    source.write_text("# 标题\n\n正文", encoding="utf-8")
    accepted = files.import_file(source)
    knowledge_base.drain()
    current = files.export_file(accepted["file_id"])
    root = files.get_working_dir() / ".generations"
    abandoned = root / f"task-{accepted['task_id']}"
    foreign = root / f"task-{accepted['task_id']}-{'f' * 32}"
    for directory, owner in ((abandoned, files.storage_owner()), (foreign, "0" * 32)):
        directory.mkdir()
        (directory / ".piece-generation").write_text(owner, encoding="ascii")
        (directory / "content.md").write_text("keep unless owned", encoding="utf-8")
    files.recover_file_storage()
    assert current.is_file() and foreign.is_dir() and not abandoned.exists()


def test_recovery_removes_paths_beyond_max_path_and_never_blocks_startup(knowledge_base, monkeypatch):
    source = knowledge_base.path / "document.md"
    source.write_text("# 标题\n\n正文", encoding="utf-8")
    accepted = files.import_file(source)
    knowledge_base.drain()
    root = files.get_working_dir() / ".generations"
    stuck, deep = (root / f"task-{accepted['task_id']}-{suffix}" for suffix in ("0" * 8, "f" * 32))
    for directory in (stuck, deep):
        directory.mkdir()
        (directory / ".piece-generation").write_text(files.storage_owner(), encoding="ascii")
    # 旧版本的代目录里可能留下超过 260 字符的插图，普通路径既看不到也删不掉
    image = deep / ("长" * 100) / f"{'e' * 120}.jpg"
    prefix = "\\\\?\\" if os.name == "nt" else ""
    os.makedirs(prefix + str(image.parent))
    with open(prefix + str(image), "wb") as output:
        output.write(b"image")
    real_remove = files.remove_tree
    def flaky(path, ignore_errors=False):
        if path == stuck:
            raise PermissionError("文件被占用")
        real_remove(path, ignore_errors)
    monkeypatch.setattr(files, "remove_tree", flaky)
    files.recover_file_storage()
    assert stuck.is_dir() and not deep.exists()


def test_stored_names_are_capped_for_windows_path_limit(knowledge_base):
    title = "作者 - 2024 - " + "很长的标题" * 30
    source = knowledge_base.path / "source.md"
    source.write_text("# 标题\n\n正文", encoding="utf-8")
    first = files.import_file(source, filename=f"{title}.md")
    source.write_text("# 标题\n\n另一份正文", encoding="utf-8")
    second = files.import_file(source, filename=f"{title}.md")
    for accepted in (first, second):
        stem = Path(accepted["filename"]).stem
        assert len(stem) <= files.MAX_STORED_STEM and title.startswith(stem.removesuffix("_1"))
        record = files.get_file_by_id(accepted["file_id"])
        assert len(Path(record["original_file_path"]).stem) <= files.MAX_STORED_STEM
    assert second["filename"].endswith("_1.md")
    knowledge_base.drain()
    generation = Path(files.get_file_by_id(first["file_id"])["file_path"]).parent.name
    assert len(generation.rsplit("-", 1)[1]) == 8


def test_reindex_does_not_collide_with_downloaded_task_directory(knowledge_base):
    source = knowledge_base.path / "document.md"
    source.write_text("# 标题\n\n正文", encoding="utf-8")
    accepted = files.import_file(source)
    foreign = files.get_working_dir() / ".generations" / f"task-{accepted['task_id']}"
    foreign.mkdir(parents=True)
    (foreign / "foreign.md").write_text("downloaded", encoding="utf-8")
    knowledge_base.drain()
    assert tasks.get_task(accepted["task_id"])["status"] == "completed"
    assert (foreign / "foreign.md").read_text() == "downloaded"


def test_reindex_preview_checks_source_and_conflict_without_writes(api, knowledge_base):
    file_id = files.create_empty_file("笔记")["file_id"]
    missing = api.post("/api/v1/file/reindex", json={"file_id": file_id, "source": "original", "dry_run": True}).json()
    assert missing["error"]["code"] == "NOT_FOUND"
    preview = api.post("/api/v1/file/reindex", json={"file_id": file_id, "dry_run": True}).json()
    assert preview["success"] and preview["data"]["source"] == "working"
    assert tasks.get_active_tasks() == []
    chunks.create_chunk_add_task(file_id, "标题", "正文")
    busy = api.post("/api/v1/file/reindex", json={"file_id": file_id, "dry_run": True}).json()
    assert busy["error"]["code"] == "FILE_BUSY"
    assert len(tasks.get_active_tasks()) == 1


def test_missing_chunk_list_is_not_success(api):
    result = api.post("/api/v1/chunk/list", json={"file_id": 555}).json()
    assert not result["success"] and result["error"]["code"] == "NOT_FOUND"


def test_request_lookup_keeps_inputs_private_and_handles_literal_keys(api, knowledge_base):
    file_id = files.create_empty_file("笔记")["file_id"]
    result = chunks.create_chunks(file_id, [{"doc_title": "A", "chunk_text": "private-body"}], request_key="batch_%")
    chunks.create_chunk_add_task(file_id, "other", "body", request_key="batch-other")
    response = api.post("/api/v1/task/list", json={"request_key": "batch_%"}).json()
    assert response["data"]["total"] == 1
    assert response["data"]["tasks"][0]["id"] == result["task_ids"][0]
    assert "private-body" not in json.dumps(response)


def test_export_snapshot_contains_referenced_resources_and_survives_delete(knowledge_base):
    file_id = files.create_empty_file("中文资料")["file_id"]
    chunks.create_chunk_add_task(file_id, "图示", "正文 ![图](插图/a.png)")
    knowledge_base.drain()
    work = files.export_file(file_id)
    asset = work.parent / "插图" / "a.png"
    asset.parent.mkdir()
    asset.write_bytes(b"image content")
    snapshot = files.export_snapshot(file_id, include_resources=True)
    try:
        files.delete_file(file_id)
        with ZipFile(snapshot["path"]) as archive:
            assert set(archive.namelist()) == {"中文资料.md", "插图/a.png"}
            assert "正文" in archive.read("中文资料.md").decode("utf-8")
            assert archive.read("插图/a.png") == b"image content"
    finally:
        shutil.rmtree(snapshot["temporary_dir"])


def test_export_bundle_rejects_missing_or_escaping_resources(knowledge_base):
    file_id = files.create_empty_file("保密")["file_id"]
    chunks.create_chunk_add_task(file_id, "标题", "![](../../config.json)")
    knowledge_base.drain()
    with pytest.raises(BusinessError, match="资源"):
        files.export_snapshot(file_id, include_resources=True)


def test_old_config_keeps_effective_keys_when_adding_admin_key(knowledge_base):
    path = settings._get_config_file_path()
    content = knowledge_base.settings.model_dump()
    content.pop("api")
    content["mcp"]["index_api_key"] = None
    path.write_text(json.dumps(content), encoding="utf-8")
    loaded = settings.load_settings()
    assert loaded.mcp.get_api_key("retrieval") == loaded.mcp.get_api_key("index") == "read-test-key"
    assert loaded.api.admin_key != "read-test-key"
    assert json.loads(path.read_text(encoding="utf-8"))["api"]["admin_key"] == loaded.api.admin_key


def test_redaction_does_not_hide_nonsecret_token_counts(knowledge_base):
    shown = config_service.show_config()["config"]
    assert shown["embedding"]["max_tokens"] == 8192
    assert shown["embedding"]["api_key"] == "***"
    assert "test-embedding-key" not in json.dumps(shown)


def test_gui_edit_preserves_pending_and_concurrent_configuration(knowledge_base, monkeypatch):
    pytest.importorskip("nicegui")
    from app.ui.handlers.settings_handlers import SettingsHandlers
    from nicegui import ui
    monkeypatch.setattr(ui, "notify", lambda *a, **kw: None)
    form = {}
    handler = SettingsHandlers(form)
    handler.init_settings_form()
    next_path = str(knowledge_base.path / "next")
    config_service.update_config({"data_path": next_path, "webdav": {"last_sync_time": "2026-09-09T10:00:00"}})
    form["theme"] = "dark"
    handler.save_settings_form()
    saved = config_service.get_saved_settings()
    assert saved.data_path == next_path and settings.get_settings().data_path != next_path
    assert saved.appearance.theme == "dark"
    assert saved.webdav.last_sync_time == "2026-09-09T10:00:00"


def test_gui_busy_failures_are_visible_and_do_not_clear_selection(knowledge_base, monkeypatch):
    pytest.importorskip("nicegui")
    from app.ui.handlers.file_handlers import FileHandlers
    from app.ui.handlers.chunk_handlers import ChunkHandlers
    from nicegui import ui
    notifications = []
    monkeypatch.setattr(ui, "notify", lambda message, **kw: notifications.append((message, kw)))
    file_id = files.create_empty_file("pending")["file_id"]
    files.reindex_file(file_id)
    state = {"selected_file_id": file_id}
    handler = FileHandlers(state, {})
    asyncio.run(handler._do_delete_file(file_id))
    assert state["selected_file_id"] == file_id and files.file_exists(file_id)
    cards = ChunkHandlers(state, {}, AsyncMock())
    asyncio.run(cards._save_new_chunk(file_id, "标题", "正文"))
    assert len(notifications) == 2 and all(item[1]["type"] == "negative" for item in notifications)


def test_shutdown_cancellation_drains_sync_request_before_lifespan():
    import uvicorn
    from app.asgi_server import DrainingServer
    from indexing.utils import run_sync
    started, release = threading.Event(), threading.Event()
    events = []
    def write():
        started.set()
        assert release.wait(5)
        events.append("write-finished")
    async def scenario():
        server = DrainingServer(uvicorn.Config("unused", timeout_graceful_shutdown=1))
        server.config.timeout_graceful_shutdown = 0.01
        server.servers = []
        server.lifespan = SimpleNamespace(shutdown=AsyncMock(side_effect=lambda: events.append("lifespan-stop")))
        task = asyncio.create_task(run_sync(write))
        server.server_state.tasks.add(task)
        task.add_done_callback(server.server_state.tasks.discard)
        assert await asyncio.to_thread(started.wait, 5)
        stopping = asyncio.create_task(server.shutdown())
        try:
            await asyncio.sleep(0.2)
            assert not stopping.done() and not server.lifespan.shutdown.called
        finally:
            release.set()
        await stopping
        assert task.cancelled()
    asyncio.run(scenario())
    assert events == ["write-finished", "lifespan-stop"]
