"""
设置视图

职责:
- 渲染设置中栏（设置分类列表）
- 渲染设置右栏（各类设置表单）
"""

from nicegui import ui

from app.i18n import t, SUPPORTED_LANGUAGES
from app.utils import open_external
from indexing.mcp.config import get_mcp_port
from indexing.settings import MINERU_LANGUAGES

# PaddleOCR 官方云服务（aistudio-app.com）的 API Key 在这里申请
PADDLE_TOKEN_URL = "https://aistudio.baidu.com/account/accessToken"

# MinerU 的 Token 申请入口与接口说明同页
MINERU_TOKEN_URL = "https://mineru.net/apiManage/docs"


def render_settings_middle(
    selected_setting: dict,
    ui_refs: dict,
    on_select_setting: callable,
):
    """
    渲染设置中栏

    Args:
        selected_setting: 选中的设置分类 {"value": str | None}
        ui_refs: UI 组件引用字典
        on_select_setting: 选择设置分类的回调
    """
    with ui.column().classes(
        "w-64 h-full flex flex-col overflow-hidden theme-panel gap-0"
    ).style("border-right: 1px solid var(--border-color)"):
        # 顶部标题栏
        with ui.row().classes(
            "w-full px-3 items-center justify-between"
        ).style("border-bottom: 1px solid var(--border-color); height: 49px"):
            ui.label(t("settings.title")).classes("text-sm font-medium theme-text")

        # 设置分类列表（同文件列表：包一层自带 gap 的 column，对齐左栏导航）
        with ui.scroll_area().classes("flex-1 scroll-flush"):
            @ui.refreshable
            def settings_list():
                settings_items = [
                    ("appearance", "palette", t("settings.appearance")),
                    ("embedding", "smart_toy", t("settings.embedding")),
                    ("mcp", "hub", t("settings.mcp")),
                    ("storage", "folder_open", t("settings.storage")),
                    ("webdav", "cloud_sync", t("settings.webdav")),
                    ("ocr", "document_scanner", t("settings_ocr.title")),
                    ("office", "description", t("settings_office.title")),
                ]

                with ui.column().classes("w-full gap-0.5 px-2 pt-2"):
                    for key, icon, label in settings_items:
                        is_selected = selected_setting["value"] == key
                        container_classes = "w-full px-3 py-2 cursor-pointer transition-colors rounded-md "
                        if is_selected:
                            container_classes += "theme-selected"
                        else:
                            container_classes += "theme-hover"

                        with ui.element("div").classes(container_classes).on(
                            "click", lambda _, k=key: on_select_setting(k)
                        ):
                            with ui.row().classes("items-center gap-2"):
                                ui.icon(icon, size="xs").classes(
                                    "theme-text-accent" if is_selected else "theme-text-muted"
                                )
                                ui.label(label).classes("text-sm theme-text")

            ui_refs["settings_list"] = settings_list
            settings_list()


def render_settings_right(
    selected_setting: dict,
    settings_form: dict,
    settings_handlers,
    apply_theme_callback: callable,
):
    """
    渲染设置右栏

    Args:
        selected_setting: 选中的设置分类
        settings_form: 设置表单数据
        settings_handlers: 设置处理器实例
        apply_theme_callback: 应用主题的回调
    """
    with ui.column().classes("flex-1 h-full flex flex-col theme-content min-w-0 gap-0"):
        # 顶部信息区
        setting_titles = {
            "appearance": ("palette", t("settings_appearance.title")),
            "embedding": ("smart_toy", t("settings_embedding.title")),
            "mcp": ("hub", t("settings_mcp.title")),
            "storage": ("folder_open", t("settings_storage.title")),
            "webdav": ("cloud_sync", t("settings_webdav.title")),
            "ocr": ("document_scanner", t("settings_ocr.title")),
            "office": ("description", t("settings_office.title")),
        }

        if selected_setting["value"] and selected_setting["value"] in setting_titles:
            icon, title = setting_titles[selected_setting["value"]]
        else:
            icon, title = "settings", t("settings.title")

        with ui.row().classes(
            "w-full px-5 items-center justify-between theme-sidebar"
        ).style("border-bottom: 1px solid var(--border-color); height: 49px"):
            with ui.row().classes("items-center gap-2"):
                ui.icon(icon, size="xs").classes("theme-text-accent")
                ui.label(title).classes("text-sm font-medium theme-text")

        # 设置内容区
        with ui.scroll_area().classes("flex-1 min-w-0"):
            if selected_setting["value"] is None:
                with ui.column().classes("w-full h-full items-center justify-center"):
                    ui.icon("tune", size="lg").classes("theme-text-muted")
                    ui.label(t("settings.select_item")).classes("text-sm mt-2 theme-text-muted")
            elif selected_setting["value"] == "appearance":
                _render_appearance_settings(settings_form, settings_handlers, apply_theme_callback)
            elif selected_setting["value"] == "embedding":
                _render_embedding_settings(settings_form, settings_handlers)
            elif selected_setting["value"] == "mcp":
                _render_mcp_settings(settings_form, settings_handlers)
            elif selected_setting["value"] == "storage":
                _render_storage_settings(settings_form, settings_handlers)
            elif selected_setting["value"] == "webdav":
                _render_webdav_settings(settings_form, settings_handlers)
            elif selected_setting["value"] == "ocr":
                _render_ocr_settings(settings_form, settings_handlers)
            elif selected_setting["value"] == "office":
                _render_office_settings(settings_form, settings_handlers)


