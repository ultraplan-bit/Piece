"""第 4 步：真实解析 helper 的成功、崩溃、超时、停机和无数据库边界。"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indexing.services import converter, office_convert, parser_helper
from indexing.services.parser_helper import ParserError, ParserStopped, run_parser


@pytest.fixture(autouse=True)
def helpers():
    parser_helper.start_parser_helpers(2)
    yield
    parser_helper.stop_parser_helpers()
    parser_helper.start_parser_helpers(2)


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "中文 空格.pdf"
    with pymupdf.open() as document:
        for index in range(5):
            document.new_page().insert_text((72, 72), f"Piece page {index + 1}")
        document.save(path)
    return path


def _record_processes(monkeypatch):
    original = parser_helper.Popen
    processes = []
    directories = []

    def spawn(command, **kwargs):
        directories.append(Path(command[-1]).parent)
        process = original(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(parser_helper, "Popen", spawn)
    return processes, directories


def test_process_recording_does_not_replace_stdlib_popen(monkeypatch, pdf):
    import subprocess

    original = subprocess.Popen
    processes, _ = _record_processes(monkeypatch)
    assert subprocess.Popen is original
    # MCP SDK 在导入时就会求值这个注解，不能等到创建进程才检查。
    assert getattr(subprocess, "Popen")[bytes].__origin__ is original
    assert run_parser("page_count", pdf) == 5
    assert len(processes) == 1 and processes[0].returncode == 0


def test_native_operations_never_import_database_or_service(monkeypatch, pdf, tmp_path):
    # 在子进程中检查导入边界，不能用父进程的 monkeypatch 伪装成隔离。
    script = (
        "import sys; from indexing.worker_process import parser_main; "
        "parser_main(sys.argv[1]); "
        "assert 'indexing.database' not in sys.modules; "
        "assert 'indexing.services.processor' not in sys.modules; "
        "assert 'nicegui' not in sys.modules; "
        "assert 'httpx' not in sys.modules"
    )
    monkeypatch.setattr(parser_helper, "_helper_command", lambda path: [sys.executable, "-c", script, str(path)])
    processes, directories = _record_processes(monkeypatch)

    assert run_parser("page_count", pdf) == 5
    pages = list(converter.iter_pdf_pages(pdf))
    assert [page["page_number"] for page in pages] == [1, 2, 3, 4, 5]
    assert "Piece page 3" in pages[2]["page_text"]
    assert run_parser("text_stats", pdf)["chars"] > 0
    assert run_parser("render_page", pdf, 1, 72, "png").startswith(b"\x89PNG")
    assert run_parser("render_page", pdf, 1, 72, "jpeg").startswith(b"\xff\xd8")
    assert run_parser("probe_image").startswith(b"\xff\xd8")
    part = tmp_path / "part.pdf"
    run_parser("extract_pages", pdf, 2, 3, part)
    assert run_parser("page_count", part) == 2

    assert processes and all(process.pid != os.getpid() and process.returncode == 0 for process in processes)
    assert all(not directory.exists() for directory in directories)
    assert not parser_helper._active and parser_helper._calls == 0


def test_text_is_requested_in_bounded_batches(monkeypatch, pdf):
    original = parser_helper._helper_command
    requests = []

    def command(path):
        requests.append(json.loads(path.read_text(encoding="utf-8")))
        return original(path)

    monkeypatch.setattr(parser_helper, "_helper_command", command)
    monkeypatch.setattr(converter, "PDF_BATCH_PAGES", 2)
    assert len(list(converter.iter_pdf_pages(pdf))) == 5
    assert [request["args"][1:] for request in requests] == [[0, 2], [2, 4], [4, 6]]


def test_bad_file_and_invalid_page_do_not_break_next_request(pdf, tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a PDF")
    with pytest.raises(ParserError):
        run_parser("page_count", bad)
    with pytest.raises(FileNotFoundError):
        run_parser("page_count", tmp_path / "missing.pdf")
    with pytest.raises(ValueError, match="页码越界"):
        run_parser("extract_pages", pdf, 0, 1, tmp_path / "invalid.pdf")
    assert run_parser("render_page", pdf, 100, 72, "png") is None
    assert run_parser("page_count", pdf) == 5


@pytest.mark.parametrize("script, message", [
    ("import os; os._exit(73)", "exit_code=73"),
    ("import time; time.sleep(60)", "超时"),
    ("pass", "未返回有效结果"),
    ("import sys; from pathlib import Path; Path(sys.argv[1]).with_name('result.json').write_text('{')", "未返回有效结果"),
])
def test_failed_helper_is_reaped_and_next_call_works(monkeypatch, pdf, script, message):
    original = parser_helper._helper_command
    processes, directories = _record_processes(monkeypatch)
    monkeypatch.setattr(parser_helper, "_helper_command", lambda path: [sys.executable, "-c", script, str(path)])
    with pytest.raises(ParserError, match=message):
        run_parser("page_count", pdf, timeout=0.5)
    assert all(process.poll() is not None for process in processes)
    assert all(not directory.exists() for directory in directories)
    assert not parser_helper._active and parser_helper._calls == 0

    monkeypatch.setattr(parser_helper, "_helper_command", original)
    assert run_parser("page_count", pdf) == 5


def test_shutdown_drains_active_and_waiting_calls(monkeypatch, pdf):
    parser_helper.start_parser_helpers(1)
    monkeypatch.setattr(parser_helper, "_helper_command", lambda path: [
        sys.executable, "-c", "import time; time.sleep(60)", str(path),
    ])
    processes, directories = _record_processes(monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_parser, "page_count", pdf) for _ in range(2)]
        try:
            deadline = time.monotonic() + 5
            while not parser_helper._active or parser_helper._calls != 2:
                assert time.monotonic() < deadline
                time.sleep(0.01)
        finally:
            parser_helper.stop_parser_helpers()
        for future in futures:
            with pytest.raises(ParserStopped):
                future.result(timeout=5)

    assert len(processes) == 1, "等待名额的调用不能在停止后再启动进程"
    assert all(process.poll() is not None for process in processes)
    assert all(not directory.exists() for directory in directories)
    assert not parser_helper._active and parser_helper._calls == 0
    with pytest.raises(ParserStopped):
        run_parser("page_count", pdf)


def test_exe_dispatch_happens_before_app_imports(monkeypatch, pdf):
    script = """
