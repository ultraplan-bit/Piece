"""桌面式偏好设置：分类导航、设置行与明确的分组保存。"""

import json
import math
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

from nicegui import ui

from app.i18n import t, SUPPORTED_LANGUAGES, LOCALES_DIR
from app.ui.components import confirm_dialog, help_hint, write_clipboard
from app.utils import open_external
from indexing.mcp.config import SERVICE_NAMES, get_service_ports
from indexing.services.config_service import mcp_service_status
from indexing.settings import MINERU_LANGUAGES, MCP_SERVICES, get_settings


PADDLE_TOKEN_URL = "https://aistudio.baidu.com/account/accessToken"
MINERU_TOKEN_URL = "https://mineru.net/apiManage/docs"
SETTING_ITEMS = (
    ("appearance", "settings.appearance"), ("embedding", "settings.embedding"),
    ("ocr", "settings_ocr.title"), ("office", "settings_office.title"),
    ("mcp", "settings.mcp"), ("zotero", "zotero.title"),
    ("storage", "settings.storage"), ("webdav", "settings.webdav"),
)

# 搜索同时匹配中文概念、英文界面名和配置键；不读取配置值。
SETTING_FIELDS = {
    "appearance": (("theme", "settings_appearance.theme"), ("language", "settings_appearance.language")),
    "embedding": tuple((key, f"settings_embedding.{key}") for key in
                       ("base_url", "api_key", "model", "vector_dim", "max_tokens")) + tuple(
        (key, f"settings_performance.{label}") for key, label in (
            ("embedding_tpm", "tpm"), ("embedding_rpm", "rpm"), ("embedding_concurrency", "concurrency"),
            ("embedding_batch_size", "batch_size"), ("worker_concurrency", "worker_concurrency"))),
    "ocr": tuple((f"ocr_{key}", f"settings_ocr.{label}") for key, label in (
        ("provider", "provider"), ("base_url", "base_url"), ("api_key", "api_key"), ("model", "model"),
        ("vlm_base_url", "vlm_base_url"), ("vlm_api_key", "vlm_api_key"), ("vlm_model", "vlm_model"),
        ("vlm_dpi", "vlm_dpi"), ("vlm_concurrency", "vlm_concurrency"), ("mineru_token", "mineru_token"),
        ("mineru_model_version", "mineru_model_version"), ("mineru_language", "mineru_language"),
        ("mineru_is_ocr", "mineru_is_ocr"), ("markdown_ignore_labels", "markdown_ignore_labels"))) + tuple(
            (f"ocr_{key}", f"settings_ocr.ocr_{key}") for key in (
                "use_doc_unwarping", "use_doc_orientation_classify", "use_chart_recognition",
                "use_seal_recognition", "use_ocr_for_image_block")),
    "office": (("office_converter", "settings_office.converter"),
               ("office_libreoffice_path", "settings_office.libreoffice_path")),
    "mcp": (("mcp_port", "settings_mcp.retrieval_port"), ("mcp_auth_enabled", "settings_mcp.auth_enabled"),
            ("mcp_include_images_default", "settings_mcp.include_images"),
            ("mcp_max_images_per_call", "settings_mcp.max_images")) + tuple(
                ("mcp_api_key" if service == "retrieval" else f"mcp_{service}_api_key",
                 f"settings_mcp.{service}_api_key") for service in MCP_SERVICES if service != "zotero"),
    "zotero": (("zotero_write_enabled", "zotero.enable_write"), ("mcp_zotero_enabled", "zotero.enable_mcp"),
               ("mcp_zotero_port", "zotero.mcp_port"), ("mcp_zotero_api_key", "settings_mcp.zotero_api_key"),
               ("mcp_zotero_auth_enabled", "zotero.mcp_auth")),
    "storage": (("data_path", "settings_storage.data_path"),),
    "webdav": tuple((f"webdav_{key}", f"settings_webdav.{key}") for key in ("enabled", "hostname", "username", "password")),
}
SETTING_ALIASES = {
    "appearance": "外观 主题 深色 亮色 语言 通用 appearance theme language general",
    "embedding": "向量 嵌入 索引 模型 并发 性能 embedding indexing model performance",
    "ocr": "识别 解析 扫描 图像 文字 paddle vlm mineru parser ocr",
    "office": "文档 转换 word excel powerpoint libreoffice office converter",
    "mcp": "服务 接入 mcp server",
    "zotero": "文献 写入 接入 zotero reference write",
    "storage": "存储 目录 路径 数据 storage directory path data",
    "webdav": "同步 云盘 备份 账号 sync cloud backup webdav",
}


@cache
def _search_labels():
    translated = {language: json.loads((LOCALES_DIR / f"{language}.json").read_text(encoding="utf-8"))
                  for language in SUPPORTED_LANGUAGES}
    labels = {}
    for fields in SETTING_FIELDS.values():
        for key, label in fields:
            names = []
            for data in translated.values():
                value = data
                for part in label.split("."):
                    value = value.get(part, {}) if isinstance(value, dict) else {}
                if isinstance(value, str):
                    names.append(value)
            labels[key] = f"{key} {key.replace('_', ' ')} {' '.join(names)}".casefold()
    return labels


