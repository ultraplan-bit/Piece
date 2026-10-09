"""桌面式偏好设置：分类导航、设置行与明确的分组保存。"""

import math
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from nicegui import ui

from app.i18n import t, SUPPORTED_LANGUAGES
from app.ui.components import confirm_dialog, help_hint, write_clipboard
from app.utils import open_external
from indexing.mcp.config import get_mcp_port
from indexing.settings import MINERU_LANGUAGES, get_settings


PADDLE_TOKEN_URL = "https://aistudio.baidu.com/account/accessToken"
MINERU_TOKEN_URL = "https://mineru.net/apiManage/docs"
SETTING_ITEMS = (
    ("appearance", "settings.appearance"), ("embedding", "settings.embedding"),
    ("ocr", "settings_ocr.title"), ("office", "settings_office.title"),
    ("mcp", "settings.mcp"), ("storage", "settings.storage"), ("webdav", "settings.webdav"),
)


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
def _setting_row(label, hint=None, *, key=None):
    with ui.row().classes("setting-row"):
        with ui.column().classes("setting-label"):
            heading = ui.element("label") if key else ui.element("div")
            if key:
                heading.props(f'for="setting-{key}"')
            with heading:
                ui.label(label).classes("setting-title")
            if hint:
                ui.label(hint).classes("setting-hint")
        with ui.column().classes("setting-control"):
            yield


def _text_field(form, key, label, *, hint=None, password=False, validation=None, readonly=False):
    with _setting_row(label, hint, key=key):
        def change(event):
            form[key] = event.value
            if field.error:
                field.validate()

        field = ui.input(value=form.get(key, ""), password=password, password_toggle_button=password,
                         on_change=change, validation=validation).props("dense outlined hide-bottom-space").classes("w-full")
        field._props.update({"input-id": f"setting-{key}", "aria-label": label})
        if readonly:
            field.props("readonly")
        field.without_auto_validation().on("blur", field.validate)
    return field


def _number_field(form, key, label, minimum, maximum, *, step=1, hint=None):
    with _setting_row(label, hint, key=key):
        def change(event):
            form[key] = event.value
            if field.error:
                field.validate()

        field = ui.number(value=form.get(key), min=minimum, max=maximum, step=step,
                          validation=lambda value: _integer_error(value, minimum, maximum),
                          on_change=change).props("dense outlined hide-bottom-space").classes("settings-number")
        field._props.update({"input-id": f"setting-{key}", "aria-label": label})
        field.without_auto_validation().on("blur", field.validate)
    return field


def _select_field(form, key, label, options, *, hint=None, with_input=False):
    with _setting_row(label, hint, key=key):
        field = ui.select(options=options, value=form.get(key), with_input=with_input,
                          on_change=lambda event: form.update({key: event.value})).props(
                              "dense outlined hide-bottom-space").classes("w-full")
        field._props.update({"input-id": f"setting-{key}", "aria-label": label})
    return field


def _switch_field(form, key, label, *, hint=None):
    with _setting_row(label, hint):
        field = ui.switch(value=bool(form.get(key)),
                          on_change=lambda event: form.update({key: event.value})).props("dense")
        field._props["aria-label"] = label
    return field


def _section(title, hint=None):
    with ui.column().classes("settings-section-heading"):
        ui.label(title).classes("text-sm font-semibold theme-text")
        if hint:
            ui.label(hint).classes("text-xs theme-text-muted leading-relaxed")


def _connection_test(handlers, group):
    callback = getattr(handlers, f"test_{'office_converter' if group == 'office' else group + '_connection'}")
    with ui.row().classes("settings-connection-test"):
        label = ui.label().classes("text-xs theme-text-secondary flex-1").props('role="status" aria-live="polite"')
        label.bind_text_from(handlers, "test_message")
        ui.icon("error_outline", size="xs").classes("theme-danger").bind_visibility_from(handlers, "test_failed")
        button = ui.button(t("settings.test_current"), on_click=lambda: callback(label, button), color=None).props(
            "outline dense no-caps")
        button.bind_enabled_from(handlers, "testing", backward=lambda value: not value)


def render_settings_middle(selected_setting: dict, ui_refs: dict, on_select_setting, settings_handlers):
    with ui.column().classes("settings-navigation w-full h-full theme-panel gap-0"):
        with ui.row().classes("workspace-toolbar w-full"):
            ui.label(t("settings.title")).classes("text-sm font-medium theme-text")
        with ui.scroll_area().classes("flex-1 min-h-0 scroll-flush"):
            @ui.refreshable
            def settings_list():
                with ui.column().classes("w-full gap-1 p-2"):
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
            ui_refs["settings_list"] = settings_list
            settings_list()


