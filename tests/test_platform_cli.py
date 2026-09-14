"""平台路径、轻量 CLI 与无桌面降级；不打开窗口、不修改用户目录。"""

import builtins
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cli, platform, tray, window


@pytest.mark.parametrize("system, frozen, source, suffix", [
    ("win32", False, False, "local/Piece"),
    ("win32", False, True, "source/data"),
    ("win32", True, False, "portable/data"),
    ("darwin", False, False, "home/Library/Application Support/Piece"),
    ("darwin", True, False, "home/Library/Application Support/Piece"),
    ("linux", False, False, "xdg/piece"),
    ("linux", True, False, "xdg/piece"),
])
def test_default_data_dirs(monkeypatch, tmp_path, system, frozen, source, suffix):
    monkeypatch.delenv("PIECE_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(platform, "sys", SimpleNamespace(
        platform=system, frozen=frozen, executable=str(tmp_path / "portable/Piece.exe"),
    ))
    package = tmp_path / ("source/app" if source else "installed/app")
    monkeypatch.setattr(platform, "PACKAGE_DIR", package)
    if source:
        package.mkdir(parents=True)
        (package.parent / "pyproject.toml").touch()
    expected = tmp_path / suffix
    assert platform.get_default_data_dir() == expected
    assert not expected.exists(), "查询目录不应创建文件或迁移数据"


def test_data_override_and_invalid_xdg(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("XDG_DATA_HOME", "relative-xdg")
    monkeypatch.delenv("PIECE_DATA_DIR", raising=False)
    assert platform.get_default_data_dir() == tmp_path / "home/.local/share/piece"
    monkeypatch.setenv("PIECE_DATA_DIR", "自定义 目录")
    assert platform.get_default_data_dir() == tmp_path / "自定义 目录"


def test_settings_and_logs_share_config_root(monkeypatch, tmp_path):
    from app import logging_config
    from indexing import settings

    root = tmp_path / "config"
    monkeypatch.setenv("PIECE_DATA_DIR", str(root))
    monkeypatch.setattr(settings, "DEFAULT_DATA_PATH", root)
    monkeypatch.setattr(settings, "_settings", None)
    config = settings.load_settings()
    assert config.get_data_path() == root
    config.data_path = str(tmp_path / "documents")
    assert settings.save_settings(config)
    assert settings.load_settings().get_data_path() == tmp_path / "documents"
    assert json.loads((root / "config.json").read_text(encoding="utf-8"))["data_path"] == config.data_path
    handler = logging_config._build_file_handler("check.log")
    assert handler is not None
    handler.close()
    assert (root / "logs/check.log").is_file()
    assert not (tmp_path / "documents/config.json").exists()


def test_icon_source_and_package_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "PACKAGE_DIR", tmp_path / "app")
    assert platform.get_icon_path() == tmp_path / "assets/icon.ico"
    packaged = tmp_path / "app/assets/icon.ico"
    packaged.parent.mkdir(parents=True)
    packaged.touch()
    assert platform.get_icon_path() == packaged


@pytest.mark.parametrize("system, executable", [("win32", "soffice.com"), ("linux", "soffice")])
def test_libreoffice_uses_platform_executable(monkeypatch, system, executable):
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform=system))
    monkeypatch.setattr(Path, "is_file", lambda _: False)
    which = Mock(return_value="/tools/soffice")
    monkeypatch.setattr(platform.shutil, "which", which)
    assert platform.find_libreoffice() == "/tools/soffice"
    which.assert_called_once_with(executable)
    assert platform.find_libreoffice("/missing/configured") is None
    assert which.call_count == 1, "显式配置失效时不应偷偷使用另一后端"


def test_mac_browser_and_libreoffice_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    chrome = tmp_path / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    soffice = tmp_path / "Applications/LibreOffice.app/Contents/MacOS/soffice"
    monkeypatch.setattr(Path, "is_file", lambda path: path in (chrome, soffice))
    assert platform.find_chromium() == str(chrome)
    assert platform.find_libreoffice() == str(soffice)
    assert not platform.com_available()
    assert not platform.supports_tray()


