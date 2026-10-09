"""
日志查看视图

职责:
- 渲染日志单栏：工具栏（级别过滤、自动刷新、总数、复制、清空）+ 可滚动内容
- 直读进程内的日志缓冲区，不回环 HTTP，也不新增后端接口

设计约定:
- 控制项与内容合并到同一栏，日志获得工作区全宽；页面渲染日志时只需调用
  ``render_logs_right``，中栏可留空（``render_logs_middle`` 保留为兼容空实现）。
- 过滤级别、自动刷新开关、滚动位置都放在 ``ui_refs`` 里持久保存，切换视图
  再回来时不丢失；自动刷新计时器随本栏重建而生灭——NiceGUI 在元素被删除时会
  自动停止其计时器（见已安装 Timer._should_stop 对 is_deleted 的判断），因此只在
  开关切换时创建/取消，不额外自造生命周期。
- 日志按时间正序显示（最新在底部）。入页时恢复上次滚动位置；自动刷新只在
  用户已贴底时跟随最新，向上阅读时保持原位。
- 读取与复制默认脱敏敏感内容（无开关，避免误泄露）。
"""

import re

from nicegui import context, ui

from app.i18n import t
from app.ui.components import write_clipboard
from app.logging_config import clear_log_buffer, count_log_buffer, get_log_buffer

# 自动刷新间隔（秒）
_AUTO_REFRESH_SECONDS = 5.0
# 单次渲染上限
_DISPLAY_LIMIT = 300
# 判定「已贴底」的像素容差：在此范围内继续跟随最新日志
_TAIL_TOLERANCE_PX = 24
# 行内摘要长度上限；完整内容进详情对话框
_SUMMARY_LIMIT = 200

# 需要脱敏的键值式敏感字段（键名不区分大小写）
_SECRET_KEYS = (
    "password", "passwd", "pwd", "token", "api_key", "apikey", "secret", "authorization",
)
_SECRET_KEY_ALT = "|".join(_SECRET_KEYS)
# URL 内嵌凭据：scheme://user:pass@host
_URL_CREDENTIALS = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
# Bearer / Basic 等鉴权头
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
# key=value / "key": "value" / key='a b'；键与值两侧都可能带引号，值可含空格
_KEY_VALUE_SECRET = re.compile(
    r"(?i)([\"']?)\b(" + _SECRET_KEY_ALT + r")\b\1(\s*[:=]\s*)"
    r"(?:([\"'])(.*?)\4|([^\"'\s,;]+))"
)


def _mask_key_value(match: re.Match) -> str:
    lead, key, sep = match.group(1), match.group(2), match.group(3)
    if match.group(4):
        quote = match.group(4)
        return f"{lead}{key}{lead}{sep}{quote}***{quote}"
    return f"{lead}{key}{lead}{sep}***"


def mask_sensitive(text: str) -> str:
    """按默认规则脱敏日志正文中的凭据，保留其余可读内容。"""
    if not text:
        return text
    masked = _URL_CREDENTIALS.sub("://***:***@", text)
    masked = _BEARER.sub(lambda match: f"{match.group(1)} ***", masked)
    return _KEY_VALUE_SECRET.sub(_mask_key_value, masked)


def display_message(message: str, reveal: bool = False) -> str:
    """读取时默认脱敏；``reveal`` 仅供内部/测试显式取原文。"""
    return message if reveal else mask_sensitive(message)


def summary_message(message: str, reveal: bool = False) -> str:
    """行内只显示首行摘要（有长度上限），多行堆栈的完整内容留给详情。"""
    first_line = message.splitlines()[0] if message else ""
    text = display_message(first_line, reveal)
    if len(text) > _SUMMARY_LIMIT:
        text = text[:_SUMMARY_LIMIT] + "…"
    return text


def should_tail(scroll: dict) -> bool:
    """未滚动过或已在底部附近时，刷新后继续跟随最新日志。"""
    position = scroll.get("position")
    if position is None:
        return True
    size = scroll.get("size") or 0
    container = scroll.get("container") or 0
    return position + container >= size - _TAIL_TOLERANCE_PX


