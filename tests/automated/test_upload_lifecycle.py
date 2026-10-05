"""上传传输、受理竞态与后台任务取消的离线回归。"""

import asyncio
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from indexing import database
from indexing.services import chunk_service as chunks, file_service as files, task_service as tasks, processor
from indexing.utils import run_sync


@pytest.fixture
def upload_handler(knowledge_base, monkeypatch):
    from app.ui.handlers import file_handlers

    notices = []
    monkeypatch.setattr(file_handlers.ui, "notify", lambda message, **kw: notices.append((message, kw)))
    refs = {"upload_input": MagicMock(), "upload_banner": Mock()}
    handler = file_handlers.FileHandlers({}, refs)
    handler.load_files = AsyncMock()
    return SimpleNamespace(handler=handler, refs=refs, notices=notices)


def _upload(name, paths, *, error=None, started=None, release=None):
    async def save(path):
        paths.append(path)
        path.write_text(f"# {name}\n正文", encoding="utf-8")
        if started:
            started.set()
        if release:
            await release.wait()
        if error:
            raise error
    return SimpleNamespace(name=name, save=save)


def test_upload_notifications_keep_client_context_without_mocking_notify(knowledge_base, monkeypatch):
    from nicegui import Client, core, events, ui
    from app.ui.handlers.file_handlers import FileHandlers

    source = knowledge_base.path / "duplicate.md"
    source.write_text("# duplicate.md\n正文", encoding="utf-8")
    files.import_file(source)
    paths = []

    async def scenario():
        monkeypatch.setattr(core, "loop", asyncio.get_running_loop())
        clients = [Client(ui.page(f"/_test_upload_{i}")) for i in range(2)]
        try:
            async def upload(client, names):
                # 父回调有上下文也不代表 gather 子任务有；保留真实通知与刷新路径。
                with client:
                    handler = FileHandlers({}, {})
                    uploader = ui.upload(multiple=True)
                    handler.ui_refs["upload_input"] = uploader

                    @ui.refreshable
                    def banner():
                        ui.label(str(handler.state["uploading_count"]))

                    handler.ui_refs["upload_banner"] = banner
                    banner()
                    event = events.MultiUploadEventArguments(sender=uploader, client=client,
                        files=[_upload(name, paths) for name in names])
                    await handler.handle_multi_upload(event)
                    assert handler.state["uploading_count"] == 0
                notices = [message for message in client.outbox.messages if message[1] == "notify"]
                assert all(target == client.id for target, _, _ in notices)
                return [options for _, _, options in notices]

            first, second = await asyncio.gather(
                upload(clients[0], ["first.md", "duplicate.md", "unsupported.exe"]),
                upload(clients[1], ["second.md"]),
            )
            assert sorted(notice["type"] for notice in first) == ["negative", "positive", "warning"]
            assert "unsupported.exe" in next(notice["message"] for notice in first if notice["type"] == "negative")
            assert [notice["type"] for notice in second] == ["positive"]
            # 让实际 refreshable 的后台刷新完成后再删除客户端。
            await asyncio.sleep(0)
        finally:
            for client in clients:
                client.delete()

    asyncio.run(scenario())
    assert len(files.get_files_list()) == 3
    assert len(paths) == 4 and all(not path.exists() for path in paths)


def test_upload_batch_waits_for_import_and_isolates_errors(upload_handler, monkeypatch):
    h, refs = upload_handler.handler, upload_handler.refs
    paths = []
    original_import = files.import_file

    def import_file(path, filename, collection_ids):
        if filename == "bad.md":
            raise sqlite3.OperationalError("private internal details")
        return original_import(path, filename, collection_ids)

    monkeypatch.setattr(files, "import_file", import_file)

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        sender = refs["upload_input"]
        h.on_upload_added(SimpleNamespace(sender=sender, args=2))
        event = SimpleNamespace(sender=sender, files=[_upload("bad.md", paths),
            _upload("good.md", paths, started=started, release=release)])
        work = asyncio.create_task(h.handle_multi_upload(event))
        await started.wait()
        async with asyncio.timeout(3):
            while h.state["uploading_count"] != 1:
                await asyncio.sleep(0.01)
        assert not work.done()
        release.set()
        await work
        assert h.state["uploading_count"] == 0
        # HTTP 回调可能比 WebSocket 的 added 先到，迟到的开始事件不能复活提示条。
        h.on_upload_added(SimpleNamespace(sender=sender, args=2))
        assert h.state["uploading_count"] == 0

    asyncio.run(scenario())
    assert len(files.get_files_list()) == 1
    assert any("bad.md" in notice[0] for notice in upload_handler.notices)
    assert "private internal details" not in str(upload_handler.notices)
    assert paths and all(not path.exists() for path in paths)
    h.load_files.assert_awaited_once()


