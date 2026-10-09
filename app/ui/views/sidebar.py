"""工作台全局导航；资料集与文档统一在中间浏览区操作。"""
from functools import partial
from types import SimpleNamespace
from typing import Callable

from nicegui import ui

from app.i18n import t
from app.skills import get_version
from app.ui.views.task_activity import render_task_activity


def render_app_header(current_view: dict, switch_view: Callable, callbacks: dict | None = None,
                      *, state: dict | None = None, file_handlers=None):
    with ui.row().classes("app-header w-full items-center flex-nowrap gap-2") as header:
        ui.button(icon="menu", color=None, on_click=(callbacks or {}).get("toggle_navigation")).props(
            f'flat dense round size=sm aria-label="{t("workspace.navigation")}"'
        ).classes("theme-text-muted").tooltip(t("workspace.navigation"))
        ui.label(t("app.name")).classes("app-brand font-semibold theme-text").tooltip(f"Piece v{get_version()}")
        ui.icon("chevron_right", size="xs").classes("theme-text-muted")
        title = ui.label().classes("app-view-title text-sm theme-text-secondary")
        ui.space()
        search = ui.button(icon="search", color=None, on_click=(callbacks or {}).get("focus_search")).props(
            f'flat dense round size=sm aria-label="{t("workspace.search_catalog")}" aria-keyshortcuts="Control+K Meta+K"'
        ).classes("theme-text-muted").tooltip(t("workspace.search_shortcut"))
        if state is not None:
            render_task_activity(state, callbacks or {}, file_handlers)
        with ui.button(t("sidebar.tools"), icon="tune", color=None).props("flat dense no-caps").classes("app-tools theme-text-secondary"):
            with ui.menu().props("auto-close"):
                for key, icon in (("recall_test", "manage_search"), ("mcp_config", "settings_input_component"),
                                  ("skills", "extension"), ("logs", "description")):
                    with ui.menu_item(on_click=partial(switch_view, key)).classes("gap-3"):
                        ui.icon(icon, size="xs").classes("theme-text-muted")
                        ui.label(t(f"sidebar.{key}"))
        for key, icon in (("cloud_sync", "cloud_sync"), ("settings", "settings")):
            ui.button(icon=icon, color=None, on_click=partial(switch_view, key)).props(
                f'flat dense round size=sm aria-label="{t(f"sidebar.{key}")}"'
            ).classes("theme-text-muted").tooltip(t(f"sidebar.{key}"))

    def refresh():
        if header.is_deleted:
            return
        title.set_text(t(f"sidebar.{current_view['value']}"))
        search.set_visibility(current_view["value"] in ("files", "wiki", "graph"))

    refresh()
    return SimpleNamespace(refresh=refresh)


def render_sidebar(current_view: dict, ui_refs: dict, state: dict, file_handlers, switch_view: Callable | None = None):
    items = {}
    with ui.column().classes("workspace-sidebar w-full h-full theme-sidebar gap-0 overflow-hidden") as sidebar:
        ui_refs["sidebar_container"] = sidebar
        with ui.column().classes("workspace-primary w-full gap-1"):
            for key, icon in (("files", "library_books"), ("wiki", "article"), ("graph", "hub")):
                with ui.button(color=None, on_click=partial(switch_view, key) if switch_view else None).props(
                    "flat dense no-caps no-ripple align=left"
                ).classes("workspace-nav-item w-full") as nav:
                    ui.icon(icon, size="18px").props("aria-hidden=true")
                    label = ui.label(t(f"sidebar.{key}")).classes("navigation-label")
                    ui.tooltip().bind_text_from(label, "text")
                    items[key] = (nav, label)
        ui.space()
        with ui.row().classes("w-full px-3 py-2 items-center gap-1 library-footer flex-nowrap"):
            ui.icon("storage", size="xs").classes("theme-text-muted shrink-0").props("aria-hidden=true")
            ui_refs["stats_label"] = ui.label(state.get("stats_text", t("stats.loading"))).classes("navigation-label text-xs theme-text-muted")
            ui.tooltip().bind_text_from(ui_refs["stats_label"], "text")

    def refresh():
        # 切换工作区只更新状态，不销毁导航按钮，快速连续点击不会丢失。
        if sidebar.is_deleted:
            return
        sidebar.classes(add="navigation-collapsed" if state.get("navigation_collapsed") else "",
                        remove="navigation-collapsed" if not state.get("navigation_collapsed") else "")
        for key, (nav, label) in items.items():
            selected = current_view["value"] == key
            label.set_text(t(f"sidebar.{key}"))
            nav._props["aria-label"] = t(f"sidebar.{key}")
            if key == "files":
                nav._props["data-import-collection"] = "all"
                nav._props["data-import-label"] = t("files.drop_library")
            nav.classes(remove="workspace-nav-active theme-text-secondary",
                        add="workspace-nav-active" if selected else "theme-text-secondary")
            nav.props("aria-current=page" if selected else "", remove="aria-current" if not selected else None)
            nav.update()

    refresh()
    return SimpleNamespace(refresh=refresh)
