"""VLM 文档级 helper：真实 PDF/子进程，模型请求全部使用离线替身。"""

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from indexing.services import parser_helper, vlm_client
from indexing.services.parser_helper import ParserError, ParserSession, ParserStopped


@pytest.fixture
def environment(tmp_path, monkeypatch):
    parser_helper.stop_parser_helpers()
    parser_helper.start_parser_helpers(2)
    pdf = tmp_path / "文档会话.pdf"
    with pymupdf.open() as document:
        for page in range(6):
            document.new_page().insert_text((72, 72), f"Document page {page + 1}")
        document.save(pdf)
    config = SimpleNamespace(vlm_concurrency=3, vlm_dpi=72, vlm_base_url="unused",
                             vlm_api_key="unused", vlm_model="offline-test")
    monkeypatch.setattr(vlm_client, "get_ocr_config", lambda: config)
    processes, directories, closed = [], [], []
    original = parser_helper.Popen

    def spawn(command, **kwargs):
        process = original(command, **kwargs)
        processes.append(process)
        directories.append(Path(command[-1]).parent)
        return process

    class Client:
        def __init__(self, *_):
            pass

        def parse_page(self, image, page, _stop_check):
            assert image.startswith(b"\xff\xd8"), "VLM 必须继续收到 JPEG"
            return f"page {page}"

        def close(self):
            closed.append(True)

    monkeypatch.setattr(parser_helper, "Popen", spawn)
    monkeypatch.setattr(vlm_client, "VlmClient", Client)
    yield SimpleNamespace(pdf=pdf, config=config, processes=processes, directories=directories,
                          client=Client, closed=closed)
    parser_helper.stop_parser_helpers()
    parser_helper.start_parser_helpers(2)


def _assert_reaped(environment):
    assert all(process.poll() is not None for process in environment.processes)
    assert all(not directory.exists() for directory in environment.directories)
    assert not parser_helper._sessions and parser_helper._calls == 0


def test_vlm_reuses_one_helper_and_returns_pages_in_order(environment, monkeypatch):
    second_finished = threading.Event()
    completed, progress = [], []

    def parse_page(_self, image, page, _stop_check):
        assert image.startswith(b"\xff\xd8")
        if page == 1:
            assert second_finished.wait(5)
        completed.append(page)
        if page == 2:
            second_finished.set()
        return f"page {page}"

    monkeypatch.setattr(environment.client, "parse_page", parse_page)
    pages = list(vlm_client.iter_vlm_pdf_pages(
        environment.pdf, on_parse_progress=lambda done, total: progress.append((done, total)),
    ))
    assert completed.index(2) < completed.index(1), "模拟 HTTP 返回乱序"
    assert [page.page_number for page in pages] == list(range(1, 7))
    assert [page.page_text for page in pages] == [f"page {page}" for page in range(1, 7)]
    assert progress == [(page, 6) for page in range(1, 7)]
    assert len(environment.processes) == 1, "页数查询与所有页图必须复用同一进程"
    assert environment.closed == [True]
    _assert_reaped(environment)


def test_next_document_uses_a_new_process(environment):
    for _ in range(2):
        assert len(list(vlm_client.iter_vlm_pdf_pages(environment.pdf))) == 6
        _assert_reaped(environment)
    assert len(environment.processes) == 2


def test_document_session_does_not_reserve_index_slot_while_idle(environment):
    parser_helper.start_parser_helpers(1)
    with ParserSession() as document:
        assert document.run("page_count", environment.pdf) == 6
        native = document.process
        slots = parser_helper._slots
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert slots.acquire(blocking=False), "HTTP 等待期间不能占着解析名额"
            try:
                image = pool.submit(parser_helper.run_preview_renderer, environment.pdf, 1, 72).result(timeout=10)
                assert image is not None and image.startswith(b"\x89PNG")
            finally:
                slots.release()
        preview = parser_helper._preview_session
        assert preview is not None and preview.process is not native
        parser_helper.stop_parser_helpers()
        _assert_reaped(environment)
        parser_helper.start_parser_helpers(1)
        with pytest.raises(ParserStopped, match="会话已关闭"):
            document.run("page_count", environment.pdf)


