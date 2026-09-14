"""WebDAV 文件同步合同：纯本地内存客户端，不触及用户云端。"""

from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from indexing.services import config_service
from indexing.services.sync_service import SyncService


class MemoryDav:
    def __init__(self, files=None):
        self.files = dict(files or {})
        self.directories = {"originals", "working"}
        for name in self.files:
            self.directories.update(str(path) for path in PurePosixPath(name).parents if str(path) != ".")
        self.actions = []
        self.failed_list = None
        self.failed_upload = None
        self.truncated = False
        self.http = SimpleNamespace(close=lambda: None)

    def exists(self, name):
        return name in self.files or name in self.directories

    def mkdir(self, name):
        self.directories.add(name)
        self.actions.append(("mkdir", name))

    def ls(self, name, detail=True):
        if name == self.failed_list:
            raise OSError("credentials must not appear in result")
        result = [{"name": path, "type": "directory", "content_length": None}
                  for path in self.directories if PurePosixPath(path).parent.as_posix() == name]
        result += [{"name": path, "type": "file", "content_length": len(data)}
                   for path, data in self.files.items() if PurePosixPath(path).parent.as_posix() == name]
        return result

    def upload_file(self, source, name, overwrite=False, headers=None):
        if name == self.failed_upload:
            raise OSError("upload failed")
        assert str(PurePosixPath(name).parent) in self.directories
        if not overwrite and name in self.files:
            raise FileExistsError(name)
        self.files[name] = source.read_bytes()
        self.actions.append(("upload", name))

    def download_file(self, name, output):
        self.actions.append(("download", name))
        data = self.files[name]
        output.write_bytes(data[:1] if self.truncated else data)

    def remove(self, name):
        del self.files[name]
        self.actions.append(("delete", name))


@pytest.fixture
def sync(knowledge_base, monkeypatch):
    config_service.update_config({"webdav": {"enabled": True, "hostname": "http://not-contacted.invalid",
        "username": "test", "password": "private-password"}})
    service = SyncService()
    client = MemoryDav()
    monkeypatch.setattr(service, "_get_client", lambda: client)
    root = knowledge_base.path / "files"
    (root / "originals").mkdir(parents=True)
    (root / "working").mkdir()
    return SimpleNamespace(service=service, client=client, root=root)


def test_recursive_generation_uploads_include_assets_but_not_ownership_marker(sync):
    generation = sync.root / "working" / ".generations" / "task-1-abc"
    (generation / "images").mkdir(parents=True)
    (generation / "document.md").write_text("![image](images/a.png)", encoding="utf-8")
    (generation / "images" / "a.png").write_bytes(b"image")
    (generation / ".piece-generation").write_text("local-owner", encoding="ascii")
    result = sync.service.sync()
    assert result.success, result.errors
    assert "working/.generations/task-1-abc/images/a.png" in sync.client.files
    assert all(".piece-generation" not in name for name in sync.client.files)
    assert config_service.get_saved_settings().webdav.last_sync_time


def test_recursive_remote_restore_and_empty_files(sync):
    sync.client.files = {"working/.generations/task-old/images/a.png": b"image", "originals/空文件.md": b""}
    sync.client.directories.update({"working/.generations", "working/.generations/task-old", "working/.generations/task-old/images"})
    (sync.root / "working" / "空笔记.md").write_bytes(b"")
    result = sync.service.sync()
    assert result.success, result.errors
    assert (sync.root / "working/.generations/task-old/images/a.png").read_bytes() == b"image"
    assert (sync.root / "originals/空文件.md").exists()
    assert sync.client.files["working/空笔记.md"] == b""


def test_listing_failure_is_not_an_empty_remote_directory(sync):
    (sync.root / "originals" / "local.md").write_bytes(b"data")
    sync.client.failed_list = "working"
    result = sync.service.sync()
    assert not result.success and result.errors
    assert sync.client.actions == []
    assert config_service.get_saved_settings().webdav.last_sync_time is None
    assert "credentials" not in str(result)


def test_partial_failure_does_not_advance_first_sync_marker(sync):
    for name in ("a.md", "b.md"):
        (sync.root / "originals" / name).write_bytes(b"data")
    sync.client.failed_upload = "originals/b.md"
    result = sync.service.sync()
    assert not result.success and result.uploaded == ["originals/a.md"]
    assert len(result.errors) == 1
    assert config_service.get_saved_settings().webdav.last_sync_time is None


def test_atomic_download_keeps_partial_file_invisible(sync):
    sync.client.files["originals/document.md"] = b"complete content"
    sync.client.truncated = True
    result = sync.service.sync()
    assert not result.success
    assert not (sync.root / "originals" / "document.md").exists()
    assert not list(sync.root.rglob(".sync-*"))


def test_first_sync_never_overwrites_local_even_when_cloud_is_larger(sync):
    sync.client.files["originals/document.md"] = b"remote content"
    local = sync.root / "originals" / "document.md"
    local.write_bytes(b"local")
    result = sync.service.sync()
    assert result.success
    assert local.read_bytes() == b"local" and sync.client.files["originals/document.md"] == b"remote content"


def test_daily_sync_deletes_only_missing_remote_files_and_preserves_zero_bytes(sync):
    config_service.update_config({"webdav": {"last_sync_time": "2026-09-09T10:00:00"}})
    sync.client.files.update({"originals/empty.md": b"", "originals/deleted.md": b"gone"})
    (sync.root / "originals" / "empty.md").write_bytes(b"")
    result = sync.service.sync()
    assert result.success
    assert "originals/empty.md" in sync.client.files and "originals/deleted.md" not in sync.client.files
    assert sync.client.actions == [("delete", "originals/deleted.md")]


def test_malicious_remote_paths_cannot_escape(sync, monkeypatch):
    monkeypatch.setattr(sync.client, "ls", lambda *a, **kw: [{"name": "originals/../../config.json", "type": "file", "content_length": 2}])
    result = sync.service.sync()
    assert not result.success and sync.client.actions == []
