"""
Skill 导出视图

职责:
- 渲染 Skill 中栏（内置 Skill 清单）
- 渲染 Skill 右栏（详情预览 + 导出到用户指定目录）
- 显示并注入当前实例的 CLI 调用前缀
"""

from functools import partial
from pathlib import Path

from nicegui import context, ui

from app.i18n import t
from app.platform import get_default_data_dir
from app.skills import cli_prefix, export_skills, get_version, list_skills, render_skill, split_skill_meta
from app.ui.components import chunk_markdown, confirm_dialog, write_clipboard as _write_clipboard
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
    on_select_skill,
):
    """
    渲染 Skill 中栏（Skill 清单）

    Args:
        selected_skill: 选中的 Skill {"value": str | None}
        ui_refs: UI 组件引用字典
        on_select_skill: 选择 Skill 的回调
    """
    with ui.column().classes(
        "w-full h-full flex flex-col overflow-hidden theme-panel gap-0"
    ).style("border-right: 1px solid var(--border-color)"):
        # 顶部标题栏
        with ui.row().classes(
            "w-full px-3 items-center justify-between"
        ).style("border-bottom: 1px solid var(--border-color); height: 44px"):
            ui.label(t("skills.title")).classes("text-sm font-medium theme-text")

        # Skill 列表（每项为可键盘操作的按钮；选中仅用背景色区分，不加色块图标）
        with ui.scroll_area().classes("flex-1 scroll-flush"):
            @ui.refreshable
            def skill_list():
                with ui.column().classes("w-full gap-0.5 px-2 py-2"):
                    if not SKILLS:
                        with ui.column().classes("w-full items-center py-6"):
                            ui.icon("extension", size="lg").classes("theme-text-muted")
                            ui.label(t("skills.empty")).classes("text-xs mt-2 theme-text-muted")
                    for skill in SKILLS:
                        is_selected = selected_skill["value"] == skill["id"]
                        row_classes = "w-full px-3 py-2 rounded-md " + (
                            "theme-selected" if is_selected else "theme-hover"
                        )
                        with ui.button(on_click=partial(on_select_skill, skill["id"])).props(
                            "flat no-caps align=left no-ripple"
                        ).classes(row_classes):
                            with ui.column().classes("w-full gap-0"):
                                with ui.row().classes("w-full items-center gap-2"):
                                    ui.icon("extension", size="xs").classes("theme-text-muted")
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
        export_state: 导出状态；同一字典持久保存 export_dir/busy/上次结果/待覆盖项
        port: 当前服务端口，用于推导 CLI 调用前缀
    """
    client = context.client
    skill = _find_skill(selected_skill["value"])
    # 服务进程内的 get_default_data_dir 已反映 --data-dir / PIECE_DATA_DIR
    prefix = cli_prefix(get_default_data_dir(), port)
    version = get_version()
    export_state.setdefault("busy", False)
    export_state.setdefault("last_result", None)
    export_state.setdefault("pending_overwrite", None)
    # 显示与保存同一个值：默认目录确实存在时即为真实导出目标，避免“看到默认路径却报未填”
    if not export_state.get("export_dir"):
        export_state["export_dir"] = _default_export_dir()

    def _refresh_panel():
        # 用共享状态里的“当前 panel”刷新，跨视图回来（旧 closure 指向已删 target）也能更新新 panel。
        panel = export_state.get("_panel")
        if panel is not None and any(not target.container.is_deleted for target in panel.targets):
            panel.refresh()

    async def _run_export(skill_ids: list, overwrite: bool = False, frozen_dir: str | None = None):
        # 导出会重建触发按钮；把异步反馈留在仍有效的页面根上下文中。
        with client:
            await _export(skill_ids, overwrite, frozen_dir)

    async def _export(skill_ids: list, overwrite: bool = False, frozen_dir: str | None = None):
        """执行导出。overwrite 时只使用冻结目录与已确认的 Skill，不重读可编辑输入。"""
        if export_state.get("busy"):
            return
        if frozen_dir is not None:
            dir_text = frozen_dir
        else:
            dir_text = (export_state.get("export_dir") or "").strip().strip('"').strip("'")
        if not dir_text:
            ui.notify(t("skills.dir_required"), type="warning")
            return
        if not skill_ids:
            ui.notify(t("skills.nothing_to_export"), type="warning")
            return
        export_state["busy"] = True
        _refresh_panel()
        try:
            try:
                result = await run_sync(
                    export_skills, skill_ids, dir_text,
                    prefix=prefix, version=version, overwrite=overwrite,
                )
            except (OSError, ValueError) as exc:
                export_state["last_result"] = {"ok": False, "dir": dir_text, "error": str(exc)}
                export_state["pending_overwrite"] = None
                ui.notify(t("skills.export_failed", error=str(exc)), type="negative")
                return

            if result["missing"]:
                ui.notify(t("skills.missing", names=", ".join(result["missing"])), type="warning")
            # 结果写入共享状态，右栏刷新或切换视图后仍然可见
            export_state["last_result"] = {
                "ok": True,
                "dir": dir_text,
                "count": len(result["exported"]),
                "exported": list(result["exported"]),
                "exists": list(result["exists"]),
                "missing": list(result["missing"]),
            }
            # 覆盖确认不在此处弹出：await 后触发元素可能已被重建/删除，弹窗会挂到失效槽位。
            # 冻结本次目录与已存在清单，交由结果区的按钮在有效上下文中确认。
            export_state["pending_overwrite"] = (
                {"dir": dir_text, "exists": list(result["exists"])}
                if result["exists"] else None
            )
        finally:
            export_state["busy"] = False
            _refresh_panel()

    async def _copy_text(text: str, copied_key: str):
        with client:
            ok = await _write_clipboard(text)
            ui.notify(
                t(copied_key) if ok else t("skills.copy_failed"),
                type="positive" if ok else "negative",
            )

    def _confirm_overwrite():
        """在按钮点击（有效上下文）里确认覆盖，使用冻结的目录与已存在清单。"""
        pending = export_state.get("pending_overwrite")
        if not pending:
            return
        confirm_dialog(
            title=t("skills.overwrite_confirm_title"),
            message=t("skills.overwrite_confirm_message",
                      dir=pending["dir"], names=", ".join(pending["exists"])),
            on_confirm=lambda: _run_export(pending["exists"], overwrite=True,
                                           frozen_dir=pending["dir"]),
            confirm_text=t("skills.overwrite_btn"),
            danger=True,
        )

    @ui.refreshable
    def export_panel():
        busy = bool(export_state.get("busy"))
        dir_text = export_state.get("export_dir") or _default_export_dir()
        target = skill["name"] if skill else t("skills.all_target")

        with ui.column().classes("w-full gap-4"):
            # 主操作与目标一目了然
            ui.label(t("skills.export_target", target=target)).classes("text-sm theme-text")

            # 导出目录（目标）；服务端路径这一事实始终可见；运行中禁止修改
            with ui.column().classes("w-full gap-1"):
                dir_input = ui.input(
                    label=t("skills.export_dir"),
                    placeholder=t("skills.export_dir_placeholder"),
                    value=dir_text,
                    on_change=lambda e: export_state.update({"export_dir": e.value}),
                ).props("dense outlined").classes("w-full")
                dir_input.set_enabled(not busy)
                ui.label(t("skills.export_dir_hint")).classes("text-xs theme-text-muted")

            # 主操作：busy 时禁用并显示加载，防止重复提交
            with ui.row().classes("w-full justify-end gap-2"):
                buttons = []
                if skill:
                    buttons.append(ui.button(
                        t("skills.export_current"),
                        icon="download",
                        on_click=partial(_run_export, [skill["id"]]),
                    ).props("dense unelevated no-caps"))
                buttons.append(ui.button(
                    t("skills.export_all"),
                    icon="download_done",
                    on_click=lambda: _run_export([s["id"] for s in SKILLS]),
                ).props("dense outline no-caps"))
                for button in buttons:
                    if busy:
                        button.props("loading disable")

            # 上次导出结果持久可见：目标目录 + 数量 + 复制路径 + 覆盖入口
            result = export_state.get("last_result")
            if result:
                with ui.column().classes("skill-export-result w-full gap-1 pt-3").style(
                    "border-top: 1px solid var(--border-color)"
                ):
                    ui.label(t("skills.result_heading")).classes(
                        "text-xs font-semibold theme-text-muted"
                    )
                    if result.get("ok"):
                        with ui.row().classes("w-full items-center gap-1 flex-nowrap"):
                            ui.label(
                                t("skills.exported", count=result["count"], dir=result["dir"])
                            ).classes("text-sm theme-text min-w-0")
                            ui.button(
                                icon="content_copy",
                                on_click=lambda: _copy_text(result["dir"], "skills.path_copied"),
                            ).props(
                                f'flat dense size=sm aria-label="{t("skills.copy_path")}"'
                            ).classes("theme-text-muted shrink-0").tooltip(t("skills.copy_path"))
                        if result.get("exists"):
                            ui.label(t("skills.result_exists", count=len(result["exists"]))).classes(
                                "text-xs theme-text-muted"
                            )
                        if result.get("missing"):
                            ui.label(t("skills.missing", names=", ".join(result["missing"]))).classes(
                                "text-xs theme-text-muted"
                            )
                    else:
                        ui.label(t("skills.export_failed", error=result.get("error", ""))).classes(
                            "text-sm theme-danger"
                        )
                    if export_state.get("pending_overwrite") and not busy:
                        ui.button(
                            t("skills.overwrite_action"),
                            icon="file_upload",
                            on_click=_confirm_overwrite,
                        ).props("dense flat color=red").classes("self-start")

            # CLI 前缀是低频命令信息，收进高级展开区（去卡片）
            with ui.expansion(t("skills.prefix_expand")).classes("w-full").props("dense"):
                with ui.column().classes("w-full gap-1 pt-1"):
                    with ui.row().classes("w-full items-center gap-1 flex-nowrap"):
                        ui.input(label=t("skills.cli_prefix"), value=prefix).props(
                            "readonly dense outlined"
                        ).classes("flex-1 min-w-0")
                        ui.button(
                            icon="content_copy",
                            on_click=lambda: _copy_text(prefix, "skills.prefix_copied"),
                        ).props(
                            f'flat dense size=sm aria-label="{t("skills.copy_prefix")}"'
                        ).classes("theme-text-accent").tooltip(t("skills.copy_prefix"))
                    ui.label(t("skills.cli_prefix_hint")).classes("text-xs theme-text-muted")

            # 下一步说明
            ui.label(t("skills.export_note")).classes("text-xs theme-text-muted")

    # 记录当前 panel：任何一次渲染都会更新，供在途导出在完成时刷新“最新”面板
    export_state["_panel"] = export_panel

    with ui.column().classes("flex-1 h-full flex flex-col theme-content gap-0"):
        # 顶部标题栏
        title = skill["name"] if skill else t("skills.title")
        with ui.row().classes(
            "w-full px-5 items-center justify-between theme-sidebar"
        ).style("border-bottom: 1px solid var(--border-color); height: 44px"):
            with ui.row().classes("items-center gap-2 min-w-0"):
                ui.icon("extension", size="xs").classes("theme-text-accent")
                ui.label(title).classes("text-sm font-medium theme-text truncate")

        with ui.scroll_area().classes("flex-1"):
            with ui.column().classes("w-full p-5 gap-5"):
                # 导出区（未选中 Skill 时也可全部导出）
                export_panel()

                # 详情 / 空态
                if skill is None:
                    with ui.column().classes("w-full h-64 items-center justify-center"):
                        ui.icon("extension", size="lg").classes("theme-text-muted")
                        ui.label(t("skills.select_skill")).classes("text-sm mt-2 theme-text-muted")
                else:
                    # 预览与导出一致：都显示渲染结果（前缀已注入、头部已生成）
                    content = render_skill(skill["id"], prefix, version=version) or ""
                    _, body = split_skill_meta(content)

                    with ui.column().classes("w-full gap-2 pt-3").style(
                        "border-top: 1px solid var(--border-color)"
                    ):
                        ui.label(t("skills.preview_heading")).classes(
                            "text-xs font-semibold theme-text-muted"
                        )
                        ui.label(skill["description"]).classes("text-sm theme-text-muted")
                        # 复用切片正文的渲染管线：chunk-content 紧凑排版
                        # （h1 1.2rem / 正文 0.875rem，主题变量适配暗色/粉色主题）
                        chunk_markdown(body)
