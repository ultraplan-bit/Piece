"""
Skill 导出视图

职责:
- 渲染 Skill 中栏（内置 Skill 清单）
- 渲染 Skill 右栏（详情预览 + 导出到用户指定目录）
- 显示并注入当前实例的 CLI 调用前缀
"""

import json
from pathlib import Path

from nicegui import ui

from app.i18n import t
from app.platform import get_default_data_dir
from app.skills import cli_prefix, export_skills, get_version, list_skills, render_skill, split_skill_meta
from app.ui.components import chunk_markdown, confirm_dialog
from indexing.utils import run_sync

# 内置 Skill 是安装期资源，运行期不变，加载一次即可
SKILLS = list_skills()


def _default_export_dir() -> str:
    """只为确定存在的 Claude Code 目录预设导出路径；其他客户端目录不猜。"""
    return "~/.claude/skills" if (Path.home() / ".claude" / "skills").is_dir() else ""


def _find_skill(skill_id):
    if not skill_id:
        return None
    return next((s for s in SKILLS if s["id"] == skill_id), None)


def render_skill_middle(
    selected_skill: dict,
    ui_refs: dict,
    on_select_skill: callable,
):
    """
    渲染 Skill 中栏（Skill 清单）

    Args:
        selected_skill: 选中的 Skill {"value": str | None}
        ui_refs: UI 组件引用字典
        on_select_skill: 选择 Skill 的回调
    """
    with ui.column().classes(
        "w-64 h-full flex flex-col overflow-hidden theme-panel gap-0"
    ).style("border-right: 1px solid var(--border-color)"):
        # 顶部标题栏
        with ui.row().classes(
            "w-full px-3 items-center justify-between"
        ).style("border-bottom: 1px solid var(--border-color); height: 49px"):
            ui.label(t("skills.title")).classes("text-sm font-medium theme-text")

        # Skill 列表（同 MCP 客户端列表的布局）
        with ui.scroll_area().classes("flex-1 scroll-flush"):
            @ui.refreshable
            def skill_list():
                with ui.column().classes("w-full gap-0.5 px-2 pt-2"):
                    if not SKILLS:
                        with ui.column().classes("w-full items-center py-6"):
                            ui.icon("extension", size="lg").classes("theme-text-muted")
                            ui.label(t("skills.empty")).classes("text-xs mt-2 theme-text-muted")
                    for skill in SKILLS:
                        is_selected = selected_skill["value"] == skill["id"]
                        container_classes = "w-full px-3 py-2 cursor-pointer transition-colors rounded-md "
                        if is_selected:
                            container_classes += "theme-selected"
                        else:
                            container_classes += "theme-hover"

                        with ui.element("div").classes(container_classes).on(
                            "click", lambda _, s=skill: on_select_skill(s["id"])
                        ):
                            with ui.row().classes("items-center gap-2"):
                                ui.icon("extension", size="xs").classes(
                                    "theme-text-accent" if is_selected else "theme-text-muted"
                                )
                                ui.label(skill["name"]).classes("text-sm theme-text")
                            # 单独占满一行，配合 truncate 生效（flex 行内无法收缩）
                            ui.label(skill["description"]).classes(
                                "w-full text-xs truncate theme-text-muted"
                            )

            ui_refs["skill_list"] = skill_list
            skill_list()