def search_settings(query):
    terms = str(query or "").casefold().split()
    if not terms:
        return []
    titles = dict(SETTING_ITEMS)
    matches = []
    labels = _search_labels()
    for group, fields in SETTING_FIELDS.items():
        for key, label in fields:
            text = f"{labels[key]} {t(titles[group])} {SETTING_ALIASES[group]}".casefold()
            if all(term in text for term in terms):
                matches.append({"group": group, "key": key, "label": t(label), "category": t(titles[group]),
                                "score": sum(term in labels[key] for term in terms)})
    return sorted(matches, key=lambda item: item["score"], reverse=True)


def _expansion(handlers, key, label, **kwargs):
    return ui.expansion(label, value=handlers.expanded.get(key, False),
                        on_value_change=lambda event: handlers.expanded.update({key: event.value}), **kwargs
                        ).classes("settings-advanced w-full").props(f'data-settings-expansion="{key}"')


def _focus_setting(element):
    for ancestor in element.ancestors():
        if isinstance(ancestor, ui.expansion):
            ancestor.open()
    target = next((ancestor for ancestor in element.ancestors(include_self=True)
                   if ancestor._props.get("data-setting-key")), element)
    # 在展开动画结束后定位，避免焦点落到尚不可见的输入。
    ui.run_javascript(f'''setTimeout(() => {{
        const element = document.getElementById('c{target.id}');
        if (!element) return;
        const row = element.closest('[data-setting-key]') || element;
        row.scrollIntoView({{block: 'center', behavior: 'instant'}});
        row.classList.remove('setting-located'); void row.offsetWidth; row.classList.add('setting-located');
        const area = row.querySelector('.setting-control') || row;
        const control = area.querySelector('input, textarea, [role="switch"]') || area.querySelector('button, [tabindex="0"]');
        control?.focus({{preventScroll: true}});
        setTimeout(() => row.classList.remove('setting-located'), 1800);
    }}, 240)''')


def _url_error(value, *, required=False):
    text = str(value or "").strip()
    if not text:
        return t("settings.required") if required else None
    try:
        parsed = urlsplit(text)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return None
    except ValueError:
        pass
    return t("settings.invalid_url")


def _integer_error(value, minimum, maximum):
    try:
        number = float(value)
        if math.isfinite(number) and number.is_integer() and minimum <= number <= maximum:
            return None
    except (TypeError, ValueError):
        pass
    return t("settings.integer_range", minimum=minimum, maximum=maximum)


@contextmanager
def _setting_row(label, hint=None, *, key=None, handlers=None, native_label=True):
    with ui.row().classes("setting-row") as row:
        if key:
            row.props(f'data-setting-key="{key}"')
        if handlers and key:
            handlers.field_labels[key] = label
        with ui.column().classes("setting-label"):
            heading = ui.element("label") if key and native_label else ui.element("div")
            if key and native_label:
                heading.props(f'for="setting-{key}"')
            with heading:
                ui.label(label).classes("setting-title")
            if hint:
                ui.label(hint).classes("setting-hint")
            if handlers and key:
                ui.label(t("settings.saved_not_active")).classes("setting-hint").bind_visibility_from(
                    handlers, "inactive_fields", backward=lambda keys: key in keys)
            if handlers and key and handlers.active_group != "appearance":
                with ui.row().classes("setting-change-actions gap-1 items-center").bind_visibility_from(
                        handlers, "dirty_groups", backward=lambda _: key in handlers.changed_fields()):
                    ui.label(t("settings.modified")).classes("settings-dirty")
                    def restore():
                        handlers.restore_field(key)
                        handlers.focus_field = key
                        if handlers.refresh_settings:
                            handlers.refresh_settings()
                    ui.button(t("settings.restore_field"), icon="undo", on_click=restore, color=None).props(
                        "flat dense no-caps").bind_enabled_from(handlers, "saving", backward=lambda value: not value)
        with ui.column().classes("setting-control"):
            yield


def _text_field(form, key, label, *, hint=None, password=False, validation=None, readonly=False, handlers=None):
    with _setting_row(label, hint, key=key, handlers=handlers):
        def change(event):
            form[key] = event.value
            if field.error:
                field.validate()

        field = ui.input(value=form.get(key, ""), password=password, password_toggle_button=password,
                         on_change=change, validation=validation).props("dense outlined hide-bottom-space").classes("w-full")
        field._props.update({"for": f"setting-{key}", "aria-label": label})
        if readonly:
            field.props("readonly")
        field.without_auto_validation().on("blur", field.validate)
        if handlers:
            field.bind_enabled_from(handlers, "saving", backward=lambda value: not value)
    return field


