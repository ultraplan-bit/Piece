"""文档栏导出：快照打包、跨页去重、无原件反馈与 HTTP 下载清理。"""

import asyncio
import io
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

import pytest

from app.i18n import t
from app.ui.handlers.file_handlers import FileHandlers
from indexing import database
from indexing.services import chunk_service, file_service as files
from indexing.services.errors import BusinessError


def test_batch_markdown_snapshot_keeps_same_names_and_latest_edits(knowledge_base):
    file_ids = [files.create_empty_file(name)["file_id"] for name in ("甲", "乙")]
    for file_id in file_ids:
        chunk_service.create_chunk_add_task(file_id, "标题", f"最新正文 {file_id}")
    knowledge_base.drain()
    for file_id in file_ids:
        source = files.export_file(file_id)
        target = source.parent / f"export-{file_id}" / "同名.md"
        target.parent.mkdir()
        source.replace(target)
        with database.get_db_cursor(write=True) as cursor:
            cursor.execute("UPDATE files SET file_path=? WHERE id=?", (str(target), file_id))
    snapshot = files.export_files_snapshot([*file_ids, file_ids[0]])
    try:
        for file_id in file_ids:
            files.delete_file(file_id)
        with ZipFile(snapshot["path"]) as archive:
            assert set(archive.namelist()) == {f"{file_id}-同名.md" for file_id in file_ids}
            for file_id in file_ids:
                assert f"最新正文 {file_id}" in archive.read(f"{file_id}-同名.md").decode("utf-8")
    finally:
        shutil.rmtree(snapshot["temporary_dir"])


def test_batch_original_snapshot_does_not_export_working_copy(knowledge_base):
    file_ids = [files.create_empty_file(name)["file_id"] for name in ("甲", "乙")]
    for file_id in file_ids:
        original = knowledge_base.settings.get_files_path() / "originals" / str(file_id) / "同名.pdf"
        original.parent.mkdir(parents=True)
        original.write_bytes(f"original-{file_id}".encode())
        with database.get_db_cursor(write=True) as cursor:
            cursor.execute("UPDATE files SET original_file_path=? WHERE id=?", (str(original), file_id))
    snapshot = files.export_files_snapshot(file_ids, "original")
    try:
        with ZipFile(snapshot["path"]) as archive:
            assert set(archive.namelist()) == {f"{file_id}-同名.pdf" for file_id in file_ids}
            for file_id in file_ids:
                assert archive.read(f"{file_id}-同名.pdf") == f"original-{file_id}".encode()
    finally:
        shutil.rmtree(snapshot["temporary_dir"])


def test_batch_export_validation_and_failed_snapshot_cleanup(knowledge_base, monkeypatch):
    file_id = files.create_empty_file("保留原文档")["file_id"]
    created = []
    mkdtemp = files.tempfile.mkdtemp

    def temporary_directory(**kwargs):
        directory = Path(mkdtemp(dir=knowledge_base.path, **kwargs))
        created.append(directory)
        return str(directory)

    monkeypatch.setattr(files.tempfile, "mkdtemp", temporary_directory)
    for ids in ([], [0], [True], ["1"], [2**63], [file_id] * (files.MAX_EXPORT_FILES + 1)):
        with pytest.raises(BusinessError):
            files.export_files_snapshot(ids)
    with pytest.raises(BusinessError):
        files.export_files_snapshot([file_id], "unknown")
    assert not created
    with pytest.raises(BusinessError):
        files.export_files_snapshot([file_id, 999999])
    assert created and all(not directory.exists() for directory in created)
    assert files.export_file(file_id).is_file()


