"""窗口生命周期回归：不打开真实 GUI，不访问用户的数据库。"""

import asyncio
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import window


@pytest.mark.parametrize("native", [True, False])
def test_owned_window_process_is_reaped(monkeypatch, native):
    process = window._spawn([sys.executable, "-c", "import time; time.sleep(60)"])
    native_open = Mock(return_value=process if native else None)
    browser_open = Mock(return_value=process)
    monkeypatch.setattr(window, "_window_process", None)
    monkeypatch.setattr(window, "_open_native", native_open)
    monkeypatch.setattr(window, "_open_browser", browser_open)
    try:
        window.open_window("http://127.0.0.1:8689")
        window.open_window("http://127.0.0.1:8689")
        assert process.poll() is None
        assert native_open.call_count == 1
        assert browser_open.call_count == (0 if native else 1)
        window.close_window()
        assert process.poll() is not None
        assert window._window_process is None
        window.close_window()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_concurrent_open_requests_share_one_window(monkeypatch):
    process = Mock()
    process.poll.return_value = None

    def open_native(_):
        time.sleep(0.05)
        return process

    native_open = Mock(side_effect=open_native)
    monkeypatch.setattr(window, "_window_process", None)
    monkeypatch.setattr(window, "_open_native", native_open)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(window.open_window, ["http://127.0.0.1:8689"] * 4))
    native_open.assert_called_once()
    window.close_window()


def test_manually_closed_window_can_be_reopened(monkeypatch):
    old_process = Mock()
    old_process.poll.return_value = 0
    new_process = Mock()
    new_process.poll.return_value = None
    monkeypatch.setattr(window, "_window_process", old_process)
    monkeypatch.setattr(window, "_open_native", Mock(return_value=new_process))
    window.open_window("http://127.0.0.1:8689")
    assert window._window_process is new_process
    window.close_window()
    old_process.terminate.assert_not_called()
    new_process.terminate.assert_called_once()


def test_cleanup_kills_unresponsive_window(monkeypatch):
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("window", 5), 0]
    monkeypatch.setattr(window, "_window_process", process)
    window.close_window()
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.wait.call_count == 2
    assert window._window_process is None


def test_cleanup_failure_does_not_interrupt_shutdown(monkeypatch, caplog):
    process = Mock()
    process.poll.return_value = None
    process.terminate.side_effect = OSError("cannot terminate")
    monkeypatch.setattr(window, "_window_process", process)
    window.close_window()
    assert "关闭窗口失败" in caplog.text
    assert window._window_process is None


def test_default_browser_is_not_closed(monkeypatch):
    monkeypatch.setattr(window, "_window_process", None)
    monkeypatch.setattr(window, "_open_native", lambda _: None)
    monkeypatch.setattr(window, "find_chromium", lambda: None)
    opened = Mock(return_value=True)
    monkeypatch.setattr(window.webbrowser, "open", opened)
    window.open_window("http://127.0.0.1:8689")
    window.close_window()
    opened.assert_called_once_with("http://127.0.0.1:8689")
    assert window._window_process is None