def _number_field(form, key, label, minimum, maximum, *, step=1, hint=None, handlers=None):
    with _setting_row(label, hint, key=key, handlers=handlers):
        def change(event):
            form[key] = event.value
            if field.error:
                field.validate()

        field = ui.number(value=form.get(key), min=minimum, max=maximum, step=step,
                          validation=lambda value: _integer_error(value, minimum, maximum),
                          on_change=change).props("dense outlined hide-bottom-space").classes("settings-number")
        field._props.update({"for": f"setting-{key}", "aria-label": label})
        field.without_auto_validation().on("blur", field.validate)
        if handlers:
            field.bind_enabled_from(handlers, "saving", backward=lambda value: not value)
    return field


def _select_field(form, key, label, options, *, hint=None, with_input=False, handlers=None):
    with _setting_row(label, hint, key=key, handlers=handlers, native_label=False):
        field = ui.select(options=options, value=form.get(key), with_input=with_input,
                          on_change=lambda event: form.update({key: event.value})).props(
                              "dense outlined hide-bottom-space").classes("w-full")
        field._props["aria-label"] = label
        if handlers:
            field.bind_enabled_from(handlers, "saving", backward=lambda value: not value)
    return field


def _switch_field(form, key, label, *, hint=None, handlers=None):
    with _setting_row(label, hint, key=key, handlers=handlers, native_label=False):
        field = ui.switch(value=bool(form.get(key)),
                          on_change=lambda event: form.update({key: event.value})).props("dense")
        field._props["aria-label"] = label
        if handlers:
            field.bind_enabled_from(handlers, "saving", backward=lambda value: not value)
    return field


def _section(title, hint=None):
    with ui.column().classes("settings-section-heading"):
        ui.label(title).classes("text-sm font-semibold theme-text")
        if hint:
            ui.label(hint).classes("text-xs theme-text-muted leading-relaxed")


def _connection_test(handlers, group):
    callback = getattr(handlers, f"test_{'office_converter' if group == 'office' else group + '_connection'}")
    with ui.row().classes("settings-connection-test"):
        with ui.column().classes("gap-1 flex-1 min-w-0"):
            label = ui.label().classes("text-xs theme-text-secondary").props('role="status" aria-live="polite"')
            label.bind_text_from(handlers, "test_message")
            ui.label().classes("setting-hint settings-test-context").bind_text_from(handlers, "test_context")
        ui.icon("error_outline", size="xs").classes("theme-danger").bind_visibility_from(handlers, "test_failed")
        button = ui.button(t("settings.test_current"), on_click=lambda: callback(label, button), color=None).props(
            "outline dense no-caps")
        button.bind_enabled_from(handlers, "testing", backward=lambda value: not value and not handlers.saving)


def render_settings_middle(selected_setting: dict, ui_refs: dict, on_select_setting, settings_handlers):
    with ui.column().classes("settings-navigation w-full h-full theme-panel gap-0"):
        with ui.row().classes("workspace-toolbar w-full"):
            ui.label(t("settings.title")).classes("text-sm font-medium theme-text")
        def search_changed(event):
            settings_handlers.search_query = event.value or ""
            settings_list.refresh()
        def select_first():
            matches = search_settings(settings_handlers.search_query)
            if matches and not settings_handlers.saving:
                settings_handlers.focus_field = matches[0]["key"]
                on_select_setting(matches[0]["group"])
        ui.input(placeholder=t("settings.search"), value=settings_handlers.search_query, on_change=search_changed).props(
            f'dense outlined clearable aria-label="{t("settings.search")}"').classes("settings-search").on(
                "keydown.escape", lambda event: event.sender.set_value("")).on("keydown.enter", select_first)
        with ui.scroll_area().classes("flex-1 min-h-0 scroll-flush"):
            @ui.refreshable
            def settings_list():
                ui_refs["settings_buttons"] = {}
                with ui.column().classes("w-full gap-1 p-2"):
                    if settings_handlers.search_query.strip():
                        matches = search_settings(settings_handlers.search_query)
                        ui.label(t("settings.search_count", count=len(matches))).classes("settings-nav-heading").props(
                            'role="status" aria-live="polite"')
                        if not matches:
                            ui.label(t("settings.search_empty")).classes("setting-hint p-2")
                        for result in matches:
                            def locate(_, item=result):
                                settings_handlers.focus_field = item["key"]
                                on_select_setting(item["group"])
                            with ui.button(on_click=locate, color=None).props("flat dense no-caps align=left").classes(
                                    "settings-search-result w-full").bind_enabled_from(
                                        settings_handlers, "saving", backward=lambda value: not value):
                                with ui.column().classes("gap-1 items-start w-full"):
                                    ui.label(result["label"]).classes("text-sm theme-text")
                                    ui.label(result["category"]).classes("setting-hint")
                        return
                    for key, label in SETTING_ITEMS:
                        if key in {"appearance", "embedding", "mcp"}:
                            heading = {"appearance": "application", "embedding": "processing", "mcp": "services"}[key]
                            ui.label(t(f"settings.nav_{heading}")).classes("settings-nav-heading")
                        with ui.button(on_click=lambda _, group=key: on_select_setting(group), color=None).props(
                            "flat dense no-caps no-ripple align=left"
                        ).classes("settings-nav-item settings-nav-active" if selected_setting["value"] == key
                                  else "settings-nav-item") as button:
                            ui.label(t(label)).classes("flex-1 text-left")
                            ui.label(t("settings.unsaved_short")).classes("settings-dirty").bind_visibility_from(
                                settings_handlers, "dirty_groups", backward=lambda groups, group=key: group in groups)
                        button._props["aria-current"] = "page" if selected_setting["value"] == key else "false"
                        button._props["data-settings-category"] = key
                        button.bind_enabled_from(settings_handlers, "saving", backward=lambda value: not value)
                        ui_refs["settings_buttons"][key] = button
            ui_refs["settings_list"] = settings_list
            settings_list()


