"""
设置操作处理器

职责:
- 初始化设置表单数据
- 保存设置
"""

from indexing.utils import run_sync

from nicegui import ui

from indexing.settings import (
    get_settings,
    AppSettings, EmbeddingSettings, McpSettings, AppearanceSettings, WebDAVSettings,
    OcrSettings, OcrOptionalPayload, OfficeSettings, PerformanceSettings, generate_api_key
)
from indexing.services.config_service import get_saved_settings, update_config
from app.i18n import t, set_language


def _payload_form_value(value):
    """三态开关转表单字符串：None→default，True→on，False→off。"""
    if value is None:
        return "default"
    return "on" if value else "off"


def _form_payload_bool(form, key):
    """表单字符串转三态布尔：default→None，on→True，off→False。"""
    state = form.get(key, "default")
    if state == "on":
        return True
    if state == "off":
        return False
    return None


def _build_ocr_payload(form):
    """从表单字段组装解析参数；无任何显式配置时返回 None（不改变旧行为）。"""
    labels_raw = str(form.get("ocr_markdown_ignore_labels", "") or "").strip()
    ignore_labels = [item.strip() for item in labels_raw.split(",") if item.strip()] or None
    extra_payload = form.get("ocr_extra_payload") or {}
    payload = OcrOptionalPayload(
        use_doc_orientation_classify=_form_payload_bool(form, "ocr_use_doc_orientation_classify"),
        use_doc_unwarping=_form_payload_bool(form, "ocr_use_doc_unwarping"),
        use_chart_recognition=_form_payload_bool(form, "ocr_use_chart_recognition"),
        use_seal_recognition=_form_payload_bool(form, "ocr_use_seal_recognition"),
        use_ocr_for_image_block=_form_payload_bool(form, "ocr_use_ocr_for_image_block"),
        markdown_ignore_labels=ignore_labels,
        extra_payload=extra_payload,
    )
    return payload


