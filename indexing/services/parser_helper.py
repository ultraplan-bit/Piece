"""隔离解析子进程：普通操作按次启动，预览和 VLM 文档各自复用独立会话。"""

from __future__ import annotations

import atexit
import json
import logging
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from subprocess import Popen
from typing import Any, Callable, Optional

from app.platform import subprocess_command, subprocess_options

logger = logging.getLogger(__name__)

PARSER_TIMEOUT = 300.0
_POLL_SECONDS = 0.1
_stopping = threading.Event()
_condition = threading.Condition()
_active: set[Popen] = set()
_calls = 0
_slots = threading.BoundedSemaphore(2)
_sessions: set[ParserSession] = set()
_preview_session: ParserSession | None = None
_exit_registered = False


class ParserError(RuntimeError):
    """解析 helper 失败、崩溃或超时，服务进程仍可继续处理其他文件。"""


class ParserStopped(ParserError):
    """服务正在退出，不再启动或等待解析。"""


def start_parser_helpers(max_concurrency: int) -> None:
    """复用索引并发配置限制原生解析的峰值内存。"""
    global _slots
    with _condition:
        if _calls:
            raise RuntimeError("解析 helper 尚未停止")
        _slots = threading.BoundedSemaphore(max(1, max_concurrency))
        _stopping.clear()


def stop_parser_helpers() -> None:
    """拒绝新解析，排空调用后回收所有会话，包括等待 HTTP 的空闲 VLM helper。"""
    global _preview_session
    with _condition:
        _stopping.set()
        while _calls:
            _condition.wait(_POLL_SECONDS)
        errors = []
        for session in tuple(_sessions):
            try:
                session.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("解析会话停止失败", errors)
        _preview_session = None


def _helper_command(request_path: Path) -> list[str]:
    return subprocess_command("indexing.worker_process", "--parse-helper", str(request_path))


def _check_stopping(stop_check: Optional[Callable[[], bool]]) -> None:
    if _stopping.is_set() or (stop_check is not None and stop_check()):
        raise ParserStopped("应用关闭，解析已停止")


