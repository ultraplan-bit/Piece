"""页面路由与可收起、可调宽的文件阅读布局。"""

from nicegui import ui

from app.i18n import t
from app.ui.views.knowledge_view import KnowledgeWorkbench
from app.ui.views.knowledge_presenter import local_source

from indexing.settings import get_settings
from app.ui.styles import inject_theme_css, init_theme, apply_theme
from app.ui.handlers import FileHandlers, ChunkHandlers, TaskHandlers, SettingsHandlers, SyncHandlers
from app.ui.views import (
    render_sidebar,
    render_files_middle,
    render_files_right,
    render_files_source,
    render_settings_middle,
    render_settings_right,
    render_mcp_config_middle,
    render_mcp_config_right,
    render_skill_middle,
    render_skill_right,
    render_cloud_sync_middle,
    render_cloud_sync_right,
    render_logs_middle,
    render_logs_right,
    render_recall_test_middle,
    render_recall_test_right,
)


def register_pages(port: int = 8689):
    """注册页面；port 用于 Skill 导出时注入 CLI 调用前缀。"""

    @ui.page("/")
    def main_page():
        current_view = {"value": "files"}
        selected_setting = {"value": None}
        selected_client = {"value": None}
        selected_skill = {"value": None}
        skill_export_state = {"export_dir": ""}
        state = {
            "selected_file_id": None,
            "search_keyword": "",
            "files_data": [],
            "filtered_files": [],
            "chunks_data": [],
            "chunk_page": 1,
            "chunk_page_size": 50,
            "chunk_scroll": 0,
            "source_pane_open": False,
            "source_page": None,
            "navigation_width": 21,
            "results_width": 36,
            "narrow": False,
            "navigation_before_narrow": None,
        }

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
            if current_view["value"] == "knowledge":
                await knowledge.remember_positions()
            current_view["value"] = view
            adapt_navigation()
            selected_setting["value"] = None
            selected_client["value"] = None
            if view == "settings":
                settings_handlers.init_settings_form()
            if view != "files" and results_splitter.value == 0:
                results_splitter.value = state["results_width"]
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

        async def open_knowledge_source(evidence):
            from indexing.services import knowledge_service
            from indexing.services.errors import BusinessError
            fresh = await knowledge.call(knowledge_service.get_record, kind="evidence", id=evidence["id"])
            if not fresh:
                return
            destination = local_source(fresh["record"], fresh["library_id"])
            if not destination:
                ui.notify(t("knowledge.source_unavailable"), type="warning")
                return
            state["knowledge_return"] = True
            await switch_view("files")
            try:
                await file_handlers.load_chunks(destination[0], chunk_id=destination[1])
            except BusinessError:
                ui.notify(t("knowledge.source_unavailable"), type="warning")
                return
            await ui.run_javascript(f'''document.querySelector(
                '.chunk-anchor[data-file-id="{destination[0]}"][data-chunk-id="{destination[1]}"]'
            )?.scrollIntoView({{block: 'center'}})''')
            page = fresh["record"].get("current_page_number")
            if page:
                await chunk_handlers.handle_view_source_page(page)

        knowledge = KnowledgeWorkbench(open_source=open_knowledge_source,
                                       show_knowledge=lambda: switch_view("knowledge"),
                                       dark=lambda: dark_mode.value)
        ui_refs["knowledge_references"] = knowledge.file_references
        ui_refs["return_knowledge"] = lambda: switch_view("knowledge")

        def remember_width(key, value):
            if value > 0:
                state[key] = value

        def toggle_navigation():
            navigation_splitter.value = 0 if navigation_splitter.value else state["navigation_width"]

        def toggle_results():
            results_splitter.value = 0 if results_splitter.value else state["results_width"]

        def toggle_reading():
            reading = navigation_splitter.value == 0 and results_splitter.value == 0
            navigation_splitter.value = state["navigation_width"] if reading else 0
            results_splitter.value = state["results_width"] if reading else 0

        ui_refs.update(toggle_navigation=toggle_navigation, toggle_results=toggle_results, toggle_reading=toggle_reading)

        with ui.splitter(value=state["navigation_width"], limits=(0, 50),
                         on_change=lambda e: remember_width("navigation_width", e.value)).classes(
            "w-full h-screen workspace-splitter"
        ) as navigation_splitter:
            with navigation_splitter.before:
                sidebar_nav = render_sidebar(
                    current_view=current_view,
                    switch_to_files=lambda: switch_view("files"),
                    switch_to_knowledge=lambda: switch_view("knowledge"),
                    switch_to_recall_test=lambda: switch_view("recall_test"),
                    switch_to_cloud_sync=lambda: switch_view("cloud_sync"),
                    switch_to_mcp_config=lambda: switch_view("mcp_config"),
                    switch_to_skills=lambda: switch_view("skills"),
                    switch_to_logs=lambda: switch_view("logs"),
                    switch_to_settings=lambda: switch_view("settings"),
                    ui_refs=ui_refs, state=state, file_handlers=file_handlers,
                )
            with navigation_splitter.after:
                with ui.splitter(value=state["results_width"], limits=(0, 75),
                                 on_change=lambda e: remember_width("results_width", e.value)).classes(
                    "w-full h-full workspace-splitter"
                ) as results_splitter:
                    with results_splitter.before:
                        @ui.refreshable
                        def middle_column():
                            view = current_view["value"]
                            if view == "files":
                                render_files_middle(state=state, ui_refs=ui_refs, file_handlers=file_handlers)
                            elif view == "knowledge":
                                knowledge.render_middle()
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

                    with results_splitter.after, ui.row().classes("w-full h-full gap-0 flex-nowrap overflow-hidden"):
                        @ui.refreshable
                        def right_column():
                            view = current_view["value"]
                            if view == "files":
                                render_files_right(state=state, ui_refs=ui_refs, chunk_handlers=chunk_handlers, file_handlers=file_handlers)
                            elif view == "knowledge":
                                with ui.column().classes("w-full h-full gap-0"):
                                    with ui.row().classes("px-3 py-1 gap-1"):
                                        ui.button(t("knowledge.navigation"), on_click=toggle_navigation).props("flat dense no-caps")
                                        ui.button(t("knowledge.results"), on_click=toggle_results).props("flat dense no-caps")
                                        ui.button(t("knowledge.reading"), on_click=toggle_reading).props("flat dense no-caps")
                                        if state.get("selected_file_id"):
                                            ui.button(t("knowledge.return_file"), on_click=lambda: switch_view("files")).props("flat dense no-caps")
                                    knowledge.render_right()
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
            previous = state["navigation_before_narrow"]
            if current_view["value"] == "knowledge" and state["narrow"]:
                if previous is None:
                    state["navigation_before_narrow"] = navigation_splitter.value
                    navigation_splitter.value = 0
            elif previous is not None:
                navigation_splitter.value = previous
                state["navigation_before_narrow"] = None

        def resize_workspace(event):
            state["narrow"] = event.args["width"] < 1000
            adapt_navigation()

        # 只调面板宽度，不重建知识正文、图或草稿；导航按钮仍可手动展开。
        ui.element("q-resize-observer").on("resize", resize_workspace)

        async def init_async():
            await file_handlers.load_files()
            await task_handlers.init_active_tasks()

        ui.timer(0.1, init_async, once=True)
        ui.timer(1.0, task_handlers.poll)
        ui.timer(1.0, sync_handlers.poll)
