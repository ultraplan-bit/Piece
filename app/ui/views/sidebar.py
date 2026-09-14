"""
侧边栏视图

职责:
- 渲染左侧导航栏
- Logo、导航按钮、统计信息
"""

from nicegui import ui

from app.i18n import t
from app.skills import get_version


def render_sidebar(
    current_view: dict,
    switch_to_files: callable,
    switch_to_recall_test: callable,
    switch_to_cloud_sync: callable,
    switch_to_mcp_config: callable,
    switch_to_skills: callable,
    switch_to_logs: callable,
    switch_to_settings: callable,
    ui_refs: dict,
):
    """
    渲染侧边栏

    Args:
        current_view: 当前视图状态 {"value": "files" | "recall_test" | "cloud_sync" | "mcp_config" | "skills" | "logs" | "settings"}
        switch_to_files: 切换到文件库的回调
        switch_to_recall_test: 切换到召回测试的回调
        switch_to_cloud_sync: 切换到云同步的回调
        switch_to_mcp_config: 切换到 MCP 配置的回调
        switch_to_skills: 切换到 Skill 导出的回调
        switch_to_logs: 切换到日志的回调
        switch_to_settings: 切换到设置的回调
        ui_refs: UI 组件引用字典（用于存储 stats_label）

    Returns:
        sidebar_nav: 可刷新的导航组件
    """
    with ui.column().classes(
        "w-44 h-full theme-sidebar gap-0"
    ).style("border-right: 1px solid var(--border-color)"):
        # Logo/标题
        with ui.row().classes("w-full px-4 items-center gap-2").style(
            "border-bottom: 1px solid var(--border-color); height: 49px"
        ):
            ui.icon("auto_stories", size="sm").classes("theme-text-accent")
            ui.label(t("app.name")).classes("text-base font-semibold theme-text")

        # 导航按钮
        with ui.column().classes("w-full gap-0.5 px-2 pt-2"):
            @ui.refreshable
            def sidebar_nav():
                is_files = current_view["value"] == "files"
                is_recall_test = current_view["value"] == "recall_test"
                is_cloud_sync = current_view["value"] == "cloud_sync"
                is_mcp_config = current_view["value"] == "mcp_config"
                is_skills = current_view["value"] == "skills"
                is_logs = current_view["value"] == "logs"
                is_settings = current_view["value"] == "settings"

                # 文件库
                files_classes = "w-full justify-start gap-2 px-3 py-1.5 rounded-md "
                if is_files:
                    files_classes += "theme-selected"
                else:
                    files_classes += "theme-hover"
                with ui.button(on_click=switch_to_files).props("flat no-caps align=left").classes(files_classes):
                    ui.icon("folder", size="xs").classes("theme-text-accent" if is_files else "theme-text-muted")
                    ui.label(t("sidebar.files")).classes("text-sm " + ("theme-text" if is_files else "theme-text-muted"))

                # 召回测试（紧跟文件库：改完卡片标题就在这里验证检索效果）
                recall_test_classes = "w-full justify-start gap-2 px-3 py-1.5 rounded-md "
                if is_recall_test:
                    recall_test_classes += "theme-selected"
                else:
                    recall_test_classes += "theme-hover"
                with ui.button(on_click=switch_to_recall_test).props("flat no-caps align=left").classes(recall_test_classes):
                    ui.icon("manage_search", size="xs").classes("theme-text-accent" if is_recall_test else "theme-text-muted")
                    ui.label(t("sidebar.recall_test")).classes("text-sm " + ("theme-text" if is_recall_test else "theme-text-muted"))

                # 云同步
                cloud_sync_classes = "w-full justify-start gap-2 px-3 py-1.5 rounded-md "
                if is_cloud_sync:
                    cloud_sync_classes += "theme-selected"
                else:
                    cloud_sync_classes += "theme-hover"
                with ui.button(on_click=switch_to_cloud_sync).props("flat no-caps align=left").classes(cloud_sync_classes):
                    ui.icon("cloud_sync", size="xs").classes("theme-text-accent" if is_cloud_sync else "theme-text-muted")
                    ui.label(t("sidebar.cloud_sync")).classes("text-sm " + ("theme-text" if is_cloud_sync else "theme-text-muted"))

                # MCP 配置
                mcp_config_classes = "w-full justify-start gap-2 px-3 py-1.5 rounded-md "
                if is_mcp_config:
                    mcp_config_classes += "theme-selected"
                else:
                    mcp_config_classes += "theme-hover"
                with ui.button(on_click=switch_to_mcp_config).props("flat no-caps align=left").classes(mcp_config_classes):
                    ui.icon("settings_input_component", size="xs").classes("theme-text-accent" if is_mcp_config else "theme-text-muted")
                    ui.label(t("sidebar.mcp_config")).classes("text-sm " + ("theme-text" if is_mcp_config else "theme-text-muted"))

                # Skill 导出
                skills_classes = "w-full justify-start gap-2 px-3 py-1.5 rounded-md "
                if is_skills:
                    skills_classes += "theme-selected"
                else:
                    skills_classes += "theme-hover"
                with ui.button(on_click=switch_to_skills).props("flat no-caps align=left").classes(skills_classes):
                    ui.icon("extension", size="xs").classes("theme-text-accent" if is_skills else "theme-text-muted")
                    ui.label(t("sidebar.skills")).classes("text-sm " + ("theme-text" if is_skills else "theme-text-muted"))

                # 日志
                logs_classes = "w-full justify-start gap-2 px-3 py-1.5 rounded-md "
                if is_logs:
                    logs_classes += "theme-selected"
                else:
                    logs_classes += "theme-hover"
                with ui.button(on_click=switch_to_logs).props("flat no-caps align=left").classes(logs_classes):
                    ui.icon("description", size="xs").classes("theme-text-accent" if is_logs else "theme-text-muted")
                    ui.label(t("sidebar.logs")).classes("text-sm " + ("theme-text" if is_logs else "theme-text-muted"))

                # 设置
                settings_classes = "w-full justify-start gap-2 px-3 py-1.5 rounded-md "
                if is_settings:
                    settings_classes += "theme-selected"
                else:
                    settings_classes += "theme-hover"
                with ui.button(on_click=switch_to_settings).props("flat no-caps align=left").classes(settings_classes):
                    ui.icon("settings", size="xs").classes("theme-text-accent" if is_settings else "theme-text-muted")
                    ui.label(t("sidebar.settings")).classes("text-sm " + ("theme-text" if is_settings else "theme-text-muted"))

            sidebar_nav()

        # 底部：统计信息 + 版本号（安装元数据单一来源）
        ui.space()
        with ui.column().classes("w-full px-4 gap-0.5 pb-2"):
            with ui.row().classes("items-center gap-1"):
                ui.icon("storage", size="xs").classes("theme-text-muted")
                stats_label = ui.label(t("stats.loading")).classes("text-xs theme-text-muted")
                ui_refs["stats_label"] = stats_label
            ui.label(f"v{get_version()}").classes("text-xs theme-text-muted")

    return sidebar_nav