def _terminate(process: Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _read_result(root: Path, operation: str) -> Any:
    try:
        response = json.loads((root / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ParserError(f"解析 helper 未返回有效结果: {operation}") from exc
    if "error" in response:
        error = response["error"]
        exception = {"ValueError": ValueError, "FileNotFoundError": FileNotFoundError}.get(
            error["type"], ParserError,
        )
        raise exception(error["message"])
    if response.get("binary"):
        return (root / "result.bin").read_bytes()
    return response["value"]


class ParserSession:
    """串行复用一个原生 helper；VLM 按文档持有，预览持有独立会话。

    每次操作仍重新打开并关闭 PDF，不跨请求持有文件锁。后台会话仅在执行
    原生操作时占解析名额，等待模型 HTTP 响应时不占用；会话本身由停机闸门回收。
    """

    def __init__(self, stop_check: Optional[Callable[[], bool]] = None, *, use_slots: bool = True):
        self.process: Popen | None = None
        self.directory: tempfile.TemporaryDirectory | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._stop_check = stop_check
        self._use_slots = use_slots
        with _condition:
            _check_stopping(stop_check)
            _sessions.add(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _close_process(self) -> None:
        if self.process is not None:
            _terminate(self.process)
            self.process = None
        if self.directory is not None:
            # Windows 日志句柄可能短暂仍被占用，只重试共享/锁冲突，不吞掉权限错误。
            for attempt in range(3):
                try:
                    self.directory.cleanup()
                    break
                except PermissionError as exc:
                    if getattr(exc, "winerror", None) not in (32, 33) or attempt == 2:
                        raise
                    time.sleep(0.05 * (attempt + 1))
            self.directory = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._close_process()
        with _condition:
            _sessions.discard(self)

    def _call(self, operation: str, args: tuple, timeout: float) -> Any:
        global _exit_registered
        if self.process is not None and self.process.poll() is not None:
            self._close_process()
        if self.process is None:
            self.directory = tempfile.TemporaryDirectory(prefix="piece-session-")
            # 注册在 TemporaryDirectory 之后，正常脚本退出时先停进程再清目录。
            with _condition:
                if not _exit_registered:
                    atexit.register(stop_parser_helpers)
                    _exit_registered = True
            root = Path(self.directory.name)
            bootstrap = root / "request.json"
            bootstrap.write_text(json.dumps({"operation": "parser_loop", "args": []}), encoding="utf-8")
            with (root / "helper.log").open("wb") as log, _condition:
                _check_stopping(self._stop_check)
                self.process = Popen(
                    _helper_command(bootstrap), stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, **subprocess_options(),
                )
        assert self.directory is not None
        root = Path(self.directory.name)
        result_path = root / "result.json"
        result_path.unlink(missing_ok=True)
        request = root / "next.tmp"
        request.write_text(json.dumps({
            "operation": operation,
            "args": [str(arg) if isinstance(arg, Path) else arg for arg in args],
        }, ensure_ascii=False), encoding="utf-8")
        request.replace(root / "next.json")
        deadline = time.monotonic() + timeout
        while not result_path.is_file():
            _check_stopping(self._stop_check)
            if self.process.poll() is not None:
                with (root / "helper.log").open(encoding="utf-8", errors="replace") as log:
                    detail = log.read(2000).strip()
                raise ParserError(f"解析 helper 异常退出（exit_code={self.process.returncode}）: {operation}\n{detail}")
            if time.monotonic() >= deadline:
                raise ParserError(f"解析超时（{timeout:g} 秒）: {operation}")
            _stopping.wait(0.01)
        _check_stopping(self._stop_check)
        return _read_result(root, operation)

    def run(self, operation: str, *args, timeout: float = PARSER_TIMEOUT) -> Any:
        """需在线程中调用；取消、超时或崩溃都会回收本会话的进程。"""
        global _calls
        with _condition:
            _check_stopping(self._stop_check)
            _calls += 1
            slots = _slots
        locked = acquired = False
        try:
            while not self._lock.acquire(timeout=_POLL_SECONDS):
                _check_stopping(self._stop_check)
            locked = True
            if self._closed:
                raise ParserStopped("解析会话已关闭")
            try:
                _check_stopping(self._stop_check)
                if self._use_slots:
                    while not slots.acquire(timeout=_POLL_SECONDS):
                        _check_stopping(self._stop_check)
                    acquired = True
                _check_stopping(self._stop_check)
                return self._call(operation, args, timeout)
            except BaseException:
                self._close_process()
                raise
        finally:
            if acquired:
                slots.release()
            if locked:
                self._lock.release()
            with _condition:
                _calls -= 1
                _condition.notify_all()


def run_preview_renderer(
    pdf_path: Path, page_number: int, dpi: int, *, timeout: float = PARSER_TIMEOUT,
) -> bytes | None:
    """预览独享会话，不与后台文档争用进程或索引名额。"""
    global _preview_session
    with _condition:
        _check_stopping(None)
        if _preview_session is None:
            _preview_session = ParserSession(use_slots=False)
        session = _preview_session
    return session.run("render_page", pdf_path, page_number, dpi, "png", timeout=timeout)


def run_parser(
    operation: str,
    *args,
    timeout: float = PARSER_TIMEOUT,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Any:
    """执行一次原生操作；需在线程中调用，超时/停止只终止本次 helper。

    JSON 和二进制结果放临时目录，既不使用 pickle，也不依赖 windowed exe
    中可能为 None 的标准流。结果只在子进程正常退出后读取。
    """
    global _calls
    with _condition:
        _check_stopping(stop_check)
        _calls += 1
        slots = _slots
    process = None
    acquired = False
    try:
        while not slots.acquire(timeout=_POLL_SECONDS):
            _check_stopping(stop_check)
        acquired = True
        _check_stopping(stop_check)
        with tempfile.TemporaryDirectory(prefix="piece-parser-") as directory:
            root = Path(directory)
            request_path = root / "request.json"
            request_path.write_text(json.dumps({
                "operation": operation,
                "args": [str(arg) if isinstance(arg, Path) else arg for arg in args],
            }, ensure_ascii=False), encoding="utf-8")
            log_path = root / "helper.log"
            with log_path.open("wb") as log:
                with _condition:
                    _check_stopping(stop_check)
                    process = Popen(
                        _helper_command(request_path),
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        **subprocess_options(),
                    )
                    _active.add(process)
                logger.debug("[Parser] %s 已启动，PID=%s", operation, process.pid)
                try:
                    deadline = time.monotonic() + timeout
                    while True:
                        _check_stopping(stop_check)
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ParserError(f"解析超时（{timeout:g} 秒）: {operation}")
                        try:
                            process.wait(timeout=min(_POLL_SECONDS, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            pass
                    _check_stopping(stop_check)
                finally:
                    _terminate(process)

            if process.returncode != 0:
                with log_path.open("r", encoding="utf-8", errors="replace") as log:
                    detail = log.read(2000).strip()
                raise ParserError(
                    f"解析 helper 异常退出（exit_code={process.returncode}）: {operation}"
                    + (f"\n{detail}" if detail else "")
                )
            return _read_result(root, operation)
    finally:
        if acquired:
            slots.release()
        with _condition:
            if process is not None:
                _active.discard(process)
            _calls -= 1
            _condition.notify_all()
