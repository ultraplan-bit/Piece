"""
页面定义模块

职责:
- 定义所有 NiceGUI 页面路由
- 三栏式主从视图布局（支持文件库/设置视图切换）
"""

from nicegui import ui

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
    """注册所有页面路由；port 用于 Skill 导出时注入 CLI 调用前缀"""

    @ui.page("/")
    def main_page():
        """主页面 - 三栏式文件管理界面"""

        # ========== 状态管理 ==========
        # 视图状态
        current_view = {"value": "files"}  # 'files' | 'recall_test' | 'cloud_sync' | 'mcp_config' | 'skills' | 'logs' | 'settings'
        selected_setting = {"value": None}  # 'appearance' | 'embedding' | 'mcp' | 'storage' | 'webdav'
        selected_client = {"value": None}  # MCP 客户端 ID
        selected_skill = {"value": None}  # Skill ID
        # Skill 导出目录（切走再切回保留）
        skill_export_state = {"export_dir": ""}

        # 文件库状态
        state = {
            "selected_file_id": None,
            "search_keyword": "",
            "files_data": [],
            "filtered_files": [],
            "chunks_data": [],
            # 切片分页状态
            "chunk_page": 1,
            "chunk_page_size": 50,
            # 原页对比栏：source_pane_open 是用户意图（跨文件保留），
            # source_page 是当前展示的那一页 {"url", "caption", "page"}
            "source_pane_open": False,
            "source_page": None,
        }

        # 云同步状态
        # 从配置文件读取上次同步时间
        from datetime import datetime
        settings = get_settings()
        last_sync_time = settings.webdav.last_sync_time
        last_sync_display = None
        if last_sync_time:
            try:
                # 将 ISO 格式转为显示格式
                dt = datetime.fromisoformat(last_sync_time)
                last_sync_display = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass

        sync_state = {
            "is_syncing": False,
            "last_sync": last_sync_display,
            "logs": [],
        }

        # 召回测试状态（放在页面级：切走再切回来不丢查询和上次结果）
        recall_state = {
            "query": "",
            "filenames": "",
            "collection_names": [],
            "is_running": False,
            "has_run": False,
            "results": [],
            "stats": None,
            "error": None,
        }

        # 设置表单数据
        settings_form = {}

        # UI 组件引用（用于跨函数刷新）
        ui_refs: dict = {
            "upload_input": None,
            "file_list_container": None,
            "collection_filter": None,
            "file_info_panel": None,
            "settings_list": None,
            "client_list": None,
            "chunk_inspector": None,
            "source_column": None,
            "stats_label": None,
        }

        # ========== 初始化处理器 ==========
        # 长任务（索引、云同步）都跑在服务进程里，界面只订阅状态：
        # task_handlers 订阅任务表，sync_handlers 轮询同步服务，谁创建的任务无所谓
        task_handlers = TaskHandlers(state, ui_refs)
        settings_handlers = SettingsHandlers(settings_form)

        file_handlers = FileHandlers(state=state, ui_refs=ui_refs)

        # 任务完成后由 task_handlers 复用文件列表的加载逻辑
        task_handlers.set_file_handlers(file_handlers)

        sync_handlers = SyncHandlers(sync_state, ui_refs)

        chunk_handlers = ChunkHandlers(
            state=state,
            ui_refs=ui_refs,
            on_refresh_files=file_handlers.load_files,
        )

        # 任务完成后由 task_handlers 复用切片的分页重载逻辑
        task_handlers.set_chunk_handlers(chunk_handlers)

        # 切换文件后由 file_handlers 把原页对比栏对齐到新文件
        file_handlers.set_chunk_handlers(chunk_handlers)

        # ========== 视图切换 ==========

        def switch_to_files():
            """切换到文件库视图"""
            current_view["value"] = "files"
            selected_setting["value"] = None
            selected_client["value"] = None
            sidebar_nav.refresh()
            middle_column.refresh()
            right_column.refresh()

        def switch_to_recall_test():
            """切换到召回测试视图"""
            current_view["value"] = "recall_test"
            selected_setting["value"] = None
            selected_client["value"] = None
            sidebar_nav.refresh()
            middle_column.refresh()
            right_column.refresh()

        def switch_to_cloud_sync():
            """切换到云同步视图"""
            current_view["value"] = "cloud_sync"
            selected_setting["value"] = None
            selected_client["value"] = None
            sidebar_nav.refresh()
            middle_column.refresh()
            right_column.refresh()

        def switch_to_mcp_config():
            """切换到 MCP 配置视图"""
            current_view["value"] = "mcp_config"
            selected_setting["value"] = None
            selected_client["value"] = None
            sidebar_nav.refresh()
            middle_column.refresh()
            right_column.refresh()

        def switch_to_skills():
            """切换到 Skill 导出视图"""
            current_view["value"] = "skills"
            selected_setting["value"] = None
            selected_client["value"] = None
            sidebar_nav.refresh()
            middle_column.refresh()
            right_column.refresh()

        def switch_to_logs():
            """切换到日志视图"""
            current_view["value"] = "logs"
            selected_setting["value"] = None
            selected_client["value"] = None
            sidebar_nav.refresh()
            middle_column.refresh()
            right_column.refresh()

        def switch_to_settings():
            """切换到设置视图"""
            current_view["value"] = "settings"
            selected_setting["value"] = None
            selected_client["value"] = None
            settings_handlers.init_settings_form()
            sidebar_nav.refresh()
            middle_column.refresh()
            right_column.refresh()

        def select_setting(setting_key: str):
            """选择设置分类"""
            selected_setting["value"] = setting_key
            if ui_refs["settings_list"]:
                ui_refs["settings_list"].refresh()
            right_column.refresh()

        def select_client(client_id: str):
            """选择 MCP 客户端"""
            selected_client["value"] = client_id
            if ui_refs["client_list"]:
                ui_refs["client_list"].refresh()
            right_column.refresh()

        def select_skill(skill_id: str):
            """选择 Skill"""
            selected_skill["value"] = skill_id
            if ui_refs["skill_list"]:
                ui_refs["skill_list"].refresh()
            right_column.refresh()

        # ========== 主题初始化 ==========
        dark_mode = ui.dark_mode()
        settings = get_settings()
        current_theme_value = settings.appearance.theme

        def on_apply_theme(theme: str):
            """应用主题回调"""
            apply_theme(dark_mode, theme)

        # 初始化主题
        init_theme(dark_mode, current_theme_value)

        # 注入主题 CSS
        inject_theme_css()

        # ========== 页面布局 ==========
        with ui.row().classes("w-full h-screen overflow-hidden gap-0"):

            # ========== 左栏: 侧边栏导航 ==========
            sidebar_nav = render_sidebar(
                current_view=current_view,
                switch_to_files=switch_to_files,
                switch_to_recall_test=switch_to_recall_test,
                switch_to_cloud_sync=switch_to_cloud_sync,
                switch_to_mcp_config=switch_to_mcp_config,
                switch_to_skills=switch_to_skills,
                switch_to_logs=switch_to_logs,
                switch_to_settings=switch_to_settings,
                ui_refs=ui_refs,
            )

            # ========== 中栏 ==========
            @ui.refreshable
            def middle_column():
                if current_view["value"] == "files":
                    render_files_middle(
                        state=state,
                        ui_refs=ui_refs,
                        file_handlers=file_handlers,
                    )
                elif current_view["value"] == "recall_test":
                    render_recall_test_middle(
                        recall_state=recall_state,
                        ui_refs=ui_refs,
                    )
                elif current_view["value"] == "cloud_sync":
                    render_cloud_sync_middle(
                        sync_state=sync_state,
                        ui_refs=ui_refs,
                        sync_handlers=sync_handlers,
                    )
                elif current_view["value"] == "mcp_config":
                    render_mcp_config_middle(
                        selected_client=selected_client,
                        ui_refs=ui_refs,
                        on_select_client=select_client,
                    )
                elif current_view["value"] == "skills":
                    render_skill_middle(
                        selected_skill=selected_skill,
                        ui_refs=ui_refs,
                        on_select_skill=select_skill,
                    )
                elif current_view["value"] == "logs":
                    render_logs_middle(ui_refs=ui_refs)
                else:
                    render_settings_middle(
                        selected_setting=selected_setting,
                        ui_refs=ui_refs,
                        on_select_setting=select_setting,
                    )

            middle_column()

            # ========== 右栏 ==========
            @ui.refreshable
            def right_column():
                if current_view["value"] == "files":
                    render_files_right(
                        state=state,
                        ui_refs=ui_refs,
                        chunk_handlers=chunk_handlers,
                        file_handlers=file_handlers,
                    )
                elif current_view["value"] == "recall_test":
                    render_recall_test_right(
                        recall_state=recall_state,
                        ui_refs=ui_refs,
                    )
                elif current_view["value"] == "cloud_sync":
                    render_cloud_sync_right(
                        sync_state=sync_state,
                        ui_refs=ui_refs,
                        sync_handlers=sync_handlers,
                    )
                elif current_view["value"] == "mcp_config":
                    render_mcp_config_right(
                        selected_client=selected_client,
                    )
                elif current_view["value"] == "skills":
                    render_skill_right(
                        selected_skill=selected_skill,
                        export_state=skill_export_state,
                        port=port,
                    )
                elif current_view["value"] == "logs":
                    render_logs_right(ui_refs=ui_refs)
                else:
                    render_settings_right(
                        selected_setting=selected_setting,
                        settings_form=settings_form,
                        settings_handlers=settings_handlers,
                        apply_theme_callback=on_apply_theme,
                    )

            right_column()

            # ========== 原页对比栏（仅文件库视图，选中原页后才占位）==========
            @ui.refreshable
            def source_column():
                if current_view["value"] != "files":
                    return
                render_files_source(
                    state=state,
                    ui_refs=ui_refs,
                    chunk_handlers=chunk_handlers,
                )

            ui_refs["source_column"] = source_column
            source_column()

        # ========== 初始化 ==========
        async def init_async():
            """异步初始化"""
            await file_handlers.load_files()
            await task_handlers.init_active_tasks()  # 装入进行中的任务并定下订阅起点

        ui.timer(0.1, init_async, once=True)  # 延迟异步初始化
        # 订阅服务侧状态：任务表增量、云同步进度。定时器随页面销毁，服务不受影响
        ui.timer(1.0, task_handlers.poll)
        ui.timer(1.0, sync_handlers.poll)
