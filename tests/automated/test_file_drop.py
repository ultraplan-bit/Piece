"""文件拖入的落点快照、导入语义与跨工作区反馈，使用临时资料库。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from indexing.services import collection_service as collections, file_service as files
from test_upload_lifecycle import _upload, upload_handler


def test_drop_locks_destination_before_transfer_and_preserves_memberships(upload_handler, knowledge_base):
    h, refs = upload_handler.handler, upload_handler.refs
    origin = collections.create_collection("原有归属")["collection_id"]
    destination = collections.create_collection("拖入目标")["collection_id"]
    source = knowledge_base.path / "duplicate.md"
    source.write_text("# duplicate.md\n正文", encoding="utf-8")
    existing = files.import_file(source, collection_ids=[origin])["file_id"]
    h.state.update(collections=collections.list_collections(), active_collection_ids=[origin],
                   selected_file_id=existing, chunk_scroll=147, source_pane_open=True)
    starts, paths = [], []
    sender = refs["upload_input"]
    sender.run_method.side_effect = lambda method: starts.append((method, h._upload_collection_ids[:]))
    h.on_upload_added(SimpleNamespace(sender=sender, args={
        "count": 2, "files": ["fresh.md", "duplicate.md"], "collection_id": str(destination),
    }))
    assert starts == [("upload", [destination])]
    assert h.state["uploading_files"] == ["fresh.md", "duplicate.md"]
    assert h.state["upload_target_name"] == "拖入目标"
    # 接收中切换当前范围，已经确认的拖入目标仍不可改变。
    h.state["active_collection_ids"] = []
    asyncio.run(h.handle_multi_upload(SimpleNamespace(sender=sender, files=[
        _upload("fresh.md", paths), _upload("duplicate.md", paths),
    ])))
    fresh = next(file for file in files.get_files_list() if file["filename"] == "fresh.md")
    assert {item["id"] for item in collections.get_file_collections(fresh["id"])} == {destination}
    assert {item["id"] for item in collections.get_file_collections(existing)} == {origin, destination}
    assert h.state["selected_file_id"] == existing and h.state["chunk_scroll"] == 147
    assert h.state["source_pane_open"] and h.state["active_collection_ids"] == []
    assert h.state["uploading_count"] == 0 and h.state["uploading_files"] == []
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("target", ["all", "uncategorized"])
def test_root_drop_does_not_inherit_current_collection(upload_handler, target):
    h, refs = upload_handler.handler, upload_handler.refs
    collection_id = collections.create_collection("当前范围")["collection_id"]
    h.state.update(collections=collections.list_collections(), active_collection_ids=[collection_id])
    h._upload_collection_ids = [collection_id]
    sender = refs["upload_input"]
    h.on_upload_added(SimpleNamespace(sender=sender, args={"count": 1, "files": ["root.md"], "collection_id": target}))
    assert h._upload_collection_ids == []
    assert h.state["active_collection_ids"] == [collection_id]
    paths = []
    asyncio.run(h.handle_multi_upload(SimpleNamespace(sender=sender, files=[_upload("root.md", paths)])))
    assert collections.get_file_collections(files.get_files_list()[0]["id"]) == []


@pytest.mark.parametrize("target", ["missing", "99999"])
def test_invalid_drop_destination_never_starts_transfer(upload_handler, target):
    from app.i18n import t

    h, refs = upload_handler.handler, upload_handler.refs
    sender = refs["upload_input"]
    h.on_upload_added(SimpleNamespace(sender=sender, args={"count": 1, "files": ["lost.md"], "collection_id": target}))
    sender.run_method.assert_not_called()
    sender.reset.assert_called_once()
    assert h.state["uploading_count"] == 0
    assert upload_handler.notices[-1][0] == t("files.drop_unavailable")


def test_cancelled_drop_cannot_overwrite_next_batch(upload_handler):
    h, refs = upload_handler.handler, upload_handler.refs
    refs["upload_control"] = SimpleNamespace(refresh=Mock(side_effect=lambda: h.set_upload_input(MagicMock())))
    old = refs["upload_input"]
    h.on_upload_added(SimpleNamespace(sender=old, args={"count": 2, "files": ["old.md", "queued.md"], "collection_id": "all"}))
    h.cancel_upload()
    new = refs["upload_input"]
    assert new is not old
    h.on_upload_added(SimpleNamespace(sender=new, args={"count": 1, "files": ["new.md"], "collection_id": "all"}))
    h.on_upload_failed(SimpleNamespace(sender=old))
    h.on_upload_added(SimpleNamespace(sender=old, args=2))
    assert h.state["uploading_count"] == 1 and h.state["uploading_files"] == ["new.md"]


def test_rejections_identify_files_without_cancelling_valid_uploads(upload_handler):
    from app.i18n import t

    h, refs = upload_handler.handler, upload_handler.refs
    h.on_upload_rejected(SimpleNamespace(args=[
        {"name": "program.exe", "reason": "accept"},
        {"name": "资料目录", "reason": "directory"},
    ]))
    message, options = upload_handler.notices[-1]
    assert "program.exe" in message and t("files.reject_format") in message
    assert "资料目录" in message and t("files.reject_directory") in message
    assert options["timeout"] == 0
    h.on_upload_added(SimpleNamespace(sender=refs["upload_input"], args={
        "count": 1, "files": ["valid.md"], "collection_id": "all",
    }))
    assert h.state["uploading_count"] == 1 and not h._upload_cancelled


def test_upload_control_lives_outside_refreshable_catalog(knowledge_base):
    from nicegui import ui
    from app.i18n import t
    from app.ui.file_drop import render_workspace_upload
    from app.ui.handlers.file_handlers import FileHandlers
    from app.ui.views.files_view import render_files_middle

    state, refs = {"selected_file_id": None, "search_keyword": "", "files_data": [], "filtered_files": []}, {}
    h = FileHandlers(state, refs)
    asyncio.run(h.load_files())
    with ui.column() as workspace:
        render_workspace_upload(refs, h)
        with ui.column() as middle:
            render_files_middle(state, refs, h)
    uploader = refs["upload_input"]
    assert not uploader._props["auto-upload"]
    assert uploader._props["multiple"] and uploader._props["batch"]
    assert refs["file_catalog"]._props["data-import-collection"] == "all"
    assert any(isinstance(element, ui.button) and element.text == t("files.upload") for element in middle.descendants())
    assert any(isinstance(element, ui.label) and element.text == t("files.drop_choose") for element in middle.descendants())
    middle.clear()
    assert not uploader.is_deleted
    with middle:
        render_files_middle(state, refs, h)
    assert refs["upload_input"] is uploader
    workspace.delete()


def test_upload_activity_shows_filenames_before_tasks_exist(knowledge_base):
    from nicegui import ui
    from app.i18n import t
    from app.ui.views.task_activity import active_task_count, render_task_activity

    state = {"uploading_count": 2, "uploading_files": ["first.pdf", "second.md"],
             "upload_target_name": "研究 / 资料", "task_activity": [], "task_progress": {}}
    refs = {}
    with ui.column() as container:
        render_task_activity(state, refs, SimpleNamespace(cancel_upload=Mock()))
    labels = [element.text for element in container.descendants() if isinstance(element, ui.label)]
    assert {"first.pdf", "second.md", "研究 / 资料", t("task_activity.importing")} <= set(labels)
    assert t("task_activity.empty") not in labels
    assert active_task_count(state) == 2 and refs["task_activity_badge"].text == "2"
    assert any(isinstance(element, ui.button) and element._props.get("aria-label") == t("files.upload_cancel")
               for element in container.descendants())
    container.delete()


def test_upload_limit_tracks_config_without_replacing_an_active_batch(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.handlers import file_handlers

    h = file_handlers.FileHandlers({}, {})
    upload = ui.upload(max_file_size=200)
    h.set_upload_input(upload)
    monkeypatch.setattr(file_handlers, "get_max_file_size", lambda: 500)
    h.state["uploading_count"] = 1
    h.refresh_upload_limit()
    assert upload._props["max-file-size"] == 200
    h.state["uploading_count"] = 0
    h.refresh_upload_limit()
    assert upload._props["max-file-size"] == 500 and h.ui_refs["upload_input"] is upload
    upload.delete()


def test_batch_collection_dialog_explains_replacement(knowledge_base):
    from nicegui import ui
    from app.i18n import t
    from app.ui.components import file_collections_dialog

    dialog = file_collections_dialog("2 个文档", [], set(), lambda _: None, batch=True)
    assert any(isinstance(element, ui.label) and element.text == t("collections.batch_overwrite_hint")
               for element in dialog.descendants())
    dialog.delete()