def render_settings_right(selected_setting: dict, settings_form: dict, settings_handlers,
                          apply_theme_callback, callbacks=None):
    group = selected_setting.get("value") or "appearance"
    settings_handlers.active_group = group
    title = t(dict(SETTING_ITEMS).get(group, "settings.title"))
    callbacks = callbacks or {}
    settings_handlers.refresh_settings = callbacks.get("refresh_settings")
    settings_handlers.field_labels.update({key: t(label) for fields in SETTING_FIELDS.values() for key, label in fields})
    scroll_position = settings_handlers.scroll_positions.get(group, 0)
    with ui.column().classes("settings-workspace flex-1 h-full min-w-0 theme-content gap-0").props(
            f'data-settings-group="{group}"') as workspace:
        with ui.row().classes("workspace-toolbar w-full justify-between"):
            ui.label(title).classes("text-sm font-medium theme-text flex-1")
            if callbacks.get("return_to_tool"):
                ui.button(callbacks.get("return_label") or t("settings.return_tool"), icon="arrow_back", color=None,
                          on_click=callbacks["return_to_tool"]).props("flat dense no-caps data-settings-return")
            if group == "mcp" and callbacks.get("open_mcp_config"):
                ui.button(t("settings_mcp.open_config"), icon="arrow_outward", color=None,
                          on_click=callbacks["open_mcp_config"]).props("flat dense no-caps")
            if group == "zotero" and callbacks.get("open_zotero"):
                ui.button(t("zotero.open_library"), icon="arrow_outward", color=None,
                          on_click=callbacks["open_zotero"]).props("flat dense no-caps")
            if group == "webdav" and callbacks.get("open_cloud_sync"):
                ui.button(t("settings_webdav.open_sync"), icon="arrow_outward", color=None,
                          on_click=callbacks["open_cloud_sync"]).props("flat dense no-caps")
        ui.label().classes("settings-effect-note").props('role="status"').bind_text_from(
            settings_handlers, "effect_message").bind_visibility_from(settings_handlers, "effect_message", backward=bool)
        location_note = ui.label().classes("settings-location-note setting-hint").props('role="status"')
        location_note.set_visibility(False)
        with ui.scroll_area(on_scroll=lambda event: settings_handlers.scroll_positions.update(
                {group: event.vertical_position})).classes("flex-1 min-h-0 settings-scroll") as scroll:
            with ui.column().classes("settings-form w-full gap-0") as form_container:
                renderers = {
                    "appearance": lambda: _render_appearance_settings(settings_form, settings_handlers, apply_theme_callback),
                    "embedding": lambda: _render_embedding_settings(settings_form, settings_handlers),
                    "mcp": lambda: _render_mcp_settings(settings_form, settings_handlers),
                    "zotero": lambda: _render_zotero_settings(settings_form, settings_handlers),
                    "storage": lambda: _render_storage_settings(settings_form, settings_handlers),
                    "webdav": lambda: _render_webdav_settings(settings_form, settings_handlers),
                    "ocr": lambda: _render_ocr_settings(settings_form, settings_handlers),
                    "office": lambda: _render_office_settings(settings_form, settings_handlers),
                }
                renderers[group]()

        def restore_position():
            if workspace.is_deleted:
                return
            key = settings_handlers.focus_field
            settings_handlers.focus_field = None
            if not key:
                scroll.scroll_to(pixels=scroll_position)
                return
            rows = {element._props.get("data-setting-key"): element for element in form_container.descendants()
                    if element._props.get("data-setting-key")}
            target = rows.get(key)
            if target is None or any(not element.visible for element in target.ancestors(include_self=True)):
                control = ("ocr_provider" if key.startswith("ocr_") else
                           "mcp_zotero_enabled" if key.startswith("mcp_zotero_") else
                           "mcp_include_images_default" if key == "mcp_max_images_per_call" else
                           "office_converter" if key == "office_libreoffice_path" else key)
                target = rows.get(control)
                location_note.set_text(t("settings.inactive_field", field=settings_handlers.field_labels.get(key, key)))
                location_note.set_visibility(True)
            if target:
                _focus_setting(target)
        ui.timer(0.1, restore_position, once=True)

        def validate():
            invalid = [field for field in form_container.descendants()
                       if isinstance(field, (ui.input, ui.number, ui.select))
                       and all(element.visible for element in field.ancestors(include_self=True)) and not field.validate()]
            if invalid:
                _focus_setting(invalid[0])
            return not invalid

        async def save():
            if not settings_handlers.can_save or not validate():
                return
            changed = settings_handlers.changed_fields(group)
            changes = "\n".join(f'{item["label"]}: {item["before"]} → {item["after"]}'
                                for item in settings_handlers.change_summary(group))
            if group == "mcp" and changed & ({"mcp_port", "mcp_api_key", "mcp_auth_enabled"}
                                             | {f"mcp_{service}_api_key" for service in MCP_SERVICES}):
                confirm_dialog(t("settings_mcp.apply_title"), t("settings_mcp.apply_message") + "\n\n" + changes,
                               lambda: settings_handlers.save_group_async(group),
                               confirm_text=t("settings.save_group"))
            elif group == "zotero":
                confirm_dialog(t("zotero.settings_confirm"), t("zotero.settings_confirm_text") + "\n\n" + changes,
                               lambda: settings_handlers.save_group_async(group),
                               confirm_text=t("settings.save_group"))
            elif group == "storage":
                confirm_dialog(t("settings_storage.apply_title"),
                               t("settings_storage.apply_message", path=settings_form["data_path"]) + "\n\n" + changes,
                               lambda: settings_handlers.save_group_async(group),
                               confirm_text=t("settings.save_group"))
            else:
                await settings_handlers.save_group_async(group)

        workspace.on("settings-save", save)

        def discard():
            settings_handlers.discard_group(group)
            if refresh := callbacks.get("refresh_settings"):
                refresh()

        def show_changes():
            with ui.dialog() as dialog, ui.card().classes("settings-changes-dialog w-[620px] max-w-full"):
                ui.label(t("settings.changes_title")).classes("text-base font-semibold")
                for item in settings_handlers.change_summary(group):
                    with ui.row().classes("settings-change-row w-full items-start"):
                        with ui.column().classes("flex-1 min-w-0 gap-1"):
                            ui.label(item["label"]).classes("text-sm font-medium")
                            ui.label(f'{item["before"]} → {item["after"]}').classes("setting-hint settings-change-value")
                        def restore(_, key=item["key"]):
                            settings_handlers.restore_field(key)
                            settings_handlers.focus_field = key
                            dialog.close()
                            if refresh := callbacks.get("refresh_settings"):
                                refresh()
                        ui.button(t("settings.restore_field"), on_click=restore, color=None).props("flat dense no-caps")
                ui.button(t("knowledge.close"), on_click=dialog.close, color=None).props("flat no-caps")
            dialog.open()

        with ui.column().classes("settings-save-bar w-full gap-2"):
            ui.label().classes("text-xs theme-danger").props('role="alert"').bind_text_from(
                settings_handlers, "error_message").bind_visibility_from(settings_handlers, "error_message", backward=bool)
            with ui.row().classes("w-full items-center justify-between gap-3"):
                with ui.column().classes("gap-1 flex-1 min-w-0"):
                    ui.label(t("settings.auto_save") if group == "appearance" else "").classes(
                        "text-xs theme-text-secondary").bind_text_from(
                            settings_handlers, "status_message", backward=lambda value: t("settings.auto_save") if group == "appearance" else value)
                    ui.label().classes("text-xs theme-text-muted").bind_text_from(
                        settings_handlers, "other_drafts_message").bind_visibility_from(
                            settings_handlers, "other_drafts_message", backward=bool)
                if group != "appearance":
                    ui.button(t("settings.view_changes"), on_click=show_changes, color=None).props("flat dense no-caps").bind_visibility_from(
                        settings_handlers, "changed_count", backward=bool).bind_enabled_from(
                            settings_handlers, "saving", backward=lambda value: not value)
                    ui.button(t("settings.revert_group"), on_click=discard, color=None).props("flat dense no-caps").bind_enabled_from(
                        settings_handlers, "can_save")
                    save_button = ui.button(t("settings.save_group"), on_click=save).props(
                        "unelevated dense no-caps data-settings-save").bind_enabled_from(settings_handlers, "can_save")
                    save_button.bind_text_from(settings_handlers, "saving", backward=lambda value:
                                               t("settings.saving") if value else t("settings.save_group"))
            ui.label(t("settings.unsaved_short")).props("data-settings-unsaved").classes("sr-only").bind_visibility_from(
                settings_handlers, "dirty")