def render_skill_right(
    selected_skill: dict,
    export_state: dict,
    port: int = 8689,
):
    """
    渲染 Skill 右栏（导出区 + 详情预览）

    Args:
        selected_skill: 选中的 Skill {"value": str | None}
        export_state: 导出状态 {"export_dir": str}
        port: 当前服务端口，用于推导 CLI 调用前缀
    """
    skill = _find_skill(selected_skill["value"])
    # 服务进程内的 get_default_data_dir 已反映 --data-dir / PIECE_DATA_DIR
    prefix = cli_prefix(get_default_data_dir(), port)
    version = get_version()

    async def _run_export(skill_ids: list, overwrite: bool = False):
        """执行导出：目标已有同名 Skill 时先确认再覆盖。"""
        dir_text = (export_state.get("export_dir") or "").strip().strip('"').strip("'")
        if not dir_text:
            ui.notify(t("skills.dir_required"), type="warning")
            return
        if not skill_ids:
            ui.notify(t("skills.nothing_to_export"), type="warning")
            return
        try:
            result = await run_sync(
                export_skills, skill_ids, dir_text,
                prefix=prefix, version=version, overwrite=overwrite,
            )
        except OSError as exc:
            ui.notify(t("skills.export_failed", error=str(exc)), type="negative")
            return

        if result["missing"]:
            ui.notify(t("skills.missing", names=", ".join(result["missing"])), type="warning")
        if result["exported"]:
            ui.notify(
                t("skills.exported", count=len(result["exported"]), dir=dir_text),
                type="positive",
            )
        if result["exists"]:
            confirm_dialog(
                title=t("skills.overwrite_confirm_title"),
                message=t("skills.overwrite_confirm_msg", names=", ".join(result["exists"])),
                on_confirm=lambda: _run_export(skill_ids, True),
                confirm_text=t("skills.overwrite_btn"),
                danger=True,
            )

    async def _copy_prefix():
        await ui.run_javascript(
            f'navigator.clipboard.writeText({json.dumps(prefix)})'
        )
        ui.notify(t("skills.prefix_copied"), type="positive")

    with ui.column().classes("flex-1 h-full flex flex-col theme-content gap-0"):
        # 顶部标题栏
        title = skill["name"] if skill else t("skills.title")
        with ui.row().classes(
            "w-full px-5 items-center justify-between theme-sidebar"
        ).style("border-bottom: 1px solid var(--border-color); height: 49px"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("extension", size="xs").classes("theme-text-accent")
                ui.label(title).classes("text-sm font-medium theme-text")

        with ui.scroll_area().classes("flex-1"):
            with ui.column().classes("w-full p-5 gap-4"):
                # 导出区（未选中 Skill 时也可全部导出）
                with ui.card().tight().classes("w-full theme-card").style(
                    "border: 1px solid var(--border-color)"
                ):
                    with ui.column().classes("w-full gap-2 p-3"):
                        ui.label(t("skills.export_title")).classes("text-sm font-medium theme-text")
                        # 当前前缀只读展示，让用户核对将要注入的路径与端口
                        with ui.row().classes("w-full items-center gap-1"):
                            ui.input(
                                label=t("skills.cli_prefix"),
                                value=prefix,
                            ).props("readonly dense outlined").classes("flex-1")
                            ui.button(
                                icon="content_copy",
                                on_click=_copy_prefix,
                            ).props("flat dense").classes("theme-text-accent").props("title=" + t("skills.copy_prefix"))
                        ui.label(t("skills.cli_prefix_hint")).classes("text-xs theme-text-muted")
                        ui.input(
                            label=t("skills.export_dir"),
                            placeholder=t("skills.export_dir_placeholder"),
                            value=export_state.get("export_dir") or _default_export_dir(),
                            on_change=lambda e: export_state.update({"export_dir": e.value}),
                        ).props("dense outlined").classes("w-full")
                        ui.label(t("skills.export_dir_hint")).classes("text-xs theme-text-muted")
                        with ui.row().classes("w-full justify-end gap-2"):
                            if skill:
                                ui.button(
                                    t("skills.export_current"),
                                    icon="download",
                                    on_click=lambda: _run_export([skill["id"]]),
                                ).props("dense")
                            ui.button(
                                t("skills.export_all"),
                                icon="download_done",
                                on_click=lambda: _run_export([s["id"] for s in SKILLS]),
                            ).props("dense outline")

                # 详情 / 空态
                if skill is None:
                    with ui.column().classes("w-full h-64 items-center justify-center"):
                        ui.icon("extension", size="lg").classes("theme-text-muted")
                        ui.label(t("skills.select_skill")).classes("text-sm mt-2 theme-text-muted")
                else:
                    # 预览与导出一致：都显示渲染结果（前缀已注入、头部已生成）
                    content = render_skill(skill["id"], prefix, version=version) or ""
                    _, body = split_skill_meta(content)

                    ui.label(skill["description"]).classes("text-sm theme-text-muted")

                    with ui.card().tight().classes("w-full theme-card").style(
                        "border: 1px solid var(--border-color)"
                    ):
                        with ui.column().classes("w-full p-4"):
                            # 复用切片正文的渲染管线：chunk-content 紧凑排版
                            # （h1 1.2rem / 正文 0.875rem，主题变量适配暗色/粉色主题）
                            chunk_markdown(body)