def render_settings_right(selected_setting: dict, settings_form: dict, settings_handlers,
                          apply_theme_callback, callbacks=None):
    group = selected_setting.get("value") or "appearance"
    settings_handlers.active_group = group
    title = t(dict(SETTING_ITEMS).get(group, "settings.title"))
    callbacks = callbacks or {}
    with ui.column().classes("settings-workspace flex-1 h-full min-w-0 theme-content gap-0") as workspace:
        with ui.row().classes("workspace-toolbar w-full justify-between"):
            ui.label(title).classes("text-sm font-medium theme-text")
            if group == "mcp" and callbacks.get("open_mcp_config"):
                ui.button(t("settings_mcp.open_config"), icon="arrow_outward", color=None,
                          on_click=callbacks["open_mcp_config"]).props("flat dense no-caps")
            if group == "webdav" and callbacks.get("open_cloud_sync"):
                ui.button(t("settings_webdav.open_sync"), icon="arrow_outward", color=None,
                          on_click=callbacks["open_cloud_sync"]).props("flat dense no-caps")
        ui.label().classes("settings-effect-note").props('role="status"').bind_text_from(
            settings_handlers, "effect_message").bind_visibility_from(settings_handlers, "effect_message", backward=bool)
        with ui.scroll_area().classes("flex-1 min-h-0 settings-scroll"):
            with ui.column().classes("settings-form w-full gap-0") as form_container:
                renderers = {
                    "appearance": lambda: _render_appearance_settings(settings_form, settings_handlers, apply_theme_callback),
                    "embedding": lambda: _render_embedding_settings(settings_form, settings_handlers),
                    "mcp": lambda: _render_mcp_settings(settings_form, settings_handlers),
                    "storage": lambda: _render_storage_settings(settings_form, settings_handlers),
                    "webdav": lambda: _render_webdav_settings(settings_form, settings_handlers),
                    "ocr": lambda: _render_ocr_settings(settings_form, settings_handlers),
                    "office": lambda: _render_office_settings(settings_form, settings_handlers),
                }
                renderers[group]()

        def validate():
            invalid = [field for field in form_container.descendants()
                       if isinstance(field, (ui.input, ui.number, ui.select)) and not field.validate()]
            if invalid:
                invalid[0].run_method("focus")
            return not invalid

        def save():
            if not settings_handlers.can_save or not validate():
                return
            changed = settings_handlers.changed_fields(group)
            if group == "mcp" and changed & {"mcp_port", "mcp_api_key", "mcp_index_api_key", "mcp_auth_enabled"}:
                confirm_dialog(t("settings_mcp.apply_title"), t("settings_mcp.apply_message"),
                               lambda: settings_handlers.save_settings_form(group, notify=False),
                               confirm_text=t("settings.save_group"))
            elif group == "storage":
                confirm_dialog(t("settings_storage.apply_title"), t("settings_storage.apply_message", path=settings_form["data_path"]),
                               lambda: settings_handlers.save_settings_form(group, notify=False),
                               confirm_text=t("settings.save_group"))
            else:
                settings_handlers.save_settings_form(group, notify=False)

        workspace.on("settings-save", save)

        def discard():
            settings_handlers.discard_group(group)
            if refresh := callbacks.get("refresh_settings"):
                refresh()

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
                    ui.button(t("settings.revert_group"), on_click=discard, color=None).props("flat dense no-caps").bind_enabled_from(
                        settings_handlers, "can_save")
                    ui.button(t("settings.save_group"), on_click=save).props("unelevated dense no-caps data-settings-save").bind_enabled_from(
                        settings_handlers, "can_save")
            ui.label(t("settings.unsaved_short")).props("data-settings-unsaved").classes("sr-only").bind_visibility_from(
                settings_handlers, "dirty")


def _render_appearance_settings(form, handlers, apply_theme_callback):
    _section(t("settings_appearance.title"), t("settings.auto_save"))
    with _setting_row(t("settings_appearance.theme")):
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
    with _setting_row(t("settings_appearance.language"), t("settings_appearance.language_hint")):
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
    _text_field(form, "base_url", t("settings_embedding.base_url"), validation=lambda value: _url_error(value, required=True))
    _text_field(form, "api_key", t("settings_embedding.api_key"), password=True)
    _text_field(form, "model", t("settings_embedding.model"), validation=lambda value: None if str(value or "").strip() else t("settings.required"))
    _number_field(form, "vector_dim", t("settings_embedding.vector_dim"), 1, 65536)
    _number_field(form, "max_tokens", t("settings_embedding.max_tokens"), 1, 1048576,
                  hint=t("settings_embedding.max_tokens_tooltip"))
    _connection_test(handlers, "embedding")
    with ui.expansion(t("settings_performance.title"), icon="tune").classes("settings-advanced w-full"):
        _render_performance_fields(form)