def is_expandable(log: dict) -> bool:
    """多行（异常堆栈）或很长的日志提供「查看详情」入口。"""
    message = log.get("message", "")
    return "\n" in message or len(message) > _SUMMARY_LIMIT


def format_copy_text(logs: list, reveal: bool = False) -> str:
    """把当前日志拼成可复制的纯文本（默认脱敏）。"""
    lines = []
    for log in logs:
        timestamp = log.get("timestamp", "")
        if "T" in timestamp:
            timestamp = timestamp.split("T")[1].split(".")[0]
        message = display_message(log.get("message", ""), reveal)
        lines.append(f'{timestamp} [{log.get("level", "INFO")}] {log.get("module", "")}: {message}')
    return "\n".join(lines)


def render_logs_middle(ui_refs: dict):
    """兼容空实现：日志已合并为单栏（``render_logs_right``），页面可不再渲染中栏。"""
    return None


def render_logs_right(ui_refs: dict):
    """渲染日志单栏：工具栏 + 内容。状态自持，不依赖中栏先渲染。"""
    # 持久状态：跨视图切换保留过滤级别、自动刷新开关与滚动位置。
    log_filter = ui_refs.setdefault("log_filter", {"level": None})
    auto_refresh_state = ui_refs.setdefault("auto_refresh_state", {"enabled": False})
    scroll = ui_refs.setdefault("log_scroll", {"position": None, "size": 0, "container": 0})
    stats = ui_refs.setdefault("stats", {"total": 0})

    with ui.column().classes(
        "flex-1 h-full flex flex-col overflow-hidden theme-panel gap-0 min-w-0"
    ):
        with ui.row().classes(
            "w-full px-4 items-center gap-3 flex-wrap"
        ).style("border-bottom: 1px solid var(--border-color); min-height: 44px"):
            with ui.row().classes("items-center gap-2 shrink-0"):
                ui.icon("receipt_long", size="xs").classes("theme-text-accent")
                ui.label(t("logs.title")).classes("text-sm font-medium theme-text")

            ui.select(
                options={
                    "ALL": t("logs.level_all"),
                    "DEBUG": "DEBUG",
                    "INFO": "INFO",
                    "WARNING": "WARNING",
                    "ERROR": "ERROR",
                    "CRITICAL": "CRITICAL",
                },
                value=log_filter.get("level") or "ALL",
                on_change=lambda e: _on_level_change(e.value, ui_refs),
            ).props("dense outlined options-dense").classes("w-28 shrink-0 text-xs")

            ui.switch(
                t("logs.auto_refresh"),
                value=auto_refresh_state["enabled"],
                on_change=lambda e: _toggle_auto_refresh(e.value, ui_refs),
            ).props("dense").classes("text-xs shrink-0")

            ui.space()

            with ui.row().classes("items-center gap-1 shrink-0"):
                ui.label(t("logs.total") + ":").classes("text-xs theme-text-muted")
                logs_stats_label = ui.label(str(stats.get("total", 0))).classes(
                    "text-xs theme-text-accent"
                )
                ui_refs["logs_stats_label"] = logs_stats_label

            with ui.row().classes("items-center gap-0 shrink-0"):
                ui.button(
                    icon="refresh",
                    on_click=lambda: _refresh_logs(ui_refs),
                ).props(f'flat dense round aria-label="{t("logs.refresh")}"').classes(
                    "theme-text-muted"
                ).tooltip(t("logs.refresh"))
                ui.button(
                    icon="content_copy",
                    on_click=lambda: _copy_logs(ui_refs),
                ).props(f'flat dense round aria-label="{t("logs.copy")}"').classes(
                    "theme-text-muted"
                ).tooltip(t("logs.copy"))
                ui.button(
                    icon="delete_sweep",
                    on_click=lambda: _confirm_clear(ui_refs),
                ).props(f'flat dense round color=negative aria-label="{t("logs.clear")}"'
                        ).tooltip(t("logs.clear"))

        # 内容区：全宽、等宽字体、可滚动
        logs_scroll = ui.scroll_area(
            on_scroll=lambda e: scroll.update(
                position=e.vertical_position,
                size=e.vertical_size,
                container=e.vertical_container_size,
            )
        ).classes("w-full flex-1 min-w-0 scroll-flush")
        ui_refs["logs_scroll_area"] = logs_scroll
        with logs_scroll:

            @ui.refreshable
            def logs_content():
                logs = ui_refs.get("logs_data", [])
                if not logs:
                    with ui.column().classes(
                        "w-full h-full items-center justify-center"
                    ):
                        ui.icon("description", size="xl").classes("theme-text-muted")
                        ui.label(t("logs.empty")).classes("text-sm theme-text-muted")
                else:
                    with ui.column().classes("w-full gap-0 py-1"):
                        for log in logs:
                            _render_log_entry(log)

            ui_refs["logs_content"] = logs_content
            logs_content()

        # 按持久状态挂载自动刷新计时器（随本栏重建而重建）
        _sync_auto_refresh(ui_refs)

    # 入页：读取数据并恢复上次滚动位置（不强制贴底看最新）
    _refresh_logs(ui_refs, restore=True)


