"""桌面工作台：常驻目录、资料浏览与文档阅读。"""
import json

from nicegui import context, ui

from app.i18n import t
from app.ui.views.wiki_view import WikiWorkbench
from app.ui.views.graph_view import GraphWorkbench
from app.ui.views.sidebar import render_app_header
from app.ui.components import workspace_controls
from app.ui.file_drop import render_workspace_upload
from app.ui.workspace_state import (
    RESULTS_WIDTHS, MIN_RESULTS_WIDTH, MIN_NAVIGATION_WIDTH, restore_workspace, storage_key, workspace_snapshot,
)
from app.ui.views.knowledge_common import local_source
from indexing.settings import get_settings
from app.ui.styles import inject_theme_css, init_theme, apply_theme
from app.ui.handlers import FileHandlers, ChunkHandlers, TaskHandlers, SettingsHandlers, SyncHandlers
from app.ui.views import (
    render_sidebar, render_files_middle, render_files_right, render_files_source,
    render_settings_middle, render_settings_right, render_mcp_config_middle,
    render_mcp_config_right, render_skill_middle, render_skill_right,
    render_cloud_sync_middle, render_cloud_sync_right, render_logs_middle,
    render_logs_right, render_recall_test_middle, render_recall_test_right,
)


# 初始栏宽使用像素，避免宽屏把导航和列表按比例拉大。
NAVIGATION_WIDTH = 160
NAVIGATION_RAIL_WIDTH = 52
RESULTS_WIDTH = 240


