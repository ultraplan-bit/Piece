"""
独立窗口进程入口

单独跑一个进程用 WebView2 显示管理界面：无浏览器扩展、无账号同步、无浏览器右键菜单。
与 NiceGUI 的 native 模式的区别在于——服务进程只负责把它拉起来，不监视它的存活，
窗口关掉只是这个进程退出，服务和两个 MCP 端口照常。

启动方式（由 app/window.py 拉起，不用手动运行）:
- 开发环境: python -m app.window_host <url>
- 打包环境: Piece.exe --window <url>
"""

import sys
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


class _DirectoryApi:
    """仅由本地工作台显式点击触发的目录操作；不接受命令或文件打开。"""

    def __init__(self, url):
        self._origin = self._url_origin(url)
        self._window = None

    @staticmethod
    def _url_origin(url):
        parsed = urlsplit(url or "")
        return parsed.scheme, parsed.hostname, parsed.port

    def _check_origin(self):
        if self._window is None or self._url_origin(self._window.get_current_url()) != self._origin:
            raise PermissionError("Directory actions are only available in the local workspace")
        if self._origin[1] not in {"localhost", "127.0.0.1", "::1"}:
            raise PermissionError("Directory actions require a local workspace")

    def choose_directory(self, initial=""):
        import webview
        self._check_origin()
        path = Path(str(initial)).expanduser() if initial else None
        result = self._window.create_file_dialog(
            webview.FileDialog.FOLDER, directory=str(path) if path and path.is_dir() else "")
        return str(Path(result[0]).resolve()) if result else None

    def open_directory(self, directory, exported):
        self._check_origin()
        path = Path(str(directory)).expanduser().resolve(strict=True)
        if not path.is_dir() or not isinstance(exported, list) or not exported:
            raise ValueError("No exported directory is available")
        # 打开位置只接受本次结果里的 Skill 目录，并验证实际存在的产物。
        for name in exported:
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError("Invalid exported skill")
            artifact = (path / name / "SKILL.md").resolve(strict=True)
            if path not in artifact.parents or not artifact.is_file():
                raise ValueError("Exported skill is no longer available")
        os.startfile(str(path))
        return True


def run(url: str, icon: str | None = None) -> None:
    """打开窗口并阻塞到窗口关闭"""
    import webview

    # 默认禁止下载，导出原件/解析产物要靠它
    webview.settings["ALLOW_DOWNLOADS"] = True

    directory_api = _DirectoryApi(url)
    directory_api._window = webview.create_window(
        "Piece - 个人知识库",
        url,
        width=1100,
        height=700,
        js_api=directory_api,
    )
    # private_mode 默认开启：不落 cookie、不存登录态，界面本身也不用浏览器存储
    webview.start(icon=icon)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--window"]
    if not args:
        sys.exit("usage: python -m app.window_host <url> [icon]")
    run(args[0], args[1] if len(args) > 1 else None)
