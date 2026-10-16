"""设置草稿、分组保存与连接测试。"""

from copy import deepcopy
from pathlib import Path

from nicegui import ui
from pydantic import ValidationError

from app.i18n import t, set_language
from indexing.settings import (
    get_settings, AppSettings, OcrOptionalPayload, OfficeSettings, generate_api_key,
)
from indexing.services.config_service import get_saved_settings, update_config
from indexing.services.errors import BusinessError
from indexing.utils import run_sync


SETTING_GROUPS = ("appearance", "embedding", "ocr", "office", "mcp", "zotero", "storage", "webdav")
CONFIG_GROUPS = {
    "appearance": ("appearance",), "embedding": ("embedding", "performance"),
    "ocr": ("ocr",), "office": ("office",), "mcp": ("mcp",),
    "zotero": ("mcp", "zotero_write_enabled"),
    "storage": ("data_path",), "webdav": ("webdav",),
}


def _payload_form_value(value):
    """三态开关转表单字符串：None→default，True→on，False→off。"""
    if value is None:
        return "default"
    return "on" if value else "off"


def _form_payload_bool(form, key):
    state = form.get(key, "default")
    return {"on": True, "off": False}.get(state)


def _build_ocr_payload(form):
    labels_raw = str(form.get("ocr_markdown_ignore_labels", "") or "").strip()
    return OcrOptionalPayload(
        use_doc_orientation_classify=_form_payload_bool(form, "ocr_use_doc_orientation_classify"),
        use_doc_unwarping=_form_payload_bool(form, "ocr_use_doc_unwarping"),
        use_chart_recognition=_form_payload_bool(form, "ocr_use_chart_recognition"),
        use_seal_recognition=_form_payload_bool(form, "ocr_use_seal_recognition"),
        use_ocr_for_image_block=_form_payload_bool(form, "ocr_use_ocr_for_image_block"),
        markdown_ignore_labels=[item.strip() for item in labels_raw.split(",") if item.strip()] or None,
        extra_payload=form.get("ocr_extra_payload") or {},
    )


def _form_values(settings):
    values = {
        **settings.embedding.model_dump(),
        **settings.appearance.model_dump(),
        **settings.performance.model_dump(),
        "data_path": settings.data_path,
        "zotero_write_enabled": settings.zotero_write_enabled,
    }
    for group in ("mcp", "webdav", "office", "ocr"):
        values.update({f"{group}_{key}": value for key, value in getattr(settings, group).model_dump().items()
                       if key not in {"payload", "last_sync_time"}})
    # 旧配置的派生服务密钥沿用检索密钥，界面展示实际凭据而非 None。
    for service in ("retrieval", "index", "graph", "wiki"):
        field = "mcp_api_key" if service == "retrieval" else f"mcp_{service}_api_key"
        values[field] = settings.mcp.get_api_key(service)
    for key, value in settings.ocr.payload.model_dump().items():
        if key.startswith("use_"):
            values[f"ocr_{key}"] = _payload_form_value(value)
    values["ocr_markdown_ignore_labels"] = ",".join(settings.ocr.payload.markdown_ignore_labels or [])
    values["ocr_extra_payload"] = deepcopy(settings.ocr.payload.extra_payload)
    return values


def _changes(before, after):
    return {key: _changes(before[key], value)
            if isinstance(value, dict) and isinstance(before.get(key), dict) and key != "extra_payload" else value
            for key, value in after.items() if value != before.get(key)}


def _safe_test_error(error, form):
    message = str(error) or t("settings.unknown_error")
    for key, value in form.items():
        if key.endswith(("api_key", "password", "token")) and isinstance(value, str) and value:
            message = message.replace(value, "***")
    return " ".join(message.split())[:160]