def test_cancel_upload_ignores_waiting_files_and_late_old_batch(upload_handler):
    h, refs = upload_handler.handler, upload_handler.refs
    h._upload_semaphore = asyncio.Semaphore(1)
    paths = []

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        old = refs["upload_input"]
        h.on_upload_added(SimpleNamespace(sender=old, args=2))
        work = asyncio.create_task(h.handle_multi_upload(SimpleNamespace(sender=old, files=[
            _upload("old.md", paths, started=started, release=release), _upload("queued.md", paths)])))
        await started.wait()
        h.cancel_upload()
        assert h.state["uploading_count"] == 0
        old.run_method.assert_called_with("abort")
        old.reset.assert_called_once()
        notices = len(upload_handler.notices)
        h.on_upload_failed(SimpleNamespace(sender=old))
        assert len(upload_handler.notices) == notices

        # 下一批组件有新身份，旧回调不能清掉新进度，也不能继续受理旧文件。
        refs["upload_control"] = SimpleNamespace(refresh=AsyncMock())
        refs["upload_control"].refresh.side_effect = lambda: refs.update(upload_input=MagicMock())
        await h.open_file_picker()
        new = refs["upload_input"]
        h.on_upload_added(SimpleNamespace(sender=new, args=1))
        release.set()
        await work
        h.on_upload_failed(SimpleNamespace(sender=old))
        assert h.state["uploading_count"] == 1
        await h.handle_multi_upload(SimpleNamespace(sender=new, files=[_upload("new.md", paths)]))
        assert h.state["uploading_count"] == 0

    asyncio.run(scenario())
    assert [file["filename"] for file in files.get_files_list()] == ["new.md"]
    assert len(paths) == 2 and all(not path.exists() for path in paths)


@pytest.mark.parametrize("duplicate", [False, True])
def test_cancel_during_import_only_cancels_new_task(upload_handler, monkeypatch, knowledge_base, duplicate):
    h, refs = upload_handler.handler, upload_handler.refs
    source = knowledge_base.path / "source.md"
    source.write_text("# race.md\n正文", encoding="utf-8")
    previous = files.import_file(source) if duplicate else None
    started, release = threading.Event(), threading.Event()
    accepted = []
    original_import = files.import_file
    paths = []

    def import_file(*args):
        result = original_import(*args)
        accepted.append(result)
        started.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(files, "import_file", import_file)

    async def scenario():
        work = asyncio.create_task(h.handle_multi_upload(SimpleNamespace(
            sender=refs["upload_input"], files=[_upload("race.md", paths)])))
        try:
            async with asyncio.timeout(3):
                while not started.is_set():
                    await asyncio.sleep(0.01)
            h.cancel_upload()
        finally:
            release.set()
        await work

    asyncio.run(scenario())
    assert accepted[0]["duplicate"] == duplicate
    task_id = previous["task_id"] if previous else accepted[0]["task_id"]
    assert tasks.get_task(task_id)["status"] == ("pending" if duplicate else "cancelled")
    assert len(files.get_files_list()) == 1
    assert all(not path.exists() for path in paths)


def test_transfer_and_temp_creation_failures_clear_progress(upload_handler, monkeypatch):
    import tempfile
    h, refs = upload_handler.handler, upload_handler.refs
    sender = refs["upload_input"]
    h.on_upload_added(SimpleNamespace(sender=sender, args=3))
    h.on_upload_failed(SimpleNamespace(sender=sender))
    assert h.state["uploading_count"] == 0
    sender.reset.assert_called_once()
    assert upload_handler.notices[-1][1]["type"] == "negative"
    h._upload_cancelled = False
    monkeypatch.setattr(tempfile, "mkstemp", Mock(side_effect=OSError("disk full")))
    asyncio.run(h.handle_multi_upload(SimpleNamespace(sender=sender, files=[_upload("no-space.md", [])])))
    assert h.state["uploading_count"] == 0
    assert "no-space.md" in upload_handler.notices[-1][0]
    assert files.get_files_list() == []


