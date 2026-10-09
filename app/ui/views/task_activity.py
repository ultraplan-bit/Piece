"""轻量任务活动入口：在任一工作区查看进行中与最近任务。

只读任务表，复用既有轮询与文件列表的进度文案，不新建后台系统：

- 面板隐藏时不刷新，打开后才跟随一秒轮询更新，避免无谓重绘
- 取消只作用于真正可取消的任务（pending/processing 且未在取消中）
- 失败/取消且无已发布结果的任务提供重试，不虚构恢复或撤销
- 查看对应文件通过 ``ui_refs["open_task_file"]`` 约定注入，未注入时
  退化为 ``file_handlers.open_reader``

面板由顶栏调用 ``render_task_activity(state, ui_refs, file_handlers)`` 渲染，
数据加载回调由 ``TaskHandlers`` 注册在 ``ui_refs["load_task_activity"]``。
"""

import inspect
import logging

from nicegui import ui

from app.i18n import t
from indexing.services import task_service
from indexing.utils import run_sync
from app.ui.views.files_view import _progress_text

logger = logging.getLogger(__name__)

# 与任务表默认分页一致：最近 20 条足够覆盖"正在进行和最近任务"
_LIST_LIMIT = 20

# 活动状态（含派生的 cancelling）到文案键
_STATUS_LABELS = {
    "pending": "task_activity.status_submitted",
    "processing": "task_activity.status_processing",
    "cancelling": "task_activity.status_cancelling",
    "completed": "task_activity.status_completed",
    "failed": "task_activity.status_failed",
    "cancelled": "task_activity.status_cancelled",
}

_STATUS_ICONS = {
    "pending": "schedule",
    "processing": "autorenew",
    "cancelling": "hourglass_top",
    "completed": "check_circle",
    "failed": "error_outline",
    "cancelled": "cancel",
}

_ACTIVE_STATES = ("pending", "processing", "cancelling")


def activity_status(task: dict) -> str:
    """把任务行归一到六种展示状态之一；取消中由 stage 派生。"""
    status = task.get("status", "pending")
    if status in task_service.ACTIVE_STATUSES and task.get("stage") == "cancelling":
        return "cancelling"
    return status if status in _STATUS_LABELS else "processing"


def is_cancellable(task: dict) -> bool:
    """只有仍在途且未在取消中的任务才可安全取消。"""
    return (
        task.get("status") in task_service.ACTIVE_STATUSES
        and task.get("stage") != "cancelling"
    )


def is_retryable(task: dict) -> bool:
    """失败/取消且尚无已发布结果的任务才能重试（与仓储约束一致）。"""
    return task.get("status") in ("failed", "cancelled") and task.get("result") is None


def split_activity(tasks: list, limit: int = _LIST_LIMIT) -> tuple:
    """保留全部在途任务，终态只展示最近 limit 条。"""
    ordered = sorted(tasks, key=lambda item: item.get("id", 0), reverse=True)
    active = [task for task in ordered if activity_status(task) in _ACTIVE_STATES]
    recent = [task for task in ordered if activity_status(task) not in _ACTIVE_STATES][:limit]
    return active, recent


async def _open_file(ui_refs: dict, file_handlers, file_id: int) -> None:
    """优先走注入的打开回调，其次退化为普通阅读。"""
    menu = ui_refs.get("task_activity_menu")
    if menu is not None and not menu.is_deleted:
        menu.close()
    callback = ui_refs.get("open_task_file")
    if callback:
        result = callback(file_id)
        if inspect.isawaitable(result):
            await result
        return
    if file_handlers is not None and hasattr(file_handlers, "open_reader"):
        await file_handlers.open_reader(file_id)


async def _cancel_task(task: dict, file_handlers) -> None:
    """复用文件处理器的取消入口（提示+刷新）；缺省时直接调用服务。"""
    task_id = task["id"]
    if file_handlers is not None and hasattr(file_handlers, "handle_cancel_task"):
        await file_handlers.handle_cancel_task(task_id)
        return
    try:
        updated = await run_sync(task_service.cancel_task, task_id)
    except Exception as exc:  # noqa: BLE001 - 统一转成可读提示
        logger.error("取消任务失败 (%s)", type(exc).__name__)
        ui.notify(t("task.action_failed"), type="negative")
        return
    key = (
        "task.cancelling"
        if updated["status"] in task_service.ACTIVE_STATUSES
        else "task.cancelled"
        if updated["status"] == "cancelled"
        else "task.already_finished"
    )
    ui.notify(t(key), type="info")