def _render_appearance_settings(settings_form: dict, settings_handlers, apply_theme_callback: callable):
    """渲染基础设置表单"""
    with ui.card().tight().classes("w-full theme-card").style("border: 1px solid var(--border-color)"):
        with ui.column().classes("w-full gap-3 p-3"):
            # 主题设置
            ui.label(t("settings_appearance.theme")).classes("text-sm font-medium theme-text")

            current_theme = settings_form.get("theme", "light")

            def on_theme_change(e):
                settings_form["theme"] = e.value
                # 立即应用主题
                apply_theme_callback(e.value)

            ui.toggle(
                options={
                    "light": t("settings_appearance.theme_light"),
                    "dark": t("settings_appearance.theme_dark"),
                    "pink": t("settings_appearance.theme_pink")
                },
                value=current_theme,
                on_change=on_theme_change,
            ).props("no-caps").classes("theme-text")

            ui.separator()

            # 语言设置
            ui.label(t("settings_appearance.language")).classes("text-sm font-medium theme-text")

            current_language = settings_form.get("language", "zh")

            def on_language_change(e):
                settings_form["language"] = e.value

            ui.toggle(
                options=SUPPORTED_LANGUAGES,
                value=current_language,
                on_change=on_language_change,
            ).props("no-caps").classes("theme-text")

            ui.label(t("settings_appearance.language_hint")).classes("text-xs theme-text-muted")

            ui.separator()

            with ui.row().classes("w-full justify-end"):
                ui.button(t("settings.btn_save"), on_click=settings_handlers.save_settings_form).props("color=primary")


def _render_embedding_settings(settings_form: dict, settings_handlers):
    """渲染嵌入模型设置表单（模型配置 + 索引性能，同属嵌入服务，合并为一页）"""
    with ui.card().tight().classes("w-full theme-card").style("border: 1px solid var(--border-color)"):
        with ui.column().classes("w-full gap-3 p-3"):
            ui.label(t("settings_embedding.section_model")).classes("text-sm font-medium theme-text")

            ui.input(
                label=t("settings_embedding.base_url"),
                value=settings_form.get("base_url", ""),
                on_change=lambda e: settings_form.update({"base_url": e.value}),
            ).props("dense outlined").classes("w-full")

            ui.input(
                label=t("settings_embedding.api_key"),
                value=settings_form.get("api_key", ""),
                password=True,
                password_toggle_button=True,
                on_change=lambda e: settings_form.update({"api_key": e.value}),
            ).props("dense outlined").classes("w-full")

            ui.input(
                label=t("settings_embedding.model"),
                value=settings_form.get("model", ""),
                on_change=lambda e: settings_form.update({"model": e.value}),
            ).props("dense outlined").classes("w-full")

            ui.number(
                label=t("settings_embedding.vector_dim"),
                value=settings_form.get("vector_dim", 1024),
                min=1,
                max=4096,
                on_change=lambda e: settings_form.update({"vector_dim": e.value}),
            ).props("dense outlined").classes("w-full")

            ui.number(
                label=t("settings_embedding.max_tokens"),
                value=settings_form.get("max_tokens", 8192),
                min=512,
                max=32768,
                step=512,
                on_change=lambda e: settings_form.update({"max_tokens": e.value}),
            ).props("dense outlined").classes("w-full").tooltip(
                t("settings_embedding.max_tokens_tooltip")
            )

            # 测试连接
            with ui.row().classes("w-full items-center justify-between"):
                test_result = ui.label("").classes("text-xs flex-1 theme-text-muted")
                test_btn = ui.button(
                    t("settings_embedding.test_btn"),
                    on_click=lambda: settings_handlers.test_embedding_connection(test_result, test_btn)
                ).props("dense outline size=sm").classes("theme-text-accent")

            ui.separator()

            # 索引性能：嵌入限流与并发，跟着上面的模型配置一起调
            ui.label(t("settings_performance.title")).classes("text-sm font-medium theme-text")

            _render_performance_fields(settings_form)

            ui.separator()

            with ui.row().classes("w-full justify-end"):
                ui.button(t("settings.btn_save"), on_click=settings_handlers.save_settings_form).props("color=primary")