def _render_log_entry(log: dict):
    """渲染单条日志（正序排列，最新在底部）。行内只给首行摘要。"""
    level = log.get("level", "INFO")
    level_colors = {
        "DEBUG": "theme-text-muted",
        "INFO": "theme-text-secondary",
        "WARNING": "text-yellow-600",
        "ERROR": "text-red-500",
        "CRITICAL": "text-purple-500",
    }
    level_color = level_colors.get(level, "theme-text-muted")

    timestamp = log.get("timestamp", "")
    time_part = timestamp.split("T")[1].split(".")[0] if "T" in timestamp else timestamp

    with ui.row().classes("w-full items-start gap-2 px-4 py-0.5 text-xs min-w-0").style(
        "font-family: var(--font-mono)"
    ):
        ui.label(time_part).classes("theme-text-muted w-20 shrink-0")
        ui.label(f"[{level}]").classes(f"{level_color} font-bold w-24 shrink-0")
        module = log.get("module", "")
        ui.label(module).classes("theme-text-muted w-40 shrink-0 truncate").tooltip(module)
        ui.label(summary_message(log.get("message", ""))).classes(
            "theme-text flex-1 min-w-0"
        ).style("white-space: pre-wrap; word-break: break-word")

        if is_expandable(log):
            ui.button(
                icon="open_in_full",
                on_click=lambda log=log: _show_detail(log),
            ).props(
                f'flat dense round size=xs aria-label="{t("logs.view_exception")}"'
            ).classes("theme-text-muted shrink-0").tooltip(t("logs.view_exception"))


def _show_detail(log: dict):
    """在对话框里完整展示单条日志（默认脱敏）。"""
    body = display_message(log.get("message", ""))
    with ui.dialog() as dialog, ui.card().classes(
        "w-[720px] max-w-full theme-card"
    ).style("max-height: 90vh"):
        with ui.row().classes("w-full items-center gap-2 pb-2").style(
            "border-bottom: 1px solid var(--border-color)"
        ):
            ui.label(t("logs.detail_title")).classes("text-base font-semibold theme-text")
            ui.space()
            ui.button(icon="close", on_click=dialog.close).props(
                f'flat dense round aria-label="{t("confirm_dialog.btn_cancel")}"'
            ).classes("theme-text-muted")
        with ui.scroll_area().classes("w-full").style("height: 60vh; min-height: 12rem"):
            ui.label(body).classes("text-xs theme-text").style(
                "font-family: var(--font-mono); white-space: pre-wrap; word-break: break-word"
            )
    # 停靠到页面根容器，避免日志行重建时对话框被一并销毁
    dialog.move(context.client.content)
    dialog.open()
    return dialog


def _on_level_change(level: str, ui_refs: dict):
    """级别过滤变更"""
    ui_refs["log_filter"]["level"] = None if level == "ALL" else level
    _refresh_logs(ui_refs)


