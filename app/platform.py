"""平台边界：只用标准库，不在导入时读取配置、创建目录或加载 GUI。"""

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from contextlib import contextmanager

PACKAGE_DIR = Path(__file__).resolve().parent


def is_windows() -> bool:
    return sys.platform == "win32"


def _env_path(name: str, default: Path) -> Path:
    """标准目录环境变量必须为绝对路径；忽略无效的相对 XDG 路径。"""
    value = os.environ.get(name)
    path = Path(value).expanduser() if value else default
    return path if path.is_absolute() else default


def get_default_data_dir() -> Path:
    """配置、日志和默认数据的位置；Windows 便携版及源码保留原目录。"""
    override = os.environ.get("PIECE_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()

    if is_windows():
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent / "data"
        if (PACKAGE_DIR.parent / "pyproject.toml").is_file():
            return PACKAGE_DIR.parent / "data"
        return _env_path("LOCALAPPDATA", Path.home() / "AppData" / "Local") / "Piece"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Piece"
    return _env_path("XDG_DATA_HOME", Path.home() / ".local" / "share") / "piece"


def get_icon_path() -> Path:
    """wheel 内的包资源优先，源码和现有 PyInstaller 壳兼容 assets/。"""
    packaged = PACKAGE_DIR / "assets" / "icon.ico"
    return packaged if packaged.is_file() else PACKAGE_DIR.parent / "assets" / "icon.ico"


def subprocess_command(module: str, frozen_flag: str, *args: str) -> list[str]:
    """源码/安装包使用独立解释器，PyInstaller 子进程复用同一个 exe。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, frozen_flag, *args]
    return [sys.executable, "-m", module, *args]


def subprocess_options() -> dict:
    """任意工作目录下都能定位源码子进程；Windows 不弹出额外控制台。"""
    return {
        "cwd": PACKAGE_DIR.parent,
        "creationflags": subprocess.CREATE_NO_WINDOW if is_windows() else 0,
    }


def copy_to_clipboard(text: str) -> bool:
    """把文本放入系统剪贴板；平台剪贴板不可用时返回 False，由调用方降级提示。"""
    if is_windows():
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
        kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
        try:
            if not user32.OpenClipboard(None):
                return False
            try:
                user32.EmptyClipboard()
                # CF_UNICODETEXT 需要 movable HGLOBAL（GMEM_MOVEWRITE = 0x0002）。
                size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
                handle = kernel32.GlobalAlloc(0x0002, size)
                if not handle:
                    return False
                locked = kernel32.GlobalLock(handle)
                if not locked:
                    return False
                ctypes.memmove(locked, ctypes.c_wchar_p(text), size)
                kernel32.GlobalUnlock(handle)
                if not user32.SetClipboardData(13, handle):  # CF_UNICODETEXT
                    kernel32.GlobalFree(handle)
                    return False
            finally:
                user32.CloseClipboard()
            return True
        except OSError:
            return False
    commands = [["pbcopy"]] if sys.platform == "darwin" else [["wl-copy"], ["xclip", "-selection", "clipboard"]]
    for command in commands:
        if not shutil.which(command[0]):
            continue
        try:
            subprocess.run(command, input=text.encode("utf-8"),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            return True
        except (OSError, subprocess.CalledProcessError):
            continue
    return False


def find_chromium() -> str | None:
    """定位 Chromium 系浏览器；未找到时由调用方降级到默认浏览器。"""
    if sys.platform == "darwin":
        for applications in (Path("/Applications"), Path.home() / "Applications"):
            for name, executable in (
                ("Google Chrome", "Google Chrome"),
                ("Brave Browser", "Brave Browser"),
                ("Microsoft Edge", "Microsoft Edge"),
                ("Chromium", "Chromium"),
            ):
                path = applications / f"{name}.app" / "Contents" / "MacOS" / executable
                if path.is_file():
                    return str(path)

    executables = ("chrome.exe", "brave.exe", "msedge.exe") if is_windows() else (
        "google-chrome", "chromium", "chromium-browser", "brave-browser", "microsoft-edge",
    )
    for executable in executables:
        if found := shutil.which(executable):
            return found

    if is_windows():
        import winreg

        for executable in executables:
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(
                        root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}",
                    ) as key:
                        path = winreg.QueryValue(key, None)
                    if path and Path(path).is_file():
                        return path
                except OSError:
                    continue
    return None


def find_libreoffice(configured: str = "") -> str | None:
    """Windows 必须用 soffice.com，soffice.exe 不会等待转换完成。"""
    configured = configured.strip()
    if configured:
        return configured if Path(configured).is_file() else None

    if is_windows():
        candidates = (
            _env_path("ProgramFiles", Path("C:/Program Files")) / "LibreOffice/program/soffice.com",
            _env_path("ProgramFiles(x86)", Path("C:/Program Files (x86)")) / "LibreOffice/program/soffice.com",
        )
    elif sys.platform == "darwin":
        candidates = (
            Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
            Path.home() / "Applications/LibreOffice.app/Contents/MacOS/soffice",
        )
    else:
        candidates = (Path("/usr/bin/soffice"), Path("/usr/local/bin/soffice"))
    for path in candidates:
        if path.is_file():
            return str(path)
    return shutil.which("soffice.com" if is_windows() else "soffice")


def com_available() -> bool:
    if not is_windows():
        return False
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        return False
    return True


def supports_tray() -> bool:
    # pystray 在 macOS 需要 Cocoa 主循环，不能与服务的 asyncio 主循环混用。
    return is_windows() or (
        sys.platform != "darwin" and bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"))
    )


_AUTOSTART_MARKER = "Managed by Piece: piece autostart"
_AUTOSTART_LABEL = "io.github.ultraplan-bit.piece"


def _autostart_command(port: int) -> tuple[list[str], Path]:
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1–65535 之间")
    # 不 resolve 解释器：Unix 虚拟环境的 python 常是符号链接，解引用会丢失环境。
    executable = Path(sys.executable)
    frozen = getattr(sys, "frozen", False)
    if is_windows() and not frozen:
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.is_file():
            executable = pythonw
    command = [str(executable)]
    if not frozen:
        command += ["-m", "app.cli"]
    command += ["serve", "--port", str(port), "--data-dir", str(get_default_data_dir())]
    if not is_windows():
        command.append("--no-tray")
    # 打包后的临时解压目录不能写进持久启动项。
    working_dir = executable.parent if frozen else PACKAGE_DIR.parent
    for value in (*command, str(working_dir)):
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("登录自启的路径和参数不能包含控制字符")
    return command, working_dir


def _systemd_quote(value: str) -> str:
    # systemd 不是 shell；% 是 specifier，反斜杠和双引号按 unit 语法转义。
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _systemctl(action: str, unit: str) -> None:
    result = subprocess.run(
        ["systemctl", "--user", action, unit],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip() or f"退出码 {result.returncode}"
        raise RuntimeError(f"systemctl --user {action} 失败：{detail}")


def _windows_shell():
    # 仅 CLI 主线程调用；不在导入平台模块时加载 COM。
    from win32com.client import Dispatch

    return Dispatch("WScript.Shell")


def configure_autostart(action: str, *, port: int = 8689) -> Path:
    """只管理当前用户、带 Piece 标记的启动项；不启动或停止当前服务。"""
    if action not in {"install", "uninstall"}:
        raise ValueError(f"未知的登录自启操作：{action}")
    shell = None
    if is_windows():
        shell = _windows_shell()
        path = Path(shell.SpecialFolders("Startup")) / "Piece.lnk"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"{_AUTOSTART_LABEL}.plist"
    elif sys.platform == "linux":
        path = _env_path("XDG_CONFIG_HOME", Path.home() / ".config") / "systemd" / "user" / "piece.service"
    else:
        raise RuntimeError("登录自启仅支持 Windows、macOS 和使用 systemd 的 Linux")

    refusal = f"拒绝改动非 Piece 管理的启动项：{path}；请先检查并手工处理该文件。"
    exists = path.exists()
    if path.is_symlink() or (exists and not path.is_file()):
        raise RuntimeError(refusal)
    if exists:
        if shell is not None:
            managed = shell.CreateShortcut(str(path)).Description == _AUTOSTART_MARKER
        else:
            content = path.read_bytes()
            managed = (
                f"<!-- {_AUTOSTART_MARKER} -->".encode() in content.split(b"<plist", 1)[0]
                if sys.platform == "darwin"
                else content.startswith(f"# {_AUTOSTART_MARKER}\n".encode())
            )
        if not managed:
            raise RuntimeError(refusal)

    if action == "uninstall":
        if exists:
            if sys.platform == "linux":
                # 不带 --now；禁用失败时保留文件，供检查和重试。
                _systemctl("disable", path.name)
            path.unlink()
        return path

    command, working_dir = _autostart_command(port)
    # ponytail: WSH 执行时展开 %VAR%；支持含 % 路径需改用不经 Shell 展开的自启入口。
    if shell is not None and any("%" in value for value in (*command, str(working_dir))):
        raise ValueError("Windows 登录自启的程序和数据目录不能含 %（快捷方式会展开环境变量），请使用不含 % 的路径。")
    content = None
    if sys.platform == "darwin":
        content = plistlib.dumps({
            "Label": _AUTOSTART_LABEL,
            "ProgramArguments": command,
            "WorkingDirectory": str(working_dir),
            "RunAtLoad": True,
            "KeepAlive": False,
            "ExitTimeOut": 0,
        })
        content = content.replace(b"<plist", f"<!-- {_AUTOSTART_MARKER} -->\n<plist".encode(), 1)
    elif sys.platform == "linux":
        content = (
            f"# {_AUTOSTART_MARKER}\n"
            "[Unit]\nDescription=Piece local MCP service\n\n"
            "[Service]\nType=exec\n"
            # ':' 禁止 $ 环境变量展开；参数直接交给解释器，不经过 shell。
            f"ExecStart=:{' '.join(_systemd_quote(arg) for arg in command)}\n"
            # WorkingDirectory 是整行字面路径，不接受 argv 引号；/. 保留末尾空格或反斜杠。
            f"WorkingDirectory={str(working_dir).replace('%', '%%')}/.\n"
            # 先通知主进程，让它依序排空 HTTP/同步/解析任务，不能先杀 helper。
            "KillMode=mixed\nTimeoutStopSec=infinity\n\n"
            "[Install]\nWantedBy=default.target\n"
        ).encode("utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".piece-autostart-", dir=path.parent) as directory:
        temporary = Path(directory) / path.name
        if shell is not None:
            shortcut = shell.CreateShortcut(str(temporary))
            shortcut.TargetPath = command[0]
            shortcut.Arguments = subprocess.list2cmdline(command[1:])
            shortcut.WorkingDirectory = str(working_dir)
            shortcut.Description = _AUTOSTART_MARKER
            shortcut.Save()
        else:
            assert content is not None
            temporary.write_bytes(content)
            temporary.chmod(0o600)
        temporary.replace(path)

    if sys.platform == "linux":
        try:
            _systemctl("enable", str(path))
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"启动项已写入 {path}，但启用失败；请检查 systemd --user 后重试：{exc}") from exc
    # launchd 在下次登录读取 LaunchAgents；不 bootstrap/bootout，避免启动或终止在途任务。
    return path


def get_process_rss_mb() -> float | None:
    """内存诊断值（MiB）：Windows 为当前工作集，Unix 为峰值 RSS。"""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("page_fault_count", wintypes.DWORD),
                ("peak_working_set_size", ctypes.c_size_t),
                ("working_set_size", ctypes.c_size_t),
                ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                ("quota_paged_pool_usage", ctypes.c_size_t),
                ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                ("quota_non_paged_pool_usage", ctypes.c_size_t),
                ("pagefile_usage", ctypes.c_size_t),
                ("peak_pagefile_usage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        get_counters = ctypes.windll.psapi.GetProcessMemoryInfo
        get_counters.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD,
        ]
        get_counters.restype = wintypes.BOOL
        if get_counters(handle, ctypes.byref(counters), counters.cb):
            return counters.working_set_size / (1024 * 1024)
        return None

    try:
        import resource
    except (ImportError, AttributeError):
        return None
    # Darwin 内核直接返回 resident_size_max（字节），Linux 返回 KiB。
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / divisor


class InstanceBusyError(RuntimeError):
    code = "INSTANCE_BUSY"


@contextmanager
def database_lock(db_path: Path):
    """服务整个生命周期独占数据库；改 UI 端口也不能拉起第二个写者。"""
    lock_path = Path(str(db_path.resolve()) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 不删锁文件，避免 Unix 上另一进程锁住旧 inode 时又创建出一把新锁。
    # 描述符关闭（包括进程崩溃）即释放锁，不依赖 PID 文件或手工清理。
    with lock_path.open("a+b") as handle:
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise InstanceBusyError(
                f"无法独占知识库 {db_path}；请确认没有其他 Piece 实例正在使用它。"
            ) from exc
        yield


def load_sqlite_vec(connection) -> None:
    """使用扩展自身的资源路径；不支持扩展的 Python 给出可操作的错误。"""
    import sqlite3

    try:
        connection.enable_load_extension(True)
    except (AttributeError, sqlite3.NotSupportedError, sqlite3.OperationalError) as exc:
        raise RuntimeError(
            "当前 Python 的 SQLite 不支持加载 sqlite-vec 扩展。"
            "请使用支持 SQLite 扩展的 Python 3.12+；macOS 可用 Homebrew 安装 Python，"
            "然后用该解释器重新创建 uv 环境（不要使用 macOS 自带的 Python）。"
        ) from exc
    try:
        import sqlite_vec

        sqlite_vec.load(connection)
    finally:
        connection.enable_load_extension(False)