def _render_performance_fields(settings_form: dict):
    """索引性能字段（嵌入限流与并发）"""
    fields = [
        ("embedding_tpm", "tpm", 400000, 1000, 10000000, 10000),
        ("embedding_rpm", "rpm", 200, 1, 5000, 10),
        ("embedding_concurrency", "concurrency", 4, 1, 32, 1),
        ("embedding_batch_size", "batch_size", 10, 1, 64, 1),
        ("worker_concurrency", "worker_concurrency", 2, 1, 8, 1),
    ]

    with ui.column().classes("w-full gap-3"):
        ui.label(t("settings_performance.hint")).classes("text-xs theme-text-muted")

        for key, label_key, default, minimum, maximum, step in fields:
            ui.number(
                label=t(f"settings_performance.{label_key}"),
                value=settings_form.get(key, default),
                min=minimum,
                max=maximum,
                step=step,
                on_change=lambda e, k=key: settings_form.update({k: e.value}),
            ).props("dense outlined").classes("w-full").tooltip(
                t(f"settings_performance.{label_key}_tooltip")
            )


def _render_mcp_settings(settings_form: dict, settings_handlers):
    """渲染 MCP 服务设置表单"""
    with ui.card().tight().classes("w-full theme-card").style("border: 1px solid var(--border-color)"):
        with ui.column().classes("w-full gap-3 p-3"):
            def on_mcp_port_change(e):
                settings_form["mcp_port"] = e.value
                index_port_input.value = get_mcp_port(int(e.value or 0))

            ui.number(
                label=t("settings_mcp.retrieval_port"),
                value=settings_form.get("mcp_port", 8686),
                min=1024,
                max=65535,
                on_change=on_mcp_port_change,
            ).props("dense outlined").classes("w-full")

            ui.label(t("settings_mcp.retrieval_port_hint")).classes("text-xs theme-text-muted")

            index_port_input = ui.number(
                label=t("settings_mcp.index_port"),
                value=get_mcp_port(int(settings_form.get("mcp_port", 8686))),
                min=1024,
                max=65535,
            ).props("dense outlined readonly").classes("w-full")

            ui.label(t("settings_mcp.index_port_hint")).classes("text-xs theme-text-muted")

            ui.separator()

            # 两个服务分别复制、轮换密钥，避免把写权限交给只读客户端。
            _render_mcp_api_key(settings_form, settings_handlers, "retrieval")
            _render_mcp_api_key(settings_form, settings_handlers, "index")
            ui.label(t("settings_mcp.key_hint")).classes("text-xs theme-text-muted")

            ui.separator()

            # 启用/禁用密钥验证
            ui.label(t("settings_mcp.auth_enabled")).classes("text-sm font-medium theme-text")

            current_auth_enabled = settings_form.get("mcp_auth_enabled", True)

            def on_auth_enabled_change(e):
                settings_form["mcp_auth_enabled"] = e.value

            ui.switch(
                value=current_auth_enabled,
                on_change=on_auth_enabled_change,
            ).classes("theme-text")

            ui.label(t("settings_mcp.auth_enabled_hint")).classes("text-xs theme-text-muted")

            ui.separator()

            # 检索结果是否附带知识卡片中的插图
            ui.label(t("settings_mcp.include_images")).classes("text-sm font-medium theme-text")

            def on_include_images_change(e):
                settings_form["mcp_include_images_default"] = e.value
                max_images_input.set_visibility(e.value)

            ui.switch(
                value=settings_form.get("mcp_include_images_default", False),
                on_change=on_include_images_change,
            ).classes("theme-text")

            ui.label(t("settings_mcp.include_images_hint")).classes("text-xs theme-text-muted")

            max_images_input = ui.number(
                label=t("settings_mcp.max_images"),
                value=settings_form.get("mcp_max_images_per_call", 6),
                min=1,
                max=20,
                on_change=lambda e: settings_form.update({"mcp_max_images_per_call": e.value}),
            ).props("dense outlined").classes("w-full")
            max_images_input.set_visibility(
                settings_form.get("mcp_include_images_default", False)
            )

            ui.separator()

            with ui.row().classes("w-full justify-end"):
                ui.button(t("settings.btn_save"), on_click=settings_handlers.save_settings_form).props("color=primary")