def register_pages(port: int = 8689):
    """注册页面；port 用于 Skill 导出时注入 CLI 调用前缀。"""
    @ui.page("/")
    def main_page():
        client = context.client
        current_view = {"value": "files"}
        navigation_revision = 0
        selected_setting = {"value": None}
        selected_client = {"value": None}
        selected_skill = {"value": None}
        skill_export_state = {"export_dir": ""}
        state = {
            "selected_file_id": None, "search_keyword": "", "files_data": [],
            "filtered_files": [], "chunks_data": [], "chunk_page": 1,
            "chunk_page_size": 50, "chunk_scroll": 0, "source_pane_open": False,
            "source_page": None, "navigation_width": NAVIGATION_WIDTH, "navigation_hidden": False,
            "navigation_collapsed": False,
            "results_width": RESULTS_WIDTHS["files"], "file_reading_mode": "reading", "library_view": "reading",
            "file_info_open": False, "workspace_ready": False,
        }
        # 目录始终展开，各工作区独立记忆宽度。
        widths = RESULTS_WIDTHS.copy()
        preferences_ready = False
        last_preferences = None
        from datetime import datetime
        settings = get_settings()
        last_sync_display = None
        if settings.webdav.last_sync_time:
            try:
                last_sync_display = datetime.fromisoformat(settings.webdav.last_sync_time).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
        sync_state = {"is_syncing": False, "last_sync": last_sync_display, "logs": []}
        recall_state = {
            "query": "", "filenames": "", "collection_names": [], "include_descendants": True,
            "is_running": False, "has_run": False, "results": [], "stats": None, "error": None,
        }
        settings_form = {}
        ui_refs: dict = {
            "upload_input": None, "file_list_container": None, "collection_filter": None,
            "file_info_panel": None, "settings_list": None, "client_list": None,
            "chunk_inspector": None, "source_column": None, "stats_label": None,
        }
        task_handlers = TaskHandlers(state, ui_refs)
        settings_handlers = SettingsHandlers(settings_form)
        file_handlers = FileHandlers(state=state, ui_refs=ui_refs)
        task_handlers.set_file_handlers(file_handlers)
        sync_handlers = SyncHandlers(sync_state, ui_refs)
        chunk_handlers = ChunkHandlers(state=state, ui_refs=ui_refs, on_refresh_files=file_handlers.load_files)
        task_handlers.set_chunk_handlers(chunk_handlers)
        file_handlers.set_chunk_handlers(chunk_handlers)

        async def switch_view(view):
            nonlocal navigation_revision
            navigation_revision += 1
            revision = navigation_revision
            previous = current_view["value"]
            if previous == view:
                return
            if state.get("collection_edit"):
                ui.notify(t("collections.finish_edit"), type="info")
                return
            if previous == "wiki" and wiki.editor and wiki.editor.busy:
                ui.notify(wiki.text("saving"), type="info")
                return
            if previous == "graph":
                await graph.remember_positions()
            if revision != navigation_revision:
                return
            current_view["value"] = view
            state["results_width"] = widths.get(view, RESULTS_WIDTH)
            results_splitter.value = state["results_width"]
            adapt_navigation()
            if view == "settings":
                settings_handlers.init_settings_form()
            header.refresh()
            sidebar_nav.refresh()
            middle_column.refresh()
            right_column.refresh()
            source_column.refresh()

        def select_setting(key):
            selected_setting["value"] = key
            if ui_refs["settings_list"]:
                ui_refs["settings_list"].refresh()
            right_column.refresh()

        def select_client(client_id):
            selected_client["value"] = client_id
            if ui_refs["client_list"]:
                ui_refs["client_list"].refresh()
            right_column.refresh()

        def select_skill(skill_id):
            selected_skill["value"] = skill_id
            if ui_refs.get("skill_list"):
                ui_refs["skill_list"].refresh()
            right_column.refresh()

        dark_mode = ui.dark_mode()
        init_theme(dark_mode, settings.appearance.theme)
        inject_theme_css()

        async def open_knowledge_source(workbench, service, return_view, evidence):
            from indexing.services.errors import BusinessError
            fresh = await workbench.call(service.get_record, kind="evidence", id=evidence["id"])
            if not fresh:
                return
            destination = local_source(fresh["record"], fresh["library_id"])
            if not destination:
                ui.notify(t("knowledge.source_unavailable"), type="warning")
                return
            state["knowledge_return"] = True
            state["knowledge_return_view"] = return_view
            await switch_view("files")
            open_reader()
            try:
                await file_handlers.load_chunks(destination[0], chunk_id=destination[1])
            except BusinessError:
                ui.notify(t("knowledge.source_unavailable"), type="warning")
                return
            await ui.run_javascript(f'''const target = document.querySelector(
                '.chunk-anchor[data-file-id="{destination[0]}"][data-chunk-id="{destination[1]}"]');
                if (target) {{
                    target.scrollIntoView({{block: 'center'}});
                    target.classList.add('source-target');
                    setTimeout(() => target.classList.remove('source-target'), 1800);
                }}''')
            page = fresh["record"].get("current_page_number")
            if page:
                await chunk_handlers.handle_view_source_page(page)

        async def open_wiki_source(evidence):
            from indexing.services import wiki_service
            await open_knowledge_source(wiki, wiki_service, "wiki", evidence)

        async def open_graph_source(evidence):
            from indexing.services import knowledge_service
            await open_knowledge_source(graph, knowledge_service, "graph", evidence)

        wiki = WikiWorkbench(open_source=open_wiki_source, show_view=lambda: switch_view("wiki"), dark=lambda: dark_mode.value)
        graph = GraphWorkbench(open_source=open_graph_source, show_view=lambda: switch_view("graph"), dark=lambda: dark_mode.value)
        ui_refs["wiki_references"] = wiki.file_references
        ui_refs["graph_references"] = graph.file_references
        ui_refs["return_knowledge"] = lambda: switch_view(state.get("knowledge_return_view") or "wiki")

        def remember_width(key, value):
            if key == "navigation_width":
                collapsed = value < MIN_NAVIGATION_WIDTH
                state["navigation_hidden"] = state["navigation_collapsed"] = collapsed
                # 图标态不覆盖上一次可读宽度，展开时不会把文字塞回窄栏。
                if not collapsed:
                    state[key] = value
                if sidebar := ui_refs.get("sidebar_container"):
                    sidebar.classes(add="navigation-collapsed" if collapsed else "",
                                    remove="navigation-collapsed" if not collapsed else "")
            elif value > 0:
                state[key] = value
                widths[current_view["value"]] = value

        def toggle_navigation():
            state["navigation_hidden"] = navigation_splitter.value >= MIN_NAVIGATION_WIDTH
            adapt_navigation()

        def focus_catalog():
            ui.run_javascript('''const catalog = document.querySelector('[data-catalog]');
                const target = catalog?.querySelector('[data-catalog-row][tabindex="0"]') || catalog?.querySelector('input');
                target?.focus({preventScroll: true});
                target?.scrollIntoView({block: 'nearest'});''')

        def focus_search():
            ui.run_javascript('''const input = document.querySelector('[data-catalog] .workspace-search input');
                input?.focus(); input?.select();''')

        def set_library_view(view):
            if view not in ("reading", "compare"):
                return
            state["library_view"] = view
            if view != "compare" and state.get("source_pane_open"):
                chunk_handlers.close_source_page()
            if view == "compare":
                state["source_pane_open"] = True
            if current_view["value"] != "files":
                return
            # 文档模式只影响右侧内容，保留资料表的宽度与显隐状态。
            if state.get("file_reading_mode") != "reading":
                chunk_handlers.set_file_reading_mode("reading")
            if toolbar := ui_refs.get("reading_mode_toggle"):
                toolbar.refresh()
            if view == "compare":
                source_column.refresh()

        def open_reader():
            set_library_view("reading")

        def open_library():
            results_splitter.value = widths["files"]

        async def open_task_file(file_id):
            await switch_view("files")
            if current_view["value"] == "files":
                await file_handlers.open_reader(file_id)

        ui_refs.update(toggle_navigation=toggle_navigation, focus_catalog=focus_catalog,
                       focus_search=focus_search, open_task_file=open_task_file,
                       open_reader=open_reader, open_library=open_library,
                       set_library_view=set_library_view)
        render_workspace_upload(ui_refs, file_handlers)
        header = render_app_header(current_view, switch_view, ui_refs, state=state, file_handlers=file_handlers)
        with ui.splitter(value=state["navigation_width"], limits=(NAVIGATION_RAIL_WIDTH, 380),
                         on_change=lambda e: remember_width("navigation_width", e.value)).props("unit=px").classes(
            "w-full app-body workspace-splitter"
        ) as navigation_splitter:
            with navigation_splitter.before:
                sidebar_nav = render_sidebar(current_view=current_view, ui_refs=ui_refs, state=state,
                                             file_handlers=file_handlers, switch_view=switch_view)
            with navigation_splitter.after:
                with ui.splitter(value=state["results_width"], limits=(MIN_RESULTS_WIDTH, 800),
                                 on_change=lambda e: remember_width("results_width", e.value)).props("unit=px").classes(
                    "w-full h-full workspace-splitter results-splitter"
                ) as results_splitter:
                    with results_splitter.before:
                        @ui.refreshable
                        def middle_column():
                            view = current_view["value"]
                            if view == "files":
                                render_files_middle(state=state, ui_refs=ui_refs, file_handlers=file_handlers)
                            elif view == "wiki":
                                wiki.render_middle()
                            elif view == "graph":
                                graph.render_middle()
                            elif view == "recall_test":
                                render_recall_test_middle(recall_state=recall_state, ui_refs=ui_refs)
                            elif view == "cloud_sync":
                                render_cloud_sync_middle(sync_state=sync_state, ui_refs=ui_refs, sync_handlers=sync_handlers)
                            elif view == "mcp_config":
                                render_mcp_config_middle(selected_client=selected_client, ui_refs=ui_refs, on_select_client=select_client)
                            elif view == "skills":
                                render_skill_middle(selected_skill=selected_skill, ui_refs=ui_refs, on_select_skill=select_skill)
                            elif view == "logs":
                                render_logs_middle(ui_refs=ui_refs)
                            else:
                                render_settings_middle(selected_setting=selected_setting, ui_refs=ui_refs, on_select_setting=select_setting)
                        middle_column()
                    with results_splitter.after, ui.row().classes("workspace-content w-full h-full gap-0 flex-nowrap overflow-hidden"):
                        @ui.refreshable
                        def right_column():
                            view = current_view["value"]
                            if view == "files":
                                render_files_right(state=state, ui_refs=ui_refs, chunk_handlers=chunk_handlers, file_handlers=file_handlers)
                            elif view in ("wiki", "graph"):
                                def controls():
                                    workspace_controls(ui_refs, include_navigation=False)
                                    if state.get("selected_file_id"):
                                        ui.button(icon="arrow_back", on_click=lambda: switch_view("files")).props(
                                            f'flat dense round size=sm aria-label="{t("knowledge.return_file")}"'
                                        ).classes("theme-text-muted").tooltip(t("knowledge.return_file"))
                                (wiki if view == "wiki" else graph).render_right(controls=controls)
                            elif view == "recall_test":
                                render_recall_test_right(recall_state=recall_state, ui_refs=ui_refs)
                            elif view == "cloud_sync":
                                render_cloud_sync_right(sync_state=sync_state, ui_refs=ui_refs, sync_handlers=sync_handlers)
                            elif view == "mcp_config":
                                render_mcp_config_right(selected_client=selected_client)
                            elif view == "skills":
                                render_skill_right(selected_skill=selected_skill, export_state=skill_export_state, port=port)
                            elif view == "logs":
                                render_logs_right(ui_refs=ui_refs)
                            else:
                                render_settings_right(selected_setting=selected_setting, settings_form=settings_form,
                                                      settings_handlers=settings_handlers,
                                                      apply_theme_callback=lambda theme: apply_theme(dark_mode, theme))
                        right_column()
                        @ui.refreshable
                        def source_column():
                            if current_view["value"] == "files":
                                render_files_source(state=state, ui_refs=ui_refs, chunk_handlers=chunk_handlers)
                        ui_refs["source_column"] = source_column
                        source_column()

        def adapt_navigation():
            collapsed = state["navigation_hidden"]
            state["navigation_collapsed"] = collapsed
            navigation_splitter.value = NAVIGATION_RAIL_WIDTH if collapsed else state["navigation_width"]
            if sidebar := ui_refs.get("sidebar_container"):
                sidebar.classes(add="navigation-collapsed" if collapsed else "",
                                remove="navigation-collapsed" if not collapsed else "")

        preferences_key = json.dumps(storage_key(settings.get_data_path()))

        async def init_async():
            nonlocal preferences_ready
            await context.client.connected()
            try:
                saved = await ui.run_javascript(
                    f'try {{ return JSON.parse(localStorage.getItem({preferences_key})); }} catch {{ return null; }}',
                    timeout=2.0,
                )
            except TimeoutError:
                saved = None
            restore_workspace(saved, state, widths)
            positions = {key: state.get(key, 0) for key in ("file_scroll", "tree_scroll", "chunk_scroll")}
            selected_file = state.get("selected_file_id")
            results_splitter.value = widths.get(current_view["value"], RESULTS_WIDTH)
            adapt_navigation()
            if saved and ui_refs.get("list_toolbar"):
                ui_refs["list_toolbar"].refresh()
            await file_handlers.load_files()
            # 只恢复已有滚动区域；重建整个侧栏会吞掉启动期间用户的导航点击。
            for key, position in (("file_scroll", positions["file_scroll"]), ("tree_scroll_area", positions["tree_scroll"])):
                area = ui_refs.get(key)
                if area is not None and not area.is_deleted:
                    area.scroll_to(pixels=position)
            if selected_file is not None and state.get("selected_file_id") == selected_file and not state["chunks_data"]:
                state["chunk_scroll"] = positions["chunk_scroll"]
                await file_handlers.load_chunks(selected_file)
            await task_handlers.init_active_tasks()
            preferences_ready = True
            state["workspace_ready"] = True
            control = ui_refs.get("file_search")
            if control is not None and not control.is_deleted:
                control.set_enabled(not bool(state.get("collection_edit")))
            if ui_refs.get("collection_filter"):
                ui_refs["collection_filter"].refresh()

        def remember_workspace():
            nonlocal last_preferences
            if not preferences_ready:
                return
            value = json.dumps(workspace_snapshot(state, widths), ensure_ascii=False)
            if value != last_preferences:
                last_preferences = value
                client.run_javascript(f'try {{ localStorage.setItem({preferences_key}, {json.dumps(value)}); }} catch {{}}')

        ui_refs["remember_workspace"] = remember_workspace
        ui.add_body_html("""<script>
            document.addEventListener('keydown', event => {
                if (event.defaultPrevented || event.isComposing || event.altKey || event.shiftKey ||
                    event.target.closest('.q-dialog, .q-menu')) return;
                if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
                    const input = document.querySelector('[data-catalog] .workspace-search input');
                    if (input) {
                        event.preventDefault();
                        if (!event.repeat) { input.focus(); input.select(); }
                    }
                }
            });
        </script>""")
        ui.timer(0.1, init_async, once=True)
        ui.timer(1.0, task_handlers.poll)
        ui.timer(1.0, sync_handlers.poll)
        ui.timer(1.0, remember_workspace)