import runpy, sys
try:
    runpy.run_module('app.server', run_name='__main__')
except SystemExit as exc:
    assert exc.code == 0
assert 'nicegui' not in sys.modules
assert 'indexing.database' not in sys.modules
assert 'app.logging_config' not in sys.modules
"""
    monkeypatch.setattr(parser_helper, "_helper_command", lambda path: [
        sys.executable, "-c", script, "--parse-helper", str(path),
    ])
    assert run_parser("page_count", pdf) == 5


def test_frozen_command_reuses_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    path = tmp_path / "request.json"
    assert parser_helper._helper_command(path) == [sys.executable, "--parse-helper", str(path)]


@pytest.mark.parametrize("same_file", [False, True])
def test_com_gate_lives_in_parent_and_covers_helper_exit(monkeypatch, tmp_path, same_file):
    import threading

    active = 0
    peak = 0
    lock = threading.Lock()
    calls = []

    def fake_parser(operation, *args, **kwargs):
        nonlocal active, peak
        assert operation == "office_com"
        with lock:
            active += 1
            peak = max(peak, active)
        calls.append((args, kwargs))
        Path(args[1]).write_bytes(b"%PDF-com")
        time.sleep(0.03)
        with lock:
            active -= 1
        return True

    monkeypatch.setattr(office_convert, "run_parser", fake_parser)

    def convert(index):
        name = 0 if same_file else index
        return office_convert._convert_with_com(tmp_path / f"{name}.docx", tmp_path / f"{name}.pdf")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(convert, range(2)))
    assert results == [True, True] and peak == 1
    assert len(calls) == (1 if same_file else 2), "同文件只转换一次，不同文件仍串行转换"
    assert all(call[1]["timeout"] == office_convert.CONVERT_TIMEOUT for call in calls)


def test_office_shutdown_does_not_try_fallback(monkeypatch, tmp_path):
    source = tmp_path / "a.docx"
    source.write_bytes(b"office")
    monkeypatch.setattr(office_convert, "list_converters", lambda: [("com", ""), ("libreoffice", "soffice")])
    monkeypatch.setattr(office_convert, "get_office_pdf_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(office_convert, "_convert_with_com", lambda *_: (_ for _ in ()).throw(ParserStopped("stop")))
    monkeypatch.setattr(office_convert, "_convert_with_libreoffice", lambda *_: pytest.fail("停止后不能再启动后端"))
    with pytest.raises(ParserStopped):
        office_convert.convert_to_pdf(source)


@pytest.mark.parametrize("backend", ["com", "libreoffice"])
def test_crashed_conversion_never_publishes_partial_cache(monkeypatch, tmp_path, backend):
    script = """
import json, os, sys
from pathlib import Path
request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
index = 1 if request['operation'] == 'office_com' else 2
Path(request['args'][index]).write_bytes(b'partial PDF')
os._exit(74)
"""
    monkeypatch.setattr(parser_helper, "_helper_command", lambda path: [sys.executable, "-c", script, str(path)])
    monkeypatch.setattr(office_convert, "_libreoffice_profile_dir", lambda: tmp_path / "profile")
    source = tmp_path / "source.docx"
    target = tmp_path / "cached.pdf"
    with pytest.raises(ParserError, match="exit_code=74"):
        if backend == "com":
            office_convert._convert_with_com(source, target)
        else:
            office_convert._convert_with_libreoffice("soffice", source, target)
    assert not target.exists()
    assert not list(tmp_path.iterdir()), "父进程必须清掉中途崩溃留下的转换产物"