def _render_mcp_api_key(settings_form: dict, settings_handlers, service: str):
    """一项密钥及其操作，独立作用域避免两个按钮引用同一个输入框。"""
    field = {"retrieval": "mcp_api_key", "index": "mcp_index_api_key"}[service]
    ui.label(t(f"settings_mcp.{service}_api_key")).classes("text-sm font-medium theme-text")
    api_key_input = ui.input(
        value=settings_form.get(field, ""),
        on_change=lambda e: settings_form.update({field: e.value}),
    ).props("dense outlined readonly").classes("w-full")

    with ui.row().classes("w-full items-center gap-2"):
        async def copy_api_key():
            import json
            key = settings_form.get(field, "")
            await ui.run_javascript(f'navigator.clipboard.writeText({json.dumps(key)})')
            ui.notify(t("settings_mcp.key_copied"), type="positive")

        ui.button(
            t("settings_mcp.copy_key"), icon="content_copy", on_click=copy_api_key,
        ).props("flat dense size=sm").classes("theme-text-accent")
        ui.button(
            t("settings_mcp.regenerate_key"), icon="refresh",
            on_click=lambda: settings_handlers.regenerate_mcp_api_key(api_key_input, service),
        ).props("flat dense size=sm").classes("theme-text-accent")


def _render_storage_settings(settings_form: dict, settings_handlers):
    """渲染数据存储设置表单"""
    with ui.card().tight().classes("w-full theme-card").style("border: 1px solid var(--border-color)"):
        with ui.column().classes("w-full gap-3 p-3"):
            ui.input(
                label=t("settings_storage.data_path"),
                value=settings_form.get("data_path", "./data"),
                on_change=lambda e: settings_form.update({"data_path": e.value}),
            ).props("dense outlined").classes("w-full")

            ui.label(t("settings_storage.data_path_hint")).classes("text-xs theme-text-muted")

            ui.separator()

            with ui.row().classes("w-full justify-end"):
                ui.button(t("settings.btn_save"), on_click=settings_handlers.save_settings_form).props("color=primary")


def _render_ocr_settings(settings_form: dict, settings_handlers):
    """渲染 PDF 解析设置表单（PaddleOCR 服务 / 自定义多模态模型）"""
    with ui.card().tight().classes("w-full theme-card").style("border: 1px solid var(--border-color)"):
        with ui.column().classes("w-full gap-3 p-3"):
            # 解析后端选择：切换时只刷新下方字段区，避免整页重建
            ui.label(t("settings_ocr.provider")).classes("text-sm font-medium theme-text")

            @ui.refreshable
            def provider_fields():
                provider = settings_form.get("ocr_provider", "paddle")
                if provider == "vlm":
                    _render_ocr_vlm_fields(settings_form)
                elif provider == "mineru":
                    _render_ocr_mineru_fields(settings_form)
                else:
                    _render_ocr_paddle_fields(settings_form)

            def on_provider_change(e):
                settings_form["ocr_provider"] = e.value
                provider_fields.refresh()

            ui.toggle(
                options={
                    "paddle": t("settings_ocr.provider_paddle"),
                    "vlm": t("settings_ocr.provider_vlm"),
                    "mineru": t("settings_ocr.provider_mineru"),
                },
                value=settings_form.get("ocr_provider", "paddle"),
                on_change=on_provider_change,
            ).props("dense unelevated no-caps").classes("theme-text")

            ui.separator()

            provider_fields()

            # 测试连接
            with ui.row().classes("w-full items-center justify-between"):
                test_result = ui.label("").classes("text-xs flex-1 theme-text-muted")
                test_btn = ui.button(
                    t("settings_ocr.test_btn"),
                    on_click=lambda: settings_handlers.test_ocr_connection(test_result, test_btn)
                ).props("dense outline size=sm").classes("theme-text-accent")

            ui.separator()

            with ui.row().classes("w-full justify-end"):
                ui.button(t("settings.btn_save"), on_click=settings_handlers.save_settings_form).props("color=primary")