def _refresh_logs(ui_refs: dict, *, restore: bool = False):
    """刷新日志。纯内存读取，同步执行；无变化不重建，但入页时仍恢复滚动位置。"""
    try:
        log_filter = ui_refs["log_filter"]
        stats = ui_refs["stats"]
        level = log_filter.get("level")
        # 缓冲区返回最新在前，这里反转为正序（最新在底部）
        logs = list(reversed(get_log_buffer(level=level, limit=_DISPLAY_LIMIT)))
        total = count_log_buffer(level=level)

        if logs != ui_refs.get("logs_data", []) or total != stats.get("total", 0):
            ui_refs["logs_data"] = logs
            stats["total"] = total
            label = ui_refs.get("logs_stats_label")
            if label is not None and not label.is_deleted:
                label.set_text(str(total))
            content = ui_refs.get("logs_content")
            if content is not None:
                content.refresh()

        _scroll_logs(ui_refs, restore=restore)
    except Exception as e:
        ui.notify(f"{t('logs.refresh_failed')}: {e}", type="negative")


def _scroll_logs(ui_refs: dict, *, restore: bool):
    """入页恢复上次位置；否则仅在贴底时跟随最新，向上阅读保持原位。"""
    area = ui_refs.get("logs_scroll_area")
    if area is None or area.is_deleted:
        return
    scroll = ui_refs["log_scroll"]
    if restore:
        position = scroll.get("position")
        if position is None:
            area.scroll_to(percent=1.0)
        else:
            area.scroll_to(pixels=position)
    elif should_tail(scroll):
        area.scroll_to(percent=1.0)
    elif scroll.get("position") is not None:
        area.scroll_to(pixels=scroll["position"])


async def _copy_logs(ui_refs: dict):
    """复制当前日志（默认脱敏）；仅在 JS 明确返回成功时才提示成功。"""
    logs = ui_refs.get("logs_data", [])
    if not logs:
        ui.notify(t("logs.copy_empty"), type="warning")
        return
    ok = await write_clipboard(format_copy_text(logs))
    if ok is True:
        ui.notify(t("logs.copied", count=len(logs)), type="positive")
    else:
        ui.notify(t("logs.copy_failed"), type="negative")


def _confirm_clear(ui_refs: dict):
    """清空前确认：清空的是进程内共享的内存日志缓冲区，磁盘日志不动。"""
    from app.ui.components import confirm_dialog

    confirm_dialog(
        title=t("logs.clear_title"),
        message=t("logs.clear_message"),
        on_confirm=lambda: _clear_logs(ui_refs),
        confirm_text=t("logs.clear"),
        danger=True,
    )


def _clear_logs(ui_refs: dict):
    """清空进程内共享的内存日志缓冲区（磁盘日志不动），随后刷新列表。"""
    try:
        clear_log_buffer()
        ui.notify(t("logs.cleared"), type="positive")
    except Exception as e:
        ui.notify(f"{t('logs.clear_failed')}: {e}", type="negative")
        return
    _refresh_logs(ui_refs)


def _toggle_auto_refresh(enabled: bool, ui_refs: dict):
    """切换自动刷新：按需创建或取消计时器。"""
    ui_refs["auto_refresh_state"]["enabled"] = enabled
    _sync_auto_refresh(ui_refs)


def _sync_auto_refresh(ui_refs: dict):
    """保证计时器与开关状态一致；随本栏重建自然销毁，无需手动清理。"""
    state = ui_refs["auto_refresh_state"]
    timer = ui_refs.get("auto_refresh_timer")
    alive = timer is not None and not timer.is_deleted
    if state["enabled"] and not alive:
        ui_refs["auto_refresh_timer"] = ui.timer(
            _AUTO_REFRESH_SECONDS, lambda: _auto_refresh_tick(ui_refs)
        )
    elif not state["enabled"] and alive:
        timer.cancel()
        ui_refs["auto_refresh_timer"] = None


def _auto_refresh_tick(ui_refs: dict):
    """自动刷新回调：关闭或视图已卸载时跳过。"""
    if not ui_refs["auto_refresh_state"]["enabled"]:
        return
    _refresh_logs(ui_refs)
