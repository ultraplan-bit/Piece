"""同步目标切换、等长修改与解析服务测试：仅临时文件及内存 WebDAV。"""

import asyncio
import os

import pytest

from indexing import settings
from indexing.services import config_service
from indexing.services.errors import BusinessError
from test_sync import sync  # noqa: F401 复用内存 WebDAV fixture


def test_sync_target_matches_actual_storage_paths(knowledge_base):
    relative = settings.AppSettings(data_path="./sync-target")
    absolute = relative.model_copy(update={"data_path": str(relative.get_data_path().resolve())})
    assert relative.get_sync_target() == absolute.get_sync_target()
    literal = relative.model_copy(update={"data_path": "~/sync-target"})
    expanded = relative.model_copy(update={"data_path": str(literal.get_data_path().expanduser())})
    # get_data_path 不展开 ~；不能把两个实际不同的目录当作同一个已同步目标。
    assert literal.get_sync_target() != expanded.get_sync_target()


@pytest.mark.parametrize("entry", ["live", "offline", "initialize", "save"])
@pytest.mark.parametrize("field", ["hostname", "username", "data_path"])
def test_config_entries_reset_history_for_new_target(knowledge_base, entry, field):
    marker = "2026-09-09T10:00:00"
    config_service.update_config({"webdav": {"last_sync_time": marker}})
    updated = config_service.get_saved_settings()
    patch = {"webdav": {"last_sync_time": marker}}
    if field == "data_path":
        updated.data_path = str(knowledge_base.path / "other")
        patch["data_path"] = updated.data_path
    else:
        value = "https://next.invalid" if field == "hostname" else "other-user"
        setattr(updated.webdav, field, value)
        patch["webdav"][field] = value
    if entry == "save":
        assert settings.save_settings(updated)
    elif entry == "initialize":
        result = config_service.initialize_config(patch)
        assert result["config"]["webdav"]["last_sync_time"] is None
    else:
        result = config_service.update_config(patch, offline=entry == "offline")
        assert result["config"]["webdav"]["last_sync_time"] is None
        assert "webdav" in result["changed"]
    assert config_service.get_saved_settings().webdav.last_sync_time is None


@pytest.mark.parametrize("patch", [
    {"webdav": {"password": "rotated-password"}},
    {"webdav": {"enabled": True}},
    {"appearance": {"theme": "dark"}},
])
def test_unrelated_config_changes_keep_history(knowledge_base, patch):
    marker = "2026-09-09T10:00:00"
    config_service.update_config({"webdav": {"last_sync_time": marker}})
    config_service.update_config(patch)
    assert config_service.get_saved_settings().webdav.last_sync_time == marker


@pytest.mark.parametrize("field", ["hostname", "username", "data_path"])
def test_new_target_restores_remote_only_files(sync, field):
    assert sync.service.sync().success
    patch = {"data_path": str(sync.root.parent / "other")} if field == "data_path" else {
        "webdav": {field: "https://next.invalid" if field == "hostname" else "other-user"}}
    config_service.update_config(patch, offline=field == "data_path")
    assert config_service.get_saved_settings().webdav.last_sync_time is None
    sync.client.files["originals/remote-only.md"] = b"keep remote"
    result = sync.service.sync()
    assert result.success, result.errors
    assert sync.client.files["originals/remote-only.md"] == b"keep remote"
    assert (settings.get_settings().get_files_path() / "originals/remote-only.md").read_bytes() == b"keep remote"
    assert not any(action == "delete" for action, _ in sync.client.actions)


@pytest.mark.parametrize("field", ["hostname", "username", "data_path"])
def test_running_sync_cannot_mark_replacement_target(sync, monkeypatch, field):
    config_service.update_config({"webdav": {"last_sync_time": "2026-09-09T10:00:00"}})
    sync.client.files["originals/old-target-only.md"] = b"old target"
    patch = {"data_path": str(sync.root.parent / "other")} if field == "data_path" else {
        "webdav": {field: "https://next.invalid" if field == "hostname" else "other-user"}}

    def connect(hostname, *, auth, timeout):
        assert hostname == "http://not-contacted.invalid" and auth[0] == "test"
        config_service.update_config(patch)
        return sync.client

    monkeypatch.setattr("indexing.services.sync_service.WebDAV4Client", connect)
    result = sync.service.sync()
    assert result.success, result.errors
    assert "originals/old-target-only.md" not in sync.client.files
    assert result.downloaded == []
    assert config_service.get_saved_settings().webdav.last_sync_time is None
    assert sync.service.status.last_sync_time is None
    assert not (sync.root.parent / "other").exists()