@pytest.mark.parametrize("kind", ["file_index", "chunk_add", "chunk_update"])
def test_cancel_running_embedding_keeps_existing_data_and_can_retry(knowledge_base, monkeypatch, kind):
    file_id = files.create_empty_file("existing")["file_id"]
    first = chunks.create_chunk_add_task(file_id, "existing", "original content")
    knowledge_base.drain()
    chunk_id = tasks.get_task(first)["result"]["chunk_id"]
    working = Path(files.get_file_by_id(file_id)["file_path"])
    before = working.read_bytes()
    if kind == "file_index":
        task_id = files.reindex_file(file_id)["task_id"]
    elif kind == "chunk_add":
        task_id = chunks.create_chunk_add_task(file_id, "new", "new content")
    else:
        task_id = chunks.create_chunk_update_task(chunk_id, "updated content")
    task = tasks.claim_next_pending_task()
    other_file = files.create_empty_file("other")["file_id"]
    other = chunks.create_chunk_add_task(other_file, "other", "other content")
    original_embed = knowledge_base.model.aembed_documents
    monkeypatch.setattr(processor, "_embedding_semaphore", None)

    async def scenario():
        entered = asyncio.Event()

        async def blocked(_texts):
            entered.set()
            await asyncio.Future()

        monkeypatch.setattr(knowledge_base.model, "aembed_documents", blocked)
        work = asyncio.create_task(processor.TaskProcessor()._dispatch_task(task, threading.Event()))
        await asyncio.wait_for(entered.wait(), 3)
        requested = await run_sync(tasks.cancel_task, task_id)
        assert requested["status"] == "processing" and requested["stage"] == "cancelling"
        await asyncio.wait_for(work, 3)

    asyncio.run(scenario())
    assert tasks.get_task(task_id)["status"] == "cancelled"
    assert tasks.get_task(task_id)["error_code"] == "CANCELLED"
    assert tasks.get_task(other)["status"] == "pending"
    assert chunks.get_chunk_by_id(chunk_id)["chunk_text"] == "original content"
    assert working.read_bytes() == before
    assert files.get_file_by_id(file_id)["status"] == "indexed"
    with database.get_db_cursor() as cursor:
        assert cursor.execute("SELECT COUNT(*) FROM staged_chunks").fetchone()[0] == 0
    monkeypatch.setattr(knowledge_base.model, "aembed_documents", original_embed)
    monkeypatch.setattr(processor, "_embedding_semaphore", None)
    retry_id = tasks.retry_task(task_id)
    knowledge_base.drain()
    assert tasks.get_task(retry_id)["status"] == "completed"


def test_cancel_parser_drains_thread_before_terminal_and_blocks_publish(knowledge_base, monkeypatch):
    source = knowledge_base.path / "parse.md"
    source.write_text("# title\nbody", encoding="utf-8")
    result = files.import_file(source)
    task_id, file_id = result["task_id"], result["file_id"]
    started, release = threading.Event(), threading.Event()

    def convert(*_):
        started.set()
        assert release.wait(5)
        return "# title\nbody"

    monkeypatch.setattr(processor, "convert_to_markdown", convert)

    async def scenario():
        work = asyncio.create_task(processor.TaskProcessor()._dispatch_task(tasks.claim_next_pending_task(), threading.Event()))
        try:
            async with asyncio.timeout(3):
                while not started.is_set():
                    await asyncio.sleep(0.01)
            await run_sync(tasks.cancel_task, task_id)
            tasks.update_page_progress(task_id, 1, 3, 0, 30, "parsing")
            assert tasks.get_task(task_id)["stage"] == "cancelling"
            with pytest.raises(ValueError, match="取消"):
                processor._publish_file(task_id, file_id, Path("unused"), None)
            await asyncio.sleep(0.5)
            assert not work.done()
            assert tasks.get_task(task_id)["status"] == "processing"
            with pytest.raises(ValueError, match="在途"):
                files.delete_file(file_id)
        finally:
            release.set()
        await asyncio.wait_for(work, 3)

    asyncio.run(scenario())
    assert tasks.get_task(task_id)["status"] == "cancelled"
    assert files.get_file_by_id(file_id)["status"] == "error"
    assert source.exists() and Path(files.get_file_by_id(file_id)["original_file_path"]).exists()
    assert not list((files.get_files_dir() / ".staging").glob("task-*"))


def test_task_cancel_api_returns_request_then_idempotent_terminal(api):
    file_id = files.create_empty_file("api cancel")["file_id"]
    task_id = chunks.create_chunk_add_task(file_id, "title", "body")
    tasks.claim_next_pending_task()
    requested = api.post("/api/v1/task/cancel", json={"task_id": task_id}).json()
    assert requested["success"] and requested["data"]["stage"] == "cancelling"
    assert requested["data"]["status"] == "processing"
    tasks.finish_cancelled_task(task_id)
    assert api.post("/api/v1/task/cancel", json={"task_id": task_id}).json()["data"]["status"] == "cancelled"