def _render_ocr_paddle_fields(settings_form: dict):
    """PaddleOCR 兼容服务的配置字段"""
    with ui.column().classes("w-full gap-3"):
        # 服务地址
        ui.input(
            label=t("settings_ocr.base_url"),
            value=settings_form.get("ocr_base_url", ""),
            on_change=lambda e: settings_form.update({"ocr_base_url": e.value}),
        ).props("dense outlined").classes("w-full")

        ui.label(t("settings_ocr.base_url_hint")).classes("text-xs theme-text-muted")

        # API Key
        ui.input(
            label=t("settings_ocr.api_key"),
            value=settings_form.get("ocr_api_key", ""),
            password=True,
            password_toggle_button=True,
            on_change=lambda e: settings_form.update({"ocr_api_key": e.value}),
        ).props("dense outlined").classes("w-full")

        # 官方云服务的 Key 要去百度 AI Studio 的访问令牌页取，给个直达入口
        ui.button(
            t("settings_ocr.api_key_help"),
            on_click=lambda: open_external(PADDLE_TOKEN_URL),
        ).props("flat dense no-caps size=sm icon-right=open_in_new").classes(
            "self-start px-1 text-xs theme-text-accent"
        ).tooltip(PADDLE_TOKEN_URL)

        # 模型
        ui.select(
            options=["PaddleOCR-VL", "PaddleOCR-VL-1.5", "PP-StructureV3"],
            with_input=True,
            value=settings_form.get("ocr_model", "PaddleOCR-VL"),
            label=t("settings_ocr.model"),
            on_change=lambda e: settings_form.update({"ocr_model": e.value}),
        ).props("dense outlined").classes("w-full")

        ui.label(t("settings_ocr.hint")).classes("text-xs theme-text-muted")

        ui.separator()

        # 解析参数（optionalPayload）：三态开关 None=跟随服务端默认
        ui.label(t("settings_ocr.payload_title")).classes("text-sm font-medium theme-text")

        def _tri_state_switch(form_key: str):
            """三态选择：default(None) / on(True) / off(False)"""
            ui.select(
                options={
                    "default": t("settings_ocr.payload_state_default"),
                    "on": t("settings_ocr.payload_state_on"),
                    "off": t("settings_ocr.payload_state_off"),
                },
                value=settings_form.get(form_key, "default"),
                label=t(f"settings_ocr.{form_key}"),
                on_change=lambda e, key=form_key: settings_form.update({key: e.value}),
            ).props("dense outlined").classes("w-full")

        with ui.row().classes("w-full gap-4"):
            with ui.column().classes("flex-1 gap-3"):
                _tri_state_switch("ocr_use_doc_unwarping")
                _tri_state_switch("ocr_use_doc_orientation_classify")
            with ui.column().classes("flex-1 gap-3"):
                _tri_state_switch("ocr_use_chart_recognition")
                _tri_state_switch("ocr_use_seal_recognition")

        _tri_state_switch("ocr_use_ocr_for_image_block")

        # 版面标签过滤：逗号分隔自由输入（如 header,footer）
        ui.input(
            label=t("settings_ocr.markdown_ignore_labels"),
            value=settings_form.get("ocr_markdown_ignore_labels", ""),
            on_change=lambda e: settings_form.update({"ocr_markdown_ignore_labels": e.value}),
        ).props("dense outlined").classes("w-full")

        ui.label(t("settings_ocr.payload_hint")).classes("text-xs theme-text-muted")


