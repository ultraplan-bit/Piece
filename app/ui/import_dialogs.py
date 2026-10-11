"""本机目录导入对话框与共用对话框布局。

候选展开在 app.import_scan，导入在服务层，与 CLI 走同一套规则。
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Callable

from nicegui import ui

from app.i18n import t
from app.ui.components import _open_dialog


def _dialog_frame(title: str, width: str = "w-[520px]"):
    dialog = ui.dialog()
    with dialog, ui.card().classes(f"{width} theme-card theme-card-shadow"):
        with ui.row().classes("w-full items-center justify-between pb-2").style(
            "border-bottom: 1px solid var(--border-color)"
        ):
            ui.label(title).classes("text-base font-semibold theme-text")
            ui.button(icon="close", on_click=dialog.close).props(
                f'flat dense round aria-label="{t("knowledge.close")}"').classes("theme-text-muted")
        body = ui.column().classes("w-full gap-3 py-3")
    return dialog, body


async def _call(callback: Callable, *args):
    result = callback(*args)
    if inspect.isawaitable(result):
        result = await result
    return result


def folder_import_dialog(on_preview: Callable, on_import: Callable):
    """导入本机目录：路径、递归、排除规则与纯链接笔记开关。

    on_preview(options) -> {"candidates": [...], "skipped": [...], "excluded": [...]}
    on_import(options) -> None（自行提示结果）
    """
    dialog, body = _dialog_frame(t("import.folder_title"))
    preview = {"data": None}

    with body:
        path_input = ui.input(
            label=t("import.folder_path"), placeholder=t("import.folder_path_hint")
        ).props("dense outlined").classes("w-full text-sm")
        with ui.row().classes("w-full gap-4"):
            recursive = ui.checkbox(t("import.recursive"), value=True).props("dense").classes("text-sm")
            skip_links = ui.checkbox(t("import.skip_link_notes"), value=True).props("dense").classes("text-sm")
        exclude_input = ui.input(
            label=t("import.exclude"), value="templates", placeholder=t("import.exclude_hint")
        ).props("dense outlined").classes("w-full text-sm")
        ui.label(t("import.folder_note")).classes("text-xs theme-text-muted")

        @ui.refreshable
        def summary():
            data = preview["data"]
            if data is None:
                return
            with ui.column().classes("w-full gap-1 p-2 rounded").style("background: var(--bg-hover)"):
                ui.label(t("import.preview_counts", candidates=len(data["candidates"]),
                           excluded=len(data["excluded"]), skipped=len(data["skipped"]))).classes("text-sm theme-text")
                for item in data["excluded"][:5]:
                    ui.label(f"− {Path(item['path']).name}  ({item['reason']})").classes("text-xs theme-text-muted truncate")
                for item in data["skipped"][:5]:
                    ui.label(f"! {Path(item['path']).name}  ({item['reason']})").classes("text-xs text-red-400 truncate")

        summary()

        def options():
            raw = (path_input.value or "").strip().strip('"')
            if not raw:
                ui.notify(t("import.folder_path_empty"), type="warning")
                return None
            patterns = [p.strip() for p in (exclude_input.value or "").replace("，", ",").split(",") if p.strip()]
            return {"path": Path(raw), "recursive": bool(recursive.value),
                    "skip_link_notes": bool(skip_links.value), "exclude": patterns}

        async def do_preview():
            opts = options()
            if opts is None:
                return
            preview["data"] = await _call(on_preview, opts)
            summary.refresh()

        async def do_import():
            opts = options()
            if opts is None:
                return
            if preview["data"] is None:
                preview["data"] = await _call(on_preview, opts)
                summary.refresh()
            if not preview["data"]["candidates"]:
                ui.notify(t("import.nothing_to_import"), type="warning")
                return
            dialog.close()
            await _call(on_import, opts)

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button(t("import.preview"), on_click=do_preview).props("flat dense").classes("theme-text-muted")
            ui.button(t("import.start"), on_click=do_import).props("dense unelevated color=primary")

    _open_dialog(dialog)
    return dialog