@pytest.mark.parametrize("system, rss", [("linux", 512 * 1024), ("darwin", 512 * 1024 * 1024)])
def test_unix_memory_diagnostic_units(monkeypatch, system, rss):
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform=system))
    monkeypatch.setitem(sys.modules, "resource", SimpleNamespace(
        RUSAGE_SELF=0, getrusage=lambda _: SimpleNamespace(ru_maxrss=rss),
    ))
    assert platform.get_process_rss_mb() == 512


def test_memory_diagnostic_on_current_platform():
    value = platform.get_process_rss_mb()
    assert value is None or value > 0


def test_sqlite_extension_and_missing_capability():
    connection = sqlite3.connect(":memory:")
    try:
        platform.load_sqlite_vec(connection)
        assert connection.execute("SELECT vec_version()").fetchone()[0]
        with pytest.raises(sqlite3.OperationalError, match="not authorized"):
            connection.load_extension("must-not-load")
    finally:
        connection.close()
    with pytest.raises(RuntimeError, match="Homebrew"):
        platform.load_sqlite_vec(object())


def test_extension_loading_is_disabled_after_failure(monkeypatch):
    import sqlite_vec

    connection = Mock()
    monkeypatch.setattr(sqlite_vec, "load", Mock(side_effect=sqlite3.OperationalError("bad extension")))
    with pytest.raises(sqlite3.OperationalError, match="bad extension"):
        platform.load_sqlite_vec(connection)
    assert connection.enable_load_extension.call_args_list == [call(True), call(False)]


def test_non_windows_never_launches_webview(monkeypatch):
    monkeypatch.setattr(window, "is_windows", lambda: False)
    spawn = Mock(side_effect=AssertionError("不应启动原生窗口"))
    monkeypatch.setattr(window, "_spawn", spawn)
    assert window._open_native("http://127.0.0.1:8689") is None
    spawn.assert_not_called()


def test_clipboard_copy_windows_roundtrip(monkeypatch):
    """Windows 剪贴板真实写入并读回（含中文）；只在 win32 运行。"""
    if not platform.is_windows():
        pytest.skip("仅 Windows 实测剪贴板")
    assert platform.copy_to_clipboard("piece-密码-测试") is True
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")
    kernel32 = ctypes.WinDLL("kernel32")
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    assert user32.OpenClipboard(0)
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        locked = kernel32.GlobalLock(handle)
        text = ctypes.wstring_at(locked)
        kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
    assert text == "piece-密码-测试"


def test_clipboard_copy_falls_back_to_cli_tools(monkeypatch):
    """非 Windows：按 pbcopy/wl-copy/xclip 顺序选择可用工具；都缺失时返回 False。"""
    commands = []
    monkeypatch.setattr(platform, "is_windows", lambda: False)
    monkeypatch.setattr(platform.sys, "platform", "linux")

    def fake_which(name):
        return f"/usr/bin/{name}" if name == "xclip" else None

    monkeypatch.setattr(platform.shutil, "which", fake_which)
    monkeypatch.setattr(platform.subprocess, "run",
                        lambda cmd, **kw: commands.append(cmd) or Mock())
    assert platform.copy_to_clipboard("secret") is True
    assert commands == [["xclip", "-selection", "clipboard"]]

    commands.clear()
    monkeypatch.setattr(platform.sys, "platform", "darwin")
    monkeypatch.setattr(platform.shutil, "which", lambda name: None)
    assert platform.copy_to_clipboard("secret") is False
    assert commands == []