def _render_appearance_settings(form, handlers, apply_theme_callback):
    _section(t("settings_appearance.title"), t("settings.auto_save"))
    with _setting_row(t("settings_appearance.theme"), key="theme", handlers=handlers, native_label=False):
        def change_theme(event):
            previous = get_settings().appearance.theme
            if event.value == previous:
                return
            form["theme"] = event.value
            if handlers.save_settings_form("appearance", notify=False):
                apply_theme_callback(event.value)
            else:
                form["theme"] = previous
                theme.set_value(previous)
                apply_theme_callback(previous)

        theme = ui.toggle({"light": t("settings_appearance.theme_light"), "dark": t("settings_appearance.theme_dark"),
                           "pink": t("settings_appearance.theme_pink")}, value=form.get("theme", "light"),
                          on_change=change_theme).props("dense unelevated no-caps")
        theme._props["aria-label"] = t("settings_appearance.theme")
    with _setting_row(t("settings_appearance.language"), t("settings_appearance.language_hint"), key="language", handlers=handlers, native_label=False):
        def change_language(event):
            previous = get_settings().appearance.language
            if event.value == previous:
                return
            form["language"] = event.value
            if not handlers.save_settings_form("appearance", notify=False):
                form["language"] = previous
                language.set_value(previous)

        language = ui.select(SUPPORTED_LANGUAGES, value=form.get("language", "zh"), on_change=change_language).props(
            "dense outlined hide-bottom-space").classes("settings-language w-full")
        language._props["aria-label"] = t("settings_appearance.language")
        ui.button(t("settings.refresh_interface"), on_click=lambda: ui.navigate.reload(), color=None).props(
            "flat dense no-caps").bind_visibility_from(handlers, "language_changed")


