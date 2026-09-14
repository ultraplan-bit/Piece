"""登录自启的隔离检查；只在临时目录生成文件，不注册本机启动项。"""

import json
import os
import plistlib
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cli, platform


def _unix_environment(monkeypatch, tmp_path, system, frozen=False):
    executable = tmp_path / "venv/bin/python"
    monkeypatch.setattr(platform, "sys", SimpleNamespace(
        platform=system, frozen=frozen, executable=str(executable),
    ))
    monkeypatch.setattr(platform, "PACKAGE_DIR", tmp_path / "source/app")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("PIECE_DATA_DIR", str(tmp_path / "知识库 %h $HOME & notes"))
    manager = Mock()
    monkeypatch.setattr(platform, "_systemctl", manager)
    return manager


@pytest.mark.parametrize("system", ["darwin", "linux"])
@pytest.mark.parametrize("frozen", [False, True])
def test_unix_install_update_uninstall_without_starting_service(monkeypatch, tmp_path, system, frozen):
    manager = _unix_environment(monkeypatch, tmp_path, system, frozen)
    path = platform.configure_autostart("install", port=9765)
    command = [platform.sys.executable]
    if not frozen:
        command += ["-m", "app.cli"]
    command += ["serve", "--port", "9765", "--data-dir", os.environ["PIECE_DATA_DIR"], "--no-tray"]
    working_dir = tmp_path / ("venv/bin" if frozen else "source")
    if system == "darwin":
        assert path == tmp_path / "home/Library/LaunchAgents/io.github.ultraplan-bit.piece.plist"
        agent = plistlib.loads(path.read_bytes())
        assert agent["ProgramArguments"] == command
        assert agent["WorkingDirectory"] == str(working_dir)
        assert agent["RunAtLoad"] is True and agent["KeepAlive"] is False
        assert agent["ExitTimeOut"] == 0
    else:
        assert path == tmp_path / "config/systemd/user/piece.service"
        unit = path.read_text(encoding="utf-8")
        line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        assert line.startswith("ExecStart=:")  # 禁止 $ 展开；不经 shell
        assert shlex.split(line.removeprefix("ExecStart=:").replace("%%", "%")) == command
        assert "%%h $HOME" in line
        assert f"WorkingDirectory={str(working_dir).replace('%', '%%')}/.\n" in unit
        assert "KillMode=mixed\nTimeoutStopSec=infinity" in unit
        assert "WantedBy=default.target" in unit
    assert not Path(os.environ["PIECE_DATA_DIR"]).exists(), "注册自启不应初始化日志、配置或数据库"
    assert list(path.parent.iterdir()) == [path], "发布后不应遗留临时启动项"

    assert platform.configure_autostart("install", port=9766) == path
    assert "9766" in path.read_text(encoding="utf-8")
    assert platform.configure_autostart("uninstall") == path
    assert not path.exists()
    assert platform.configure_autostart("uninstall") == path
    if system == "linux":
        assert manager.call_args_list == [
            call("enable", str(path)), call("enable", str(path)), call("disable", path.name),
        ]
    else:
        manager.assert_not_called()  # 不 bootstrap/bootout，不影响当前会话


@pytest.mark.parametrize("system", ["darwin", "linux"])
@pytest.mark.parametrize("kind", ["foreign", "directory", "symlink"])
def test_unix_never_overwrites_or_deletes_foreign_entries(monkeypatch, tmp_path, system, kind):
    _unix_environment(monkeypatch, tmp_path, system)
    path = platform.configure_autostart("install")
    original = path.read_bytes()
    if kind == "directory":
        path.unlink()
        path.mkdir()
    elif kind == "foreign":
        path.write_text("User maintained startup entry", encoding="utf-8")
        original = path.read_bytes()
    else:
        # Windows 未必有创建 Unix 符号链接的权限；这里只测试拒绝分支。
        original_is_symlink = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda p: p == path or original_is_symlink(p))
    for action in ("install", "uninstall"):
        with pytest.raises(RuntimeError, match="拒绝改动"):
            platform.configure_autostart(action)
    assert path.is_dir() if kind == "directory" else path.read_bytes() == original


def test_invalid_arguments_do_not_write_startup_files(monkeypatch, tmp_path):
    _unix_environment(monkeypatch, tmp_path, "linux")
    for port in (0, 65536):
        with pytest.raises(ValueError, match="端口"):
            platform.configure_autostart("install", port=port)
    for char in ("\n", "\r", "\0", "\t"):
        monkeypatch.setattr(platform, "get_default_data_dir", lambda char=char: Path(f"bad{char}path"))
        with pytest.raises(ValueError, match="控制字符"):
            platform.configure_autostart("install")
    with pytest.raises(ValueError, match="未知"):
        platform.configure_autostart("typo")
    assert not (tmp_path / "config").exists()
    platform.sys.platform = "freebsd"
    with pytest.raises(RuntimeError, match="仅支持"):
        platform.configure_autostart("uninstall")