async def _retry_task(task: dict, file_handlers) -> None:
    """复用文件处理器的重试入口；缺省时直接调用服务。"""
    task_id = task["id"]
    if file_handlers is not None and hasattr(file_handlers, "handle_retry_task"):
        await file_handlers.handle_retry_task(task_id)
        return
    try:
        await run_sync(task_service.retry_task, task_id)
    except ValueError as exc:
        ui.notify(str(exc), type="warning")
        return
    except Exception as exc:  # noqa: BLE001
        logger.error("重试任务失败 (%s)", type(exc).__name__)
        ui.notify(t("task.action_failed"), type="negative")
        return
    ui.notify(t("files.reindex_started"), type="positive")


def active_task_count(state: dict) -> int:
    """计入后台任务与尚在接收/受理的文档，不额外查询。"""
    return len(state.get("task_progress") or {}) + state.get("uploading_count", 0)


def update_activity_badge(state: dict, ui_refs: dict) -> None:
    """只更新顶栏徽标/计数，不重建按钮。

    面板关闭时也随 ``task_progress`` 缓存变化刷新；计数未变则不动 DOM，
    避免无谓重绘。
    """
    badge = ui_refs.get("task_activity_badge")
    if badge is None or getattr(badge, "is_deleted", False):
        return
    count = active_task_count(state)
    if count == state.get("task_activity_badge_count"):
        return
    state["task_activity_badge_count"] = count
    badge.set_text(str(count) if count else "")
    badge.set_visibility(bool(count))


def _action_handler(button, runner, reload):
    """局部禁用并拦住已排队的重复点击；完成后载入真实任务状态。"""
    busy = False

    async def handler(_=None):
        nonlocal busy
        if busy or button.is_deleted:
            return
        busy = True
        button.disable()
        try:
            await runner()
        finally:
            busy = False
            if not button.is_deleted:
                button.enable()
            if reload is not None:
                await reload()

    return handler


def _section_title(text: str, count: int) -> None:
    with ui.row().classes(
        "task-activity-section w-full items-center justify-between px-3 pt-2 pb-1 flex-nowrap"
    ):
        ui.label(text).classes("text-xs font-semibold theme-text-muted")
        ui.badge(str(count)).props("dense outline color=primary")


def _render_row(task: dict, ui_refs: dict, file_handlers, reload) -> None:
    """一行任务：状态、文件名/进度、以及可用操作。"""
    status = activity_status(task)
    filename = task.get("original_filename") or t("task_activity.untitled")
    is_active = status in _ACTIVE_STATES

    with ui.row().classes(
        "task-activity-row w-full items-center gap-2 px-3 py-2 flex-nowrap"
    ).props(f'data-task-id={task.get("id")} data-status={status}'):
        ui.icon(_STATUS_ICONS.get(status, "schedule"), size="xs").classes(
            "theme-text-accent" if is_active else "theme-text-muted"
        ).props("aria-hidden=true")
        with ui.column().classes("flex-1 min-w-0 gap-0"):
            ui.label(filename).classes("text-xs theme-text truncate w-full").tooltip(filename)
            if is_active:
                ui.label(_progress_text(task)).classes(
                    "text-xs theme-text-muted truncate w-full"
                ).tooltip(t(_STATUS_LABELS[status]))
            else:
                secondary = task.get("error_message") if status == "failed" else None
                secondary = secondary or t(_STATUS_LABELS[status])
                ui.label(secondary).classes(
                    "text-xs truncate w-full " + ("theme-danger" if status == "failed" else "theme-text-muted")
                ).tooltip(secondary)

        file_id = task.get("file_id")
        if file_id is not None and (
            ui_refs.get("open_task_file") or hasattr(file_handlers or object(), "open_reader")
        ):
            async def open_handler(_, fid=file_id):
                await _open_file(ui_refs, file_handlers, fid)

            ui.button(icon="open_in_new", on_click=open_handler).props(
                f'flat dense round size=xs aria-label="{t("task_activity.open_file")}"'
            ).classes("theme-text-muted").tooltip(t("task_activity.open_file"))

        if is_cancellable(task):
            cancel = ui.button(icon="stop").props(
                f'flat dense round size=xs aria-label="{t("task.cancel")}"'
            ).classes("theme-text-muted").tooltip(t("task.cancel"))
            cancel.on("click.stop", _action_handler(
                cancel, lambda: _cancel_task(task, file_handlers), reload))
        elif is_retryable(task):
            retry = ui.button(icon="refresh").props(
                f'flat dense round size=xs aria-label="{t("task.retry")}"'
            ).classes("theme-text-muted").tooltip(t("task.retry"))
            retry.on("click.stop", _action_handler(
                retry, lambda: _retry_task(task, file_handlers), reload))


