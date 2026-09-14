"""服务内任务编排、唯一数据库写进程，以及停止时的线程/HTTP 收尾。"""

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indexing import database, settings
from indexing.services import chunk_service, file_service, task_service
from indexing.services import ocr_client, parser_helper, processor, rate_limiter, vlm_client
from indexing.services.sync_service import SyncResult, SyncService
from indexing.utils import run_sync, serialize_float32
from indexing.worker_manager import WorkerManager


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    config = settings.AppSettings(
        data_path=str(tmp_path),
        embedding=settings.EmbeddingSettings(api_key="test-only", vector_dim=2),
        performance=settings.PerformanceSettings(worker_concurrency=1),
    )
    monkeypatch.setattr(settings, "_settings", config)
    monkeypatch.setattr(settings, "DEFAULT_DATA_PATH", tmp_path)
    monkeypatch.setattr(rate_limiter, "_rate_limiter", None)
    database.close_connection_pool()
    database.init_database()
    database.init_connection_pool(pool_size=4)
    file_service.ensure_files_dir()
    yield tmp_path
    parser_helper.stop_parser_helpers()
    parser_helper.start_parser_helpers(2)
    database.close_connection_pool()


def _file(root, name, pdf=False):
    working = root / "files" / "working" / f"{name}.md"
    working.write_text("# 标题\n\n需要索引的正文。", encoding="utf-8")
    original = None
    if pdf:
        original = root / "files" / "originals" / f"{name}.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), "Piece indexing in the service process")
            document.save(original)
    return file_service.insert_file_record(
        file_hash=name,
        filename=working.name,
        file_path=str(working),
        file_size=working.stat().st_size,
        original_file_type="pdf" if pdf else "md",
        original_file_path=str(original) if original else None,
    )


async def _until(predicate, timeout=10):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "等待状态变化超时"
        await asyncio.sleep(0.01)


def _fake_embeddings(monkeypatch, callback=None):
    class Embeddings:
        async def aembed_documents(self, texts):
            if callback is not None:
                await callback(texts)
            return [[1.0, 0.0] for _ in texts]

        def embed_documents(self, _texts):
            pytest.fail("索引 HTTP 应使用可取消的异步 API")

    model = Embeddings()
    monkeypatch.setattr(processor, "get_embeddings_model", lambda: model)
    monkeypatch.setattr(chunk_service, "get_embeddings_model", lambda: model)
    return model


def test_real_pdf_indexing_uses_service_loop_and_database(monkeypatch, workspace):
    requests = []
    writes = []
    children = []
    spawn = parser_helper.Popen

    async def embed(texts):
        requests.append((os.getpid(), asyncio.get_running_loop(), texts))

    def record_spawn(*args, **kwargs):
        child = spawn(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(parser_helper, "Popen", record_spawn)
    _fake_embeddings(monkeypatch, embed)
    for connection in database._connection_pool._all_connections:
        connection.set_trace_callback(lambda sql: writes.append(os.getpid()) if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) else None)

    file_id = _file(workspace, "real", pdf=True)
    task_id = task_service.create_task("real.pdf", file_id)

    async def check():
        manager = WorkerManager()
        await asyncio.gather(manager.start(), manager.start())
        try:
            assert manager._task.get_loop() is asyncio.get_running_loop()
            await _until(lambda: task_service.get_task(task_id)["status"] == "completed")
            assert task_service.get_task(task_id)["progress"] == 100
            assert file_service.get_file_by_id(file_id)["status"] == "indexed"
            assert file_service.get_chunks_by_file_id(file_id)
            assert requests and all(pid == os.getpid() and loop is asyncio.get_running_loop() for pid, loop, _ in requests)
            assert writes and set(writes) == {os.getpid()}
            assert children and all(child.pid != os.getpid() and child.poll() == 0 for child in children)
            assert not parser_helper._active
        finally:
            await manager.stop()
        await manager.stop()
        assert not manager.is_alive()

    asyncio.run(check())