def test_batch_download_endpoint_validates_and_cleans_snapshot(api, knowledge_base, monkeypatch):
    file_ids = [files.create_empty_file(name)["file_id"] for name in ("正文甲", "正文乙")]
    snapshots = []
    export = files.export_files_snapshot

    def snapshot(*args):
        result = export(*args)
        snapshots.append(result)
        return result

    monkeypatch.setattr(files, "export_files_snapshot", snapshot)
    response = api.get("/api/v1/files/content", params={"file_ids": file_ids, "format": "markdown"})
    assert response.status_code == 200
    assert "piece-markdown.zip" in response.headers["content-disposition"]
    with ZipFile(io.BytesIO(response.content)) as archive:
        assert len(archive.namelist()) == 2
    assert snapshots and not snapshots[0]["temporary_dir"].exists()
    for params in ({}, {"file_ids": [0]}, {"file_ids": [file_ids[0]], "format": "invalid"}):
        response = api.get("/api/v1/files/content", params=params)
        assert response.status_code >= 400 and not response.json()["success"]
    assert api.get("/api/v1/files/content", params={"file_ids": file_ids},
                   headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_export_captures_all_selected_ids_not_only_visible_page(knowledge_base, monkeypatch):
    from app.ui.handlers import file_handlers

    file_ids = [files.create_empty_file(name)["file_id"] for name in ("本页", "其他页", "稍后选择")]
    handler = FileHandlers({"selected_file_id": file_ids[0]}, {})
    handler.state.update(batch_mode=True, batch_selected_ids=set(file_ids[:2]),
                         filtered_files=[{"id": file_ids[0]}])
    downloads = []
    monkeypatch.setattr(file_handlers.ui.download, "from_url", downloads.append)

    async def prepare_then_change_selection(function):
        result = function()
        handler.state["batch_selected_ids"] = {file_ids[2]}
        return result

    monkeypatch.setattr(file_handlers, "run_sync", prepare_then_change_selection)
    asyncio.run(handler.handle_export_files("markdown"))
    url = urlsplit(downloads[0])
    assert url.path == "/gui/files/content"
    assert parse_qs(url.query) == {"file_ids": [str(file_id) for file_id in file_ids[:2]], "format": ["markdown"]}
    assert handler.state["batch_selected_ids"] == {file_ids[2]}
    assert not handler.state["file_exporting"]


def test_export_empty_selection_and_missing_originals_are_explicit(knowledge_base, monkeypatch):
    from app.ui.handlers import file_handlers

    file_id = files.create_empty_file("没有原件的笔记")["file_id"]
    handler = FileHandlers({"selected_file_id": file_id}, {})
    downloads, notices = [], []
    monkeypatch.setattr(file_handlers.ui.download, "from_url", downloads.append)
    monkeypatch.setattr(handler, "_notify", lambda message, **kwargs: notices.append(message))
    handler.state["batch_mode"] = True
    asyncio.run(handler.handle_export_files("markdown"))
    assert notices[-1] == t("files.export_select_first") and not downloads
    handler.state["batch_selected_ids"] = {file_id}
    asyncio.run(handler.handle_export_files("original"))
    assert notices[-1] == t("files.export_no_originals") and not downloads
    assert handler.state["batch_selected_ids"] == {file_id}
    asyncio.run(handler.handle_export_files("markdown"))
    assert downloads == [f"/gui/file/{file_id}/content?format=markdown"]
    original_id = files.create_empty_file("有原件")["file_id"]
    original = knowledge_base.settings.get_files_path() / "originals" / "source.pdf"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"original")
    with database.get_db_cursor(write=True) as cursor:
        cursor.execute("UPDATE files SET original_file_path=? WHERE id=?", (str(original), original_id))
    handler.state["batch_selected_ids"] = {file_id, original_id}
    asyncio.run(handler.handle_export_files("original"))
    assert notices[-1] == t("files.export_originals_skipped", count=1)
    assert parse_qs(urlsplit(downloads[-1]).query) == {"file_ids": [str(original_id)], "format": ["original"]}
    original.unlink()
    download_count = len(downloads)
    asyncio.run(handler.handle_export_files("original"))
    assert "有原件" in notices[-1] and len(downloads) == download_count
    assert handler.state["batch_selected_ids"] == {file_id, original_id} and not handler.state["file_exporting"]


@pytest.mark.parametrize("batch_mode", [False, True])
def test_export_actions_follow_catalog_selection(knowledge_base, batch_mode):
    from nicegui import ui
    from app.ui.views.files_view import render_files_middle

    file_id = files.create_empty_file("可导出文档")["file_id"]
    state = {"selected_file_id": None, "search_keyword": "", "chunks_data": [], "files_data": []}
    handler = FileHandlers(state, {})
    asyncio.run(handler.load_files())
    state["batch_mode"] = batch_mode
    with ui.column() as container:
        render_files_middle(state, handler.ui_refs, handler)
    try:
        assert not handler.ui_refs["file_export_button"].enabled
        if batch_mode:
            handler.toggle_file_selection(file_id)
            assert state["batch_selected_ids"] == {file_id}
        else:
            state["selected_file_id"] = file_id
            handler.refresh_file_selection()
            assert not handler.ui_refs["file_export_original"].enabled
        assert handler.ui_refs["file_export_button"].enabled
    finally:
        container.delete()