def render_task_activity(state: dict, ui_refs: dict, file_handlers=None) -> None:
    """渲染顶栏任务活动入口（按钮 + 下拉面板）。

    Args:
        state: 共享状态字典，读取 ``task_activity``、``task_progress``
        ui_refs: UI 引用字典；注册 ``task_activity_panel``，读取
            ``load_task_activity`` 与 ``open_task_file``
        file_handlers: 文件处理器实例；用于复用取消/重试与打开阅读
    """
    # 顶栏每次重建都视为面板关闭：避免对已销毁的旧面板继续刷新
    state["task_activity_open"] = False
    reload = ui_refs.get("load_task_activity")

    @ui.refreshable
    def activity_panel():
        active, recent = split_activity(state.get("task_activity") or [])
        with ui.column().classes("task-activity-panel w-80 max-w-full gap-0").style(
            "max-height: 60vh; overflow-y: auto"
        ):
            upload_count = state.get("uploading_count", 0)
            if upload_count:
                _section_title(t("task_activity.importing"), upload_count)
                with ui.row().classes("w-full items-center gap-2 px-3 pb-1 flex-nowrap"):
                    target = state.get("upload_target_name") or t("files.drop_library")
                    ui.label(target).classes("text-xs theme-text-muted truncate flex-1").tooltip(target)
                    if file_handlers is not None:
                        ui.button(icon="close", on_click=file_handlers.cancel_upload).props(
                            f'flat dense round size=xs aria-label="{t("files.upload_cancel")}"'
                        ).tooltip(t("files.upload_cancel"))
                for filename in state.get("uploading_files", []):
                    with ui.row().classes("w-full items-center gap-2 px-3 py-1 flex-nowrap"):
                        ui.spinner(size="xs").classes("theme-text-accent")
                        ui.label(filename).classes("text-xs theme-text truncate flex-1").tooltip(filename)
            if not active and not recent and not upload_count:
                ui.label(t("task_activity.empty")).classes(
                    "text-xs theme-text-muted px-3 py-3"
                )
                return
            if active:
                _section_title(t("task_activity.active"), len(active))
                for task in active:
                    _render_row(task, ui_refs, file_handlers, reload)
            if recent:
                _section_title(t("task_activity.recent"), len(recent))
                for task in recent:
                    _render_row(task, ui_refs, file_handlers, reload)

    ui_refs["task_activity_panel"] = activity_panel

    def refresh_upload_activity():
        update_activity_badge(state, ui_refs)
        if state.get("task_activity_open"):
            activity_panel.refresh()

    ui_refs["refresh_upload_activity"] = refresh_upload_activity

    async def on_show():
        state["task_activity_open"] = True
        # 关闭期间上传状态也会变化；任务表未变时仍需展示最新传输信息。
        activity_panel.refresh()
        if reload is not None:
            await reload()

    active_count = active_task_count(state)
    state["task_activity_badge_count"] = active_count
    with ui.button(icon="task_alt", color=None).props(
        f'flat dense round size=sm aria-label="{t("task_activity.open")}"'
    ).classes("theme-text-muted").tooltip(t("task_activity.title")):
        # 徽标常驻（计数为 0 时隐藏），供轮询原地更新，避免重建按钮丢焦点
        badge = ui.badge(str(active_count) if active_count else "", color="primary").props(
            "floating dense"
        ).classes("task-activity-badge")
        ui_refs["task_activity_badge"] = badge
        badge.set_visibility(bool(active_count))
        menu = ui.menu().props("auto-close=false").classes("task-activity-menu")
        ui_refs["task_activity_menu"] = menu
        with menu:
            activity_panel()
        menu.on("show", on_show)
        menu.on("hide", lambda: state.update(task_activity_open=False))