def test_systemd_escaping_is_not_shell_quoting():
    assert platform._systemd_quote('/a "quoted"/100%/$HOME/back\\slash') == '"/a \\"quoted\\"/100%%/$HOME/back\\\\slash"'


def test_interpreter_symlink_is_not_resolved(monkeypatch, tmp_path):
    _unix_environment(monkeypatch, tmp_path, "linux")
    # resolve() 会将 venv/bin/python 换成全局解释器；启动项必须保留当前环境。
    resolve = Path.resolve
    executable = tmp_path / "venv/bin/python"
    monkeypatch.setattr(Path, "resolve", lambda path, *args, **kwargs: (
        tmp_path / "system/bin/python" if path == executable else resolve(path, *args, **kwargs)
    ))
    command, cwd = platform._autostart_command(8689)
    assert command[0] == str(tmp_path / "venv/bin/python")
    assert cwd == tmp_path / "source"


def test_windows_frozen_command_uses_exe_directory_not_bundle_temp(monkeypatch, tmp_path):
    executable = tmp_path / "portable/Piece.exe"
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="win32", frozen=True, executable=str(executable)))
    monkeypatch.setattr(platform, "PACKAGE_DIR", tmp_path / "temporary-extraction/app")
    monkeypatch.delenv("PIECE_DATA_DIR", raising=False)
    command, cwd = platform._autostart_command(9765)
    assert command == [str(executable), "serve", "--port", "9765", "--data-dir", str(executable.parent / "data")]
    assert cwd == executable.parent


def test_autostart_cli_runs_with_stdlib_only_outside_repository(tmp_path):
    root = Path(__file__).resolve().parent.parent
    script = f"""
import sys
from types import SimpleNamespace
sys.path.insert(0, {str(root)!r})
from app import cli, platform
platform.sys = SimpleNamespace(platform='linux', frozen=False, executable=sys.executable)
platform._systemctl = lambda *args: None
assert cli.main(['autostart', 'install', '--data-dir', {str(tmp_path / 'data')!r}]) == 0
assert cli.main(['autostart', 'uninstall']) == 0
assert not {{'nicegui', 'indexing.database', 'indexing.settings', 'app.logging_config', 'webview', 'pystray', 'win32com'}} & sys.modules.keys()
"""
    result = subprocess.run(
        [sys.executable, "-S", "-c", script], cwd=tmp_path,
        env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "config"), "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "config/systemd/user/piece.service").exists()


@pytest.mark.parametrize("failure", ["missing", "bus", "timeout"])
def test_systemd_errors_are_reported_and_failed_uninstall_preserves_file(monkeypatch, tmp_path, failure):
    real_systemctl = platform._systemctl
    _unix_environment(monkeypatch, tmp_path, "linux")
    monkeypatch.setattr(platform, "_systemctl", real_systemctl)
    run = Mock(return_value=SimpleNamespace(returncode=1, stdout="", stderr="Failed to connect to bus"))
    if failure == "missing":
        run.side_effect = FileNotFoundError("systemctl not found")
    elif failure == "timeout":
        run.side_effect = subprocess.TimeoutExpired("systemctl", 30)
    monkeypatch.setattr(platform.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="已写入.*启用失败"):
        platform.configure_autostart("install")
    path = tmp_path / "config/systemd/user/piece.service"
    content = path.read_bytes()
    with pytest.raises((OSError, RuntimeError, subprocess.TimeoutExpired)):
        platform.configure_autostart("uninstall")
    assert path.read_bytes() == content
    assert [item.args[0] for item in run.call_args_list] == [
        ["systemctl", "--user", "enable", str(path)], ["systemctl", "--user", "disable", path.name],
    ]
    assert all(item.kwargs.get("shell", False) is False for item in run.call_args_list)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 原生快捷方式验证")
