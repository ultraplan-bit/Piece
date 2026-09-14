"""可选的系统托盘；不可用时保留 CLI/浏览器入口，退出通过回调通知核心，不导入 NiceGUI。"""

import logging
import threading

from app.platform import get_icon_path, supports_tray

logger = logging.getLogger(__name__)
_icon = None


def start(url: str, on_shutdown=None, *, runtime=None) -> None:
    """启动后台托盘；依赖导入、后端初始化和运行失败均不影响服务。

    ``runtime`` 提供一次性引导令牌与登录密码查询：托盘是本机同用户入口，
    与直接读取 config.json 等权，不构成新的暴露面。
    """
    global _icon
    if _icon is not None:
        return
    if not supports_tray():
        logger.info("[Tray] 当前平台/会话不启用托盘，可用 piece open 或访问 %s", url)
        return

    from app.window import open_window

    def on_open(icon, item):
        if runtime is not None:
            open_window(f"{url}/bootstrap?token={runtime.bootstrap_tokens.issue()}")
        else:
            open_window(url)

    def on_copy_password(icon, item):
        if runtime is None:
            return
        try:
            from app.platform import copy_to_clipboard
            if copy_to_clipboard(runtime.settings.api.admin_key):
                icon.notify("登录密码已复制到剪贴板", "Piece 管理界面")
            else:
                icon.notify(f"剪贴板不可用；密码位于配置文件的 api.admin_key", "Piece 管理界面")
        except Exception:
            logger.exception("[Tray] 无法复制登录密码")

    def on_quit(icon, item):
        icon.stop()
        if on_shutdown is not None:
            try:
                on_shutdown()
            except Exception:
                logger.exception("[Tray] 退出回调失败")

    try:
        import pystray
        from PIL import Image

        items = [pystray.MenuItem("打开界面", on_open, default=True)]
        if runtime is not None and pystray.Icon.HAS_NOTIFICATION:
            items.append(pystray.MenuItem("复制登录密码", on_copy_password))
        items.append(pystray.MenuItem("退出", on_quit))
        with Image.open(get_icon_path()) as image:
            icon = pystray.Icon(
                "piece",
                image.copy(),
                "Piece - 个人知识库",
                menu=pystray.Menu(*items),
            )
    except Exception as exc:
        logger.warning("[Tray] 托盘不可用，跳过: %s；管理界面 %s", exc, url)
        return

    _icon = icon

    def run():
        try:
            icon.run()
        except Exception as exc:
            logger.warning("[Tray] 托盘不可用: %s；管理界面 %s", exc, url)

    threading.Thread(target=run, daemon=True, name="Tray").start()
    logger.info("[Tray] 托盘图标已启动")


def stop() -> None:
    """停止托盘图标（应用关闭时调用）。"""
    global _icon
    if _icon is not None:
        try:
            _icon.stop()
        except Exception:
            pass
        _icon = None