@pytest.mark.parametrize("action", ["close", "cancel"])
def test_early_generator_exit_reaps_document_helper(environment, action):
    stop = threading.Event()
    pages = vlm_client.iter_vlm_pdf_pages(environment.pdf, stop_check=stop.is_set)
    assert next(pages).page_number == 1
    if action == "close":
        pages.close()
    else:
        stop.set()
        with pytest.raises(vlm_client.VlmError, match="取消"):
            next(pages)
    assert environment.closed == [True]
    _assert_reaped(environment)


def test_native_crash_during_render_does_not_poison_the_next_document(environment, monkeypatch):
    command = parser_helper._helper_command
    script = """
import os, sys
from indexing import worker_process as worker
original = worker._parse
def parse(operation, args):
    if operation == 'render_page' and args[1] == 2:
        os._exit(73)
    return original(operation, args)
worker._parse = parse
worker.parser_main(sys.argv[1])
"""
    monkeypatch.setattr(parser_helper, "_helper_command", lambda path: [sys.executable, "-c", script, str(path)])
    with pytest.raises(ParserError, match="exit_code=73"):
        list(vlm_client.iter_vlm_pdf_pages(environment.pdf))
    assert environment.closed == [True]
    _assert_reaped(environment)
    monkeypatch.setattr(parser_helper, "_helper_command", command)
    assert len(list(vlm_client.iter_vlm_pdf_pages(environment.pdf))) == 6
    _assert_reaped(environment)


def test_http_failure_reaps_document_helper(environment, monkeypatch):
    def fail(*_):
        raise vlm_client.VlmError("offline request failed", retriable=False)

    monkeypatch.setattr(environment.client, "parse_page", fail)
    with pytest.raises(vlm_client.VlmError, match="offline request failed"):
        list(vlm_client.iter_vlm_pdf_pages(environment.pdf))
    assert environment.closed == [True]
    _assert_reaped(environment)


def test_page_limit_reaps_helper_before_creating_http_client(environment, monkeypatch):
    monkeypatch.setattr(vlm_client, "MAX_PDF_PAGES", 1)
    with pytest.raises(ValueError, match="页数过多"):
        list(vlm_client.iter_vlm_pdf_pages(environment.pdf))
    assert not environment.closed
    _assert_reaped(environment)


@pytest.mark.parametrize("blocked_slot", [False, True])
def test_shutdown_reaps_document_active_and_waiting_calls(environment, monkeypatch, blocked_slot):
    monkeypatch.setattr(parser_helper, "_helper_command", lambda path: [
        sys.executable, "-c", "import time; time.sleep(30)", str(path),
    ])
    parser_helper.start_parser_helpers(1)
    slots = parser_helper._slots
    if blocked_slot:
        slots.acquire()
    try:
        with ParserSession() as document, ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(document.run, "page_count", environment.pdf) for _ in range(2)]
            try:
                deadline = time.monotonic() + 5
                while parser_helper._calls < 2 or (not blocked_slot and document.process is None):
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
            finally:
                parser_helper.stop_parser_helpers()
            for future in futures:
                with pytest.raises(ParserStopped):
                    future.result(timeout=5)
    finally:
        if blocked_slot:
            slots.release()
    assert len(environment.processes) == (0 if blocked_slot else 1)
    _assert_reaped(environment)


@pytest.mark.parametrize("winerror", [32, 33, 5])
def test_cleanup_retries_only_transient_sharing_conflicts(environment, monkeypatch, winerror):
    with ParserSession() as document:
        assert document.run("page_count", environment.pdf) == 6
        directory = document.directory
        assert directory is not None
        original = directory.cleanup
        calls = []

        def cleanup():
            calls.append(True)
            if len(calls) == 1:
                error = PermissionError("injected sharing conflict")
                error.winerror = winerror
                raise error
            original()

        with monkeypatch.context() as patch:
            patch.setattr(directory, "cleanup", cleanup)
            if winerror == 5:
                with pytest.raises(PermissionError):
                    document.close()
                assert len(calls) == 1, "真正的权限错误不能被重试或忽略"
            else:
                document.close()
                assert len(calls) == 2
    _assert_reaped(environment)
