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


def run(url: str, icon: str | None = None) -> None:
    """打开窗口并阻塞到窗口关闭"""
    import webview

    # 默认禁止下载，导出原件/解析产物要靠它
    webview.settings["ALLOW_DOWNLOADS"] = True

    webview.create_window(
        "Piece - 个人知识库",
        url,
        width=1100,
        height=700,
    )
    # private_mode 默认开启：不落 cookie、不存登录态，界面本身也不用浏览器存储
    webview.start(icon=icon)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--window"]
    if not args:
        sys.exit("usage: python -m app.window_host <url> [icon]")
    run(args[0], args[1] if len(args) > 1 else None)
