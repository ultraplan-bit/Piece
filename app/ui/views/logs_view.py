"""
日志查看视图

职责:
- 渲染日志控制中栏（级别过滤、自动刷新、操作按钮）
- 渲染日志内容右栏（日志列表、彩色标记）
"""

from nicegui import ui
from app.i18n import t
from app.logging_config import clear_log_buffer, count_log_buffer, get_log_buffer


def render_logs_middle(ui_refs: dict):
    """
    渲染日志控制中栏

    Args:
        ui_refs: UI 组件引用字典
    """
    # 日志过滤状态
    log_filter = {"level": None}  # None = 全部
    auto_refresh_state = {"enabled": False}  # 自动刷新开关
    stats = {"total": 0}  # 统计信息

    with ui.column().classes(
        "w-64 h-full flex flex-col overflow-hidden theme-panel gap-0"
    ).style("border-right: 1px solid var(--border-color)"):
        # 顶部标题栏
        with ui.row().classes(
            "w-full px-3 items-center justify-between"
        ).style("border-bottom: 1px solid var(--border-color); height: 49px"):
            ui.label(t("logs.title")).classes("text-sm font-medium theme-text")

        # 日志控制区域
        with ui.column().classes("w-full px-3 py-4 gap-4").style(
            "border-bottom: 1px solid var(--border-color)"
        ):
            # 级别过滤
            ui.label(t("logs.filter_level")).classes("text-xs theme-text-muted")
            ui.select(
                options={
                    "ALL": t("logs.level_all"),
                    "DEBUG": "DEBUG",
                    "INFO": "INFO",
                    "WARNING": "WARNING",
                    "ERROR": "ERROR",
                    "CRITICAL": "CRITICAL",
                },
                value="ALL",
                on_change=lambda e: _on_level_change(e.value, log_filter, ui_refs),
            ).classes("w-full").props("dense outlined")

            # 自动刷新开关
            ui.switch(
                t("logs.auto_refresh"),
                value=False,
                on_change=lambda e: _toggle_auto_refresh(
                    e.value, auto_refresh_state, ui_refs
                ),
            ).classes("text-sm theme-text")

            # 统计信息
            with ui.row().classes("w-full items-center gap-2"):
                ui.label(t("logs.total") + ":").classes("text-xs theme-text-muted")
                logs_stats_label = ui.label("0").classes("text-xs theme-text-accent")
                ui_refs["logs_stats_label"] = logs_stats_label

            # 操作按钮
            with ui.row().classes("w-full gap-2"):
                ui.button(
                    t("logs.refresh"),
                    icon="refresh",
                    on_click=lambda: _refresh_logs(log_filter, ui_refs, stats),
                ).props("flat dense").classes("flex-1")

                ui.button(
                    t("logs.clear"),
                    icon="delete_sweep",
                    on_click=lambda: _clear_logs(log_filter, ui_refs, stats),
                ).props("flat dense color=negative").classes("flex-1")

    # 保存引用
    ui_refs["log_filter"] = log_filter
    ui_refs["auto_refresh_state"] = auto_refresh_state
    ui_refs["stats"] = stats


def render_logs_right(ui_refs: dict):
    """
    渲染日志内容右栏

    Args:
        ui_refs: UI 组件引用字典
    """
    with ui.column().classes("flex-1 h-full flex flex-col overflow-hidden theme-panel gap-0"):
        # 顶部标题栏
        with ui.row().classes(
            "w-full px-4 items-center justify-between"
        ).style("border-bottom: 1px solid var(--border-color); height: 49px"):
            ui.label(t("logs.content")).classes("text-sm font-medium theme-text")

        # 日志内容区域（等宽字体，滚动）
        with ui.scroll_area().classes("flex-1"):

            @ui.refreshable
            def logs_content():
                """日志内容渲染器"""
                logs = ui_refs.get("logs_data", [])

                if not logs:
                    with ui.column().classes("w-full h-full items-center justify-center"):
                        ui.icon("description", size="xl").classes("theme-text-muted")
                        ui.label(t("logs.empty")).classes("text-sm theme-text-muted")
                else:
                    with ui.column().classes("w-full gap-1"):
                        for log in logs:
                            _render_log_entry(log)

            ui_refs["logs_content"] = logs_content
            logs_content()

    # 进入日志页时先拉一次，否则在用户点「刷新」前一直是空状态
    # （此处中栏已渲染完，log_filter / stats / logs_stats_label 均已就绪）
    _refresh_logs(ui_refs["log_filter"], ui_refs, ui_refs["stats"])