def _render_performance_fields(form):
    ui.label(t("settings_performance.restart_hint")).classes("setting-hint")
    for key, label, minimum, maximum, step in (
        ("embedding_tpm", "tpm", 1000, 10000000, 10000),
        ("embedding_rpm", "rpm", 1, 5000, 10),
        ("embedding_concurrency", "concurrency", 1, 32, 1),
        ("embedding_batch_size", "batch_size", 1, 64, 1),
        ("worker_concurrency", "worker_concurrency", 1, 8, 1),
    ):
        _number_field(form, key, t(f"settings_performance.{label}"), minimum, maximum, step=step,
                      hint=t(f"settings_performance.{label}_tooltip"))


def _render_mcp_settings(form, handlers):
    _section(t("settings_mcp.services_title"), t("settings_mcp.key_hint"))
    port = _number_field(form, "mcp_port", t("settings_mcp.retrieval_port"), 1024, 65535,
                         hint=t("settings_mcp.retrieval_port_hint"))
    port.validation = lambda value: _integer_error(value, 1024, 65535) or _integer_error(
        get_mcp_port(int(value or 0)), 1024, 65535)
    with _setting_row(t("settings_mcp.index_port"), t("settings_mcp.index_port_hint")):
        ui.label().classes("text-sm font-mono theme-text").bind_text_from(
            port, "value", backward=lambda value: str(get_mcp_port(int(value))) if value is not None else "—")
    _render_mcp_api_key(form, handlers, "retrieval")
    _render_mcp_api_key(form, handlers, "index")
    _switch_field(form, "mcp_auth_enabled", t("settings_mcp.auth_enabled"), hint=t("settings_mcp.auth_enabled_hint"))
    _section(t("settings_mcp.include_images"))
    _switch_field(form, "mcp_include_images_default", t("settings_mcp.include_images"), hint=t("settings_mcp.include_images_hint"))
    with ui.column().classes("w-full gap-0").bind_visibility_from(form, "mcp_include_images_default"):
        _number_field(form, "mcp_max_images_per_call", t("settings_mcp.max_images"), 1, 20)


async def _copy_text(text, success):
    copied = await write_clipboard(text)
    ui.notify(success if copied else t("settings.copy_failed"), type="positive" if copied else "warning")


def _render_mcp_api_key(form, handlers, service):
    key = {"retrieval": "mcp_api_key", "index": "mcp_index_api_key"}[service]
    field = _text_field(form, key, t(f"settings_mcp.{service}_api_key"), password=True, readonly=True)
    with ui.row().classes("settings-field-actions"):
        ui.button(t("settings_mcp.copy_key"), icon="content_copy", color=None,
                  on_click=lambda: _copy_text(form.get(key, ""), t("settings_mcp.key_copied"))).props("flat dense no-caps")
        ui.button(t("settings_mcp.regenerate_key"), icon="refresh", color=None,
                  on_click=lambda: handlers.regenerate_mcp_api_key(field, service)).props("flat dense no-caps")


def _render_storage_settings(form, handlers):
    _section(t("settings_storage.title"), t("settings_storage.server_path_hint"))
    with _setting_row(t("settings_storage.current_path")):
        ui.label(str(get_settings().get_data_path())).classes("settings-path text-sm font-mono theme-text")
        ui.button(t("settings_storage.copy_path"), icon="content_copy", color=None,
                  on_click=lambda: _copy_text(str(get_settings().get_data_path()), t("settings_storage.path_copied"))).props(
                      "flat dense no-caps")
    _text_field(form, "data_path", t("settings_storage.data_path"), hint=t("settings_storage.no_migration"),
                validation=lambda value: None if str(value or "").strip() and Path(str(value).strip()).expanduser().is_absolute()
                else t("settings.invalid_path"))


def _render_ocr_settings(form, handlers):
    _section(t("settings_ocr.title"), t("settings.parser_effect_hint"))
    with _setting_row(t("settings_ocr.provider")):
        def change_provider(event):
            form["ocr_provider"] = event.value
            provider_fields.refresh()

        ui.toggle({"paddle": t("settings_ocr.provider_paddle"), "vlm": t("settings_ocr.provider_vlm"),
                   "mineru": t("settings_ocr.provider_mineru")}, value=form.get("ocr_provider", "paddle"),
                  on_change=change_provider).props("dense unelevated no-caps").classes("settings-provider")

    @ui.refreshable
    def provider_fields():
        {"paddle": _render_ocr_paddle_fields, "vlm": _render_ocr_vlm_fields,
         "mineru": _render_ocr_mineru_fields}.get(form.get("ocr_provider"), _render_ocr_paddle_fields)(form)

    provider_fields()
    _connection_test(handlers, "ocr")


