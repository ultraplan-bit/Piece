"""设置分组提交、草稿保护和测试结果的离线回归。"""
import asyncio
from copy import deepcopy

import pytest

from app.i18n import t
from indexing import settings
from indexing.services import config_service


@pytest.fixture
def editor(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.handlers.settings_handlers import SettingsHandlers
    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    form = {}
    handler = SettingsHandlers(form)
    handler.init_settings_form()
    return handler, form


def test_group_save_keeps_other_invalid_drafts(editor):
    handler, form = editor
    original = config_service.get_saved_settings()
    form["base_url"] = "unfinished-address"
    form["vector_dim"] = None
    form["theme"] = "dark"
    assert handler.save_settings_form("appearance")
    saved = config_service.get_saved_settings()
    assert saved.appearance.theme == "dark"
    assert saved.embedding == original.embedding
    assert form["base_url"] == "unfinished-address" and form["vector_dim"] is None
    assert handler.dirty_groups == ("embedding",)
    handler.active_group = "appearance"
    assert not handler.can_save
    assert handler.other_drafts_message


def test_saving_one_section_does_not_apply_another_valid_draft(editor):
    handler, form = editor
    form["office_converter"] = "off"
    form["mcp_include_images_default"] = True
    assert handler.save_settings_form("mcp")
    saved = config_service.get_saved_settings()
    assert saved.mcp.include_images_default
    assert saved.office.converter == "auto"
    assert form["office_converter"] == "off" and handler.dirty_groups == ("office",)
    assert handler.save_settings_form("office")
    assert not handler.dirty
    assert "office" in handler.reindex_groups


def test_revert_section_keeps_other_drafts_and_latest_saved_values(editor):
    handler, form = editor
    form["model"] = "draft-model"
    form["webdav_username"] = "draft-user"
    config_service.update_config({"appearance": {"theme": "dark"}})
    handler.discard_group("embedding")
    assert form["model"] == config_service.get_saved_settings().embedding.model
    assert form["webdav_username"] == "draft-user"
    assert form["theme"] == "dark"
    assert handler.dirty_groups == ("webdav",)
    handler.discard_group()
    assert not handler.dirty


def test_pending_restart_survives_reopening_settings(editor):
    handler, form = editor
    original_port = settings.get_settings().mcp.port
    form["mcp_port"] = original_port + 10
    assert handler.save_settings_form("mcp")
    assert settings.get_settings().mcp.port == original_port
    assert config_service.get_saved_settings().mcp.port == original_port + 10
    handler.init_settings_form()
    handler.active_group = "mcp"
    assert form["mcp_port"] == original_port + 10
    assert handler.pending_restart == ("mcp",)
    assert t("settings.effect_mcp") in handler.effect_message
    assert not handler.dirty


def test_new_mcp_key_is_a_draft_until_save_and_restart(editor):
    from unittest.mock import Mock
    handler, form = editor
    old = settings.get_settings().mcp.get_api_key("index")
    handler.regenerate_mcp_api_key(Mock(), "index")
    new = form["mcp_index_api_key"]
    assert new != old
    assert config_service.get_saved_settings().mcp.get_api_key("index") == old
    assert handler.save_settings_form("mcp")
    assert config_service.get_saved_settings().mcp.get_api_key("index") == new
    assert settings.get_settings().mcp.get_api_key("index") == old


def test_ocr_edit_preserves_unexposed_options_and_complete_extra_payload(editor):
    handler, form = editor
    config_service.update_config({"ocr": {"payload": {"visualize": True, "extra_payload": {"temperature": 0.1}}}})
    handler.init_settings_form()
    form["ocr_extra_payload"]["topP"] = 0.9
    form["theme"] = "dark"
    assert handler.save_settings_form("appearance")
    assert form["ocr_extra_payload"] == {"temperature": 0.1, "topP": 0.9}
    assert handler.save_settings_form("ocr")
    payload = config_service.get_saved_settings().ocr.payload
    assert payload.visualize is True
    assert payload.extra_payload == {"temperature": 0.1, "topP": 0.9}


def test_blocked_model_change_keeps_input_and_does_not_save_other_sections(editor, knowledge_base):
    from indexing.services import file_service, chunk_service
    handler, form = editor
    file_id = file_service.create_empty_file("设置回归资料")["file_id"]
    chunk_service.create_chunk_add_task(file_id, "已有向量", "已有资料")
    knowledge_base.drain()
    original = config_service.get_saved_settings()
    form["model"] = "another-model"
    form["webdav_username"] = "another-user"
    handler.active_group = "embedding"
    assert not handler.save_settings_form("embedding")
    assert config_service.get_saved_settings() == original
    assert form["model"] == "another-model"
    assert form["webdav_username"] == "another-user"
    assert handler.error_message == t("settings.model_change_blocked")


def test_invalid_current_section_stays_dirty_and_reports_safe_error(editor):
    handler, form = editor
    original = deepcopy(form)
    form["vector_dim"] = None
    handler.active_group = "embedding"
    assert not handler.save_settings_form("embedding")
    assert handler.dirty and form["vector_dim"] is None
    assert handler.error_message == t("settings.invalid_form")
    assert original["api_key"] not in handler.error_message


def test_connection_results_are_bound_to_the_tested_snapshot(editor):
    from nicegui import ui
    handler, form = editor
    handler.active_group = "embedding"
    with ui.column() as container:
        label = ui.label()
        button = ui.button("test")

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def probe(snapshot):
            started.set()
            await release.wait()
            assert snapshot["model"] != "changed-during-test"
            return True, "connected"
        task = asyncio.create_task(handler._run_test("embedding", label, button, probe))
        await started.wait()
        assert handler.testing and not button.enabled
        form["model"] = "changed-during-test"
        release.set()
        await task
        assert handler.test_message == t("settings.test_stale")
        assert label.text == t("settings.test_stale")
        assert not handler.testing and button.enabled
    try:
        asyncio.run(scenario())
    finally:
        container.delete()


def test_finishing_a_test_after_its_view_is_deleted_keeps_the_result(editor):
    from nicegui import ui
    handler, _ = editor
    handler.active_group = "office"
    with ui.column() as container:
        label, button = ui.label(), ui.button("test")
    async def probe(_):
        container.delete()
        await asyncio.sleep(0)
        return True, "available"
    asyncio.run(handler._run_test("office", label, button, probe))
    assert handler.test_message == "available"
    assert not handler.testing


def test_test_errors_redact_credentials(editor):
    from app.ui.handlers.settings_handlers import _safe_test_error
    _, form = editor
    form["webdav_password"] = "password with spaces"
    text = f'api_key="{form["api_key"]}" password="password with spaces"'
    redacted = _safe_test_error(text, form)
    assert form["api_key"] not in redacted and "password with spaces" not in redacted


@pytest.mark.parametrize("value,valid", [(None, False), (0, False), (1, True), (2.5, False), (8, True), (9, False)])
def test_number_validation(value, valid):
    from app.ui.views.settings_view import _integer_error
    assert (_integer_error(value, 1, 8) is None) == valid


@pytest.mark.parametrize("value,valid", [("http://localhost:8080/v1", True), ("https://service.example/v1", True),
                                         ("", False), ("service.example", False), ("https://", False)])
def test_connection_url_validation(value, valid):
    from app.ui.views.settings_view import _url_error
    assert (_url_error(value, required=True) is None) == valid


def test_settings_and_tools_have_translations_for_literal_keys():
    import ast
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    files = [root / "app/ui/handlers/settings_handlers.py", root / "app/ui/pages.py", root / "app/ui/views/sidebar.py"]
    files.extend(root / f"app/ui/views/{name}_view.py" for name in
                 ("settings", "mcp_config", "skill", "logs", "recall_test", "cloud_sync", "zotero"))
    keys = {node.args[0].value for path in files for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "t"
            and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)}
    for language in ("zh", "en"):
        translations = json.loads((root / f"app/i18n/locales/{language}.json").read_text(encoding="utf-8"))
        for key in sorted(keys):
            value = translations
            for part in key.split("."):
                assert isinstance(value, dict) and part in value, f"Missing {language}: {key}"
                value = value[part]
            assert isinstance(value, str) and value, f"Empty {language}: {key}"
