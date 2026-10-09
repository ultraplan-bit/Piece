"""
文件库视图

职责:
- 渲染文件库中栏（宽栏资料表 / 窄栏分层列表）
- 渲染文件库右栏（默认连续阅读，可切到卡片管理；文件检查信息按需展开）
"""

import inspect
from functools import wraps
from types import SimpleNamespace

from nicegui import ui

from app.i18n import t
from app.ui.components import (
    status_badge,
    chunk_card,
    chunk_markdown,
    collection_labels,
    collection_path_label,
    help_hint,
    workspace_controls,
    CATALOG_KEY_HANDLER,
)
from app.ui.resource_tree import branch_key, document_key, is_tree_view, visible_tree_rows
from indexing.services import metadata_service
from indexing.services.chunking.utils import HEADING_SEPARATOR
from indexing.services.page_render import (
    PAGE_VIEWABLE_FORMATS,
    page_number_from_heading,
)

# 阅读线位于视口上方 1/3；上下缓冲带防止边界抖动，候选页稳定 250ms 才上报。
_SOURCE_SCROLL_HANDLER = """(info) => {
    const area = document.querySelector('.chunk-scroll');
    const pane = document.querySelector('.source-pane');
    if (!area || !pane) return;
    const fileId = Number(pane.dataset.fileId);
    const cards = Array.from(area.querySelectorAll('.chunk-anchor[data-page]'))
        .filter(card => Number(card.dataset.fileId) === fileId && Number(card.dataset.page) > 0);
    if (!cards.length) return;
    const revision = pane.dataset.followRevision;
    let follow = area._sourceFollow;
    if (!follow || follow.pane !== pane || follow.first !== cards[0] || follow.revision !== revision) {
        clearTimeout(follow?.timer);
        follow = {pane, first: cards[0], revision, page: Number(pane.dataset.page), pending: null};
        area._sourceFollow = follow;
    }
    const pick = () => {
        const bounds = area.getBoundingClientRect();
        const line = bounds.top + bounds.height / 3;
        const margin = Math.min(80, bounds.height * 0.12);
        const positions = cards.map(card => ({card, rect: card.getBoundingClientRect()}));
        if (positions.some(({card, rect}) => Number(card.dataset.page) === follow.page
                && rect.bottom >= line - margin && rect.top <= line + margin)) return null;
        return (positions.find(({rect}) => rect.bottom > line) || positions[positions.length - 1]).card;
    };
    const card = pick();
    const page = card ? Number(card.dataset.page) : follow.page;
    if (page === follow.page) {
        clearTimeout(follow.timer);
        follow.pending = null;
        return;
    }
    if (follow.pending === page) return;
    clearTimeout(follow.timer);
    follow.pending = page;
    follow.timer = setTimeout(() => {
        follow.pending = null;
        if (!area.isConnected || !pane.isConnected || !card.isConnected
                || area._sourceFollow !== follow || pane.dataset.followRevision !== revision) return;
        const target = pick();
        if (!target || Number(target.dataset.page) !== page) return;
        follow.page = page;
        emit({file_id: fileId, chunk_id: Number(target.dataset.chunkId)});
    }, 250);
}"""

# 排序菜单项：state["sort_key"] -> 文案
_SORT_LABELS = {
    "created_at": "files.sort_created",
    "updated_at": "files.sort_updated",
    "filename": "files.sort_name",
}

# 阅读区两种形态：默认连续阅读（像文档），可切到卡片管理（逐条带管理栏）
READING_MODES = ("reading", "cards")

# 整行只响应"空白处"的单击/双击/回车：落在按钮、输入框等交互元素上的事件不冒泡上来。
_ROW_INTERACTIVE = "button, a, input, textarea, select, .q-checkbox, [role=menuitem]"
_ROW_DBLCLICK_HANDLER = "(e) => { if (!e.target.closest('%s')) emit(); }" % _ROW_INTERACTIVE
_ROW_ENTER_HANDLER = CATALOG_KEY_HANDLER


def _batch_count_text(selected_ids: set, page_ids: set) -> str:
    """批量计数文案：勾选跨页时点明范围，避免被当成只作用于当前页。"""
    key = "workspace.selected" if selected_ids <= page_ids else "workspace.selected_cross_page"
    return t(key, count=len(selected_ids))


def _file_icon(file: dict) -> str:
    extension = file["filename"].rsplit(".", 1)[-1].lower()
    return {"pdf": "picture_as_pdf", "md": "article", "docx": "description",
            "pptx": "slideshow", "xlsx": "table_chart", "epub": "menu_book"}.get(extension, "insert_drive_file")


def _file_type_label(file: dict) -> str:
    """资料表的类型列：原件类型优先，其次取文件名后缀，统一大写。"""
    extension = (file.get("original_file_type") or file["filename"].rsplit(".", 1)[-1] or "").lower()
    return extension.upper()


def _file_date(file: dict) -> str:
    """日期列只取日期部分；完整时间放在 tooltip 里。"""
    return (file.get("created_at") or "")[:10]


def _file_summary(file: dict) -> str:
    """作者与年份摘要，作为标题下的次级信息。"""
    properties = metadata_service.decode_metadata(file.get("metadata"))
    author = properties.get("author") or properties.get("authors")
    if isinstance(author, list):
        author = ", ".join(str(value) for value in author)
    return " · ".join(str(value) for value in (author, properties.get("year")) if value)


def _parent_path(heading_path: str) -> str:
    """取切片的上级标题路径（去掉末段，末段即切片自身的标题）。"""
    if not heading_path:
        return ""
    segments = [part for part in heading_path.split(HEADING_SEPARATOR) if part]
    return HEADING_SEPARATOR.join(segments[:-1])


# 后端 tasks.stage 到展示文案的映射
_STAGE_LABELS = {
    "queued": "task.stage_queued",
    "parsing": "task.stage_parsing",
    "embedding": "task.stage_embedding",
}


def _progress_text(task_progress: dict) -> str:
    """拼出"阶段 x/y · nn%"形式的进度文案。

    解析阶段的 x/y 是页数，向量阶段是已入库页数（非 PDF 为切片数），
    具体含义由后端写入 current_page/total_pages 时决定。
    """
    if task_progress.get("stage") == "cancelling":
        return t("task.cancelling")
    percent = task_progress.get("progress", 0)
    stage = task_progress.get("stage") or "queued"
    if task_progress.get("status") == "pending":
        stage = "queued"

    parts = [t(_STAGE_LABELS.get(stage, "task.stage_queued"))]
    current = task_progress.get("current_page", 0)
    total = task_progress.get("total_pages", 0)
    if total:
        parts.append(f"{current}/{total}")
    parts.append(f"{percent}%")
    return " · ".join(parts)