def test_equal_size_changes_sync_even_when_mtime_is_unchanged(sync):
    local = sync.root / "originals" / "note.md"
    local.write_bytes(b"before")
    assert sync.service.sync().success
    original_stat = local.stat()
    local.write_bytes(b"after!")
    os.utime(local, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    result = sync.service.sync()
    assert result.success and result.uploaded == ["originals/note.md"]
    assert sync.client.files["originals/note.md"] == b"after!"
    sync.client.actions.clear()
    result = sync.service.sync()
    assert result.success and result.skipped == ["originals/note.md"]
    assert not any(action == "upload" for action, _ in sync.client.actions)
    sync.client.files["originals/note.md"] = b"remote"
    result = sync.service.sync()
    assert result.success and result.uploaded == ["originals/note.md"]
    assert sync.client.files["originals/note.md"] == local.read_bytes() == b"after!"


def test_failed_content_comparison_keeps_both_versions(sync):
    marker = "2026-09-09T10:00:00"
    config_service.update_config({"webdav": {"last_sync_time": marker}})
    local = sync.root / "originals" / "note.md"
    local.write_bytes(b"new")
    sync.client.files["originals/note.md"] = b"old"
    sync.client.truncated = True
    result = sync.service.sync()
    assert not result.success and result.errors
    assert local.read_bytes() == b"new" and sync.client.files["originals/note.md"] == b"old"
    assert config_service.get_saved_settings().webdav.last_sync_time == marker


def test_cancel_in_callback_does_not_start_transfer(sync):
    (sync.root / "originals" / "note.md").write_bytes(b"local")
    result = sync.service.sync(lambda *args: sync.service.stop())
    assert not result.success and sync.client.actions == []
    assert config_service.get_saved_settings().webdav.last_sync_time is None


@pytest.mark.parametrize("change_target", [False, True])
def test_gui_form_preserves_only_same_target_sync_history(knowledge_base, monkeypatch, change_target):
    pytest.importorskip("nicegui")
    from app.ui.handlers.settings_handlers import SettingsHandlers
    from nicegui import ui
    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    form = {}
    handler = SettingsHandlers(form)
    handler.init_settings_form()
    marker = "2026-09-09T10:00:00"
    config_service.update_config({"webdav": {"last_sync_time": marker}})
    form["theme"] = "dark"
    if change_target:
        form["webdav_hostname"] = "https://next.invalid"
    handler.save_settings_form()
    saved = config_service.get_saved_settings()
    assert saved.appearance.theme == "dark"
    assert saved.webdav.last_sync_time == (None if change_target else marker)


@pytest.mark.parametrize("available", [True, False])
def test_ocr_probe_uses_mineru_provider(knowledge_base, monkeypatch, available):
    from indexing.services import mineru_client, ocr_client
    config_service.update_config({"ocr": {"provider": "mineru",
        "mineru_token": "mineru-test-token", "mineru_model_version": "pipeline"}})
    calls = []

    def probe(token, model_version):
        calls.append((token, model_version))
        return available, "mineru-test-token internal details"

    def unexpected_paddle(*args, **kwargs):
        raise AssertionError("MinerU 配置不能测试 PaddleOCR")

    monkeypatch.setattr(mineru_client, "test_mineru_connection", probe)
    monkeypatch.setattr(ocr_client, "test_ocr_connection", unexpected_paddle)
    if available:
        assert asyncio.run(config_service.test_config("ocr")) == {"component": "ocr", "available": True}
    else:
        with pytest.raises(BusinessError) as caught:
            asyncio.run(config_service.test_config("ocr"))
        assert caught.value.code == "CONNECTION_FAILED"
        assert "mineru-test-token" not in str(caught.value)
    assert calls == [("mineru-test-token", "pipeline")]