class SettingsHandlers:
    """设置操作处理器"""

    def __init__(self, settings_form: dict):
        """
        初始化设置处理器

        Args:
            settings_form: 设置表单数据字典
        """
        self.settings_form = settings_form

    def init_settings_form(self):
        """初始化设置表单数据"""
        settings = get_saved_settings()
        self._form_settings = settings.model_copy(deep=True)
        self.settings_form.clear()
        self.settings_form.update({
            "base_url": settings.embedding.base_url,
            "api_key": settings.embedding.api_key,
            "model": settings.embedding.model,
            "vector_dim": settings.embedding.vector_dim,
            "max_tokens": settings.embedding.max_tokens,
            "mcp_port": settings.mcp.port,
            "mcp_api_key": settings.mcp.get_api_key("retrieval"),
            "mcp_index_api_key": settings.mcp.get_api_key("index"),
            "mcp_auth_enabled": settings.mcp.auth_enabled,
            "mcp_include_images_default": settings.mcp.include_images_default,
            "mcp_max_images_per_call": settings.mcp.max_images_per_call,
            "data_path": settings.data_path,
            "theme": settings.appearance.theme,
            "language": settings.appearance.language,
            # WebDAV 配置
            "webdav_enabled": settings.webdav.enabled,
            "webdav_hostname": settings.webdav.hostname,
            "webdav_username": settings.webdav.username,
            "webdav_password": settings.webdav.password,
            # OCR 配置
            "ocr_provider": settings.ocr.provider,
            "ocr_base_url": settings.ocr.base_url,
            "ocr_api_key": settings.ocr.api_key,
            "ocr_model": settings.ocr.model,
            "ocr_vlm_base_url": settings.ocr.vlm_base_url,
            "ocr_vlm_api_key": settings.ocr.vlm_api_key,
            "ocr_vlm_model": settings.ocr.vlm_model,
            "ocr_vlm_dpi": settings.ocr.vlm_dpi,
            "ocr_vlm_concurrency": settings.ocr.vlm_concurrency,
            "ocr_mineru_token": settings.ocr.mineru_token,
            "ocr_mineru_model_version": settings.ocr.mineru_model_version,
            "ocr_mineru_is_ocr": settings.ocr.mineru_is_ocr,
            "ocr_mineru_language": settings.ocr.mineru_language,
            # OCR 解析参数（三态开关以字符串存表单，None/True/False 落库时转换）
            "ocr_use_doc_unwarping": _payload_form_value(settings.ocr.payload.use_doc_unwarping),
            "ocr_use_doc_orientation_classify": _payload_form_value(
                settings.ocr.payload.use_doc_orientation_classify
            ),
            "ocr_use_chart_recognition": _payload_form_value(settings.ocr.payload.use_chart_recognition),
            "ocr_use_seal_recognition": _payload_form_value(settings.ocr.payload.use_seal_recognition),
            "ocr_use_ocr_for_image_block": _payload_form_value(
                settings.ocr.payload.use_ocr_for_image_block
            ),
            "ocr_markdown_ignore_labels": ",".join(settings.ocr.payload.markdown_ignore_labels or []),
            "ocr_extra_payload": settings.ocr.payload.extra_payload,
            # Office 转换配置
            "office_converter": settings.office.converter,
            "office_libreoffice_path": settings.office.libreoffice_path,
            # 性能配置（嵌入限流与并发）
            "embedding_tpm": settings.performance.embedding_tpm,
            "embedding_rpm": settings.performance.embedding_rpm,
            "embedding_concurrency": settings.performance.embedding_concurrency,
            "embedding_batch_size": settings.performance.embedding_batch_size,
            "worker_concurrency": settings.performance.worker_concurrency,
        })

    def save_settings_form(self):
        """保存设置"""
        try:
            old_settings = get_settings()
            baseline = getattr(self, "_form_settings", None) or get_saved_settings()
            new_settings = AppSettings(
                api=baseline.api,
                embedding=EmbeddingSettings(
                    base_url=self.settings_form["base_url"],
                    api_key=self.settings_form["api_key"],
                    model=self.settings_form["model"],
                    vector_dim=int(self.settings_form["vector_dim"]),
                    max_tokens=int(self.settings_form.get("max_tokens", 8192)),
                ),
                mcp=McpSettings(
                    port=int(self.settings_form["mcp_port"]),
                    api_key=self.settings_form.get("mcp_api_key", old_settings.mcp.api_key),
                    index_api_key=self.settings_form.get(
                        "mcp_index_api_key", old_settings.mcp.index_api_key
                    ),
                    auth_enabled=self.settings_form.get("mcp_auth_enabled", True),
                    include_images_default=self.settings_form.get(
                        "mcp_include_images_default", False
                    ),
                    max_images_per_call=int(
                        self.settings_form.get("mcp_max_images_per_call", 6)
                    ),
                ),
                appearance=AppearanceSettings(
                    theme=self.settings_form["theme"],
                    language=self.settings_form["language"],
                ),
                webdav=WebDAVSettings(
                    enabled=self.settings_form.get("webdav_enabled", False),
                    hostname=self.settings_form.get("webdav_hostname", ""),
                    username=self.settings_form.get("webdav_username", ""),
                    password=self.settings_form.get("webdav_password", ""),
                    last_sync_time=baseline.webdav.last_sync_time,
                ),
                ocr=OcrSettings(
                    provider=self.settings_form.get("ocr_provider", "paddle"),
                    base_url=self.settings_form.get("ocr_base_url", "").strip(),
                    api_key=self.settings_form.get("ocr_api_key", "").strip(),
                    model=self.settings_form.get("ocr_model", "PaddleOCR-VL"),
                    payload=_build_ocr_payload(self.settings_form),
                    vlm_base_url=self.settings_form.get("ocr_vlm_base_url", "").strip(),
                    vlm_api_key=self.settings_form.get("ocr_vlm_api_key", "").strip(),
                    vlm_model=self.settings_form.get("ocr_vlm_model", "").strip(),
                    vlm_dpi=int(self.settings_form.get("ocr_vlm_dpi") or 150),
                    vlm_concurrency=int(self.settings_form.get("ocr_vlm_concurrency") or 4),
                    mineru_token=self.settings_form.get("ocr_mineru_token", "").strip(),
                    mineru_model_version=self.settings_form.get(
                        "ocr_mineru_model_version", "vlm"
                    ),
                    mineru_is_ocr=bool(self.settings_form.get("ocr_mineru_is_ocr", False)),
                    mineru_language=self.settings_form.get("ocr_mineru_language", "ch"),
                ),
                office=OfficeSettings(
                    converter=self.settings_form.get("office_converter", "auto"),
                    libreoffice_path=self.settings_form.get(
                        "office_libreoffice_path", ""
                    ).strip(),
                ),
                performance=PerformanceSettings(
                    embedding_tpm=int(self.settings_form.get("embedding_tpm", 400000)),
                    embedding_rpm=int(self.settings_form.get("embedding_rpm", 200)),
                    embedding_concurrency=int(
                        self.settings_form.get("embedding_concurrency", 4)
                    ),
                    embedding_batch_size=int(
                        self.settings_form.get("embedding_batch_size", 10)
                    ),
                    worker_concurrency=int(
                        self.settings_form.get("worker_concurrency", 2)
                    ),
                ),
                data_path=self.settings_form["data_path"],
            )

            def changes(before, after):
                patch = {}
                for key, value in after.items():
                    if value == before[key]:
                        continue
                    patch[key] = changes(before[key], value) if isinstance(value, dict) else value
                return patch

            result = update_config(changes(baseline.model_dump(), new_settings.model_dump()))
            self.init_settings_form()
            if old_settings.appearance.language != get_settings().appearance.language:
                set_language(get_settings().appearance.language)
            if result["requires_restart"] or result["requires_reindex"]:
                details = []
                if result["requires_restart"]:
                    details.append("需重启：" + "、".join(result["requires_restart"]))
                if result["requires_reindex"]:
                    details.append("已有索引未改变，重新索引后生效：" + "、".join(result["requires_reindex"]))
                ui.notify("；".join(details), type="warning", close_button=True, timeout=5000)
            else:
                ui.notify(t("settings.saved_immediately"), type="positive")
        except Exception as e:
            ui.notify(f"{t('settings.save_failed')}: {e}", type="negative")

    async def test_embedding_connection(self, test_result_label, test_btn):
        """测试嵌入模型连接"""
        test_btn.props("loading")
        test_result_label.set_text(t("settings_embedding.testing"))

        try:
            from indexing.services.embedding_client import get_embeddings_model_with_config

            # 使用表单配置创建临时实例（不缓存）
            # 将实例化放到线程中，避免首次创建时阻塞事件循环
            embeddings = await run_sync(
                lambda: get_embeddings_model_with_config(
                    base_url=self.settings_form["base_url"],
                    api_key=self.settings_form["api_key"],
                    model=self.settings_form["model"],
                )
            )

            result = await run_sync(
                embeddings.embed_query, "test"
            )

            actual_dim = len(result)
            expected_dim = int(self.settings_form["vector_dim"])

            if actual_dim == expected_dim:
                test_result_label.set_text(t("settings_embedding.test_success", dim=actual_dim))
                test_result_label.classes(remove="theme-text-muted", add="text-green-500")
            else:
                test_result_label.set_text(
                    t("settings_embedding.test_dim_mismatch", actual=actual_dim, expected=expected_dim)
                )
                test_result_label.classes(remove="theme-text-muted", add="text-red-500")

        except Exception as e:
            error_msg = str(e)
            if len(error_msg) > 100:
                error_msg = error_msg[:100] + "..."
            test_result_label.set_text(t("settings_embedding.test_failed", error=error_msg))
            test_result_label.classes(remove="theme-text-muted", add="text-red-500")
        finally:
            test_btn.props(remove="loading")

    async def test_webdav_connection(self, test_result_label, test_btn):
        """测试 WebDAV 连接"""
        test_btn.props("loading")
        test_result_label.set_text(t("settings_webdav.testing"))
        test_result_label.classes(remove="text-green-500 text-red-500", add="theme-text-muted")

        try:
            from webdav4.client import Client

            hostname = self.settings_form.get("webdav_hostname", "").strip()
            username = self.settings_form.get("webdav_username", "").strip()
            password = self.settings_form.get("webdav_password", "").strip()

            if not hostname or not username or not password:
                test_result_label.set_text(t("settings_webdav.test_failed", error="请填写完整配置"))
                test_result_label.classes(remove="theme-text-muted", add="text-red-500")
                return

            client = Client(
                hostname,
                auth=(username, password),
                timeout=10.0
            )

            # 尝试列出根目录来验证连接
            def test_connection():
                try:
                    client.ls("/")
                    return True, None
                except Exception as e:
                    return False, str(e)

            success, error = await run_sync(test_connection)

            if success:
                test_result_label.set_text(t("settings_webdav.test_success"))
                test_result_label.classes(remove="theme-text-muted", add="text-green-500")
            else:
                error_msg = error or "未知错误"
                if len(error_msg) > 80:
                    error_msg = error_msg[:80] + "..."
                test_result_label.set_text(t("settings_webdav.test_failed", error=error_msg))
                test_result_label.classes(remove="theme-text-muted", add="text-red-500")

        except Exception as e:
            error_msg = str(e)
            if len(error_msg) > 80:
                error_msg = error_msg[:80] + "..."
            test_result_label.set_text(t("settings_webdav.test_failed", error=error_msg))
            test_result_label.classes(remove="theme-text-muted", add="text-red-500")
        finally:
            test_btn.props(remove="loading")

    async def test_ocr_connection(self, test_result_label, test_btn):
        """测试 PDF 解析服务连接（按所选后端分派）"""
        test_btn.props("loading")
        test_result_label.set_text(t("settings_ocr.testing"))
        test_result_label.classes(remove="text-green-500 text-red-500", add="theme-text-muted")

        try:
            provider = self.settings_form.get("ocr_provider", "paddle")
            if provider == "vlm":
                from indexing.services.vlm_client import test_vlm_connection

                test_func = test_vlm_connection
                test_args = (
                    self.settings_form.get("ocr_vlm_base_url", "").strip(),
                    self.settings_form.get("ocr_vlm_api_key", "").strip(),
                    self.settings_form.get("ocr_vlm_model", "").strip(),
                )
            elif provider == "mineru":
                from indexing.services.mineru_client import test_mineru_connection

                test_func = test_mineru_connection
                test_args = (
                    self.settings_form.get("ocr_mineru_token", "").strip(),
                    self.settings_form.get("ocr_mineru_model_version", "vlm"),
                )
            else:
                from indexing.services.ocr_client import test_ocr_connection

                test_func = test_ocr_connection
                test_args = (
                    self.settings_form.get("ocr_base_url", "").strip(),
                    self.settings_form.get("ocr_api_key", "").strip(),
                    self.settings_form.get("ocr_model", "PaddleOCR-VL"),
                )

            if not all(test_args):
                test_result_label.set_text(t("settings_ocr.test_failed", error="请填写完整配置"))
                test_result_label.classes(remove="theme-text-muted", add="text-red-500")
                return

            success, message = await run_sync(test_func, *test_args)

            if success:
                test_result_label.set_text(t("settings_ocr.test_success"))
                test_result_label.classes(remove="theme-text-muted", add="text-green-500")
            else:
                error_msg = message
                if len(error_msg) > 80:
                    error_msg = error_msg[:80] + "..."
                test_result_label.set_text(t("settings_ocr.test_failed", error=error_msg))
                test_result_label.classes(remove="theme-text-muted", add="text-red-500")
        except Exception as e:
            error_msg = str(e)
            if len(error_msg) > 80:
                error_msg = error_msg[:80] + "..."
            test_result_label.set_text(t("settings_ocr.test_failed", error=error_msg))
            test_result_label.classes(remove="theme-text-muted", add="text-red-500")
        finally:
            test_btn.props(remove="loading")

    async def test_office_converter(self, test_result_label, test_btn):
        """探测 Office 转换后端是否可用。

        表单里的配置还没落盘，直接用表单值临时探测，免得用户必须先保存再测。
        """
        test_btn.props("loading")
        test_result_label.set_text(t("settings_office.testing"))
        test_result_label.classes(remove="text-green-500 text-red-500", add="theme-text-muted")

        try:
            from indexing.services import office_convert

            probe = OfficeSettings(
                converter=self.settings_form.get("office_converter", "auto"),
                libreoffice_path=self.settings_form.get(
                    "office_libreoffice_path", ""
                ).strip(),
            )
            # 探测要访问文件系统（甚至 PATH 查找），放线程里避免卡住 UI
            converters = await run_sync(office_convert.list_converters, probe)

            if not converters:
                test_result_label.set_text(t("settings_office.test_none"))
                test_result_label.classes(remove="theme-text-muted", add="text-red-500")
                return

            kind, executable = converters[0]
            name = t(f"settings_office.backend_{kind}")
            detail = f"{name} · {executable}" if executable else name
            test_result_label.set_text(t("settings_office.test_success", backend=detail))
            test_result_label.classes(remove="theme-text-muted", add="text-green-500")
        except Exception as e:
            error_msg = str(e)
            if len(error_msg) > 80:
                error_msg = error_msg[:80] + "..."
            test_result_label.set_text(t("settings_office.test_failed", error=error_msg))
            test_result_label.classes(remove="theme-text-muted", add="text-red-500")
        finally:
            test_btn.props(remove="loading")

    def regenerate_mcp_api_key(self, api_key_input, service: str = "retrieval"):
        """只轮换指定服务的表单密钥，保存并重启后生效。"""
        field = {"retrieval": "mcp_api_key", "index": "mcp_index_api_key"}[service]
        new_key = generate_api_key()
        self.settings_form[field] = new_key
        api_key_input.set_value(new_key)
        ui.notify(t("settings_mcp.key_regenerated"), type="info")