def _render_embedding_settings(form, handlers):
    _section(t("settings_embedding.section_model"), t("settings_embedding.change_hint"))
    _text_field(form, "base_url", t("settings_embedding.base_url"), validation=lambda value: _url_error(value, required=True), handlers=handlers)
    _text_field(form, "api_key", t("settings_embedding.api_key"), password=True, handlers=handlers)
    _text_field(form, "model", t("settings_embedding.model"), validation=lambda value: None if str(value or "").strip() else t("settings.required"), handlers=handlers)
    _number_field(form, "vector_dim", t("settings_embedding.vector_dim"), 1, 65536, handlers=handlers)
    _number_field(form, "max_tokens", t("settings_embedding.max_tokens"), 1, 1048576,
                  hint=t("settings_embedding.max_tokens_tooltip"), handlers=handlers)
    _connection_test(handlers, "embedding")
    with _expansion(handlers, "embedding-performance", t("settings_performance.title"), icon="tune"):
        _render_performance_fields(form, handlers)


def _render_performance_fields(form, handlers):
    ui.label(t("settings_performance.restart_hint")).classes("setting-hint")
    for key, label, minimum, maximum, step in (
        ("embedding_tpm", "tpm", 1000, 10000000, 10000),
        ("embedding_rpm", "rpm", 1, 5000, 10),
        ("embedding_concurrency", "concurrency", 1, 32, 1),
        ("embedding_batch_size", "batch_size", 1, 64, 1),
        ("worker_concurrency", "worker_concurrency", 1, 8, 1),
    ):
        _number_field(form, key, t(f"settings_performance.{label}"), minimum, maximum, step=step,
                      hint=t(f"settings_performance.{label}_tooltip"), handlers=handlers)


def _render_mcp_settings(form, handlers):
    _section(t("settings_mcp.services_title"), t("settings_mcp.key_hint"))
    port = _number_field(form, "mcp_port", t("settings_mcp.retrieval_port"), 1024, 65535,
                         hint=t("settings_mcp.retrieval_port_hint"), handlers=handlers)
    port.validation = lambda value: _integer_error(value, 1024, 65535) or next(
        (error for error in (_integer_error(derived, 1024, 65535)
                             for derived in get_service_ports(int(value or 0)).values()) if error), None)
    _switch_field(form, "mcp_auth_enabled", t("settings_mcp.auth_enabled"), hint=t("settings_mcp.auth_enabled_hint"), handlers=handlers)
    _section(t("settings.service_overview"), t("settings.service_overview_hint"))

    def address(base_port, service):
        if _integer_error(base_port, 1024, 65535):
            return "—"
        return f"http://127.0.0.1:{get_service_ports(int(base_port))[SERVICE_NAMES[service]]}/mcp"

    for service in MCP_SERVICES:
        if service == "zotero":
            continue
        with _expansion(handlers, f"mcp-{service}", SERVICE_NAMES[service], icon="dns").classes("settings-service") as expansion:
            with expansion.add_slot("header"):
                with ui.column().classes("settings-service-summary gap-1 flex-1 min-w-0"):
                    ui.label(SERVICE_NAMES[service]).classes("text-sm font-medium theme-text")
                    ui.label().classes("setting-hint font-mono").bind_text_from(
                        port, "value", backward=lambda value, item=service: address(value, item))
                ui.label().classes("settings-service-status text-xs theme-text-secondary").bind_text_from(
                    handlers, "effect_message", backward=lambda _, item=service:
                    t("settings.service_" + ((mcp_service_status(item) or {}).get("status") or "unknown")))
            ui.label().classes("setting-hint").bind_text_from(
                handlers, "effect_message", backward=lambda _, item=service:
                t("settings.active_address", address=address(get_settings().mcp.port, item)))
            _render_mcp_api_key(form, handlers, service)
    _section(t("settings_mcp.include_images"))
    _switch_field(form, "mcp_include_images_default", t("settings_mcp.include_images"), hint=t("settings_mcp.include_images_hint"), handlers=handlers)
    with ui.column().classes("w-full gap-0").bind_visibility_from(form, "mcp_include_images_default"):
        _number_field(form, "mcp_max_images_per_call", t("settings_mcp.max_images"), 1, 20, handlers=handlers)


