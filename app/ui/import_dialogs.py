"""外部知识库一次性导入对话框：本机目录（Obsidian vault）与 Zotero 文献库。

对话框只收集参数、展示预览与结果；候选展开在 app.import_scan，导入在服务层，
与 CLI 走同一套规则。
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
            ui.button(icon="close", on_click=dialog.close).props("flat dense round").classes("theme-text-muted")
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


def zotero_import_dialog(on_preview: Callable, on_import: Callable):
    """从本机 Zotero 导入：打开即探测，列出集合供勾选，预览后导入。

    on_preview(collection_keys, mode) -> zotero_service.preview_import 的返回值，
    失败时抛 BusinessError（code 为 ZOTERO_DISABLED / ZOTERO_UNAVAILABLE 等）。
    on_import(collection_keys, mode) -> None（自行提示结果）
    """
    dialog, body = _dialog_frame(t("import.zotero_title"))
    state = {"preview": None, "error": None, "chosen": set(), "mode": "path"}

    async def refresh_preview():
        try:
            state["preview"] = await _call(on_preview, sorted(state["chosen"]) or None, state["mode"])
            state["error"] = None
        except Exception as exc:  # BusinessError 或连接层异常，都要在对话框里给出指引
            state["preview"] = None
            state["error"] = (getattr(exc, "code", "ERROR"), str(exc))
        content.refresh()

    with body:
        @ui.refreshable
        def content():
            if state["error"]:
                code, message = state["error"]
                hint_key = {"ZOTERO_DISABLED": "import.zotero_disabled", "ZOTERO_UNAVAILABLE": "import.zotero_unavailable"}.get(code)
                ui.label(t(hint_key) if hint_key else message).classes("text-sm text-red-400")
                if hint_key:
                    ui.label(message).classes("text-xs theme-text-muted")
                ui.button(t("import.retry"), on_click=refresh_preview).props("flat dense").classes("theme-text-accent")
                return
            data = state["preview"]
            if data is None:
                with ui.row().classes("items-center gap-2"):
                    ui.spinner(size="sm").classes("theme-text-accent")
                    ui.label(t("import.zotero_probing")).classes("text-sm theme-text-muted")
                return
            info = data["zotero"]
            ui.label(t("import.zotero_connected", version=info.get("zotero_version") or "?",
                       total=data["items_total"])).classes("text-sm theme-text")

            ui.label(t("import.zotero_collections")).classes("text-xs theme-text-muted")
            with ui.column().classes("w-full gap-1 max-h-48 overflow-auto"):
                if not data["collections"]:
                    ui.label(t("collections.empty")).classes("text-xs theme-text-muted")
                for item in data["collections"]:
                    label = item["path"] + (f"  ({item['item_count']})" if item.get("item_count") is not None else "")

                    def toggle(e, key=item["key"]):
                        if e.value:
                            state["chosen"].add(key)
                        else:
                            state["chosen"].discard(key)

                    ui.checkbox(label, value=item["key"] in state["chosen"], on_change=toggle).props("dense").classes("text-sm")

            def change_mode(e):
                state["mode"] = e.value

            ui.select(
                {"path": t("import.mode_path"), "top": t("import.mode_top"), "none": t("import.mode_none")},
                value=state["mode"], label=t("import.collection_mode"), on_change=change_mode,
            ).props("dense outlined").classes("w-full text-sm")

            with ui.column().classes("w-full gap-1 p-2 rounded").style("background: var(--bg-hover)"):
                ui.label(t("import.zotero_counts", importable=data["importable_count"],
                           skipped=data["skipped_count"])).classes("text-sm theme-text")
                for item in data["items"][:3]:
                    ui.label(f"• {item['filename']}").classes("text-xs theme-text-muted truncate")
                if data["importable_count"] > 3:
                    ui.label("…").classes("text-xs theme-text-muted")

        content()

        async def do_import():
            if state["preview"] is None:
                return
            await refresh_preview()
            if state["error"] or not state["preview"]["importable_count"]:
                if not state["error"]:
                    ui.notify(t("import.nothing_to_import"), type="warning")
                return
            dialog.close()
            await _call(on_import, sorted(state["chosen"]) or None, state["mode"])

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button(t("import.preview"), on_click=refresh_preview).props("flat dense").classes("theme-text-muted")
            ui.button(t("import.start"), on_click=do_import).props("dense unelevated color=primary")

    _open_dialog(dialog)
    ui.timer(0.05, refresh_preview, once=True)
    dialog.refresh_preview = refresh_preview  # 供测试与重试直接触发，不经过定时器
    return dialog