def _render_ocr_paddle_fields(form):
    _text_field(form, "ocr_base_url", t("settings_ocr.base_url"), hint=t("settings_ocr.base_url_hint"), validation=_url_error)
    _text_field(form, "ocr_api_key", t("settings_ocr.api_key"), password=True)
    with ui.row().classes("settings-field-actions"):
        ui.button(t("settings_ocr.api_key_help"), on_click=lambda: open_external(PADDLE_TOKEN_URL), color=None).props(
            "flat dense no-caps icon-right=open_in_new")
    _select_field(form, "ocr_model", t("settings_ocr.model"), ["PaddleOCR-VL", "PaddleOCR-VL-1.5", "PP-StructureV3"],
                  with_input=True, hint=t("settings_ocr.hint"))
    with ui.expansion(t("settings_ocr.payload_title"), icon="tune").classes("settings-advanced w-full"):
        ui.label(t("settings_ocr.payload_hint")).classes("setting-hint")
        states = {"default": t("settings_ocr.payload_state_default"), "on": t("settings_ocr.payload_state_on"),
                  "off": t("settings_ocr.payload_state_off")}
        for key in ("ocr_use_doc_unwarping", "ocr_use_doc_orientation_classify", "ocr_use_chart_recognition",
                    "ocr_use_seal_recognition", "ocr_use_ocr_for_image_block"):
            _select_field(form, key, t(f"settings_ocr.{key}"), states)
        _text_field(form, "ocr_markdown_ignore_labels", t("settings_ocr.markdown_ignore_labels"))


def _render_ocr_vlm_fields(form):
    _text_field(form, "ocr_vlm_base_url", t("settings_ocr.vlm_base_url"), hint=t("settings_ocr.vlm_base_url_hint"), validation=_url_error)
    _text_field(form, "ocr_vlm_api_key", t("settings_ocr.vlm_api_key"), password=True)
    _text_field(form, "ocr_vlm_model", t("settings_ocr.vlm_model"), hint=t("settings_ocr.vlm_model_hint"))
    with ui.expansion(t("settings.advanced"), icon="tune").classes("settings-advanced w-full"):
        ui.label(t("settings_ocr.vlm_hint")).classes("setting-hint")
        _number_field(form, "ocr_vlm_dpi", t("settings_ocr.vlm_dpi"), 72, 400, step=10)
        _number_field(form, "ocr_vlm_concurrency", t("settings_ocr.vlm_concurrency"), 1, 32)


def _render_ocr_mineru_fields(form):
    ui.label(t("settings_ocr.third_party_warning")).classes("settings-privacy-note text-xs theme-danger")
    _text_field(form, "ocr_mineru_token", t("settings_ocr.mineru_token"), password=True)
    with ui.row().classes("settings-field-actions"):
        help_hint(t("settings_ocr.mineru_hint"), label=t("settings_ocr.mineru_token"))
        ui.button(t("settings_ocr.mineru_token_help"), on_click=lambda: open_external(MINERU_TOKEN_URL), color=None).props(
            "flat dense no-caps icon-right=open_in_new")
    _select_field(form, "ocr_mineru_model_version", t("settings_ocr.mineru_model_version"), ["vlm", "pipeline"],
                  hint=t("settings_ocr.mineru_model_version_hint"))
    _select_field(form, "ocr_mineru_language", t("settings_ocr.mineru_language"), dict(MINERU_LANGUAGES),
                  hint=t("settings_ocr.mineru_language_hint"))
    _switch_field(form, "ocr_mineru_is_ocr", t("settings_ocr.mineru_is_ocr"), hint=t("settings_ocr.mineru_is_ocr_hint"))


def _render_office_settings(form, handlers):
    _section(t("settings_office.title"), t("settings.parser_effect_hint"))
    _select_field(form, "office_converter", t("settings_office.converter"), {
        "auto": t("settings_office.converter_auto"), "com": t("settings_office.backend_com"),
        "libreoffice": t("settings_office.backend_libreoffice"), "off": t("settings_office.converter_off"),
    }, hint=t("settings_office.converter_hint"))
    _text_field(form, "office_libreoffice_path", t("settings_office.libreoffice_path"), hint=t("settings_office.libreoffice_path_hint"))
    _connection_test(handlers, "office")


def _render_webdav_settings(form, handlers):
    _section(t("settings_webdav.title"), t("settings_webdav.connection_only"))
    _switch_field(form, "webdav_enabled", t("settings_webdav.enabled"))
    _text_field(form, "webdav_hostname", t("settings_webdav.hostname"), hint=t("settings_webdav.hostname_hint"),
                validation=lambda value: _url_error(value, required=bool(form.get("webdav_enabled"))))
    _text_field(form, "webdav_username", t("settings_webdav.username"))
    _text_field(form, "webdav_password", t("settings_webdav.password"), hint=t("settings_webdav.password_hint"), password=True)
    _connection_test(handlers, "webdav")