def _render_zotero_settings(form, handlers):
    _section(t("zotero.local_title"), t("zotero.local_hint"))
    _switch_field(form, "zotero_write_enabled", t("zotero.enable_write"), hint=t("zotero.write_hint"), handlers=handlers)
    _section(t("zotero.mcp_title"), t("zotero.mcp_hint"))
    _switch_field(form, "mcp_zotero_enabled", t("zotero.enable_mcp"), hint=t("zotero.mcp_restart"), handlers=handlers)
    with ui.column().classes("w-full gap-0").bind_visibility_from(form, "mcp_zotero_enabled"):
        _number_field(form, "mcp_zotero_port", t("zotero.mcp_port"), 1024, 65535, handlers=handlers)
        _render_mcp_api_key(form, handlers, "zotero")
        _switch_field(form, "mcp_zotero_auth_enabled", t("zotero.mcp_auth"), hint=t("zotero.mcp_auth_hint"), handlers=handlers)


async def _copy_text(text, success):
    copied = await write_clipboard(text)
    ui.notify(success if copied else t("settings.copy_failed"), type="positive" if copied else "warning")


def _render_mcp_api_key(form, handlers, service):
    key = f"mcp_{service}_api_key" if service != "retrieval" else "mcp_api_key"
    field = _text_field(form, key, t(f"settings_mcp.{service}_api_key"), password=True, readonly=True, handlers=handlers)
    with ui.row().classes("settings-field-actions"):
        ui.button(t("settings_mcp.copy_key"), icon="content_copy", color=None,
                  on_click=lambda: _copy_text(form.get(key, ""), t("settings_mcp.key_copied"))).props("flat dense no-caps")
        ui.button(t("settings_mcp.regenerate_key"), icon="refresh", color=None,
                  on_click=lambda: handlers.regenerate_mcp_api_key(field, service)).props("flat dense no-caps").bind_enabled_from(
                      handlers, "saving", backward=lambda value: not value)


def _render_storage_settings(form, handlers):
    _section(t("settings_storage.title"), t("settings_storage.server_path_hint"))
    with _setting_row(t("settings_storage.current_path")):
        ui.label(str(get_settings().get_data_path())).classes("settings-path text-sm font-mono theme-text")
        ui.button(t("settings_storage.copy_path"), icon="content_copy", color=None,
                  on_click=lambda: _copy_text(str(get_settings().get_data_path()), t("settings_storage.path_copied"))).props(
                      "flat dense no-caps")
    _text_field(form, "data_path", t("settings_storage.data_path"), hint=t("settings_storage.no_migration"),
                validation=lambda value: None if str(value or "").strip() and Path(str(value).strip()).expanduser().is_absolute()
                else t("settings.invalid_path"), handlers=handlers)


def _render_ocr_settings(form, handlers):
    _section(t("settings_ocr.title"), t("settings.parser_effect_hint"))
    with _setting_row(t("settings_ocr.provider"), key="ocr_provider", handlers=handlers, native_label=False):
        def change_provider(event):
            form["ocr_provider"] = event.value
            provider_fields.refresh()

        ui.toggle({"paddle": t("settings_ocr.provider_paddle"), "vlm": t("settings_ocr.provider_vlm"),
                   "mineru": t("settings_ocr.provider_mineru")}, value=form.get("ocr_provider", "paddle"),
                  on_change=change_provider).props(
                      f'dense unelevated no-caps aria-label="{t("settings_ocr.provider")}"').classes("settings-provider").bind_enabled_from(
                          handlers, "saving", backward=lambda value: not value)

    @ui.refreshable
    def provider_fields():
        {"paddle": _render_ocr_paddle_fields, "vlm": _render_ocr_vlm_fields,
         "mineru": _render_ocr_mineru_fields}.get(form.get("ocr_provider"), _render_ocr_paddle_fields)(form, handlers)

    provider_fields()
    _connection_test(handlers, "ocr")