def _render_log_entry(log: dict):
    """
    渲染单条日志

    Args:
        log: 日志数据 {"timestamp", "level", "module", "message", "line", "function"}
    """
    level = log.get("level", "INFO")

    # 级别颜色映射
    level_colors = {
        "DEBUG": "text-gray-500",
        "INFO": "text-blue-600",
        "WARNING": "text-yellow-600",
        "ERROR": "text-red-600",
        "CRITICAL": "text-purple-600",
    }
    level_color = level_colors.get(level, "text-gray-600")

    # 时间戳（只显示时分秒）
    timestamp = log.get("timestamp", "")
    if "T" in timestamp:
        time_part = timestamp.split("T")[1].split(".")[0]  # 提取 HH:MM:SS
    else:
        time_part = timestamp

    with ui.row().classes("w-full items-start gap-2 text-xs").style(
        "font-family: var(--font-mono)"
    ):
        # 时间
        ui.label(time_part).classes("text-gray-500 w-20 flex-shrink-0")

        # 级别（固定宽度）
        ui.label(f"[{level}]").classes(f"{level_color} font-bold w-24 flex-shrink-0")

        # 模块名（固定宽度）
        module = log.get("module", "")
        ui.label(module).classes("text-gray-600 w-48 flex-shrink-0 truncate").tooltip(
            module
        )

        # 消息内容
        ui.label(log.get("message", "")).classes("theme-text flex-1").style(
            "word-break: break-all"
        )


def _on_level_change(level: str, log_filter: dict, ui_refs: dict):
    """级别过滤变更"""
    log_filter["level"] = None if level == "ALL" else level
    _refresh_logs(log_filter, ui_refs, ui_refs["stats"])


def _refresh_logs(log_filter: dict, ui_refs: dict, stats: dict):
    """刷新日志"""

    if ui_refs.get("logs_refreshing"):
        return
    ui_refs["logs_refreshing"] = True

    async def _do_refresh():
        try:
            # 日志缓冲区已在当前进程内，直接读取，避免回环 HTTP 请求和端口不一致。
            level = log_filter.get("level")
            logs = get_log_buffer(level=level, limit=200)
            # 总计取缓冲区内符合过滤条件的实际条数，不受展示上限影响。
            total = count_log_buffer(level=level)

            # 无变化时不重建日志组件，降低自动刷新开销。
            if logs == ui_refs.get("logs_data", []) and total == stats.get("total", 0):
                return

            ui_refs["logs_data"] = logs
            stats["total"] = total
            ui_refs["logs_stats_label"].set_text(str(total))
            ui_refs["logs_content"].refresh()
        except Exception as e:
            ui.notify(f"{t('logs.refresh_failed')}: {e}", type="negative")
        finally:
            ui_refs["logs_refreshing"] = False

    ui.timer(0.01, _do_refresh, once=True)


def _clear_logs(log_filter: dict, ui_refs: dict, stats: dict):
    """清空日志"""

    async def _do_clear():
        try:
            clear_log_buffer()
            ui.notify(t("logs.cleared"), type="positive")
            # 刷新日志列表
            _refresh_logs(log_filter, ui_refs, stats)
        except Exception as e:
            ui.notify(f"{t('logs.clear_failed')}: {e}", type="negative")

    ui.timer(0.01, _do_clear, once=True)


def _toggle_auto_refresh(enabled: bool, auto_refresh_state: dict, ui_refs: dict):
    """切换自动刷新"""
    auto_refresh_state["enabled"] = enabled

    if enabled:
        # 启动定时器（每 5 秒刷新一次）
        def _auto_refresh_callback():
            if auto_refresh_state["enabled"]:
                _refresh_logs(
                    ui_refs["log_filter"],
                    ui_refs,
                    ui_refs["stats"],
                )

        timer = ui.timer(5.0, _auto_refresh_callback)
        ui_refs["auto_refresh_timer"] = timer
    else:
        # 停止定时器
        timer = ui_refs.get("auto_refresh_timer")
        if timer:
            timer.cancel()
