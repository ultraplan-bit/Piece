"""独立窗口：关窗不停止服务，服务退出时回收自己创建的窗口。"""

import importlib.util
import logging
import subprocess
import threading
import webbrowser
from pathlib import Path

from app.platform import find_chromium, get_icon_path, is_windows, subprocess_command, subprocess_options
from indexing.settings import get_settings

logger = logging.getLogger(__name__)
_window_process: subprocess.Popen | None = None
_window_lock = threading.Lock()


def _spawn(cmd: list[str]) -> subprocess.Popen:
    """拉起一个不继承控制台输入输出的子进程。"""
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **subprocess_options(),
    )


def _open_native(url: str) -> subprocess.Popen | None:
    """仅 Windows 使用原生窗口，不在 mac/Linux 引入 GUI 工具包。"""
    if not is_windows() or importlib.util.find_spec("webview") is None:
        return None

    icon = get_icon_path()
    cmd = subprocess_command("app.window_host", "--window", url)
    if icon.is_file():
        cmd.append(str(icon))
    try:
        process = _spawn(cmd)
    except OSError as exc:
        logger.warning(f"[Window] 原生窗口进程启动失败: {exc}")
        return None
    logger.info(f"[Window] 已打开原生窗口: {url}")
    return process


def _open_browser(url: str) -> subprocess.Popen | None:
    """降级路径：Chromium --app 窗口，再不行就默认浏览器标签页。"""
    browser = find_chromium()
    if browser is None:
        logger.info("[Window] 未找到 Chromium 系浏览器，降级为默认浏览器标签页")
        if not webbrowser.open(url):
            logger.warning("[Window] 无法自动打开浏览器，请手动访问 %s", url)
        return None

    # 独立 profile 防止请求被用户现有浏览器接管；关闭 Edge 隐式同步及扩展。
    profile_dir = get_settings().get_data_path() / "browser"
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        process = _spawn([
            browser,
            f"--app={url}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
        ])
    except OSError as exc:
        logger.warning(f"[Window] 启动 {browser} 失败，降级为默认浏览器: {exc}")
        if not webbrowser.open(url):
            logger.warning("[Window] 无法自动打开浏览器，请手动访问 %s", url)
        return None
    logger.info(f"[Window] 已用 {Path(browser).name} 打开应用窗口: {url}")
    return process


def open_window(url: str) -> None:
    """打开管理界面窗口；托盘和 CLI 并发请求也只创建一个窗口。"""
    global _window_process
    with _window_lock:
        if _window_process is not None and _window_process.poll() is None:
            logger.info("[Window] 窗口已在运行，跳过重复打开")
            return
        _window_process = _open_native(url) or _open_browser(url)


def close_window() -> None:
    """回收服务创建的独立窗口，不关闭用户默认浏览器中的标签页。"""
    global _window_process
    with _window_lock:
        process = _window_process
        _window_process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            # 窗口清理失败不能阻断 Worker、云同步和数据库的停机清理。
            logger.warning("[Window] 关闭窗口失败 (PID=%s): %s", process.pid, exc)
        else:
            logger.info("[Window] 应用窗口已关闭")