def test_parser_crash_only_fails_its_file_and_can_retry(monkeypatch, workspace):
    settings.get_settings().performance.worker_concurrency = 2
    _fake_embeddings(monkeypatch)
    original_command = parser_helper._helper_command

    def crash_one(path):
        request = json.loads(path.read_text(encoding="utf-8"))
        if request["args"] and str(request["args"][0]).endswith("broken.pdf"):
            return [sys.executable, "-c", "import os; os._exit(71)", str(path)]
        return original_command(path)

    monkeypatch.setattr(parser_helper, "_helper_command", crash_one)
    broken_file = _file(workspace, "broken", pdf=True)
    good_file = _file(workspace, "good", pdf=True)
    broken = task_service.create_task("broken.pdf", broken_file)
    good = task_service.create_task("good.pdf", good_file)

    async def check():
        manager = WorkerManager()
        await manager.start()
        try:
            await _until(lambda: task_service.get_task(broken)["status"] == "failed" and task_service.get_task(good)["status"] == "completed")
            assert "exit_code=71" in task_service.get_task(broken)["error_message"]
            assert manager.is_alive()
            assert file_service.get_file_by_id(good_file)["status"] == "indexed"
            monkeypatch.setattr(parser_helper, "_helper_command", original_command)
            retry = task_service.create_task("broken.pdf", broken_file)
            await _until(lambda: task_service.get_task(retry)["status"] == "completed")
            assert file_service.get_file_by_id(broken_file)["status"] == "indexed"
        finally:
            await manager.stop()

    asyncio.run(check())


def test_stop_waits_for_database_thread_and_preserves_pending_tasks(monkeypatch, workspace):
    _fake_embeddings(monkeypatch)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    insert = processor.insert_chunks_batch

    def slow_insert(*args):
        started.set()
        assert release.wait(5)
        insert(*args)
        finished.set()

    monkeypatch.setattr(processor, "insert_chunks_batch", slow_insert)
    file_id = _file(workspace, "slow")
    first = task_service.create_task("slow.md", file_id)
    second = task_service.create_task("queued.md", _file(workspace, "queued"))

    async def check():
        manager = WorkerManager(shutdown_timeout=0.02)
        await manager.start()
        closing = None
        try:
            await _until(started.is_set)
            closing = asyncio.create_task(manager.stop())
            await asyncio.sleep(0.1)
            assert not closing.done(), "不能遗留写线程后就宣称 Worker 已停止"
            assert database._connection_pool is not None
        finally:
            release.set()
            if closing is not None:
                await closing
            else:
                await manager.stop()
        assert finished.is_set()
        assert task_service.get_task(first)["status"] == "failed"
        assert task_service.get_task(second)["status"] == "pending"
        assert not task_service.get_processing_tasks()
        assert not manager.is_alive()

    asyncio.run(check())


@pytest.mark.parametrize("kind", ["file", "chunk_add", "chunk_update"])
def test_shutdown_cancels_async_embedding_for_all_task_types(monkeypatch, workspace, kind):
    entered = threading.Event()
    cancelled = []

    async def embedding(_texts):
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    _fake_embeddings(monkeypatch, embedding)
    file_id = _file(workspace, "cancel")
    if kind == "file":
        task_id = task_service.create_task("cancel.md", file_id)
    elif kind == "chunk_add":
        task_id = chunk_service.create_chunk_add_task(file_id, "新切片", "正文")
    else:
        chunk_id = chunk_service._chunk_repo.insert(file_id, "原切片", "原正文", serialize_float32([1.0, 0.0]))
        task_id = chunk_service.create_chunk_update_task(chunk_id, "新正文")

    async def check():
        manager = WorkerManager(shutdown_timeout=0.01)
        await manager.start()
        try:
            await _until(entered.is_set)
        finally:
            await manager.stop()
        assert cancelled == [True]
        assert task_service.get_task(task_id)["status"] == "failed"
        assert not manager.is_alive()

    asyncio.run(check())


def test_run_sync_survives_repeated_cancellation():
    started = threading.Event()
    release = threading.Event()
    finished = []

    def write():
        started.set()
        assert release.wait(5)
        finished.append(True)

    async def check():
        task = asyncio.create_task(run_sync(write))
        try:
            await _until(started.is_set)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0.01)
                assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == [True]

    asyncio.run(check())