def test_tray_backend_import_failure_is_optional(monkeypatch, caplog):
    original = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "pystray":
            raise RuntimeError("no DISPLAY")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(tray, "_icon", None)
    monkeypatch.setattr(tray, "supports_tray", lambda: True)
    monkeypatch.setattr(builtins, "__import__", unavailable)
    tray.start("http://127.0.0.1:8689")
    assert tray._icon is None and "no DISPLAY" in caplog.text


def test_cli_data_dir_applies_before_service_import(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PIECE_DATA_DIR", "old")
    monkeypatch.setattr(cli, "_is_listening", lambda _: False)
    main = Mock(side_effect=lambda **_: (
        os.environ["PIECE_DATA_DIR"] == str(tmp_path / "新 目录") or pytest.fail("目录覆盖未生效")
    ))
    monkeypatch.setitem(sys.modules, "app.server", SimpleNamespace(main=main))
    assert cli.main(["serve", "--data-dir", "新 目录"]) == 0
    assert not (tmp_path / "新 目录").exists()


@pytest.mark.parametrize("port", ["0", "65536", "-1", "oops"])
def test_cli_rejects_invalid_ports(port):
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--port", port])
    assert exc.value.code == 2


def test_database_lock_excludes_other_processes_and_recovers_after_crash(tmp_path):
    root = Path(__file__).resolve().parent.parent
    db_path = tmp_path / "共享 知识库/kb.db"
    script = """
import os, sys
from pathlib import Path
from app.platform import database_lock
with database_lock(Path(sys.argv[1])):
    if sys.argv[2] == 'crash':
        os._exit(73)
"""

    def attempt(mode="normal"):
        return subprocess.run(
            [sys.executable, "-S", "-c", script, str(db_path), mode],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(root), "PYTHONIOENCODING": "utf-8"},
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )

    with platform.database_lock(db_path):
        blocked = attempt()
        assert blocked.returncode != 0 and "无法独占知识库" in blocked.stderr
        with platform.database_lock(tmp_path / "other.db"):
            pass
    assert attempt().returncode == 0
    assert attempt("crash").returncode == 73
    with platform.database_lock(db_path):
        assert not db_path.exists(), "拿锁不能初始化数据库"


def test_cli_reports_startup_failure(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_is_listening", lambda _: False)
    monkeypatch.setitem(sys.modules, "app.server", SimpleNamespace(
        main=Mock(side_effect=RuntimeError("知识库已被占用")),
    ))
    assert cli.main(["serve"]) == 1
    assert "知识库已被占用" in capsys.readouterr().err


@pytest.mark.parametrize("entry", ["app.cli", "app.server", "script"])
@pytest.mark.parametrize("help_args", [
    ["--help"], ["autostart", "--help"],
    ["autostart", "install", "--help"], ["autostart", "uninstall", "--help"],
])
def test_help_is_stdlib_only_from_another_directory(tmp_path, entry, help_args):
    root = Path(__file__).resolve().parent.parent
    script = f"""
import runpy, sys
from pathlib import Path
root = Path({str(root)!r})
sys.path.insert(0, str(root))
entry = {entry!r}
sys.argv = ['piece', *{help_args!r}]
try:
    if entry == 'script':
        sys.path[0] = str(root / 'app')
        runpy.run_path(str(root / 'app/server.py'), run_name='__main__')
    else:
        runpy.run_module(entry, run_name='__main__')
except SystemExit as exc:
    assert exc.code == 0
else:
    raise AssertionError('help must exit')
import platform
assert callable(platform.system), platform.__file__
assert not {{'nicegui', 'indexing.database', 'indexing.settings', 'app.logging_config', 'webview', 'pystray'}} & sys.modules.keys()
"""
    result = subprocess.run(
        [sys.executable, "-S", "-c", script], cwd=tmp_path,
        env={**os.environ, "PIECE_DATA_DIR": str(tmp_path / "unused"), "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "usage: piece" in result.stdout
    if help_args == ["--help"]:
        assert "serve" in result.stdout and "open" in result.stdout and "autostart" in result.stdout
    assert not (tmp_path / "unused").exists()