def render_files_middle(
    state: dict,
    ui_refs: dict,
    file_handlers,
):
    """
    渲染文件库中栏：宽栏按标题 / 类型 / 状态 / 日期对齐，窄栏分层展示。

    单击预览，双击或回车打开阅读；目录始终保留，便于连续浏览。

    Args:
        state: 共享状态字典
        ui_refs: UI 组件引用字典
        file_handlers: 文件处理器实例
    """
    with ui.column().classes(
        "w-full h-full flex flex-col overflow-hidden theme-panel gap-0 library-catalog"
    ).style("border-right: 1px solid var(--border-color)").props("data-catalog=files") as catalog:
        ui_refs["file_catalog"] = catalog

        def in_catalog(callback):
            @wraps(callback)
            async def run(*args, **kwargs):
                # 异步操作可以重建发起事件的树行，不能继续依赖那一行的 slot。
                with catalog.client:
                    result = callback(*args, **kwargs)
                    return await result if inspect.isawaitable(result) else result
            return run

        catalog.on("catalog-cancel", file_handlers.exit_batch_mode, args=[])
        catalog.on("catalog-select-all", file_handlers.select_all_visible, args=[])
        def export_items(file_ids=None, *, has_original=True):
            ui.menu_item(t("files.export_working"), on_click=in_catalog(
                lambda: file_handlers.handle_export_files("markdown", file_ids))).props("role=menuitem")
            original = ui.menu_item(t("files.export_original"), on_click=in_catalog(
                lambda: file_handlers.handle_export_files("original", file_ids))).props("role=menuitem")
            original.set_enabled(has_original)
            return original

        def export_button():
            with ui.button(icon="file_download", color=None).props(
                f'flat dense round size=sm aria-label="{t("files.export")}"'
            ).classes("theme-text-secondary").tooltip(t("files.export")) as button:
                with ui.menu().props("auto-close"):
                    ui_refs["file_export_original"] = export_items()
                    ui.separator()
                    ui.label(t("files.export_hint")).classes("export-menu-hint text-xs theme-text-muted")
            ui_refs["file_export_button"] = button

        # 文档操作留在目录栏；批量模式使用同一导出入口。
        with ui.row().classes("w-full items-center library-heading workspace-toolbar catalog-heading"):
            heading = ui.label(t("files.title")).classes("text-sm font-medium theme-text flex-1 min-w-0 truncate")
            ui.tooltip().bind_text_from(heading, "text")

            def selection_status():
                if heading.is_deleted:
                    return
                if state.get("batch_mode"):
                    page_ids = {f["id"] for f in state.get("filtered_files", [])}
                    heading.set_text(_batch_count_text(state.get("batch_selected_ids", set()), page_ids))
                else:
                    heading.set_text(t("files.title"))
                checkbox = ui_refs.get("file_select_all")
                if checkbox is not None and not checkbox.is_deleted:
                    checkbox.set_value(file_handlers.is_all_selected())
                    label = t("files.select_visible" if is_tree_view(state) else "files.batch_select_all")
                    checkbox.props(f'aria-label="{label}"')
                    ui_refs["file_select_all_tooltip"].set_text(label)
                button = ui_refs.get("file_export_button")
                if button is not None and not button.is_deleted:
                    button.set_enabled(bool(file_handlers.get_export_file_ids()) and not state.get("file_exporting"))
                    button.props("loading" if state.get("file_exporting") else "", remove="loading" if not state.get("file_exporting") else None)
                original = ui_refs.get("file_export_original")
                if original is not None and not original.is_deleted:
                    selected = next((f for f in state.get("files_data", []) if f["id"] == state.get("selected_file_id")), {})
                    original.set_enabled(state.get("batch_mode") or bool(selected.get("original_file_path")))

            ui_refs["selection_status"] = selection_status

            @ui.refreshable
            def toolbar_buttons():
                ui_refs["file_select_all"] = None
                with ui.row().classes("catalog-actions items-center gap-0 flex-nowrap shrink-0"):
                    if state.get("batch_mode"):
                        checkbox = ui.checkbox(value=file_handlers.is_all_selected()).props("dense size=xs").on(
                            "update:model-value", lambda e: file_handlers.toggle_select_all(bool(e.args)), [None],
                        )
                        ui_refs["file_select_all"] = checkbox
                        with checkbox:
                            ui_refs["file_select_all_tooltip"] = ui.tooltip()
                        export_button()
                        ui.button(icon="playlist_add", color=None, on_click=file_handlers.handle_batch_collections).props(
                            f'flat dense round size=sm aria-label="{t("collections.batch_assign")}"'
                        ).classes("theme-text-secondary").tooltip(t("collections.batch_assign"))
                        with ui.button(icon="more_horiz", color=None).props(
                            f'flat dense round size=sm aria-label="{t("workspace.more")}"'
                        ).classes("theme-text-secondary"):
                            with ui.menu().props("auto-close"):
                                ui.menu_item(t("collections.replace_batch"), on_click=file_handlers.handle_replace_batch_collections)
                                ui.separator()
                                ui.menu_item(t("files.batch_confirm"), on_click=file_handlers.confirm_batch_delete).classes("theme-danger")
                        ui.button(icon="close", color=None, on_click=file_handlers.exit_batch_mode).props(
                            f'flat dense round size=sm aria-label="{t("files.batch_cancel")}"'
                        ).classes("theme-text-muted").tooltip(t("files.batch_cancel"))
                    else:
                        ui.button(icon="refresh", color=None, on_click=file_handlers.refresh_and_scan).props(
                            f'flat dense round size=sm aria-label="{t("files.refresh")}"'
                        ).classes("theme-text-muted").tooltip(t("files.refresh"))
                        with ui.button(icon="add", color=None).props(
                            f'flat dense round size=sm aria-label="{t("files.create_menu")}"'
                        ).classes("theme-text-secondary").tooltip(t("files.create_menu")):
                            with ui.menu().props("auto-close"):
                                ui.menu_item(t("collections.create"), on_click=lambda: file_handlers.begin_collection_edit(
                                    parent_id=next(iter(state.get("active_collection_ids", [])), None)))
                                ui.separator()
                                ui.menu_item(t("cards.create"), on_click=file_handlers.handle_create_card)
                                ui.menu_item(t("files.create"), on_click=file_handlers.handle_create_file)
                                ui.separator()
                                ui.menu_item(t("files.upload"), on_click=file_handlers.open_file_picker)
                                ui.menu_item(t("import.folder_menu"), on_click=file_handlers.handle_import_folder)
                                ui.menu_item(t("import.zotero_menu"), on_click=file_handlers.handle_import_zotero)
                        export_button()
                        ui.button(icon="checklist", color=None, on_click=file_handlers.enter_batch_mode).props(
                            f'flat dense round size=sm aria-label="{t("workspace.select")}"'
                        ).classes("theme-text-muted").tooltip(t("workspace.select"))
                selection_status()

            ui_refs["toolbar_buttons"] = toolbar_buttons
            toolbar_buttons()

        # 上传阶段提示条：浏览器→服务端的传输期间文件还没有数据库记录，
        # 无法出现在下方列表里，这里单独给一条反馈避免看起来没反应
        @ui.refreshable
        def upload_banner():
            count = state.get("uploading_count", 0)
            if not count:
                return
            with ui.row().classes("w-full items-center gap-2 px-3 py-1.5"):
                ui.spinner(size="xs").classes("theme-text-accent")
                with ui.column().classes("flex-1 min-w-0 gap-0"):
                    ui.label(t("files.uploading", count=count)).classes("text-xs theme-text-muted")
                    names = " · ".join(state.get("uploading_files", []))
                    ui.label(names).classes("text-xs theme-text truncate w-full").tooltip(names)
                ui.button(icon="close", on_click=file_handlers.cancel_upload).props(
                    f'flat dense round size=sm aria-label="{t("files.upload_cancel")}"'
                ).tooltip(t("files.upload_cancel"))

        ui_refs["upload_banner"] = upload_banner
        upload_banner()

        # 搜索 + 排序（debounce：避免每次按键都重建整个文件列表）
        @ui.refreshable
        def list_toolbar():
            with ui.row().classes("w-full items-center gap-1 px-2 py-2 flex-nowrap workspace-search"):
                search = ui.input(value=state.get("search_keyword", ""), placeholder=t("files.search")).props(
                    f'dense outlined debounce=300 clearable aria-label="{t("files.search")}"'
                ).classes("flex-1 min-w-0 text-sm").on(
                    "update:model-value", file_handlers.on_search_change
                )
                search.add_slot("prepend", '<q-icon name="search" size="18px" />')
                search.tooltip(t("files.search_hint"))
                search.set_enabled(state.get("workspace_ready", True) and not bool(state.get("collection_edit")))
                search.on("keydown", file_handlers.clear_search, js_handler="""event => {
                    if (event.key === 'Escape' && !event.isComposing) {
                        event.preventDefault(); event.stopPropagation(); emit();
                    }
                }""")
                ui_refs["file_search"] = search
                with ui.button(icon="sort", color=None).props(f'flat dense round size=sm aria-label="{t("files.sort")}"').classes(
                    "shrink-0 theme-text-muted"
                ).tooltip(t("files.sort")):
                    with ui.menu().props("auto-close"):
                        current = state.get("sort_key", "created_at")
                        for key, label_key in _SORT_LABELS.items():
                            ui.menu_item(
                                t(label_key),
                                on_click=lambda k=key: file_handlers.on_sort_change(k),
                            ).props("dense").classes(
                                "theme-text-accent" if key == current else ""
                            )

        ui_refs["list_toolbar"] = list_toolbar
        list_toolbar()

        mode_buttons = {}
        with ui.column().classes("w-full gap-0 library-scope"):
            with ui.row().classes("w-full px-2 pb-1 gap-1 items-center flex-nowrap"):
                with ui.row().classes("library-view-switch flex-1 min-w-0 gap-0 flex-nowrap").props(
                    f'role=group aria-label="{t("files.view")}"'):
                    for value, label, hint in (("tree", "files.tree_view", "files.tree_view_hint"),
                                               ("all", "files.all_files", "files.all_files_hint"),
                                               ("uncategorized", "collections.none", "files.uncategorized_hint")):
                        mode_buttons[value] = ui.button(t(label), color=None, on_click=in_catalog(
                            lambda mode=value: file_handlers.set_library_mode(mode))).props(
                                "flat dense no-caps no-ripple").classes("library-view-button").tooltip(t(hint))
                collapse = ui.button(icon="unfold_less", color=None,
                    on_click=in_catalog(file_handlers.collapse_all_collections)).props(
                        f'flat dense round size=sm aria-label="{t("files.collapse_all")}"'
                    ).classes("theme-text-muted").tooltip(t("files.collapse_all"))
                help_hint(t("collections.manage_hint"), label=t("collections.tree_title"))
            with ui.row().classes("library-location w-full items-center flex-nowrap gap-0") as location:
                ui.button(t("files.drop_library"), color=None,
                    on_click=in_catalog(lambda: file_handlers.on_collection_change([]))).props(
                        f'flat dense no-caps size=sm aria-label="{t("collections.root")}"'
                    ).classes("theme-text-muted shrink-0").tooltip(t("collections.root"))
                ui.icon("chevron_right", size="14px").classes("theme-text-muted shrink-0").props("aria-hidden=true")
                scope_label = ui.label("").classes("text-xs theme-text-muted truncate flex-1 min-w-0")
                with scope_label:
                    ui.tooltip().bind_text_from(scope_label, "text")
        ui_refs["library_mode_buttons"] = mode_buttons

        def collection_filter():
            if catalog.is_deleted:
                return
            selected = next((item for item in state.get("collections", [])
                             if item["id"] in state.get("active_collection_ids", [])), None)
            location.set_visibility(selected is not None)
            scope_label.set_text(collection_path_label(selected) if selected else "")
            target = selected if not is_tree_view(state) else None
            catalog._props["data-import-label"] = collection_path_label(target) if target else t("files.drop_library")
            catalog.props(f'data-import-collection={target["id"] if target else "all"}')
            enabled = state.get("workspace_ready", True) and not bool(state.get("collection_edit"))
            for value, button in mode_buttons.items():
                button.props(f'aria-pressed={str(value == state.get("library_mode", "tree")).lower()}')
                button.set_enabled(enabled)
            collapse.set_enabled(enabled and is_tree_view(state) and bool(state.get("expanded_collection_ids")))

        ui_refs["collection_filter"] = SimpleNamespace(refresh=collection_filter)
        collection_filter()

        def _table_header(batch_mode):
            """资料表表头：与数据行共用同一套列宽。"""
            keys = (None, "library.table_title", "library.table_type", "library.table_status", "library.table_date") if batch_mode else (
                "library.table_title", "library.table_type", "library.table_status", "library.table_date", None)
            with ui.element("div").classes("library-table-header" + (" library-header-batch" if batch_mode else "")):
                for key in keys:
                    column_class = " library-col-type" if key == "library.table_type" else " library-col-date" if key == "library.table_date" else ""
                    ui.label(t(key) if key else "").classes("library-th text-xs theme-text-muted" + column_class)

        def _render_file_row(f: dict, batch_mode: bool, batch_selected_ids: set, *, scope=None, depth=0, row_key=None):
            file_id = f["id"]
            key = row_key or f"f:flat:{file_id}"
            tree_view = is_tree_view(state)
            is_selected = file_id == state["selected_file_id"]
            is_batch_selected = file_id in batch_selected_ids

            # 通过索引直接获取进行中的任务
            latest_task = state.get("latest_file_tasks", {}).get(file_id)
            task_progress = state.get("task_progress_by_file_id", {}).get(file_id)
            if latest_task and latest_task["status"] in ("pending", "processing") and (
                not task_progress or latest_task.get("stage") == "cancelling"
            ):
                # 受理/取消后立即可操作，不必等下一轮订阅。
                task_progress = {**latest_task, "task_id": latest_task["id"]}

            container_classes = "w-full library-row "
            if batch_mode and is_batch_selected:
                container_classes += "theme-selected"
            elif is_selected and not batch_mode:
                container_classes += "theme-selected"
            else:
                container_classes += "theme-hover"

            # 单击只预览；打开阅读不收起目录，行内控件保留自己的键盘行为。
            def click_handler(event, fid=file_id):
                modifiers = event.args or {}
                return file_handlers.select_file(fid,
                    additive=bool(modifiers.get("ctrlKey") or modifiers.get("metaKey")),
                    extend=bool(modifiers.get("shiftKey")), row_key=key, collection_id=scope)

            row = ui.element("div").classes(container_classes + (" resource-file-row" if tree_view else "")).props(
                f'tabindex=-1 role={"treeitem" if tree_view else "button"} '
                f'data-catalog-row={key} data-document-id={file_id} draggable=true '
                f'data-open-on-enter={str(not batch_mode).lower()} '
                f'data-import-collection={scope if scope is not None else "all"}'
            ).on("click", in_catalog(click_handler), ["ctrlKey", "metaKey", "shiftKey"])
            row._props["data-parent-row"] = f"c:{scope}" if tree_view and scope is not None else ""
            collection = next((item for item in state.get("collections", []) if item["id"] == scope), None)
            row._props["data-import-label"] = collection_path_label(collection) if collection else t("files.drop_library")
            if tree_view:
                row.props(f'aria-level={depth + 1}').style(f'--tree-depth: {min(depth, 8)}')
            row._props["aria-label"] = f'{f["filename"]} — {t("workspace.select") if batch_mode else t("library.row_hint")}'
            ui_refs["file_rows"][key] = row
            ui_refs["catalog_rows"][key] = row
            state["visible_file_rows"].append((key, file_id))
            row.on("keydown", js_handler=_ROW_ENTER_HANDLER)
            if not batch_mode:
                row.on("dblclick", in_catalog(lambda _, fid=file_id: file_handlers.open_reader(fid)),
                        js_handler=_ROW_DBLCLICK_HANDLER)
            with row:
                # 文件导出和管理都从目录操作，不必先打开阅读。
                if not batch_mode:
                    with ui.context_menu():
                        export_items([file_id], has_original=bool(f.get("original_file_path")))
                        ui.separator()
                        ui.menu_item(t("collections.add_title"), in_catalog(lambda fid=file_id: file_handlers.handle_add_file_collections(fid))).props("role=menuitem")
                        if scope is not None:
                            ui.menu_item(t("collections.remove_here"), in_catalog(
                                lambda fid=file_id, cid=scope: file_handlers.remove_from_collection(fid, cid))).props("role=menuitem")
                        ui.menu_item(t("files.locate"), in_catalog(
                            lambda fid=file_id: file_handlers.handle_locate_file(fid))).props("role=menuitem")
                        ui.menu_item(t("collections.assign_title"), in_catalog(
                            lambda fid=file_id: file_handlers.handle_edit_file_collections(fid))).props("role=menuitem")
                        ui.menu_item(t("properties.dialog_title"), in_catalog(
                            lambda fid=file_id: file_handlers.handle_edit_properties(fid))).props("role=menuitem")
                        ui.separator()
                        ui.menu_item(t("files.delete_confirm_title"), in_catalog(
                            lambda fid=file_id: file_handlers.confirm_delete_file(fid))).props("role=menuitem").classes("theme-danger")

                with ui.element("div").classes("library-grid" + (" library-grid-batch" if batch_mode else "")):
                    if batch_mode:
                        ui_refs["file_checks"][key] = ui.checkbox(
                            value=is_batch_selected,
                        ).props("dense size=xs disable").classes("library-row-check").style("pointer-events: none")

                    # 图标不收缩，文本列从标题到集合名都受同一宽度约束。
                    with ui.row().classes("library-title-cell w-full min-w-0 items-start gap-1.5 flex-nowrap"):
                        ui.icon(_file_icon(f), size="18px").classes(
                            "shrink-0 mt-0.5 theme-text-muted"
                        ).props("aria-hidden=true")
                        with ui.column().classes("gap-0 min-w-0 flex-1"):
                            ui.label(f["filename"]).classes("library-file-name text-sm theme-text w-full").tooltip(
                                " · ".join(filter(None, [
                                    f["filename"],
                                    t("files.imported_at", date=f["created_at"][:16]) if f.get("created_at") else "",
                                ]))
                            )
                            summary = _file_summary(f)
                            if summary:
                                ui.label(summary).classes("text-xs theme-text-secondary truncate w-full").tooltip(summary)
                            # 所属集合只显示直接归类。
                            if not tree_view:
                                collection_labels(state.get("collections_by_file", {}).get(file_id, []))

                    with ui.element("div").classes("library-row-meta"):
                        ui.label(_file_type_label(f)).classes("text-xs theme-text-muted truncate library-col-type").tooltip(
                            _file_type_label(f)
                        )
                        with ui.element("div").classes("library-col-status justify-self-start"):
                            status_badge(f["status"])
                        ui.label(_file_date(f)).classes("text-xs theme-text-muted whitespace-nowrap library-col-date").tooltip(
                            t("files.imported_at", date=f["created_at"][:16]) if f.get("created_at") else ""
                        )
                    if not batch_mode:
                        # 行尾操作随悬停/焦点出现；Enter 始终可直接打开阅读。
                        ui.button(icon="menu_book", color=None).props(
                            f'flat dense round size=sm tabindex=-1 aria-label="{t("library.open_reader")}"'
                        ).classes("theme-text-muted justify-self-end library-row-action").tooltip(
                            t("library.open_reader")
                        ).on("click.stop", in_catalog(lambda _, fid=file_id: file_handlers.open_reader(fid)))

                # 任务反馈放整行：进度条需要横向空间，不塞进标题列
                if not batch_mode and task_progress and task_progress["status"] in ("pending", "processing"):
                    with ui.row().classes("w-full items-center gap-2 mt-1 library-row-progress"):
                        ui.linear_progress(
                            value=task_progress["progress"] / 100,
                            show_value=False,
                        ).props("size=2px color=primary").classes("flex-1")
                        ui.label(_progress_text(task_progress)).classes(
                            "text-xs theme-text-muted whitespace-nowrap"
                        )
                        ui.button(icon="stop").props(
                            f'flat dense round size=xs aria-label="{t("task.cancel")}"'
                        ).on("click.stop", lambda _, tid=task_progress["task_id"]:
                             file_handlers.handle_cancel_task(tid)).tooltip(
                            t("task.cancel")
                        ).set_enabled(task_progress.get("stage") != "cancelling")
                elif not batch_mode and latest_task and latest_task["status"] in ("failed", "cancelled"):
                    message = t("task.cancelled") if latest_task["status"] == "cancelled" else (
                        latest_task.get("error_message") or t("task.failed"))
                    with ui.row().classes("w-full items-center gap-1 flex-nowrap mt-1"):
                        ui.label(message).classes("text-xs text-red-400 truncate flex-1").tooltip(message)
                        ui.button(icon="refresh").props(
                            f'flat dense round size=xs aria-label="{t("task.retry")}"'
                        ).on("click.stop", lambda _, tid=latest_task["id"]:
                             file_handlers.handle_retry_task(tid)).tooltip(t("task.retry"))

        def _render_collection_editor(depth):
            edit = state["collection_edit"]
            with ui.column().classes("collection-inline-editor w-full gap-1").style(f'--tree-depth: {min(depth, 8)}').props("data-collection-editor"):
                with ui.row().classes("w-full items-center gap-1 flex-nowrap"):
                    ui.icon("folder", size="18px").classes("theme-text-muted")
                    name = ui.input(value=edit["name"], on_change=lambda e: edit.update(name=e.value or "")).props(
                        f'dense outlined autofocus maxlength=200 aria-label="{t("collections.name")}"'
                    ).classes("flex-1 min-w-0")
                    name.on("keydown.enter", lambda e: file_handlers.commit_collection_edit(e.args, expected=edit),
                        js_handler="event => { if (!event.isComposing && event.keyCode !== 229) { event.preventDefault(); emit(event.target.value); } }")
                    name.on("keydown.escape", file_handlers.cancel_collection_edit,
                        js_handler="event => { if (!event.isComposing) { event.preventDefault(); event.stopPropagation(); emit(); } }")
                    save = ui.button(icon="check", on_click=lambda: file_handlers.commit_collection_edit(expected=edit)).props(
                        f'flat dense round size=sm aria-label="{t("chunk_dialog.btn_save")}"')
                    cancel = ui.button(icon="close", on_click=file_handlers.cancel_collection_edit).props(
                        f'flat dense round size=sm aria-label="{t("chunk_dialog.btn_cancel")}"')
                    ui_refs["collection_edit_controls"] = [name, save, cancel]
                ui_refs["collection_edit_error"] = ui.label(edit["error"]).classes("text-xs theme-danger").props("role=alert")

        def _collection_actions(item):
            cid = item["id"]
            ui.menu_item(t("collections.create_child"), in_catalog(lambda: file_handlers.begin_collection_edit(parent_id=cid))).props("role=menuitem")
            ui.menu_item(t("collections.rename"), in_catalog(lambda: file_handlers.begin_collection_edit(cid))).props("role=menuitem")
            ui.menu_item(t("collections.move"), in_catalog(lambda: file_handlers.handle_move_collection(cid))).props("role=menuitem")
            ui.separator()
            delete = ui.menu_item(t("collections.delete"), in_catalog(lambda: file_handlers.confirm_delete_collection(cid))).props("role=menuitem")
            if item.get("child_count"):
                delete.props("disable").tooltip(t("collections.delete_children_first"))

        def _render_collection_row(entry):
            item = entry["node"]["collection"]
            cid, key, depth = item["id"], entry["key"], entry["depth"]
            expanded = str(cid) in state.get("expanded_collection_ids", [])
            with ui.element("div").classes("resource-collection-row theme-hover w-full").props(
                f'role=treeitem tabindex=-1 aria-level={depth + 1} aria-expanded={str(expanded).lower()} '
                f'data-catalog-row={key} data-collection-id={cid} data-import-collection={cid}'
            ).style(f'--tree-depth: {min(depth, 8)}') as row:
                row._props["aria-label"] = item["name"]
                row._props["data-loaded"] = str(branch_key(cid) in state.get("tree_pages", {})).lower()
                row._props["data-import-label"] = collection_path_label(item)
                row._props["data-parent-row"] = f'c:{entry["scope"]}' if entry["scope"] is not None else ""
                ui_refs["catalog_rows"][key] = row
                row.on("click", in_catalog(lambda: file_handlers.select_collection(cid)), args=[])
                row.on("dblclick", in_catalog(lambda: file_handlers.toggle_collection(cid)), js_handler=_ROW_DBLCLICK_HANDLER)
                row.on("keydown", js_handler=CATALOG_KEY_HANDLER)
                row.on("rename-collection", in_catalog(lambda: file_handlers.begin_collection_edit(cid)), args=[])
                edit = state.get("collection_edit")
                if edit and edit["id"] == cid:
                    _render_collection_editor(0)
                    return
                with ui.context_menu():
                    _collection_actions(item)
                toggle = ui.button(icon="expand_more" if expanded else "chevron_right", color=None).props("flat dense round size=xs").classes("tree-expander theme-text-muted")
                ui_refs["collection_expanders"][cid] = toggle
                toggle._props["aria-label"] = t("collections.collapse" if expanded else "collections.expand", name=item["name"])
                toggle.on("click.stop", in_catalog(lambda: file_handlers.toggle_collection(cid)))
                ui.icon("folder_open" if expanded else "folder", size="18px").classes("collection-tree-icon theme-text-muted").props("aria-hidden=true")
                ui.label(item["name"]).classes("collection-tree-name text-sm theme-text").tooltip(collection_path_label(item))
                ui.label(str(item["file_count"])).classes("collection-tree-count").tooltip(t("collections.counts",
                    direct=item.get("direct_file_count", 0), subtree=item["file_count"]))
                with ui.button(icon="more_horiz", color=None).props(
                    f'flat dense round size=xs aria-label="{t("workspace.more")}"').classes("collection-row-action theme-text-muted"):
                    with ui.menu().props("auto-close"):
                        _collection_actions(item)

        def _render_branch_page(entry):
            scope, depth, page = entry["scope"], entry["depth"], entry["page"]
            collection = next((item for item in state.get("collections", []) if item["id"] == scope), None)
            with ui.column().classes("resource-branch-footer w-full gap-0").style(f'--tree-depth: {min(depth, 8)}').props(
                f'data-import-collection={scope if scope is not None else "all"}') as footer:
                footer._props["data-import-label"] = collection_path_label(collection) if collection else t("files.drop_library")
                if page is None:
                    ui.label(t("chunks.loading")).classes("text-xs theme-text-muted py-1")
                    return
                if not page["files"] and not entry["has_children"]:
                    ui.label(t("files.empty")).classes("text-xs theme-text-muted py-2")
                    if scope is None:
                        ui.label(t("files.drop_choose")).classes("text-xs theme-text-muted")
                        ui.button(t("files.upload"), on_click=file_handlers.open_file_picker).props("flat dense no-caps size=sm")
                size = state.get("file_page_size", 50)
                pages = max(1, (page["total"] + size - 1) // size)
                if pages > 1:
                    with ui.row().classes("resource-tree-pagination w-full items-center justify-between gap-1 flex-nowrap").props(
                        f'data-tree-page={branch_key(scope)}'):
                        ui.label(t("files.branch_page", page=page["page"], pages=pages, total=page["total"])).classes("text-xs theme-text-muted")
                        with ui.row().classes("gap-0"):
                            ui.button(icon="chevron_left", on_click=lambda: file_handlers.go_to_tree_page(scope, page["page"] - 1)).props(
                                f'flat dense round size=xs aria-label="{t("files.previous_page")}"').set_enabled(page["page"] > 1)
                            ui.button(icon="chevron_right", on_click=lambda: file_handlers.go_to_tree_page(scope, page["page"] + 1)).props(
                                f'flat dense round size=xs aria-label="{t("files.next_page")}"').set_enabled(page["page"] < pages)

        with ui.scroll_area(on_scroll=lambda e: state.update({"tree_scroll" if is_tree_view(state) else "file_scroll": e.vertical_position})).classes("flex-1 scroll-flush") as file_scroll:
            ui_refs["file_scroll"] = file_scroll
            @ui.refreshable
            def file_list_container():
                ui_refs["file_rows"] = {}
                ui_refs["file_checks"] = {}
                ui_refs["catalog_rows"] = {}
                ui_refs["collection_expanders"] = {}
                state["visible_file_rows"] = []
                tree_view = is_tree_view(state)
                ui_refs["tree_scroll_area"] = file_scroll if tree_view else None
                catalog.props(f'data-library-mode={state.get("library_mode", "tree")} data-multiselect={str(state.get("batch_mode", False)).lower()} '
                              f'data-tree={str(tree_view).lower()} data-collection-editing={str(bool(state.get("collection_edit"))).lower()}')
                batch_mode = state.get("batch_mode", False)
                selected_ids = state.get("batch_selected_ids", set())
                if tree_view:
                    with ui.element("div").classes("resource-tree w-full").props(f'role=tree aria-label="{t("files.tree_view")}"'):
                        try:
                            for entry in visible_tree_rows(state):
                                if entry["kind"] == "collection":
                                    _render_collection_row(entry)
                                elif entry["kind"] == "file":
                                    _render_file_row(entry["file"], batch_mode, selected_ids,
                                        scope=entry["scope"], depth=entry["depth"], row_key=entry["key"])
                                elif entry["kind"] == "page":
                                    _render_branch_page(entry)
                                elif entry["kind"] == "branch":
                                    edit = state.get("collection_edit")
                                    if edit and edit["id"] is None and edit["parent_id"] == entry["scope"]:
                                        _render_collection_editor(entry["depth"])
                        except ValueError as exc:
                            ui.label(str(exc)).classes("text-sm theme-danger px-3")
                    file_handlers.refresh_file_selection()
                    return
                if not state["filtered_files"]:
                    with ui.element("div").classes("w-full h-32 flex flex-col items-center justify-center gap-1 px-3"):
                        ui.icon("folder_open", size="md").classes("theme-text-muted")
                        if state["search_keyword"]:
                            ui.label(t("files.not_found")).classes("text-xs theme-text-muted")
                            ui.button(t("files.clear_search"), on_click=file_handlers.clear_search).props("flat dense no-caps size=sm")
                        else:
                            ui.label(t("files.empty")).classes("text-xs theme-text-muted")
                            ui.label(t("files.drop_choose")).classes("text-xs theme-text-muted text-center")
                            ui.button(t("files.upload"), on_click=file_handlers.open_file_picker).props("flat dense no-caps size=sm")
                    return
                with ui.column().classes("w-full gap-0 px-2"):
                    _table_header(batch_mode)
                    for file in state["filtered_files"]:
                        _render_file_row(file, batch_mode, selected_ids,
                            scope=next(iter(state.get("active_collection_ids", [])), None))
                file_handlers.refresh_file_selection()

            # 保存引用以便外部刷新
            ui_refs["file_list_container"] = file_list_container
            file_list_container()
        file_scroll.scroll_to(pixels=state.get("tree_scroll" if is_tree_view(state) else "file_scroll", 0))

        @ui.refreshable
        def file_pagination():
            page = state.get("file_page", 1)
            size = state.get("file_page_size", 50)
            total = state.get("file_total", 0)
            pages = max(1, (total + size - 1) // size)
            with ui.row().classes("w-full px-3 py-2 items-center justify-between gap-1 library-footer flex-nowrap"):
                if is_tree_view(state):
                    folders = sum(key.startswith("c:") for key in ui_refs.get("catalog_rows", {}))
                    ui.label(t("files.tree_summary", folders=folders, documents=len(state.get("filtered_files", [])))).classes("text-xs theme-text-muted")
                    return
                ui.label(t("files.pagination", page=page, pages=pages, total=total)).classes("text-xs theme-text-muted")
                with ui.row().classes("items-center gap-0 flex-nowrap"):
                    ui.button(icon="chevron_left", on_click=lambda: file_handlers.go_to_file_page(page - 1)).props(
                        "flat dense round size=sm"
                    ).props(f'aria-label="{t("files.previous_page")}"').tooltip(t("files.previous_page")).set_enabled(page > 1)
                    ui.button(icon="chevron_right", on_click=lambda: file_handlers.go_to_file_page(page + 1)).props(
                        "flat dense round size=sm"
                    ).props(f'aria-label="{t("files.next_page")}"').tooltip(t("files.next_page")).set_enabled(page < pages)
        ui_refs["file_pagination"] = file_pagination
        file_pagination()


def render_files_source(
    state: dict,
    ui_refs: dict,
    chunk_handlers,
):
    """
    渲染原页对比栏

    与切片栏并排显示 PDF 原件对应页：切片正文是 OCR / 文本层加工后的结果，
    校对时需要和原页对照着看，弹窗会把版面压得太小。未选中原页时不占位。

    Args:
        state: 共享状态字典
        ui_refs: 原页组件引用，切页时原地更新
        chunk_handlers: 切片处理器实例（关闭对比栏）
    """
    for key in ("source_pane", "source_image", "source_caption", "source_tooltip", "source_scroll",
                "source_page_input", "source_total", "source_previous", "source_next",
                "source_follow_toggle", "source_follow_status", "source_zoom_label", "source_input_page"):
        ui_refs[key] = None
    source = state.get("source_page")
    if not source or not state.get("source_pane_open"):
        return

    with ui.column().classes("flex-1 h-full min-w-0 gap-0 source-pane").props(
        f'data-file-id={state["selected_file_id"]} data-page={source["page"]} '
        f'data-follow-revision={state.get("source_follow_revision", 0)}'
    ) as pane:
        ui_refs["source_pane"] = pane
        with ui.row().classes("w-full items-center gap-1 library-heading source-toolbar flex-nowrap"):
            caption = ui.label(source["caption"]).classes("source-caption text-xs font-medium theme-text truncate")
            ui_refs["source_caption"] = caption
            with caption:
                ui_refs["source_tooltip"] = ui.tooltip(source.get("source_name") or source["caption"])
            with ui.row().classes("items-center gap-0 flex-nowrap"):
                ui_refs["source_previous"] = ui.button(icon="chevron_left", color=None,
                    on_click=lambda: chunk_handlers.step_source(-1)).props(
                    f'flat dense round size=sm aria-label="{t("library.previous_source_page")}"'
                ).tooltip(t("library.previous_source_page"))
                page_input = ui.input(value=str(source["page"])).props(
                    f'dense outlined inputmode=numeric aria-label="{t("library.source_page_number")}"'
                ).classes("source-page-input")
                async def jump_page():
                    try:
                        number = int(page_input.value)
                    except (TypeError, ValueError):
                        page_input.set_value(str((state.get("source_page") or source)["page"]))
                        ui.notify(t("library.page_invalid"), type="warning")
                        return
                    await chunk_handlers.navigate_source(number)
                page_input.on("keydown.enter", jump_page)
                ui_refs["source_page_input"] = page_input
                ui_refs["source_input_page"] = source["page"]
                ui_refs["source_total"] = ui.label(f'/ {source.get("total_pages") or "?"}').classes("text-xs theme-text-muted px-1")
                ui_refs["source_next"] = ui.button(icon="chevron_right", color=None,
                    on_click=lambda: chunk_handlers.step_source(1)).props(
                    f'flat dense round size=sm aria-label="{t("library.next_source_page")}"'
                ).tooltip(t("library.next_source_page"))
            ui.space()
            with ui.row().classes("items-center gap-0 flex-nowrap source-zoom-controls"):
                ui.button(t("library.fit_width"), color=None, on_click=lambda: chunk_handlers.set_source_zoom(100)).props("flat dense no-caps size=sm")
                ui.button(icon="remove", color=None, on_click=lambda: chunk_handlers.set_source_zoom(state.get("source_zoom", 100) - 25)).props(
                    f'flat dense round size=sm aria-label="{t("library.zoom_out")}"'
                ).tooltip(t("library.zoom_out"))
                ui_refs["source_zoom_label"] = ui.label(f'{state.get("source_zoom", 100)}%').classes("source-zoom-label text-xs theme-text-muted")
                ui.button(icon="add", color=None, on_click=lambda: chunk_handlers.set_source_zoom(state.get("source_zoom", 100) + 25)).props(
                    f'flat dense round size=sm aria-label="{t("library.zoom_in")}"'
                ).tooltip(t("library.zoom_in"))
                help_hint(t("library.zoom_hint"), label=t("library.zoom_hint"))
            ui_refs["source_follow_toggle"] = ui.switch(t("library.follow_text"), value=state.get("source_follow", True),
                on_change=lambda e: chunk_handlers.set_source_follow(e.value)).props("dense size=xs").classes("text-xs shrink-0")
            help_hint(t("library.follow_hint"), label=t("library.follow_hint"))
            ui.button(icon="close", color=None, on_click=chunk_handlers.close_source_page).props(
                f'flat dense round size=sm aria-label="{t("chunks.source_page_close")}"'
            ).tooltip(t("chunks.source_page_close"))
        ui_refs["source_follow_status"] = ui.label(t("library.follow_paused")).classes(
            "w-full px-3 py-1 text-xs theme-text-muted theme-panel"
        ).props("role=status")
        ui_refs["source_follow_status"].set_visibility(not state.get("source_follow", True))
        with ui.element("div").classes("source-scroll w-full flex-1 min-h-0 overflow-auto") as scroll:
            ui_refs["source_scroll"] = scroll
            ui_refs["source_image"] = ui.image(source["url"]).props("no-transition no-spinner").classes("source-image").style(
                f'width: {state.get("source_zoom", 100)}%; max-width: none'
            )
        chunk_handlers.refresh_source_controls()


def render_files_right(
    state: dict,
    ui_refs: dict,
    chunk_handlers,
    file_handlers,
):
    """
    渲染文件库右栏

    顶部以当前文件名为主标题，卡片管理收进次级操作；正文默认"连续阅读"，
    可切到"卡片管理"逐条编辑。文件属性收进按需展开的检查区域。

    Args:
        state: 共享状态字典
        ui_refs: UI 组件引用字典
        chunk_handlers: 切片处理器实例
        file_handlers: 文件处理器实例（集合归类、属性编辑）
    """
    def toggle_info():
        state["file_info_open"] = not state.get("file_info_open", False)
        panel = ui_refs.get("file_info_drawer")
        if panel is not None and not panel.is_deleted:
            panel.set_visibility(state["file_info_open"])

    def choose_view(view):
        if callback := ui_refs.get("set_library_view"):
            callback(view)
        else:
            state["library_view"] = view

    def manage_cards():
        choose_view("reading")
        chunk_handlers.set_file_reading_mode("cards")

    with ui.column().classes("flex-1 h-full flex flex-col theme-content min-w-0 gap-0 relative reader-panel"):
        @ui.refreshable
        def chunk_toolbar_buttons():
            if state.get("chunk_batch_mode"):
                with ui.row().classes("items-center gap-1 flex-nowrap"):
                    page_ids = {chunk["id"] for chunk in chunk_handlers.get_visible_chunks()}
                    ui.label(_batch_count_text(state.get("chunk_batch_selected_ids", set()), page_ids)).classes("text-xs theme-text-muted")
                    ui.checkbox(t("chunks.batch_select_all"), value=chunk_handlers.is_all_chunks_selected(),
                                on_change=lambda: chunk_handlers.toggle_chunk_select_all()).props("dense").classes("text-xs")
                    ui.button(icon="delete", color=None, on_click=chunk_handlers.confirm_chunk_batch_delete).props(
                        f'flat dense round size=sm aria-label="{t("chunks.batch_confirm")}"'
                    ).classes("theme-danger").tooltip(t("chunks.batch_confirm"))
                    ui.button(icon="close", color=None, on_click=chunk_handlers.exit_chunk_batch_mode).props(
                        f'flat dense round size=sm aria-label="{t("chunks.batch_cancel")}"'
                    ).tooltip(t("chunks.batch_cancel"))
                return
            selected_file = next((f for f in state.get("files_data", []) if f["id"] == state.get("selected_file_id")), None)
            with ui.row().classes("items-center gap-0 flex-nowrap"):
                ui.button(icon="info_outline", color=None, on_click=toggle_info).props(
                    f'flat dense round size=sm aria-label="{t("files.info_title")}"'
                ).tooltip(t("files.info_title")).set_enabled(selected_file is not None)
                more = ui.button(icon="more_horiz", color=None).props(
                    f'flat dense round size=sm aria-label="{t("workspace.more")}"'
                ).tooltip(t("workspace.more"))
                more.set_enabled(selected_file is not None)
                with more, ui.menu():
                    ui.menu_item(t("library.mode_cards"), on_click=manage_cards)
                    ui.menu_item(t("chunks.add"), on_click=chunk_handlers.handle_add_chunk)
                    ui.menu_item(t("workspace.select"), on_click=chunk_handlers.enter_chunk_batch_mode)
                    ui.separator()
                    ui.menu_item(t("chunks.refresh"), on_click=chunk_handlers._reload_chunks)
                    if selected_file:
                        ui.menu_item(t("files.reindex"), on_click=lambda fid=selected_file["id"]: file_handlers.handle_reindex_file(fid))

        with ui.row().classes("w-full items-center library-heading workspace-toolbar reader-toolbar"):
            workspace_controls(ui_refs, include_navigation=False)
            if state.get("knowledge_return") and ui_refs.get("return_knowledge"):
                return_view = state.get("knowledge_return_view") or "wiki"
                return_label = t("wiki.return" if return_view == "wiki" else "graph.return")
                ui.button(icon="arrow_back", color=None, on_click=ui_refs["return_knowledge"]).props(
                    f'flat dense round size=sm aria-label="{return_label}"'
                ).tooltip(return_label)
            with ui.column().classes("flex-1 min-w-0 gap-0"):
                @ui.refreshable
                def reader_title():
                    current_file = next((f for f in state.get("files_data", []) if f["id"] == state.get("selected_file_id")), None)
                    title = current_file["filename"] if current_file else t("library.preview_title")
                    ui.label(title).classes("text-sm font-medium theme-text truncate library-reader-title").tooltip(title)
                ui_refs["reader_title"] = reader_title
                reader_title()
            @ui.refreshable
            def reading_mode_toggle():
                if state.get("chunk_batch_mode"):
                    return
                mode = state.get("library_view", "reading")
                with ui.row().classes("reader-view-switch items-center gap-0 flex-nowrap").props(
                    f'role=group aria-label="{t("library.view_mode")}"'
                ):
                    can_compare = chunk_handlers.can_compare()
                    for value in ("reading", "compare"):
                        callback = chunk_handlers.open_comparison if value == "compare" else lambda v=value: choose_view(v)
                        enabled = can_compare if value == "compare" else state.get("selected_file_id") is not None

                        def make_button(value=value, callback=callback, enabled=enabled):
                            button = ui.button(t("library.view_" + value), color=None, on_click=callback).props(
                                f'flat dense no-caps no-ripple aria-pressed={str(mode == value).lower()}'
                            ).classes("reader-mode-active" if mode == value else "theme-text-muted")
                            button.set_enabled(enabled)
                            return button

                        if enabled or value != "compare":
                            make_button()
                            continue
                        # 禁用的 Quasar 按钮不派发鼠标事件，把原因提示挂到外层容器。
                        hint = t("library.compare_unavailable") if state.get("selected_file_id") else t("chunks.select_file_first")
                        with ui.element("div").classes("inline-flex items-center").tooltip(hint):
                            make_button()
            ui_refs["reading_mode_toggle"] = reading_mode_toggle
            reading_mode_toggle()
            chunk_toolbar_buttons()
        ui_refs["chunk_toolbar_buttons"] = chunk_toolbar_buttons

        # 文件检查区：集合归类 + 自定义属性（frontmatter 解析或手动维护），
        # 默认收起，把版面留给正文。
        @ui.refreshable
        def file_info_panel():
            ui_refs["file_info_drawer"] = None
            file_id = state.get("selected_file_id")
            if file_id is None:
                return

            file_info = next(
                (f for f in state.get("files_data", []) if f["id"] == file_id), None
            )
            if not file_info:
                return

            collections = state.get("collections_by_file", {}).get(file_id, [])
            properties = metadata_service.decode_metadata(file_info.get("metadata"))

            with ui.column().classes("file-info-drawer theme-panel gap-0") as panel:
                ui_refs["file_info_drawer"] = panel
                panel.set_visibility(state.get("file_info_open", False))
                with ui.row().classes("w-full items-center justify-between px-3 library-heading"):
                    ui.label(t("files.info_title")).classes("text-sm font-semibold theme-text")
                    ui.button(icon="close", color=None, on_click=toggle_info).props(
                        f'flat dense round size=sm aria-label="{t("library.close_info")}"'
                    ).tooltip(t("library.close_info"))
                with ui.scroll_area().classes("w-full flex-1 min-h-0"):
                    ui.label(file_info["filename"]).classes("text-sm theme-text break-words")
                    if ui_refs.get("wiki_references"):
                        ui.button(t("knowledge.references_wiki"), icon="menu_book",
                                  on_click=lambda: ui_refs["wiki_references"](file_id)).props("flat dense no-caps")
                    if ui_refs.get("graph_references"):
                        ui.button(t("knowledge.references_graph"), icon="hub",
                                  on_click=lambda: ui_refs["graph_references"](file_id)).props("flat dense no-caps")
                    # 集合
                    with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                        ui.icon("folder", size="xs").classes("theme-text-muted")
                        if collections:
                            for name in collections:
                                ui.badge(name, color="primary").props("dense outline")
                        else:
                            ui.label(t("collections.none")).classes(
                                "text-xs theme-text-muted"
                            )
                        ui.button(
                            icon="my_location", on_click=lambda: file_handlers.handle_locate_file(file_id)
                        ).props(f'flat dense round size=xs aria-label="{t("files.locate")}"').classes("theme-text-muted").tooltip(t("files.locate"))
                        ui.button(
                            icon="edit",
                            on_click=lambda: file_handlers.handle_edit_file_collections(file_id),
                        ).props("flat dense round size=xs").classes("theme-text-muted").tooltip(
                            t("collections.assign_title")
                        )

                    # 属性（frontmatter 解析或手动维护）
                    with ui.row().classes("w-full items-start gap-2 flex-wrap"):
                        ui.icon("label", size="xs").classes("theme-text-muted mt-1")
                        if properties:
                            with ui.column().classes("gap-0.5 min-w-0"):
                                for key, value in properties.items():
                                    text = (
                                        ", ".join(str(item) for item in value)
                                        if isinstance(value, list)
                                        else str(value)
                                    )
                                    with ui.row().classes("items-baseline gap-2 min-w-0"):
                                        ui.label(key).classes(
                                            "text-xs theme-text-muted whitespace-nowrap"
                                        )
                                        ui.label(text).classes(
                                            "text-xs theme-text-secondary break-all"
                                        )
                        else:
                            ui.label(t("properties.none")).classes(
                                "text-xs theme-text-muted"
                            )
                        ui.button(
                            icon="edit",
                            on_click=lambda: file_handlers.handle_edit_properties(file_id),
                        ).props("flat dense round size=xs").classes("theme-text-muted").tooltip(
                            t("properties.dialog_title")
                        )

                    # 原始文件：仅上传的文件有，应用内新建的卡片/笔记没有原件。
                    # 另存与重新索引的按钮在顶栏，这里只标注原件类型
                    if file_info.get("original_file_path"):
                        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                            ui.icon("attach_file", size="xs").classes("theme-text-muted")
                            ui.label(
                                (file_info.get("original_file_type") or "").upper()
                            ).classes("text-xs theme-text-secondary")

        ui_refs["file_info_panel"] = file_info_panel
        file_info_panel()

        def _render_chunk_pagination():
            total_chunks = state.get("total_chunks", 0)
            if total_chunks <= state["chunk_page_size"]:
                return
            current_page = state["chunk_page"]
            total_pages = chunk_handlers.get_total_chunk_pages()

            with ui.row().classes("w-full items-center justify-between mt-4 pt-4").style("border-top: 1px solid var(--border-color)"):
                ui.label(t("chunks.pagination_info_v2", current=current_page, total=total_pages, count=total_chunks)).classes("text-xs theme-text-muted")

                with ui.row().classes("items-center gap-2"):
                    ui.button(
                        icon="chevron_left",
                        on_click=chunk_handlers.prev_chunk_page
                    ).props("flat dense round size=sm").classes("theme-text-muted").props(
                        "disable" if current_page == 1 else ""
                    )

                    with ui.row().classes("items-center gap-1"):
                        page_input = ui.input(
                            value=str(current_page),
                            placeholder=str(current_page)
                        ).props("dense outlined").classes("text-sm text-center").style(
                            "width: 50px"
                        )

                        # 回车键跳转
                        async def handle_page_jump(_):
                            try:
                                page = int(page_input.value)
                                if 1 <= page <= total_pages:
                                    await chunk_handlers.go_to_chunk_page(page)
                                else:
                                    ui.notify(t("chunks.page_out_of_range", total=total_pages), type="warning")
                                    page_input.value = str(current_page)
                            except ValueError:
                                ui.notify(t("chunks.page_invalid"), type="warning")
                                page_input.value = str(current_page)

                        page_input.on('keydown.enter', handle_page_jump)

                        ui.label(f"/ {total_pages}").classes("text-sm theme-text-muted")

                    ui.button(
                        icon="chevron_right",
                        on_click=chunk_handlers.next_chunk_page
                    ).props("flat dense round size=sm").classes("theme-text-muted").props(
                        "disable" if current_page == total_pages else ""
                    )

        # 切片内容区
        # chunk-scroll / chunk-anchor 供滚动联动的 js_handler 定位当前可见切片
        chunk_scroll = ui.scroll_area(on_scroll=lambda e: state.update(chunk_scroll=e.vertical_position)).classes("flex-1 min-w-0 chunk-scroll scroll-flush")
        # 前端按阅读区和停留时间筛选，只把稳定的新目标交给服务端。
        chunk_scroll.on(
            "scroll",
            chunk_handlers.handle_chunk_scroll,
            js_handler=_SOURCE_SCROLL_HANDLER,
        )

        def _render_reading_block(chunk: dict, has_source_pages: bool):
            """连续阅读的一块：标题 + 正文，管理操作悬停才显现（不再是一条条卡片）。"""
            chunk_id = chunk["id"]
            source_page = (
                page_number_from_heading(chunk.get("heading_path"))
                if has_source_pages else None
            )
            # chunk-anchor：滚动联动据此定位当前可见的切片
            with ui.column().classes(
                "w-full min-w-0 library-reading-block chunk-anchor"
            ).props(
                f'data-file-id={state["selected_file_id"]} '
                f'data-chunk-id={chunk_id} data-page={source_page or 0}'
            ):
                with ui.row().classes("w-full items-start gap-2 min-w-0 library-reading-heading"):
                    with ui.column().classes("gap-0 min-w-0 flex-1"):
                        ui.label(chunk["doc_title"]).classes("library-reading-title theme-text")
                        parent = _parent_path(chunk.get("heading_path"))
                        if parent:
                            ui.label(parent).classes("text-xs theme-text-muted truncate").tooltip(parent)
                    with ui.row().classes("items-center gap-1 shrink-0 library-block-actions"):
                        # 原页按钮：PDF 切片是 OCR/文本层加工后的结果，校对时需要回看原件
                        if source_page:
                            ui.button(
                                icon="image",
                                on_click=lambda p=source_page: chunk_handlers.handle_view_source_page(p),
                            ).props(f'flat dense round size=xs aria-label="{t("chunks.view_source_page", page=source_page)}"').classes(
                                "theme-text-muted"
                            ).tooltip(t("chunks.view_source_page", page=source_page))
                        ui.button(
                            icon="edit",
                            on_click=lambda cid=chunk_id: chunk_handlers.handle_edit_chunk(cid),
                        ).props(f'flat dense round size=xs aria-label="{t("chunks.edit")}"').classes(
                            "theme-text-muted"
                        ).tooltip(t("chunks.edit"))
                        with ui.button(icon="more_horiz").props(
                            f'flat dense round size=xs aria-label="{t("workspace.more")}"'
                        ).classes("theme-text-muted").tooltip(t("workspace.more")):
                            with ui.menu():
                                ui.menu_item(
                                    t("chunks.delete_confirm_title"),
                                    on_click=lambda cid=chunk_id: chunk_handlers.handle_delete_chunk(cid),
                                ).classes("theme-danger")
                chunk_markdown(chunk["chunk_text"], chunk_id=chunk_id).classes("library-reading-body")

        def _render_chunk_card(chunk: dict, has_source_pages: bool, batch_mode: bool, batch_selected_ids: set):
            chunk_id = chunk["id"]
            is_batch_selected = chunk_id in batch_selected_ids
            source_page = (
                page_number_from_heading(chunk.get("heading_path"))
                if has_source_pages else None
            )
            if batch_mode:
                # 批量模式：显示带复选框的卡片
                with ui.row().classes("w-full mb-3 items-start gap-2 min-w-0"):
                    ui.checkbox(
                        value=is_batch_selected,
                        on_change=lambda _, cid=chunk_id: chunk_handlers.toggle_chunk_selection(cid)
                    ).props("dense")
                    with ui.column().classes("flex-1 min-w-0"):
                        chunk_card(
                            doc_title=chunk["doc_title"],
                            chunk_text=chunk["chunk_text"],
                            chunk_id=chunk["id"],
                            on_edit=None,
                            on_delete=None,
                            parent_path=_parent_path(chunk.get("heading_path")),
                        )
            else:
                chunk_card(
                    doc_title=chunk["doc_title"],
                    chunk_text=chunk["chunk_text"],
                    chunk_id=chunk["id"],
                    on_edit=chunk_handlers.handle_edit_chunk,
                    on_delete=chunk_handlers.handle_delete_chunk,
                    parent_path=_parent_path(chunk.get("heading_path")),
                    source_page=source_page,
                    on_view_source=chunk_handlers.handle_view_source_page,
                ).classes("chunk-anchor").props(
                    f'data-file-id={state["selected_file_id"]} '
                    f'data-chunk-id={chunk_id} data-page={source_page or 0}'
                )

        with chunk_scroll:
            @ui.refreshable
            def chunk_inspector():
                if state["selected_file_id"] is None:
                    with ui.column().classes(
                        "w-full h-full items-center justify-center"
                    ):
                        ui.icon("touch_app", size="lg").classes("theme-text-muted")
                        ui.label(t("chunks.select_file")).classes(
                            "text-sm mt-2 theme-text-muted"
                        )
                    return

                # 加载中状态
                if not state["chunks_data"] and state.get("total_chunks", 0) > 0:
                    with ui.column().classes(
                        "w-full h-full items-center justify-center"
                    ):
                        ui.spinner(size="lg", color="primary")
                        ui.label(t("chunks.loading")).classes(
                            "text-sm mt-2 theme-text-muted"
                        )
                    return

                # 文件无切片
                if state.get("total_chunks", 0) == 0:
                    with ui.column().classes(
                        "w-full h-full items-center justify-center"
                    ):
                        ui.icon("content_cut", size="lg").classes("theme-text-muted")
                        ui.label(t("chunks.empty")).classes(
                            "text-sm mt-2 theme-text-muted"
                        )
                    return

                chunk_batch_mode = state.get("chunk_batch_mode", False)
                chunk_batch_selected_ids = state.get("chunk_batch_selected_ids", set())

                # 获取当前页的切片
                visible_chunks = chunk_handlers.get_visible_chunks()

                # PDF 切片的页码写在 heading_path 里，可据此回看原件对应页。
                # Word/PPT 回看的是 Office 转换产出的 PDF，同样按页对齐；
                # 其它格式没有页码概念
                current_file = next(
                    (
                        f
                        for f in state.get("files_data", [])
                        if f["id"] == state["selected_file_id"]
                    ),
                    None,
                )
                has_source_pages = bool(
                    current_file
                    and current_file.get("original_file_path")
                    and f".{current_file.get('original_file_type')}".lower()
                    in PAGE_VIEWABLE_FORMATS
                )

                # 批量选择时一律用卡片形态（需要复选框）；此外由阅读模式决定
                mode = "cards" if chunk_batch_mode else state.get("file_reading_mode", "reading")
                # gap-0：间距由各块自己的样式决定，否则会再叠一层 .nicegui-column 默认 16px
                with ui.column().classes(
                    "w-full min-w-0 gap-0 library-document" if mode != "cards"
                    else "w-full min-w-0 gap-0 library-cards"
                ):
                    for chunk in visible_chunks:
                        if mode == "cards":
                            _render_chunk_card(chunk, has_source_pages, chunk_batch_mode, chunk_batch_selected_ids)
                        else:
                            _render_reading_block(chunk, has_source_pages)

                    # 分页控件
                    _render_chunk_pagination()

            ui_refs["chunk_inspector"] = chunk_inspector
            chunk_inspector()
        chunk_scroll.scroll_to(pixels=state.get("chunk_scroll", 0))
        ui_refs["chunk_scroll"] = chunk_scroll