def test_vlm_stops_retries_and_drains_http_before_closing(monkeypatch, workspace):
    started = threading.Event()
    release = threading.Event()
    stop = threading.Event()
    closed = []
    active = []
    settings.get_settings().ocr.vlm_concurrency = 2

    class Client:
        def __init__(self, *_args):
            pass

        def parse_page(self, _image, page, stop_check=None):
            active.append(page)
            started.set()
            assert release.wait(5)
            active.remove(page)
            return f"page {page}"

        def close(self):
            assert not active
            closed.append(True)

    monkeypatch.setattr(vlm_client, "VlmClient", Client)
    monkeypatch.setattr(vlm_client.ParserSession, "run", lambda *_: 2)
    monkeypatch.setattr(vlm_client, "_render_page_jpeg", lambda *_: b"jpeg")
    pages = vlm_client.iter_vlm_pdf_pages(workspace / "unused.pdf", stop_check=stop.is_set)

    async def check():
        advancing = asyncio.create_task(run_sync(next, pages, None))
        try:
            await _until(started.is_set)
            stop.set()
            await asyncio.sleep(0.15)
            assert not advancing.done() and not closed
        finally:
            release.set()
        with pytest.raises(vlm_client.VlmError, match="取消"):
            await advancing
        assert closed == [True]
        assert not active

    asyncio.run(check())


def test_vlm_task_timeout_is_not_mistaken_for_wait_timeout(monkeypatch, workspace):
    from time import monotonic

    def parse_page(*_args):
        raise TimeoutError("request timed out")

    client = SimpleNamespace(parse_page=parse_page, close=lambda: None)
    monkeypatch.setattr(vlm_client, "VlmClient", lambda *_: client)
    monkeypatch.setattr(vlm_client.ParserSession, "run", lambda *_: 1)
    monkeypatch.setattr(vlm_client, "_render_page_jpeg", lambda *_: b"jpeg")
    deadline = monotonic() + 2
    pages = vlm_client.iter_vlm_pdf_pages(
        workspace / "unused.pdf", stop_check=lambda: monotonic() >= deadline,
    )
    with pytest.raises(TimeoutError, match="request timed out"):
        next(pages)


@pytest.mark.parametrize("cancel_after_poll", [False, True])
def test_ocr_http_and_progress_stay_in_service(monkeypatch, workspace, cancel_after_poll):
    calls = []
    closed = []
    stop = threading.Event()
    file_id = _file(workspace, "ocr", pdf=True)
    original = Path(file_service.get_file_by_id(file_id)["original_file_path"])

    class Client(ocr_client.OcrClient):
        def __init__(self, *_args):
            pass

        def submit_job(self, *_args, **_kwargs):
            calls.append(os.getpid())
            return "local-job"

        def poll_until_done(self, _job, **kwargs):
            calls.append(os.getpid())
            kwargs["progress_callback"](1, 1)
            if cancel_after_poll:
                stop.set()
            return "local-result"

        def fetch_result_jsonl(self, _url):
            assert not stop.is_set()
            calls.append(os.getpid())
            return [{"result": {"layoutParsingResults": [{"markdown": {"text": "OCR text"}}]}}]

        def close(self):
            closed.append(True)

    monkeypatch.setattr(ocr_client, "OcrClient", Client)
    pages = ocr_client.iter_ocr_pdf_pages(
        original, workspace / "images",
        on_parse_progress=lambda *_: calls.append(os.getpid()),
        stop_check=stop.is_set,
    )
    if cancel_after_poll:
        with pytest.raises(ocr_client.OcrError, match="取消"):
            list(pages)
    else:
        assert [page.page_text for page in pages] == ["OCR text"]
    assert calls and set(calls) == {os.getpid()}
    assert closed == [True]


def test_sync_shutdown_waits_for_registration_callback(monkeypatch, workspace):
    service = SyncService()
    entered = threading.Event()
    release = threading.Event()
    registered = []
    monkeypatch.setattr(service, "sync", lambda *_: SyncResult(success=True, message="done"))

    def register(_result):
        entered.set()
        assert release.wait(5)
        registered.append(task_service.create_task("downloaded.md", _file(workspace, "downloaded")))

    async def check():
        assert service.start_background_sync(register)
        await _until(entered.is_set)
        stopping = asyncio.create_task(run_sync(service.stop))
        try:
            await asyncio.sleep(0.05)
            assert not stopping.done()
            assert service.last_finished_at is None
            assert not service.start_background_sync(register)
        finally:
            release.set()
            await stopping
        assert not service.is_running()
        assert task_service.get_task(registered[0])["status"] == "pending"
        assert service.last_finished_at is not None

    asyncio.run(check())
