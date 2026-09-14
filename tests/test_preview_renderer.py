"""专用原页进程的复用、去重、隔离和退出回收；不接触真实知识库。"""

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf
import pytest

from indexing.services import page_render, parser_helper
from indexing.services.parser_helper import ParserError, ParserStopped, run_preview_renderer


def _preview():
    session = parser_helper._preview_session
    assert session is not None
    return session


@pytest.fixture(autouse=True)
def helpers():
    parser_helper.stop_parser_helpers()
    parser_helper.start_parser_helpers(2)
    yield
    parser_helper.stop_parser_helpers()
    parser_helper.start_parser_helpers(2)


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "预览.pdf"
    with pymupdf.open() as document:
        for number in range(3):
            document.new_page().insert_text((72, 72), f"Preview page {number + 1}")
        document.save(path)
    return path


def test_pages_reuse_one_process_without_taking_index_slots(pdf):
    parser_helper.start_parser_helpers(1)
    # 即使索引占满所有名额，交互预览仍能完成。
    with ThreadPoolExecutor(max_workers=1) as pool, parser_helper._slots:
        image = pool.submit(run_preview_renderer, pdf, 1, 100).result(timeout=10)
    assert image is not None and image.startswith(b"\x89PNG")
    process = _preview().process
    image = run_preview_renderer(pdf, 2, 100)
    assert image is not None and image.startswith(b"\x89PNG")
    assert _preview().process is process
    assert run_preview_renderer(pdf, 99, 100) is None
    assert _preview().process is process


def test_concurrent_same_page_publishes_only_one_cached_image(pdf, tmp_path, monkeypatch):
    cache = tmp_path / "pages"
    cache.mkdir()
    monkeypatch.setattr(page_render, "get_page_cache_dir", lambda: cache)
    original = page_render.run_preview_renderer
    calls = []

    def render(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(page_render, "run_preview_renderer", render)
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: page_render.render_pdf_page(pdf, 1), range(4)))
    assert paths[0] is not None and all(path == paths[0] for path in paths)
    assert len(calls) == 1
    assert list(cache.iterdir()) == [paths[0]]
    assert page_render.render_pdf_page(pdf, 1) == paths[0]
    assert len(calls) == 1


def test_stop_reaps_idle_process_and_supports_restart(pdf):
    processes = []
    for _ in range(2):
        parser_helper.start_parser_helpers(2)
        assert run_preview_renderer(pdf, 1, 100)
        session = _preview()
        process = session.process
        assert process is not None and session.directory is not None
        directory = Path(session.directory.name)
        processes.append(process)
        parser_helper.stop_parser_helpers()
        assert process.poll() is not None and not directory.exists()
        assert parser_helper._preview_session is None
        assert not parser_helper._sessions
        with pytest.raises(ParserStopped):
            run_preview_renderer(pdf, 2, 100)
    assert processes[0] is not processes[1]


@pytest.mark.parametrize(("script", "message"), [
    ("import os; os._exit(73)", "exit_code=73"),
    ("import time; time.sleep(60)", "超时"),
])
def test_crash_or_timeout_is_reaped_and_next_request_recovers(pdf, monkeypatch, script, message):
    command = parser_helper._helper_command
    processes, directories = [], []
    popen = parser_helper.Popen

    def spawn(args, **kwargs):
        process = popen(args, **kwargs)
        processes.append(process)
        directories.append(Path(args[-1]).parent)
        return process

    monkeypatch.setattr(parser_helper, "Popen", spawn)
    monkeypatch.setattr(parser_helper, "_helper_command", lambda path: [sys.executable, "-c", script, str(path)])
    with pytest.raises(ParserError, match=message):
        run_preview_renderer(pdf, 1, 100, timeout=0.5)
    assert all(process.poll() is not None for process in processes)
    assert all(not directory.exists() for directory in directories)
    assert parser_helper._calls == 0
    monkeypatch.setattr(parser_helper, "_helper_command", command)
    assert (image := run_preview_renderer(pdf, 1, 100)) is not None and image.startswith(b"\x89PNG")


def test_shutdown_drains_active_and_waiting_preview_calls(pdf, monkeypatch):
    monkeypatch.setattr(parser_helper, "_helper_command", lambda path: [
        sys.executable, "-c", "import time; time.sleep(60)", str(path),
    ])
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_preview_renderer, pdf, 1, 100) for _ in range(2)]
        deadline = time.monotonic() + 5
        try:
            while (parser_helper._preview_session is None or _preview().process is None
                   or parser_helper._calls < 2):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            session = _preview()
            process = session.process
            assert process is not None and session.directory is not None
            directory = Path(session.directory.name)
        finally:
            parser_helper.stop_parser_helpers()
        for future in futures:
            with pytest.raises(ParserStopped):
                future.result(timeout=5)
    assert process.poll() is not None and not directory.exists()
    assert parser_helper._calls == 0


def test_idle_crash_is_restarted_and_bad_pdf_does_not_poison_renderer(pdf, tmp_path):
    assert run_preview_renderer(pdf, 1, 100)
    process = _preview().process
    assert process is not None
    process.terminate()
    process.wait(timeout=5)
    assert run_preview_renderer(pdf, 2, 100)
    assert _preview().process is not process
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a PDF")
    with pytest.raises(ParserError):
        run_preview_renderer(bad, 1, 100)
    assert run_preview_renderer(pdf, 1, 100)


def test_cli_dispatch_works_without_standard_streams_or_gui(pdf, monkeypatch):
    script = """
import sys
from indexing import worker_process as worker
original = worker._parse
def checked(*args):
    result = original(*args)
    assert not {'nicegui', 'httpx', 'indexing.database', 'indexing.services.processor'} & sys.modules.keys()
    return result
worker._parse = checked
sys.stdin = sys.stdout = sys.stderr = None
from app.cli import main
main(['--parse-helper', sys.argv[1]])
"""
    monkeypatch.setattr(parser_helper, "_helper_command", lambda path: [sys.executable, "-c", script, str(path)])
    assert (image := run_preview_renderer(pdf, 1, 100)) is not None and image.startswith(b"\x89PNG")
    assert (image := run_preview_renderer(pdf, 2, 100)) is not None and image.startswith(b"\x89PNG")


def test_standalone_script_exit_cleans_up_preview_process(pdf, tmp_path):
    report = tmp_path / "preview.json"
    script = """
import json, sys
from pathlib import Path
from indexing.services import parser_helper as helper
assert helper.run_preview_renderer(Path(sys.argv[1]), 1, 100)
Path(sys.argv[2]).write_text(json.dumps({'directory': helper._preview_session.directory.name}), encoding='utf-8')
"""
    result = subprocess.run([sys.executable, "-c", script, str(pdf), str(report)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not Path(json.loads(report.read_text(encoding="utf-8"))["directory"]).exists()