def _render_ocr_vlm_fields(settings_form: dict):
    """自定义多模态模型的配置字段"""
    with ui.column().classes("w-full gap-3"):
        # 服务地址
        ui.input(
            label=t("settings_ocr.vlm_base_url"),
            value=settings_form.get("ocr_vlm_base_url", ""),
            on_change=lambda e: settings_form.update({"ocr_vlm_base_url": e.value}),
        ).props("dense outlined").classes("w-full")

        ui.label(t("settings_ocr.vlm_base_url_hint")).classes("text-xs theme-text-muted")

        # API Key
        ui.input(
            label=t("settings_ocr.vlm_api_key"),
            value=settings_form.get("ocr_vlm_api_key", ""),
            password=True,
            password_toggle_button=True,
            on_change=lambda e: settings_form.update({"ocr_vlm_api_key": e.value}),
        ).props("dense outlined").classes("w-full")

        # 模型名（自定义输入，模型列表随服务商变化）
        ui.input(
            label=t("settings_ocr.vlm_model"),
            value=settings_form.get("ocr_vlm_model", ""),
            on_change=lambda e: settings_form.update({"ocr_vlm_model": e.value}),
        ).props("dense outlined").classes("w-full")

        ui.label(t("settings_ocr.vlm_model_hint")).classes("text-xs theme-text-muted")

        # 渲染精度和并发数
        with ui.row().classes("w-full gap-4"):
            ui.number(
                label=t("settings_ocr.vlm_dpi"),
                value=settings_form.get("ocr_vlm_dpi", 150),
                min=72,
                max=400,
                step=10,
                on_change=lambda e: settings_form.update({"ocr_vlm_dpi": e.value}),
            ).props("dense outlined").classes("flex-1")

            ui.number(
                label=t("settings_ocr.vlm_concurrency"),
                value=settings_form.get("ocr_vlm_concurrency", 4),
                min=1,
                max=32,
                step=1,
                on_change=lambda e: settings_form.update({"ocr_vlm_concurrency": e.value}),
            ).props("dense outlined").classes("flex-1")

        ui.label(t("settings_ocr.vlm_hint")).classes("text-xs theme-text-muted")


def _render_ocr_mineru_fields(settings_form: dict):
    """MinerU 精准解析的配置字段"""
    with ui.column().classes("w-full gap-3"):
        ui.input(
            label=t("settings_ocr.mineru_token"),
            value=settings_form.get("ocr_mineru_token", ""),
            password=True,
            password_toggle_button=True,
            on_change=lambda e: settings_form.update({"ocr_mineru_token": e.value}),
        ).props("dense outlined").classes("w-full")

        ui.button(
            t("settings_ocr.mineru_token_help"),
            on_click=lambda: open_external(MINERU_TOKEN_URL),
        ).props("flat dense no-caps size=sm icon-right=open_in_new").classes(
            "self-start px-1 text-xs theme-text-accent"
        ).tooltip(MINERU_TOKEN_URL)

        ui.select(
            options=["vlm", "pipeline"],
            value=settings_form.get("ocr_mineru_model_version", "vlm"),
            label=t("settings_ocr.mineru_model_version"),
            on_change=lambda e: settings_form.update({"ocr_mineru_model_version": e.value}),
        ).props("dense outlined").classes("w-full")

        ui.label(t("settings_ocr.mineru_model_version_hint")).classes("text-xs theme-text-muted")

        # 文档语言只影响 OCR 阶段；选项为官方 language 取值全表
        ui.select(
            options=dict(MINERU_LANGUAGES),
            value=settings_form.get("ocr_mineru_language", "ch"),
            label=t("settings_ocr.mineru_language"),
            on_change=lambda e: settings_form.update({"ocr_mineru_language": e.value}),
        ).props("dense outlined").classes("w-full")

        ui.label(t("settings_ocr.mineru_language_hint")).classes("text-xs theme-text-muted")

        ui.switch(
            t("settings_ocr.mineru_is_ocr"),
            value=bool(settings_form.get("ocr_mineru_is_ocr", False)),
            on_change=lambda e: settings_form.update({"ocr_mineru_is_ocr": e.value}),
        ).props("dense").classes("theme-text")

        ui.label(t("settings_ocr.mineru_is_ocr_hint")).classes("text-xs theme-text-muted")
        ui.label(t("settings_ocr.mineru_hint")).classes("text-xs theme-text-muted")