def _render_ocr_paddle_fields(form, handlers):
    _text_field(form, "ocr_base_url", t("settings_ocr.base_url"), hint=t("settings_ocr.base_url_hint"), validation=_url_error, handlers=handlers)
    _text_field(form, "ocr_api_key", t("settings_ocr.api_key"), password=True, handlers=handlers)
    with ui.row().classes("settings-field-actions"):
        ui.button(t("settings_ocr.api_key_help"), on_click=lambda: open_external(PADDLE_TOKEN_URL), color=None).props(
            "flat dense no-caps icon-right=open_in_new")
    _select_field(form, "ocr_model", t("settings_ocr.model"), ["PaddleOCR-VL", "PaddleOCR-VL-1.5", "PP-StructureV3"],
                  with_input=True, hint=t("settings_ocr.hint"), handlers=handlers)
    with _expansion(handlers, "ocr-paddle-advanced", t("settings_ocr.payload_title"), icon="tune"):
        ui.label(t("settings_ocr.payload_hint")).classes("setting-hint")
        states = {"default": t("settings_ocr.payload_state_default"), "on": t("settings_ocr.payload_state_on"),
                  "off": t("settings_ocr.payload_state_off")}
        for key in ("ocr_use_doc_unwarping", "ocr_use_doc_orientation_classify", "ocr_use_chart_recognition",
                    "ocr_use_seal_recognition", "ocr_use_ocr_for_image_block"):
            _select_field(form, key, t(f"settings_ocr.{key}"), states, handlers=handlers)
        _text_field(form, "ocr_markdown_ignore_labels", t("settings_ocr.markdown_ignore_labels"), handlers=handlers)


def _render_ocr_vlm_fields(form, handlers):
    _text_field(form, "ocr_vlm_base_url", t("settings_ocr.vlm_base_url"), hint=t("settings_ocr.vlm_base_url_hint"), validation=_url_error, handlers=handlers)
    _text_field(form, "ocr_vlm_api_key", t("settings_ocr.vlm_api_key"), password=True, handlers=handlers)
    _text_field(form, "ocr_vlm_model", t("settings_ocr.vlm_model"), hint=t("settings_ocr.vlm_model_hint"), handlers=handlers)
    with _expansion(handlers, "ocr-vlm-advanced", t("settings.advanced"), icon="tune"):
        ui.label(t("settings_ocr.vlm_hint")).classes("setting-hint")
        _number_field(form, "ocr_vlm_dpi", t("settings_ocr.vlm_dpi"), 72, 400, step=10, handlers=handlers)
        _number_field(form, "ocr_vlm_concurrency", t("settings_ocr.vlm_concurrency"), 1, 32, handlers=handlers)


def _render_ocr_mineru_fields(form, handlers):
    ui.label(t("settings_ocr.third_party_warning")).classes("settings-privacy-note text-xs theme-danger")
    _text_field(form, "ocr_mineru_token", t("settings_ocr.mineru_token"), password=True, handlers=handlers)
    with ui.row().classes("settings-field-actions"):
        help_hint(t("settings_ocr.mineru_hint"), label=t("settings_ocr.mineru_token"))
        ui.button(t("settings_ocr.mineru_token_help"), on_click=lambda: open_external(MINERU_TOKEN_URL), color=None).props(
            "flat dense no-caps icon-right=open_in_new")
    _select_field(form, "ocr_mineru_model_version", t("settings_ocr.mineru_model_version"), ["vlm", "pipeline"],
                  hint=t("settings_ocr.mineru_model_version_hint"), handlers=handlers)
    _select_field(form, "ocr_mineru_language", t("settings_ocr.mineru_language"), dict(MINERU_LANGUAGES),
                  hint=t("settings_ocr.mineru_language_hint"), handlers=handlers)
    _switch_field(form, "ocr_mineru_is_ocr", t("settings_ocr.mineru_is_ocr"), hint=t("settings_ocr.mineru_is_ocr_hint"), handlers=handlers)


def _render_office_settings(form, handlers):
    _section(t("settings_office.title"), t("settings.parser_effect_hint"))
    _select_field(form, "office_converter", t("settings_office.converter"), {
        "auto": t("settings_office.converter_auto"), "com": t("settings_office.backend_com"),
        "libreoffice": t("settings_office.backend_libreoffice"), "off": t("settings_office.converter_off"),
    }, hint=t("settings_office.converter_hint"), handlers=handlers)
    with ui.column().classes("w-full gap-0").bind_visibility_from(
            form, "office_converter", backward=lambda value: value in {"auto", "libreoffice"}):
        _text_field(form, "office_libreoffice_path", t("settings_office.libreoffice_path"), hint=t("settings_office.libreoffice_path_hint"), handlers=handlers)
    _connection_test(handlers, "office")


def _render_webdav_settings(form, handlers):
    _section(t("settings_webdav.title"), t("settings_webdav.connection_only"))
    _switch_field(form, "webdav_enabled", t("settings_webdav.enabled"), handlers=handlers)
    ui.label(t("settings.connection_disabled_hint")).classes("setting-hint settings-disabled-note").bind_visibility_from(
        form, "webdav_enabled", backward=lambda value: not value)
    _text_field(form, "webdav_hostname", t("settings_webdav.hostname"), hint=t("settings_webdav.hostname_hint"),
                validation=lambda value: _url_error(value, required=bool(form.get("webdav_enabled"))), handlers=handlers)
    _text_field(form, "webdav_username", t("settings_webdav.username"), handlers=handlers)
    _text_field(form, "webdav_password", t("settings_webdav.password"), hint=t("settings_webdav.password_hint"), password=True, handlers=handlers)
    _connection_test(handlers, "webdav")
