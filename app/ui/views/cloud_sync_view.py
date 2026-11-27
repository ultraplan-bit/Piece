"""
云同步视图

职责:
- 渲染云同步中栏（连接状态、实际同步策略、上次结果与操作）
- 渲染云同步右栏（同步日志）

同步在服务进程的后台线程里跑，与本界面会话解耦；这里只读 ``sync_state``
（由 SyncHandlers 从服务侧同步），不新增上传/下载模式，也不虚构百分比。
"""

import inspect

from nicegui import ui

from app.i18n import t
from app.ui.components import write_clipboard


def render_cloud_sync_middle(
    sync_state: dict,
    ui_refs: dict,
    sync_handlers,
):
    """
    渲染云同步中栏

    Args:
        sync_state: 同步状态 {"is_syncing", "last_sync", "logs", "last_result"}
        ui_refs: UI 组件引用字典
        sync_handlers: 同步处理器实例
    """
    with ui.column().classes(
        "cloud-sync-sidebar w-64 h-full flex flex-col overflow-hidden theme-panel gap-0"
    ):
        # 顶部标题栏
        with ui.row().classes("cloud-sync-sidebar-header workspace-toolbar w-full items-center shrink-0"):
            ui.icon("cloud_sync").classes("cloud-sync-header-icon").props("aria-hidden=true")
            ui.label(t("cloud_sync.title")).classes("text-sm font-medium theme-text")

        with ui.scroll_area().classes("cloud-sync-content w-full flex-1 min-h-0 scroll-flush"):
            with ui.column().classes("cloud-sync-overview w-full gap-3"):
            # 连接状态：未配置时直接给出前往设置的入口
                @ui.refreshable
                def connection_status():
                    with ui.column().classes("cloud-sync-status w-full gap-2"):
                        if sync_handlers.is_enabled():
                            with ui.row().classes("cloud-sync-status-heading items-center gap-2"):
                                ui.icon("cloud_done").classes("cloud-sync-status-icon cloud-sync-connected")
                                ui.label(t("cloud_sync.connected")).classes("text-sm font-medium theme-text")
                        else:
                            with ui.row().classes("cloud-sync-status-heading items-center gap-2"):
                                ui.icon("cloud_off").classes("cloud-sync-status-icon theme-text-muted")
                                ui.label(t("cloud_sync.not_configured")).classes("text-sm font-medium theme-text-secondary")
                            ui.label(t("cloud_sync.config_hint")).classes("cloud-sync-config-hint text-xs theme-text-muted")
                            if ui_refs.get("open_webdav_settings"):
                                ui.button(
                                    t("cloud_sync.go_settings"),
                                    icon="settings",
                                    on_click=lambda: _open_webdav_settings(ui_refs),
                                ).props("flat dense no-caps").classes("cloud-sync-settings-link self-start")

                ui_refs["connection_status"] = connection_status
                connection_status()

                ui.separator().classes("cloud-sync-divider")

                # 上次同步时间
                @ui.refreshable
                def last_sync_info():
                    last_sync = sync_state.get("last_sync")
                    with ui.row().classes("cloud-sync-last-sync items-start gap-2"):
                        ui.icon("schedule").classes("cloud-sync-subtle-icon").props("aria-hidden=true")
                        if last_sync:
                            ui.label(t("cloud_sync.last_sync", time=last_sync)).classes("text-xs theme-text-secondary")
                        else:
                            ui.label(t("cloud_sync.never_synced")).classes("text-xs theme-text-muted")

                ui_refs["last_sync_info"] = last_sync_info
                last_sync_info()

                # 同步进行中的当前阶段：取最新一条同步日志，不虚构百分比
                @ui.refreshable
                def current_stage():
                    if not sync_state.get("is_syncing"):
                        return
                    logs = sync_state.get("logs") or []
                    if not logs:
                        return
                    message = logs[-1].get("message", "")
                    with ui.row().classes("cloud-sync-stage items-start gap-2"):
                        ui.spinner(size="xs").classes("cloud-sync-stage-spinner")
                        with ui.column().classes("min-w-0 gap-1"):
                            ui.label(t("cloud_sync.current_stage")).classes("text-xs font-medium theme-text-muted")
                            ui.label(message).classes("cloud-sync-stage-message text-xs theme-text-secondary").tooltip(message)

                ui_refs["current_stage"] = current_stage
                current_stage()

                # 上次结果：直接展示服务侧真实计数，无真实总数不造百分比
                @ui.refreshable
                def last_result_info():
                    result = sync_state.get("last_result")
                    if not result:
                        return
                    with ui.column().classes("cloud-sync-result w-full gap-2"):
                        with ui.row().classes("w-full items-center gap-2"):
                            ui.icon(
                                "check_circle" if result.get("success") else "error"
                            ).classes("cloud-sync-result-icon cloud-sync-result-success" if result.get("success") else "cloud-sync-result-icon cloud-sync-result-error")
                            ui.label(t("cloud_sync.last_result")).classes("text-xs font-medium theme-text-secondary")
                            if result.get("finished_at"):
                                ui.label(result["finished_at"]).classes("cloud-sync-result-time text-xs theme-text-muted")
                        if result.get("message"):
                            ui.label(result["message"]).classes("cloud-sync-result-message text-sm theme-text").style(
                                "white-space: pre-wrap"
                            )
                        ui.label(
                            t(
                                "cloud_sync.result_counts",
                                uploaded=result.get("uploaded", 0),
                                downloaded=result.get("downloaded", 0),
                                skipped=result.get("skipped", 0),
                                errors=result.get("errors", 0),
                            )
                        ).classes("text-xs theme-text-muted")

                ui_refs["last_result_info"] = last_result_info
                last_result_info()

                # 实际同步策略（与 sync_service 行为一致，不用推测的措辞）
                with ui.column().classes("cloud-sync-strategy w-full gap-2"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("info_outline").classes("cloud-sync-subtle-icon").props("aria-hidden=true")
                        ui.label(t("cloud_sync.strategy")).classes("text-xs font-medium theme-text-secondary")
                    ui.label(t("cloud_sync.sync_hint")).classes("text-xs theme-text-muted").style(
                        "white-space: pre-line"
                    )

        # 同步操作固定在底部，状态与说明较多时仍容易找到。
        with ui.column().classes("cloud-sync-action-bar w-full gap-2 shrink-0"):
            @ui.refreshable
            def sync_buttons():
                is_syncing = sync_state.get("is_syncing", False)
                is_enabled = sync_handlers.is_enabled()

                sync_btn = ui.button(
                    t("cloud_sync.sync_now"),
                    on_click=sync_handlers.do_sync,
                    icon="sync"
                ).props("color=primary no-caps").classes("cloud-sync-primary w-full")

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
    with ui.column().classes("cloud-sync-log-pane flex-1 h-full flex flex-col theme-content gap-0"):
        # 顶部信息区
        with ui.row().classes("cloud-sync-log-header workspace-toolbar w-full items-center justify-between theme-sidebar"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("history", size="xs").classes("theme-text-accent")
                ui.label(t("cloud_sync.log_title")).classes("text-sm font-medium theme-text")

            # 清空日志按钮（仅清空服务侧的同步日志缓冲，不影响同步结果）
            ui.button(
                icon="delete_sweep",
                on_click=sync_handlers.clear_logs,
            ).props(
                f'flat dense size=sm aria-label="{t("cloud_sync.clear_logs")}"'
            ).classes("theme-text-muted").tooltip(t("cloud_sync.clear_logs"))

        # 日志内容区
        @ui.refreshable
        def result_summary():
            result = sync_state.get("last_result") or {}
            errors = result.get("error_details") or []
            if errors:
                with ui.column().classes("sync-failure-summary w-full gap-2 px-4 py-3"):
                    ui.label(t("cloud_sync.failed_items", count=len(errors))).classes("text-sm font-medium theme-danger")
                    for message in errors:
                        ui.label(message).classes("text-xs theme-text break-words")
                    with ui.row().classes("items-center gap-2"):
                        async def copy_failures():
                            ok = await write_clipboard("\n".join(errors))
                            ui.notify(t("logs.copied", count=len(errors)) if ok else t("logs.copy_failed"),
                                      type="positive" if ok else "negative")
                        ui.button(t("cloud_sync.copy_failures"), icon="content_copy", on_click=copy_failures).props("flat dense no-caps")
                        if ui_refs.get("open_webdav_settings"):
                            ui.button(t("cloud_sync.go_settings"), icon="settings",
                                      on_click=lambda: _open_webdav_settings(ui_refs)).props("flat dense no-caps")
                    ui.label(t("cloud_sync.failure_next_step")).classes("text-xs theme-text-muted")

        ui_refs["sync_result_summary"] = result_summary
        result_summary()
        with ui.row().classes("w-full items-center justify-between px-4 py-1"):
            ui.label(t("cloud_sync.newest_first")).classes("text-xs theme-text-muted")
            def filter_errors(event):
                sync_state["errors_only"] = event.value
                sync_logs.refresh()
            ui.switch(t("cloud_sync.errors_only"), value=sync_state.get("errors_only", False),
                      on_change=filter_errors).props("dense").classes("text-xs")
        with ui.scroll_area().classes("cloud-sync-log-scroll flex-1 min-h-0 scroll-flush"):
            @ui.refreshable
            def sync_logs():
                logs = sync_state.get("logs", [])
                if sync_state.get("errors_only"):
                    logs = [log for log in logs if log.get("type") == "error"]
                if not logs:
                    with ui.column().classes("cloud-sync-empty w-full h-full items-center justify-center"):
                        ui.icon("event_note", size="lg").classes("theme-text-muted")
                        ui.label(t("cloud_sync.no_errors" if sync_state.get("errors_only") else "cloud_sync.no_logs")).classes("text-sm mt-2 theme-text-muted")
                else:
                    with ui.column().classes("cloud-sync-log-list w-full gap-0"):
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
        "info": ("info", "cloud-sync-log-info"),
        "success": ("check_circle", "cloud-sync-log-success"),
        "error": ("error", "cloud-sync-log-error"),
        "upload": ("cloud_upload", "cloud-sync-log-transfer"),
        "download": ("cloud_download", "cloud-sync-log-transfer"),
    }

    icon, icon_class = icon_map.get(log_type, ("info", "theme-text-muted"))

    with ui.row().classes("cloud-sync-log-row w-full items-start gap-3"):
        ui.icon(icon, size="xs").classes(f"cloud-sync-log-icon {icon_class}").props("aria-hidden=true")
        with ui.column().classes("cloud-sync-log-copy flex-1 min-w-0 gap-1"):
            ui.label(message).classes("cloud-sync-log-message text-sm theme-text")
            if timestamp:
                ui.label(timestamp).classes("cloud-sync-log-time text-xs theme-text-muted")


async def _open_webdav_settings(ui_refs: dict):
    """跳转到「设置 → 云同步」；由 pages.py 注入 ``ui_refs["open_webdav_settings"]``。"""
    callback = ui_refs.get("open_webdav_settings")
    if callback is None:
        return
    result = callback()
    if inspect.isawaitable(result):
        await result
