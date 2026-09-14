"""
云同步视图

职责:
- 渲染云同步中栏（同步状态和操作）
- 渲染云同步右栏（同步日志）
"""

from nicegui import ui

from app.i18n import t


def render_cloud_sync_middle(
    sync_state: dict,
    ui_refs: dict,
    sync_handlers,
):
    """
    渲染云同步中栏

    Args:
        sync_state: 同步状态 {"is_syncing", "last_sync", "logs"}，由 SyncHandlers 从服务侧同步过来
        ui_refs: UI 组件引用字典
        sync_handlers: 同步处理器实例
    """
    with ui.column().classes(
        "w-64 h-full flex flex-col overflow-hidden theme-panel gap-0"
    ).style("border-right: 1px solid var(--border-color)"):
        # 顶部标题栏
        with ui.row().classes(
            "w-full px-3 items-center justify-between"
        ).style("border-bottom: 1px solid var(--border-color); height: 49px"):
            ui.label(t("cloud_sync.title")).classes("text-sm font-medium theme-text")

        # 同步状态区域
        with ui.column().classes("w-full p-4 gap-4"):
            # 连接状态
            @ui.refreshable
            def connection_status():
                is_enabled = sync_handlers.is_enabled()
                if is_enabled:
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("cloud_done", size="sm").classes("text-green-500")
                        ui.label(t("cloud_sync.connected")).classes("text-sm theme-text")
                else:
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("cloud_off", size="sm").classes("theme-text-muted")
                        ui.label(t("cloud_sync.not_configured")).classes("text-sm theme-text-muted")
                    ui.label(t("cloud_sync.config_hint")).classes("text-xs theme-text-muted")

            ui_refs["connection_status"] = connection_status
            connection_status()

            ui.separator()

            # 上次同步时间
            @ui.refreshable
            def last_sync_info():
                last_sync = sync_state.get("last_sync")
                if last_sync:
                    ui.label(t("cloud_sync.last_sync", time=last_sync)).classes("text-xs theme-text-muted")
                else:
                    ui.label(t("cloud_sync.never_synced")).classes("text-xs theme-text-muted")

            ui_refs["last_sync_info"] = last_sync_info
            last_sync_info()

            # 同步按钮
            with ui.column().classes("w-full gap-2 mt-4"):
                @ui.refreshable
                def sync_buttons():
                    is_syncing = sync_state.get("is_syncing", False)
                    is_enabled = sync_handlers.is_enabled()

                    # 云同步按钮（智能同步）
                    sync_btn = ui.button(
                        t("cloud_sync.sync_now"),
                        on_click=sync_handlers.do_sync,
                        icon="sync"
                    ).props("color=primary").classes("w-full")

                    if is_syncing:
                        sync_btn.props("loading")
                    if not is_enabled:
                        sync_btn.props("disable")

                ui_refs["sync_buttons"] = sync_buttons
                sync_buttons()


def render_cloud_sync_right(
    sync_state: dict,
    ui_refs: dict,
    sync_handlers,
):
    """
    渲染云同步右栏（同步日志）

    Args:
        sync_state: 同步状态
        ui_refs: UI 组件引用字典
        sync_handlers: 同步处理器实例（清空日志）
    """
    with ui.column().classes("flex-1 h-full flex flex-col theme-content gap-0"):
        # 顶部信息区
        with ui.row().classes(
            "w-full px-5 items-center justify-between theme-sidebar"
        ).style("border-bottom: 1px solid var(--border-color); height: 49px"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("history", size="xs").classes("theme-text-accent")
                ui.label(t("cloud_sync.log_title")).classes("text-sm font-medium theme-text")

            # 清空日志按钮
            ui.button(
                icon="delete_sweep",
                on_click=sync_handlers.clear_logs,
            ).props("flat dense size=sm").classes("theme-text-muted")

        # 日志内容区
        with ui.scroll_area().classes("flex-1"):
            @ui.refreshable
            def sync_logs():
                logs = sync_state.get("logs", [])
                if not logs:
                    with ui.column().classes("w-full h-full items-center justify-center"):
                        ui.icon("event_note", size="lg").classes("theme-text-muted")
                        ui.label(t("cloud_sync.no_logs")).classes("text-sm mt-2 theme-text-muted")
                else:
                    with ui.column().classes("w-full gap-2"):
                        for log in reversed(logs):  # 最新的在上面
                            _render_log_item(log)

            ui_refs["sync_logs"] = sync_logs
            sync_logs()


def _render_log_item(log: dict):
    """渲染单条日志"""
    log_type = log.get("type", "info")
    message = log.get("message", "")
    timestamp = log.get("timestamp", "")

    icon_map = {
        "info": ("info", "theme-text-muted"),
        "success": ("check_circle", "text-green-500"),
        "error": ("error", "text-red-500"),
        "upload": ("cloud_upload", "text-blue-500"),
        "download": ("cloud_download", "text-purple-500"),
    }

    icon, icon_class = icon_map.get(log_type, ("info", "theme-text-muted"))

    with ui.row().classes("w-full items-start gap-2 p-2 rounded theme-hover"):
        ui.icon(icon, size="xs").classes(icon_class)
        with ui.column().classes("flex-1 gap-0"):
            ui.label(message).classes("text-sm theme-text")
            if timestamp:
                ui.label(timestamp).classes("text-xs theme-text-muted")