def test_windows_real_shortcut_round_trip_in_temporary_directory(monkeypatch, tmp_path):
    shell = platform._windows_shell()
    # 只重定向 Startup 目录；创建、保存和读取 .lnk 均使用真正的 Windows COM。
    monkeypatch.setattr(platform, "_windows_shell", lambda: SimpleNamespace(
        SpecialFolders=lambda _: str(tmp_path / "Startup"), CreateShortcut=shell.CreateShortcut,
    ))
    data = tmp_path / "知识库 $HOME & notes"
    monkeypatch.setenv("PIECE_DATA_DIR", str(data))
    for port in (9765, 9766):
        path = platform.configure_autostart("install", port=port)
        assert path == tmp_path / "Startup/Piece.lnk"
        shortcut = shell.CreateShortcut(str(path))
        assert Path(shortcut.TargetPath) == Path(sys.executable).with_name("pythonw.exe")
        assert shortcut.Arguments == subprocess.list2cmdline([
            "-m", "app.cli", "serve", "--port", str(port), "--data-dir", str(data),
        ])
        assert Path(shortcut.WorkingDirectory) == platform.PACKAGE_DIR.parent
        assert shortcut.Description == platform._AUTOSTART_MARKER
    platform.configure_autostart("uninstall")
    platform.configure_autostart("uninstall")
    assert not path.exists() and not data.exists()

    shortcut = shell.CreateShortcut(str(path))
    shortcut.TargetPath = sys.executable
    shortcut.Description = "User maintained shortcut"
    shortcut.Save()
    original = path.read_bytes()
    for action in ("install", "uninstall"):
        with pytest.raises(RuntimeError, match="拒绝改动"):
            platform.configure_autostart(action)
    assert path.read_bytes() == original


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 原生快捷方式验证")
@pytest.mark.parametrize("directory", ["程序 $price & more", "程序 %TEMP% $price & more"])
def test_windows_shortcut_preserves_literal_executable_path(monkeypatch, tmp_path, directory):
    shell = platform._windows_shell()
    monkeypatch.setattr(platform, "_windows_shell", lambda: SimpleNamespace(
        SpecialFolders=lambda _: str(tmp_path / "Startup"), CreateShortcut=shell.CreateShortcut,
    ))
    executable = tmp_path / directory / "Piece.exe"
    executable.parent.mkdir()
    executable.touch()  # 只作为快捷方式目标，不执行。
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="win32", frozen=True, executable=str(executable)))
    monkeypatch.setenv("PIECE_DATA_DIR", str(tmp_path / "data"))
    if "%" in directory:
        with pytest.raises(ValueError, match="不能含 %"):
            platform.configure_autostart("install")
        assert not (tmp_path / "Startup").exists()
        return
    path = platform.configure_autostart("install")
    shortcut = shell.CreateShortcut(str(path))
    assert Path(shortcut.TargetPath) == executable
    assert Path(shortcut.WorkingDirectory) == executable.parent
    assert shortcut.Arguments.startswith("serve --port 8689 --data-dir ")
    platform.configure_autostart("uninstall")
    assert not path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 原生快捷方式验证")
def test_windows_shell_passes_arguments_literally(monkeypatch, tmp_path):
    shell = platform._windows_shell()
    monkeypatch.setattr(platform, "_windows_shell", lambda: SimpleNamespace(
        SpecialFolders=lambda _: str(tmp_path / "Startup"), CreateShortcut=shell.CreateShortcut,
    ))
    result = tmp_path / "args.json"
    arguments = ["知识库 $HOME & notes", 'quote"backslash\\']
    # 只启动写回 argv 即退出的探针，不执行 Piece，也不注册真正的启动目录。
    probe = (
        "import json, pathlib, sys; p = pathlib.Path(sys.argv[1]); t = p.with_suffix('.tmp'); "
        "t.write_text(json.dumps(sys.argv[2:]), encoding='utf-8'); t.replace(p)"
    )
    command = [str(Path(sys.executable).with_name("pythonw.exe")), "-c", probe, str(result), *arguments]
    monkeypatch.setattr(platform, "_autostart_command", lambda _: (command, tmp_path))
    path = platform.configure_autostart("install")
    os.startfile(str(path))
    deadline = time.monotonic() + 10
    while not result.exists():
        assert time.monotonic() < deadline, "快捷方式探针未能写回参数"
        time.sleep(0.05)
    assert json.loads(result.read_text(encoding="utf-8")) == arguments
    original = path.read_bytes()
    command.append("%TEMP%")
    with pytest.raises(ValueError, match="不能含 %"):
        platform.configure_autostart("install")
    assert path.read_bytes() == original, "拒绝环境变量展开时必须保留旧启动项"
    platform.configure_autostart("uninstall")


def test_cli_autostart_is_lightweight_and_applies_data_override(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PIECE_DATA_DIR", "old")
    configure = Mock(return_value=tmp_path / "startup")
    monkeypatch.setattr(platform, "configure_autostart", configure)
    monkeypatch.setattr(cli, "_is_listening", Mock(side_effect=AssertionError("不应探测或启动服务")))
    assert cli.main(["autostart", "install", "--port", "9765", "--data-dir", "知识库"]) == 0
    configure.assert_called_once_with("install", port=9765)
    assert os.environ["PIECE_DATA_DIR"] == str(tmp_path / "知识库")
    assert "下次登录生效" in capsys.readouterr().out
    assert cli.main(["autostart", "uninstall"]) == 0
    assert configure.call_args == call("uninstall", port=8689)
    assert "不停止" in capsys.readouterr().out
    configure.side_effect = RuntimeError("foreign entry")
    assert cli.main(["autostart", "uninstall"]) == 1
    assert "foreign entry" in capsys.readouterr().err
    assert not (tmp_path / "知识库").exists()
    with pytest.raises(SystemExit) as exc:
        cli.main(["autostart", "install", "--port", "0"])
    assert exc.value.code == 2