class SettingsHandlers:
    def __init__(self, settings_form: dict):
        self.settings_form = settings_form
        self.active_group = "appearance"
        self._form_settings = None
        self._baseline_form = {}
        self.pending_restart = ()
        self.reindex_groups = set()
        self.errors = {}
        self.saved_groups = set()
        self.test_results = {}
        self.testing_groups = set()
        self.language_changed = False

    def init_settings_form(self, *, keep_fields=()):
        """刷新已保存基线；分组保存时仅保留其他分组确实改过的字段。"""
        drafts = {key: deepcopy(self.settings_form[key]) for key in keep_fields if key in self.settings_form}
        settings = get_saved_settings()
        self._form_settings = settings.model_copy(deep=True)
        self._baseline_form = _form_values(settings)
        self.settings_form.clear()
        self.settings_form.update(deepcopy(self._baseline_form))
        self.settings_form.update(drafts)
        effective = get_settings()
        self.pending_restart = tuple(key for key in ("api", "mcp", "performance", "data_path")
                                     if getattr(settings, key) != getattr(effective, key))

    def group_fields(self, group=None):
        if group is None:
            return set(self.settings_form)
        if group not in SETTING_GROUPS:
            raise ValueError(f"Unknown settings group: {group}")
        if group == "appearance":
            return {"theme", "language"}
        if group == "storage":
            return {"data_path"}
        if group == "embedding":
            return {"base_url", "api_key", "model", "vector_dim", "max_tokens", "worker_concurrency"} | {
                key for key in self.settings_form if key.startswith("embedding_")
            }
        if group == "mcp":
            return {key for key in self.settings_form if key.startswith("mcp_") and not key.startswith("mcp_zotero_")}
        if group == "zotero":
            return {"zotero_write_enabled"} | {key for key in self.settings_form if key.startswith("mcp_zotero_")}
        return {key for key in self.settings_form if key.startswith(f"{group}_")}

    def changed_fields(self, group=None):
        return {key for key in self.group_fields(group)
                if self.settings_form.get(key) != self._baseline_form.get(key)}

    def group_values(self, group):
        return {key: deepcopy(self.settings_form.get(key)) for key in self.group_fields(group)}

    @property
    def dirty_groups(self):
        return tuple(group for group in SETTING_GROUPS if self.changed_fields(group))

    @property
    def dirty(self):
        return bool(self.changed_fields())

    @property
    def can_save(self):
        return bool(self.changed_fields(self.active_group))

    @property
    def error_message(self):
        return self.errors.get(self.active_group, "")

    @property
    def status_message(self):
        if self.can_save:
            return t("settings.unsaved_group")
        if self.active_group in self.saved_groups:
            return t("settings.saved_group")
        return t("settings.no_changes")

    @property
    def other_drafts_message(self):
        count = len([group for group in self.dirty_groups if group != self.active_group])
        return t("settings.other_drafts", count=count) if count else ""

    @property
    def effect_message(self):
        messages = []
        if self.pending_restart:
            names = [t(f"settings.effect_{key}") for key in self.pending_restart]
            messages.append(t("settings.pending_restart", groups="、".join(names)))
        if any(key in self.reindex_groups for key in CONFIG_GROUPS[self.active_group]):
            messages.append(t("settings.pending_reindex"))
        return "\n".join(messages)

    @property
    def test_message(self):
        group = self.active_group
        if group in self.testing_groups:
            return t("settings.testing_draft")
        result = self.test_results.get(group)
        if not result:
            return t("settings.not_tested")
        if result["values"] != self.group_values(group):
            return t("settings.test_stale")
        return result["text"]

    @property
    def test_failed(self):
        result = self.test_results.get(self.active_group)
        return bool(result and not result["ok"] and self.active_group not in self.testing_groups
                    and result["values"] == self.group_values(self.active_group))

    @property
    def testing(self):
        return self.active_group in self.testing_groups

    def discard_group(self, group=None):
        fields = self.group_fields(group)
        self.init_settings_form(keep_fields=self.changed_fields() - fields)
        for key in (SETTING_GROUPS if group is None else (group,)):
            self.errors.pop(key, None)
            self.saved_groups.discard(key)

    def _settings_from_form(self, groups):
        """只校验待保存分组，不让其他分类的半成品阻塞本次保存。"""
        baseline = self._form_settings or get_saved_settings()
        data = baseline.model_dump()
        form = self.settings_form
        if "appearance" in groups:
            data["appearance"].update({key: form[key] for key in ("theme", "language")})
        if "embedding" in groups:
            data["embedding"].update({key: form[key] for key in data["embedding"]})
            data["performance"].update({key: form[key] for key in data["performance"]})
        if "mcp" in groups:
            data["mcp"].update({key: form[f"mcp_{key}"] for key in data["mcp"] if not key.startswith("zotero_")})
        if "zotero" in groups:
            data["mcp"].update({key: form[f"mcp_{key}"] for key in data["mcp"] if key.startswith("zotero_")})
            data["zotero_write_enabled"] = bool(form["zotero_write_enabled"])
        if "storage" in groups:
            data["data_path"] = str(form["data_path"] or "").strip()
            if not data["data_path"] or not Path(data["data_path"]).expanduser().is_absolute():
                raise BusinessError("INVALID_PATH", "data_path must be absolute")
        if "webdav" in groups:
            data["webdav"].update({key: form[f"webdav_{key}"] for key in ("enabled", "hostname", "username", "password")})
        if "office" in groups:
            data["office"].update({key: form[f"office_{key}"] for key in data["office"]})
        if "ocr" in groups:
            data["ocr"].update({key: form[f"ocr_{key}"] for key in data["ocr"] if key != "payload"})
            payload = _build_ocr_payload(form).model_dump(exclude={"visualize"})
            data["ocr"]["payload"].update(payload)
        return AppSettings.model_validate(data)

    def save_settings_form(self, group=None, *, notify=True):
        """按分组提交差异；不传分组时兼容管理代码原有的整表保存。"""
        groups = SETTING_GROUPS if group is None else (group,)
        fields = self.group_fields(group)
        drafts = self.changed_fields() - fields
        try:
            baseline = self._form_settings or get_saved_settings()
            new_settings = self._settings_from_form(groups)
            previous_language = get_settings().appearance.language
            result = update_config(_changes(baseline.model_dump(), new_settings.model_dump()))
            self.init_settings_form(keep_fields=drafts)
            self.reindex_groups.update(result["requires_reindex"])
            self.saved_groups.update(groups)
            for key in groups:
                self.errors.pop(key, None)
            language = get_settings().appearance.language
            if previous_language != language:
                set_language(language)
                self.language_changed = True
            if notify:
                warning = bool(result["requires_restart"] or result["requires_reindex"])
                ui.notify(t("settings.saved_pending") if warning else t("settings.saved_immediately"),
                          type="warning" if warning else "positive")
            return True
        except (BusinessError, ValidationError, ValueError, TypeError, OSError) as error:
            code = error.code if isinstance(error, BusinessError) else "INVALID_CONFIG"
            key = {
                "REINDEX_REQUIRED": "model_change_blocked", "FILE_BUSY": "tasks_busy",
                "INVALID_PATH": "invalid_path", "INVALID_CONFIG": "invalid_form",
            }.get(code, "save_failed")
            message = t(f"settings.{key}")
        except Exception:
            message = t("settings.save_failed")
        for key in groups:
            self.errors[key] = message
        if notify:
            ui.notify(message, type="negative")
        return False

    async def _run_test(self, group, label, button, probe):
        if group in self.testing_groups:
            return
        form = deepcopy(self.settings_form)
        values = self.group_values(group)
        self.testing_groups.add(group)
        button.props("loading")
        button.disable()
        label.set_text(t("settings.testing_draft"))
        label.classes(remove="text-green-500 text-red-500 theme-danger", add="theme-text-muted")
        try:
            ok, message = await probe(form)
        except Exception as error:
            ok, message = False, t("settings.test_failed", error=_safe_test_error(error, form))
        finally:
            self.testing_groups.discard(group)
        self.test_results[group] = {"values": values, "ok": ok, "text": message}
        if not label.is_deleted:
            unchanged = values == self.group_values(group)
            label.set_text(message if unchanged else t("settings.test_stale"))
        if not button.is_deleted:
            button.props(remove="loading")
            button.enable()

    async def test_embedding_connection(self, test_result_label, test_btn):
        async def probe(form):
            from indexing.services.embedding_client import get_embeddings_model_with_config

            def embed():
                model = get_embeddings_model_with_config(
                    base_url=form["base_url"], api_key=form["api_key"], model=form["model"],
                )
                return model.embed_query("test")

            if not str(form.get("base_url", "") or "").strip() or not str(form.get("model", "") or "").strip():
                return False, t("settings.complete_connection")
            expected = int(form["vector_dim"])
            result = await run_sync(embed)
            actual = len(result)
            if actual != expected:
                return False, t("settings_embedding.test_dim_mismatch", actual=actual, expected=expected)
            return True, t("settings_embedding.test_success", dim=actual)

        await self._run_test("embedding", test_result_label, test_btn, probe)

    async def test_webdav_connection(self, test_result_label, test_btn):
        async def probe(form):
            from webdav4.client import Client

            hostname = str(form.get("webdav_hostname", "") or "").strip()
            username = str(form.get("webdav_username", "") or "").strip()
            password = form.get("webdav_password", "")
            if not hostname or not username or not password:
                return False, t("settings.complete_connection")

            def check():
                client = Client(hostname, auth=(username, password), timeout=10.0)
                try:
                    client.ls("/")
                finally:
                    client.http.close()

            await run_sync(check)
            return True, t("settings_webdav.test_success")

        await self._run_test("webdav", test_result_label, test_btn, probe)

    async def test_ocr_connection(self, test_result_label, test_btn):
        async def probe(form):
            provider = form.get("ocr_provider", "paddle")
            if provider == "vlm":
                from indexing.services.vlm_client import test_vlm_connection as test
                keys = ("ocr_vlm_base_url", "ocr_vlm_api_key", "ocr_vlm_model")
            elif provider == "mineru":
                from indexing.services.mineru_client import test_mineru_connection as test
                keys = ("ocr_mineru_token", "ocr_mineru_model_version")
            else:
                from indexing.services.ocr_client import test_ocr_connection as test
                keys = ("ocr_base_url", "ocr_api_key", "ocr_model")
            args = tuple(str(form.get(key, "") or "").strip() for key in keys)
            if not all(args):
                return False, t("settings.complete_connection")
            success, message = await run_sync(test, *args)
            return success, t("settings_ocr.test_success") if success else t(
                "settings_ocr.test_failed", error=_safe_test_error(message, form))

        await self._run_test("ocr", test_result_label, test_btn, probe)

    async def test_office_converter(self, test_result_label, test_btn):
        async def probe(form):
            from indexing.services import office_convert

            if form.get("office_converter") == "off":
                return True, t("settings_office.test_disabled")
            settings = OfficeSettings(
                converter=form.get("office_converter", "auto"),
                libreoffice_path=str(form.get("office_libreoffice_path", "") or "").strip(),
            )
            converters = await run_sync(office_convert.list_converters, settings)
            if not converters:
                return False, t("settings_office.test_none")
            kind, executable = converters[0]
            name = t(f"settings_office.backend_{kind}")
            return True, t("settings_office.test_success", backend=f"{name} · {executable}" if executable else name)

        await self._run_test("office", test_result_label, test_btn, probe)

    def regenerate_mcp_api_key(self, api_key_input, service: str = "retrieval"):
        """只生成草稿；保存并重启后才替换运行中的凭据。"""
        field = "mcp_api_key" if service == "retrieval" else f"mcp_{service}_api_key"
        self.settings_form[field] = generate_api_key()
        api_key_input.set_value(self.settings_form[field])
        ui.notify(t("settings_mcp.key_regenerated"), type="info")