def _render_office_settings(settings_form: dict, settings_handlers):
    """渲染 Office 文档转换设置表单"""
    with ui.card().tight().classes("w-full theme-card").style("border: 1px solid var(--border-color)"):
        with ui.column().classes("w-full gap-3 p-3"):
            ui.label(t("settings_office.converter")).classes("text-sm font-medium theme-text")

            ui.toggle(
                options={
                    "auto": t("settings_office.converter_auto"),
                    "com": t("settings_office.backend_com"),
                    "libreoffice": t("settings_office.backend_libreoffice"),
                    "off": t("settings_office.converter_off"),
                },
                value=settings_form.get("office_converter", "auto"),
                on_change=lambda e: settings_form.update({"office_converter": e.value}),
            ).props("dense unelevated no-caps").classes("theme-text")

            ui.label(t("settings_office.converter_hint")).classes("text-xs theme-text-muted")

            ui.separator()

            # LibreOffice 路径：留空自动探测常见安装位置
            ui.input(
                label=t("settings_office.libreoffice_path"),
                value=settings_form.get("office_libreoffice_path", ""),
                on_change=lambda e: settings_form.update(
                    {"office_libreoffice_path": e.value}
                ),
            ).props("dense outlined").classes("w-full")

            ui.label(t("settings_office.libreoffice_path_hint")).classes(
                "text-xs theme-text-muted"
            )

            # 探测当前会用哪个后端
            with ui.row().classes("w-full items-center justify-between"):
                test_result = ui.label("").classes("text-xs flex-1 theme-text-muted")
                test_btn = ui.button(
                    t("settings_office.test_btn"),
                    on_click=lambda: settings_handlers.test_office_converter(
                        test_result, test_btn
                    ),
                ).props("dense outline size=sm").classes("theme-text-accent")

            ui.separator()

            with ui.row().classes("w-full justify-end"):
                ui.button(
                    t("settings.btn_save"),
                    on_click=settings_handlers.save_settings_form,
                ).props("color=primary")


def _render_webdav_settings(settings_form: dict, settings_handlers):
    """渲染 WebDAV 云同步设置表单"""
    with ui.card().tight().classes("w-full theme-card").style("border: 1px solid var(--border-color)"):
        with ui.column().classes("w-full gap-3 p-3"):
            # 启用开关
            ui.label(t("settings_webdav.enabled")).classes("text-sm font-medium theme-text")

            current_enabled = settings_form.get("webdav_enabled", False)

            def on_enabled_change(e):
                settings_form["webdav_enabled"] = e.value

            ui.switch(
                value=current_enabled,
                on_change=on_enabled_change,
            ).classes("theme-text")

            ui.separator()

            # WebDAV 服务器地址
            ui.input(
                label=t("settings_webdav.hostname"),
                value=settings_form.get("webdav_hostname", ""),
                on_change=lambda e: settings_form.update({"webdav_hostname": e.value}),
            ).props("dense outlined").classes("w-full")

            ui.label(t("settings_webdav.hostname_hint")).classes("text-xs theme-text-muted")

            # 用户名
            ui.input(
                label=t("settings_webdav.username"),
                value=settings_form.get("webdav_username", ""),
                on_change=lambda e: settings_form.update({"webdav_username": e.value}),
            ).props("dense outlined").classes("w-full")

            # 密码
            ui.input(
                label=t("settings_webdav.password"),
                value=settings_form.get("webdav_password", ""),
                password=True,
                password_toggle_button=True,
                on_change=lambda e: settings_form.update({"webdav_password": e.value}),
            ).props("dense outlined").classes("w-full")

            ui.label(t("settings_webdav.password_hint")).classes("text-xs theme-text-muted")

            # 测试连接
            with ui.row().classes("w-full items-center justify-between"):
                test_result = ui.label("").classes("text-xs flex-1 theme-text-muted")
                test_btn = ui.button(
                    t("settings_webdav.test_btn"),
                    on_click=lambda: settings_handlers.test_webdav_connection(test_result, test_btn)
                ).props("dense outline size=sm").classes("theme-text-accent")

            ui.separator()

            with ui.row().classes("w-full justify-end"):
                ui.button(t("settings.btn_save"), on_click=settings_handlers.save_settings_form).props("color=primary")
