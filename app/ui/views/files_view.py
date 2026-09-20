"""
文件库视图

职责:
- 渲染文件库中栏（文件列表）
- 渲染文件库右栏（切片详情）
"""

from nicegui import ui

from app.i18n import t
from app.ui.components import status_badge, chunk_card, collection_labels
from app.utils import MAX_TOTAL_UPLOAD_SIZE, MAX_UPLOAD_FILES
from indexing.services.file_service import get_max_file_size
from indexing.services import metadata_service
from indexing.services.chunking import ChunkerFactory
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
    渲染文件库中栏

    Args:
        state: 共享状态字典
        ui_refs: UI 组件引用字典
        file_handlers: 文件处理器实例
    """
    with ui.column().classes(
        "w-64 h-full flex flex-col overflow-hidden theme-panel gap-0"
    ).style("border-right: 1px solid var(--border-color)"):
        # 顶部标题栏
        with ui.row().classes(
            "w-full px-3 items-center justify-between"
        ).style("border-bottom: 1px solid var(--border-color); height: 49px"):
            ui.label(t("files.title")).classes("text-sm font-medium theme-text")

            @ui.refreshable
            def toolbar_buttons():
                if state.get("batch_mode"):
                    # 批量模式：显示全选、归类、确认删除、取消按钮
                    with ui.row().classes("items-center gap-1"):
                        # 全选复选框
                        ui.checkbox(
                            t("files.batch_select_all"),
                            value=file_handlers.is_all_selected(),
                            on_change=lambda: file_handlers.toggle_select_all()
                        ).props("dense").classes("text-xs")
                        # 批量归入集合
                        ui.button(
                            icon="drive_file_move",
                            on_click=file_handlers.handle_batch_collections
                        ).props("flat dense round size=sm").classes("theme-text-accent").tooltip(t("collections.batch_assign"))
                        # 确认删除按钮
                        ui.button(
                            icon="delete",
                            on_click=file_handlers.confirm_batch_delete
                        ).props("flat dense round size=sm").classes("text-red-400").tooltip(t("files.batch_confirm"))
                        # 取消按钮
                        ui.button(
                            icon="close",
                            on_click=file_handlers.exit_batch_mode
                        ).props("flat dense round size=sm").classes("theme-text-muted").tooltip(t("files.batch_cancel"))
                else:
                    # 普通模式：刷新 + 新建菜单 + 批量删除（图标太多会挤爆 256px 的中栏）
                    with ui.row().classes("items-center gap-1"):
                        ui.button(icon="refresh", on_click=file_handlers.refresh_and_scan).props(
                            "flat dense round size=sm"
                        ).classes("theme-text-muted").tooltip(t("files.refresh"))

                        with ui.button(icon="add").props(
                            "flat dense round size=sm"
                        ).classes("theme-text-accent").tooltip(t("files.create_menu")):
                            with ui.menu().props("auto-close"):
                                ui.menu_item(
                                    t("cards.create"),
                                    on_click=file_handlers.handle_create_card,
                                )
                                ui.menu_item(
                                    t("files.create"),
                                    on_click=file_handlers.handle_create_file,
                                )
                                ui.separator()
                                ui.menu_item(
                                    t("files.upload"),
                                    on_click=file_handlers.open_file_picker,
                                )
                                ui.menu_item(
                                    t("import.folder_menu"),
                                    on_click=file_handlers.handle_import_folder,
                                )
                                ui.menu_item(
                                    t("import.zotero_menu"),
                                    on_click=file_handlers.handle_import_zotero,
                                )

                        ui.button(
                            icon="delete_sweep",
                            on_click=file_handlers.enter_batch_mode
                        ).props("flat dense round size=sm").classes("text-red-400").tooltip(t("files.batch_delete"))

            ui_refs["toolbar_buttons"] = toolbar_buttons
            toolbar_buttons()

        # 隐藏的上传组件（支持批量上传）
        ui_refs["upload_input"] = ui.upload(
            on_begin_upload=file_handlers.on_begin_upload,
            on_upload=file_handlers.handle_upload,
            on_multi_upload=file_handlers.on_multi_upload_complete,
            on_rejected=file_handlers.on_upload_rejected,
            auto_upload=True,
            multiple=True,
            max_files=MAX_UPLOAD_FILES,
            # 上限随当前解析后端变化；这里取渲染时的值，服务端导入时还会按同一
            # 规则复核（切换解析后端后未重进页面，以服务端校验为准）
            max_file_size=get_max_file_size(),
            max_total_size=MAX_TOTAL_UPLOAD_SIZE,
        ).props(
            f"accept={','.join(ChunkerFactory.get_supported_extensions())}"
        ).classes("hidden")


        # 上传阶段提示条：浏览器→服务端的传输期间文件还没有数据库记录，
        # 无法出现在下方列表里，这里单独给一条反馈避免看起来没反应
        @ui.refreshable
        def upload_banner():
            count = state.get("uploading_count", 0)
            if not count:
                return
            with ui.row().classes("w-full items-center gap-2 px-3 py-1.5"):
                ui.spinner(size="xs").classes("theme-text-accent")
                ui.label(t("files.uploading", count=count)).classes(
                    "text-xs theme-text-muted"
                )

        ui_refs["upload_banner"] = upload_banner
        upload_banner()

        # 搜索 + 排序（debounce：避免每次按键都重建整个文件列表）
        @ui.refreshable
        def list_toolbar():
            with ui.row().classes("w-full items-center gap-1 px-2 py-2 flex-nowrap"):
                ui.input(placeholder=t("files.search")).props(
                    "dense outlined rounded debounce=300"
                ).classes("flex-1 min-w-0 text-sm theme-card theme-border-soft").on(
                    "update:model-value", file_handlers.on_search_change
                )
                with ui.button(icon="sort").props("flat dense round size=sm").classes(
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

        # 集合筛选：文件多、跨领域时先缩小范围；多选取并集，留空为全部
        @ui.refreshable
        def collection_filter():
            options = {
                item["id"]: f"{item['name']} ({item['file_count']})"
                for item in state.get("collections", [])
            }

            with ui.row().classes("w-full items-center gap-1 px-2 pb-2 flex-nowrap"):
                ui.select(
                    options,
                    value=list(state.get("active_collection_ids") or []),
                    multiple=True,
                    label=t("collections.filter_label"),
                    on_change=lambda e: file_handlers.on_collection_change(e.value),
                ).props("dense outlined options-dense use-chips").classes(
                    "flex-1 min-w-0 text-sm theme-card theme-border-soft"
                )
                ui.button(
                    icon="settings",
                    on_click=file_handlers.handle_manage_collections,
                ).props("flat dense round size=sm").classes(
                    "shrink-0 theme-text-muted"
                ).tooltip(t("collections.manage"))

        ui_refs["collection_filter"] = collection_filter
        collection_filter()

        # 文件列表（scroll-flush + 自带 gap：间距对齐左栏导航的 gap-0.5）
        with ui.scroll_area().classes("flex-1 scroll-flush"):
            @ui.refreshable
            def file_list_container():
                if not state["filtered_files"]:
                    with ui.element("div").classes("w-full h-32 flex flex-col items-center justify-center gap-1 px-3"):
                        ui.icon("folder_open", size="md").classes("theme-text-muted")
                        if state["search_keyword"]:
                            ui.label(t("files.not_found")).classes("text-xs theme-text-muted")
                        elif state.get("active_collection_ids"):
                            # 集合筛选下为空：提示怎么把文件放进来，否则用户只会以为没文件
                            ui.label(t("collections.no_files")).classes("text-xs theme-text-muted")
                            ui.label(t("collections.hint_assign")).classes(
                                "text-xs theme-text-muted text-center"
                            )
                        else:
                            ui.label(t("files.empty")).classes("text-xs theme-text-muted")
                    return

                batch_mode = state.get("batch_mode", False)
                batch_selected_ids = state.get("batch_selected_ids", set())

                # 包一层自带 gap 的 column：否则列表项会直接落进滚动区内容层，
                # 吃到 NiceGUI 默认的 gap:1rem（左栏导航同理用 gap-0.5 px-2）
                with ui.column().classes("w-full gap-0.5 px-2"):
                    for f in state["filtered_files"]:
                        file_id = f["id"]
                        is_selected = file_id == state["selected_file_id"]
                        is_batch_selected = file_id in batch_selected_ids

                        # 通过索引直接获取进行中的任务
                        task_progress = state.get("task_progress_by_file_id", {}).get(file_id)

                        container_classes = "w-full px-3 py-1.5 cursor-pointer transition-colors rounded-md "
                        if batch_mode and is_batch_selected:
                            container_classes += "theme-selected"
                        elif is_selected and not batch_mode:
                            container_classes += "theme-selected"
                        else:
                            container_classes += "theme-hover"

                        # 批量模式下点击切换选中，普通模式下点击加载切片
                        if batch_mode:
                            click_handler = lambda _, fid=file_id: file_handlers.toggle_file_selection(fid)
                        else:
                            click_handler = lambda _, fid=file_id: file_handlers.load_chunks(fid)

                        # 不画分隔线：项间距压到 2px 后，圆角项下方的横线会显得零碎，
                        # 选中/悬停底色已足够区隔（与左栏导航一致）
                        with ui.element("div").classes(container_classes).style(
                            "padding: 6px 8px"
                        ).on("click", click_handler):
                            # 右键菜单：归类/属性/删除，避免必须先选中文件才能操作
                            if not batch_mode:
                                with ui.context_menu():
                                    ui.menu_item(
                                        t("collections.assign_title"),
                                        lambda fid=file_id: file_handlers.handle_edit_file_collections(fid),
                                    )
                                    ui.menu_item(
                                        t("properties.dialog_title"),
                                        lambda fid=file_id: file_handlers.handle_edit_properties(fid),
                                    )
                                    ui.separator()
                                    ui.menu_item(
                                        t("files.delete_confirm_title"),
                                        lambda fid=file_id: file_handlers.confirm_delete_file(fid),
                                    ).classes("text-red-400")

                            if batch_mode:
                                # 批量模式：显示复选框
                                with ui.element("div").classes(
                                    "grid items-center gap-2 w-full"
                                ).style("grid-template-columns: auto auto 1fr auto"):
                                    ui.checkbox(
                                        value=is_batch_selected,
                                    ).props("dense size=xs disable").style("pointer-events: none")
                                    ui.icon("description", size="xs").classes(
                                        "theme-text-accent" if is_batch_selected else "theme-text-muted"
                                    ).style("pointer-events: none")
                                    ui.label(f["filename"]).classes(
                                        "text-sm truncate theme-text"
                                    ).tooltip(f["filename"])
                                    with ui.element("div").classes("justify-self-end"):
                                        status_badge(f["status"])
                            else:
                                # 普通模式：显示文件名和状态
                                with ui.column().classes("w-full gap-1"):
                                    with ui.element("div").classes(
                                        "grid items-center gap-2 w-full"
                                    ).style("grid-template-columns: auto 1fr auto"):
                                        ui.icon("description", size="xs").classes(
                                            "theme-text-accent" if is_selected else "theme-text-muted"
                                        )
                                        ui.label(f["filename"]).classes(
                                            "text-sm truncate theme-text"
                                        ).tooltip(f["filename"])
                                        with ui.element("div").classes("justify-self-end"):
                                            status_badge(f["status"])

                                    # 所属集合（跨领域归类，一个文件可属于多个集合）
                                    collection_labels(
                                        state.get("collections_by_file", {}).get(file_id, [])
                                    )

                                    # 如果有进行中的任务，显示进度条
                                    if task_progress and task_progress["status"] in ["pending", "processing"]:
                                        progress_value = task_progress["progress"]
                                        with ui.row().classes("w-full items-center gap-2"):
                                            ui.linear_progress(
                                                value=progress_value / 100,
                                                show_value=False,
                                            ).props("size=2px color=primary").classes("flex-1")
                                            ui.label(
                                                _progress_text(task_progress)
                                            ).classes(
                                                "text-xs theme-text-muted whitespace-nowrap"
                                            )

            # 保存引用以便外部刷新
            ui_refs["file_list_container"] = file_list_container
            file_list_container()


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
    for key in ("source_pane", "source_image", "source_caption", "source_tooltip"):
        ui_refs[key] = None
    source = state.get("source_page")
    if not source:
        return

    # flex-1：与切片栏均分剩余宽度，原页得到的版面比弹窗大得多
    # source-pane：滚动联动据此判断对比栏是否展开，收起时前端直接跳过上报
    with ui.column().classes(
        "flex-1 h-full flex flex-col theme-content min-w-0 gap-0 source-pane"
    ).style("border-left: 1px solid var(--border-color)").props(
        f'data-file-id={state["selected_file_id"]} data-page={source["page"]} '
        f'data-follow-revision={state.get("source_follow_revision", 0)}'
    ) as pane:
        ui_refs["source_pane"] = pane
        # 顶部信息区：高度与其它栏的 49px 标题栏对齐
        with ui.row().classes(
            "w-full px-3 items-center justify-between gap-2 theme-sidebar flex-nowrap"
        ).style("border-bottom: 1px solid var(--border-color); height: 49px"):
            with ui.row().classes("items-center gap-2 min-w-0"):
                ui.icon("picture_as_pdf", size="xs").classes("theme-text-accent")
                with ui.label(source["caption"]).classes(
                    "text-sm font-medium theme-text truncate"
                ) as caption:
                    ui_refs["source_caption"] = caption
                    ui_refs["source_tooltip"] = ui.tooltip(source["caption"])
            ui.button(
                icon="close",
                on_click=chunk_handlers.close_source_page,
            ).props("flat dense round size=sm").classes(
                "theme-text-muted shrink-0"
            ).tooltip(t("chunks.source_page_close"))

        # 原页按栏宽铺满，纵向滚动查看整页
        with ui.scroll_area().classes("flex-1 min-w-0"):
            # QImg 会保留上一张图，待新图加载完成后替换；无需重建整个原页栏。
            ui_refs["source_image"] = ui.image(source["url"]).classes("w-full").props(
                "no-transition no-spinner"
            )


def render_files_right(
    state: dict,
    ui_refs: dict,
    chunk_handlers,
    file_handlers,
):
    """
    渲染文件库右栏

    Args:
        state: 共享状态字典
        ui_refs: UI 组件引用字典
        chunk_handlers: 切片处理器实例
        file_handlers: 文件处理器实例（集合归类、属性编辑）
    """
    with ui.column().classes("flex-1 h-full flex flex-col theme-content min-w-0 gap-0"):
        # 顶部信息区
        with ui.row().classes(
            "w-full px-5 items-center justify-between theme-sidebar"
        ).style("border-bottom: 1px solid var(--border-color); height: 49px"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("article", size="xs").classes("theme-text-accent")
                ui.label(t("chunks.title")).classes("text-sm font-medium theme-text")

            @ui.refreshable
            def chunk_toolbar_buttons():
                if state.get("chunk_batch_mode"):
                    # 批量模式：显示全选、确认删除、取消按钮
                    with ui.row().classes("items-center gap-1"):
                        ui.checkbox(
                            t("chunks.batch_select_all"),
                            value=chunk_handlers.is_all_chunks_selected(),
                            on_change=lambda: chunk_handlers.toggle_chunk_select_all()
                        ).props("dense").classes("text-xs")
                        ui.button(
                            icon="delete",
                            on_click=chunk_handlers.confirm_chunk_batch_delete
                        ).props("flat dense round size=sm").classes("text-red-400").tooltip(t("chunks.batch_confirm"))
                        ui.button(
                            icon="close",
                            on_click=chunk_handlers.exit_chunk_batch_mode
                        ).props("flat dense round size=sm").classes("theme-text-muted").tooltip(t("chunks.batch_cancel"))
                else:
                    # 普通模式：显示常规按钮
                    selected_file = next(
                        (
                            f
                            for f in state.get("files_data", [])
                            if f["id"] == state.get("selected_file_id")
                        ),
                        None,
                    )

                    with ui.row().classes("items-center gap-1"):
                        ui.button(icon="add", on_click=chunk_handlers.handle_add_chunk).props(
                            "flat dense round size=sm"
                        ).classes("theme-text-accent").tooltip(t("chunks.add"))
                        ui.button(
                            icon="refresh",
                            on_click=lambda: chunk_handlers._reload_chunks() if state["selected_file_id"] else None
                        ).props("flat dense round size=sm").classes("theme-text-muted").tooltip(t("chunks.refresh"))

                        # 解析产物：知识卡片的来源文档，导出它即可拿到整篇 Markdown
                        if selected_file:
                            ui.button(
                                icon="text_snippet",
                                on_click=lambda f=selected_file: file_handlers.handle_export_working(f["id"]),
                            ).props("flat dense round size=sm").classes("theme-text-muted").tooltip(t("files.export_working"))

                        # 原件相关操作：仅上传的文件有原件，应用内新建的没有
                        if selected_file and selected_file.get("original_file_path"):
                            ui.button(
                                icon="download",
                                on_click=lambda f=selected_file: file_handlers.handle_export_original(f["id"]),
                            ).props("flat dense round size=sm").classes("theme-text-muted").tooltip(t("files.export_original"))

                        # 换了解析后端或分块参数后，已入库的切片仍是旧配置的产物
                        if selected_file:
                            ui.button(
                                icon="restart_alt",
                                on_click=lambda f=selected_file: file_handlers.handle_reindex_file(f["id"]),
                            ).props("flat dense round size=sm").classes("theme-text-muted").tooltip(t("files.reindex"))

                        ui.button(
                            icon="delete_sweep",
                            on_click=chunk_handlers.enter_chunk_batch_mode
                        ).props("flat dense round size=sm").classes("text-red-400").tooltip(t("chunks.batch_delete"))

            ui_refs["chunk_toolbar_buttons"] = chunk_toolbar_buttons
            chunk_toolbar_buttons()

        # 文件信息区：集合归类 + 自定义属性（frontmatter 解析或手动维护）
        @ui.refreshable
        def file_info_panel():
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

            # 折叠态只给一行摘要，展开后属性完整铺开，不再被截断
            caption = " · ".join(
                filter(
                    None,
                    [
                        t("collections.summary", count=len(collections))
                        if collections
                        else t("collections.none"),
                        t("properties.summary", count=len(properties))
                        if properties
                        else t("properties.none"),
                    ],
                )
            )

            with ui.expansion(
                file_info["filename"], icon="info", caption=caption
            ).props("dense expand-separator").classes(
                "w-full theme-panel"
            ).style("border-bottom: 1px solid var(--border-color)"):
                with ui.column().classes("w-full gap-2 pb-2"):
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

        # 切片内容区
        # chunk-scroll / chunk-anchor 供滚动联动的 js_handler 定位当前可见切片
        chunk_scroll = ui.scroll_area().classes("flex-1 min-w-0 chunk-scroll")
        # 前端按阅读区和停留时间筛选，只把稳定的新目标交给服务端。
        chunk_scroll.on(
            "scroll",
            chunk_handlers.handle_chunk_scroll,
            js_handler=_SOURCE_SCROLL_HANDLER,
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

                # gap-0：卡片间距由各自的 mb-2 决定，否则会再叠一层
                # .nicegui-column 默认的 16px
                with ui.column().classes("w-full min-w-0 gap-0"):
                    for chunk in visible_chunks:
                        chunk_id = chunk["id"]
                        is_batch_selected = chunk_id in chunk_batch_selected_ids

                        if chunk_batch_mode:
                            # 批量模式：显示带复选框的卡片
                            with ui.row().classes("w-full mb-3 items-start gap-2 min-w-0"):
                                # 复选框
                                ui.checkbox(
                                    value=is_batch_selected,
                                    on_change=lambda _, cid=chunk_id: chunk_handlers.toggle_chunk_selection(cid)
                                ).props("dense")
                                # 卡片
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
                            # 普通模式
                            # chunk-anchor：滚动联动据此定位当前可见的切片
                            source_page = (
                                page_number_from_heading(chunk.get("heading_path"))
                                if has_source_pages else None
                            )
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

                    # 分页控件
                    total_chunks = state.get("total_chunks", 0)
                    if total_chunks > state["chunk_page_size"]:
                        current_page = state["chunk_page"]
                        total_pages = chunk_handlers.get_total_chunk_pages()

                        with ui.row().classes("w-full items-center justify-between mt-4 pt-4").style("border-top: 1px solid var(--border-color)"):
                            # 左侧：统计信息
                            ui.label(t("chunks.pagination_info_v2", current=current_page, total=total_pages, count=total_chunks)).classes("text-xs theme-text-muted")

                            # 右侧：分页按钮
                            with ui.row().classes("items-center gap-2"):
                                # 上一页按钮
                                ui.button(
                                    icon="chevron_left",
                                    on_click=chunk_handlers.prev_chunk_page
                                ).props("flat dense round size=sm").classes("theme-text-muted").props(
                                    "disable" if current_page == 1 else ""
                                )

                                # 页码输入框
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

                                # 下一页按钮
                                ui.button(
                                    icon="chevron_right",
                                    on_click=chunk_handlers.next_chunk_page
                                ).props("flat dense round size=sm").classes("theme-text-muted").props(
                                    "disable" if current_page == total_pages else ""
                                )

            ui_refs["chunk_inspector"] = chunk_inspector
            chunk_inspector()
