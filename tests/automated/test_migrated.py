"""由旧回归迁入的有效合同：新任务模型、受控路径与临时配置，不保留旧负载兼容。"""

import asyncio
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from indexing import database, settings
from indexing.services import chunk_service, file_service, task_service
from indexing.services.errors import BusinessError


# ---- 任务订阅（GUI 只观察任务表） ------------------------------------------


class _Notify(list):
    def __call__(self, message, **kwargs):
        self.append((message, kwargs.get("type")))


class _FakeFileHandlers:
    def __init__(self):
        self.loads = 0

    async def load_files(self):
        self.loads += 1


class _Refreshable:
    def __init__(self):
        self.refreshes = 0

    def refresh(self):
        self.refreshes += 1


@pytest.fixture
def subscription(knowledge_base, monkeypatch):
    pytest.importorskip("nicegui")
    from indexing.repositories import task_repository

    class Clock:
        value = datetime(2026, 1, 1)

        @classmethod
        def now(cls):
            return cls.value

    def advance():
        Clock.value += timedelta(milliseconds=1)

    monkeypatch.setattr(task_repository, "datetime", Clock)
    monkeypatch.setattr(task_service, "datetime", Clock)
    from app.ui.handlers import task_handlers as module
    from app.ui.handlers.task_handlers import TaskHandlers
    notify = _Notify()
    monkeypatch.setattr(module, "ui", type("U", (), {"notify": staticmethod(notify)})())
    file_id = file_service.create_empty_file("订阅")["file_id"]
    state = {"files_data": [{"id": file_id}], "selected_file_id": None}
    ui_refs = {"file_list_container": _Refreshable()}
    handlers = TaskHandlers(state, ui_refs)
    files = _FakeFileHandlers()
    handlers.set_file_handlers(files)
    return SimpleNamespace(module=module, handlers=handlers, state=state, ui_refs=ui_refs,
                           notify=notify, files=files, file_id=file_id, advance=advance)


def test_task_from_any_source_shows_up_and_finishes_once(subscription):
    s = subscription
    asyncio.run(s.handlers.init_active_tasks())
    s.advance()
    task_id = file_service.reindex_file(s.file_id)["task_id"]
    asyncio.run(s.handlers.poll())
    assert s.state["task_progress"][task_id]["status"] == "pending"
    assert s.ui_refs["file_list_container"].refreshes == 1 and s.files.loads == 0
    s.advance()
    task_service.update_page_progress(task_id, 3, 10, 5, 40, stage="parsing")
    asyncio.run(s.handlers.poll())
    snapshot = s.state["task_progress"][task_id]
    assert snapshot["progress"] == 40 and snapshot["stage"] == "parsing"
    s.advance()
    task_service.update_task_status(task_id, "completed", progress=100)
    for _ in range(4):
        asyncio.run(s.handlers.poll())
    assert task_id not in s.state["task_progress"]
    assert s.notify == [(s.module.t("task.completed"), "positive")]
    assert s.files.loads == 1


def test_fast_failed_task_reports_real_error_only(subscription):
    s = subscription
    asyncio.run(s.handlers.init_active_tasks())
    s.advance()
    task_id = chunk_service.create_chunk_add_task(s.file_id, "标题", "private body")
    task_service.update_task_status(task_id, "failed", error_message="embedding failed", error_code="EMBEDDING_FAILED")
    asyncio.run(s.handlers.poll())
    asyncio.run(s.handlers.poll())
    assert s.notify == [(f"{s.module.t('task.failed')}: embedding failed", "negative")]
    assert "private body" not in json.dumps(s.notify, ensure_ascii=False)
    assert task_id not in s.state["task_progress"]


def test_old_finished_tasks_are_not_replayed_on_page_load(subscription):
    s = subscription
    done = chunk_service.create_chunk_add_task(s.file_id, "a", "b")
    task_service.update_task_status(done, "completed", progress=100)
    s.advance()
    asyncio.run(s.handlers.init_active_tasks())
    asyncio.run(s.handlers.poll())
    assert s.notify == [] and s.state["task_progress"] == {} and s.files.loads == 0


def test_processing_task_survives_page_reload(subscription):
    s = subscription
    task_id = file_service.reindex_file(s.file_id)["task_id"]
    assert task_service.claim_next_pending_task()["id"] == task_id
    task_service.update_page_progress(task_id, 2, 4, 1, 30, stage="embedding")
    asyncio.run(s.handlers.init_active_tasks())
    snapshot = s.state["task_progress"][task_id]
    assert snapshot["status"] == "processing" and snapshot["stage"] == "embedding"
    assert s.ui_refs["file_list_container"].refreshes == 1


def test_task_for_unknown_file_triggers_full_refresh(subscription):
    s = subscription
    s.state["files_data"] = []
    asyncio.run(s.handlers.init_active_tasks())
    s.advance()
    file_service.reindex_file(s.file_id)
    asyncio.run(s.handlers.poll())
    assert s.files.loads == 1
    s.state["files_data"] = [{"id": s.file_id}]
    asyncio.run(s.handlers.poll())
    assert s.files.loads == 1


def test_safe_cancel_is_silent_and_deleted_row_is_dropped(subscription):
    s = subscription
    asyncio.run(s.handlers.init_active_tasks())
    s.advance()
    task_id = chunk_service.create_chunk_add_task(s.file_id, "a", "b")
    asyncio.run(s.handlers.poll())
    assert task_id in s.state["task_progress"]
    s.advance()
    task_service.cancel_task(task_id)
    asyncio.run(s.handlers.poll())
    assert task_id not in s.state["task_progress"] and s.notify == [] and s.files.loads == 1
    s.advance()
    other = chunk_service.create_chunk_add_task(s.file_id, "c", "d")
    asyncio.run(s.handlers.poll())
    assert other in s.state["task_progress"]
    with database.get_db_cursor(write=True) as cursor:
        cursor.execute("DELETE FROM tasks WHERE id = ?", (other,))
    asyncio.run(s.handlers.poll())
    assert other not in s.state["task_progress"] and s.files.loads == 2


def test_completed_task_updated_before_marker_still_clears(subscription):
    s = subscription
    asyncio.run(s.handlers.init_active_tasks())
    s.advance()
    task_id = file_service.reindex_file(s.file_id)["task_id"]
    asyncio.run(s.handlers.poll())
    with database.get_db_cursor(write=True) as cursor:
        cursor.execute("UPDATE tasks SET status='completed', progress=100, updated_at='2000-01-01T00:00:00' WHERE id=?", (task_id,))
    asyncio.run(s.handlers.poll())
    assert task_id not in s.state["task_progress"]
    assert s.notify == [(s.module.t("task.completed"), "positive")]


@pytest.mark.parametrize("tracked", [False, True])
@pytest.mark.parametrize("updated_at", [
    "2025-12-31T23:59:59", "2026-01-01T00:00:00", "2026-01-01T00:00:01",
])
def test_terminal_task_marker_boundary_is_idempotent(subscription, tracked, updated_at):
    s = subscription
    task_id = chunk_service.create_chunk_add_task(s.file_id, "边界", "正文")
    task_service.update_task_status(task_id, "completed", progress=100)
    task = task_service.get_task(task_id)
    assert task is not None
    task["updated_at"] = updated_at
    marker = "2026-01-01T00:00:00"
    if tracked:
        s.state["task_progress"][task_id] = s.handlers._task_snapshot(task)
    expected = tracked or updated_at > marker
    _, refresh = s.handlers._absorb([task], marker)
    assert refresh is expected
    assert task_id not in s.state["task_progress"]
    s.handlers._absorb([task], s.handlers._marker)
    assert s.notify == ([(s.module.t("task.completed"), "positive")] if expected else [])


def test_register_untracked_files(knowledge_base):
    originals = file_service.get_originals_dir()
    originals.mkdir(parents=True, exist_ok=True)
    (originals / "笔记.md").write_text("# 标题\n正文", encoding="utf-8")
    result = file_service.register_untracked_files()
    assert [item["filename"] for item in result["created"]] == ["笔记.md"]
    created = result["created"][0]
    assert task_service.get_task(created["task_id"])["status"] == "pending"
    (originals / "副本.md").write_text("# 标题\n正文", encoding="utf-8")
    again = file_service.register_untracked_files()
    assert again["created"] == [] and again["skipped"] == ["副本.md"]


# ---- 任务模型 -----------------------------------------------------------------


def test_task_statuses_report_missing_ids_without_inputs(knowledge_base):
    file_id = file_service.create_empty_file("状态")["file_id"]
    from indexing.mcp.tools.task_tools import get_tasks_status
    ids = [chunk_service.create_chunk_add_task(file_id, f"t{i}", "private-payload") for i in range(5)]
    assert task_service.claim_next_pending_task()["id"] == ids[0]
    task_service.update_task_status(ids[1], "completed", result={"chunk_id": 456})
    task_service.update_task_status(ids[2], "failed", error_message="embedding failed")
    task_service.cancel_task(ids[3])
    response = get_tasks_status([ids[1], ids[0], 999999, ids[2], ids[1], ids[3], ids[4]])
    data = response["data"]
    assert not response["success"] and data["not_found"] == [999999]
    assert [row["task_id"] for row in data["tasks"]] == [ids[1], ids[0], ids[2], ids[3], ids[4]]
    assert data["summary"] == dict(pending=1, processing=1, completed=1, failed=1, cancelled=1, not_found=1, total=6)
    assert data["unfinished_task_ids"] == [ids[0], ids[4]] and data["poll_after_ms"] > 0
    assert not data["all_done"] and not data["all_succeeded"]
    assert "private-payload" not in json.dumps(data)
    assert data["tasks"][0]["chunk_id"] == 456 and data["tasks"][2]["error_message"] == "embedding failed"
    for task_ids, done, succeeded in [([ids[1], ids[2], ids[3]], True, False), ([ids[1]], True, True), ([999999], False, False)]:
        data = get_tasks_status(task_ids)["data"]
        assert data["all_done"] is done and data["all_succeeded"] is succeeded


def test_claiming_is_atomic_and_serializes_files(knowledge_base):
    a = file_service.create_empty_file("a")["file_id"]
    b = file_service.create_empty_file("b")["file_id"]
    first, second, third = [chunk_service.create_chunk_add_task(a, f"a{i}", "body") for i in range(3)]
    other = chunk_service.create_chunk_add_task(b, "b", "body")
    with database.get_db_cursor(write=True) as cursor:
        cursor.execute("UPDATE tasks SET created_at = '2000-01-01' WHERE id = ?", (third,))
    with ThreadPoolExecutor(max_workers=6) as executor:
        claimed = list(executor.map(lambda _: task_service.claim_next_pending_task(), range(6)))
    assert {task["id"] for task in claimed if task} == {first, other}
    assert task_service.get_task(second)["status"] == task_service.get_task(third)["status"] == "pending"
    task_service.update_task_status(first, "failed", error_message="expected")
    assert task_service.claim_next_pending_task()["id"] == second
    task_service.update_task_status(second, "cancelled")
    assert task_service.claim_next_pending_task()["id"] == third
    with pytest.raises(BusinessError, match="在途"):
        file_service.reindex_file(a)


def test_partial_acceptance_injected_in_shared_business(knowledge_base, monkeypatch):
    original = chunk_service.create_chunk_add_task
    def fail_one(file_id, doc_title, chunk_text, request_key=None):
        if doc_title == "入队失败":
            raise RuntimeError("模拟写入失败")
        return original(file_id, doc_title, chunk_text, request_key)
    monkeypatch.setattr(chunk_service, "create_chunk_add_task", fail_one)
    from indexing.mcp.tools.chunk_tools import ChunkInput, create_chunks
    file_id = file_service.create_empty_file("批量")["file_id"]
    result = create_chunks(file_id, [ChunkInput(doc_title=t, chunk_text="正文") for t in ("一", "  ", "入队失败", "四")])
    data = result["data"]
    assert not result["success"] and data["accepted_count"] == data["rejected_count"] == 2
    assert [item["index"] for item in data["items"] if item["task_id"] is None] == [1, 2]
    assert data["task_ids"] == [data["items"][0]["task_id"], data["items"][3]["task_id"]]
    assert len(task_service.get_active_tasks()) == 2


# ---- 原页关联与受控路径 -----------------------------------------------------


@pytest.mark.parametrize("file_type, segments, source_page", [
    ("pdf", ["报告_修订版", "第2页"], 2),
    ("pdf", ["报告_修订版", "第2页", "第2部分"], 2),
    ("docx", ["报告_修订版", "第2页"], 2),
    ("pptx", ["报告_修订版", "第2页", "第2部分"], 2),
    ("md", ["笔记", "概述"], None),
    ("md", ["笔记", "第2页"], None),
])
def test_chunk_edit_preserves_source_location(knowledge_base, file_type, segments, source_page):
    from indexing.repositories import ChunkRepository
    from indexing.services.chunking.utils import build_chunk
    from indexing.services.page_render import page_number_from_heading
    from indexing.utils import serialize_float32
    file_id = file_service.create_empty_file("报告")["file_id"]
    working = file_service.managed_path(file_service.get_file_by_id(file_id)["file_path"])
    original_path = file_service.get_originals_dir() / f"original.{file_type}"
    original_path.write_bytes(b"source")
    with database.get_db_cursor(write=True) as cursor:
        cursor.execute("UPDATE files SET original_file_type=?, original_file_path=? WHERE id=?",
                       (file_type, str(original_path), file_id))
    original = build_chunk(segments, "原正文")
    chunk_id = ChunkRepository().insert(file_id, embedding=serialize_float32([0.0, 0.0]), **original)
    for title, leaf in [("修改后的标题", "修改后的标题"), ("新名称_第99页", "第99页")]:
        updated = chunk_service.update_chunk_title(chunk_id, title)
        expected = original
        assert updated["doc_title"] == title and updated["heading_path"] == expected["heading_path"]
        if source_page:
            assert page_number_from_heading(updated["heading_path"]) == source_page
        heading = "#" * max(2, expected["heading_level"])
        assert working.read_text(encoding="utf-8") == f"{heading} {leaf}\n\n原正文\n\n"
    chunk_service.create_chunk_update_task(chunk_id, "修改后的正文")
    knowledge_base.drain()
    updated = chunk_service.get_chunk_by_id(chunk_id)
    assert updated["heading_path"] == expected["heading_path"] and updated["chunk_text"] == "修改后的正文"
    assert working.read_text(encoding="utf-8") == f"{heading} {leaf}\n\n修改后的正文\n\n"


def test_unmanaged_paths_are_rejected(knowledge_base, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(BusinessError, match="受控"):
        file_service.managed_path(outside)


def test_gui_form_materializes_legacy_key_and_rotates_one(knowledge_base, monkeypatch):
    pytest.importorskip("nicegui")
    from app.ui.handlers import settings_handlers
    from indexing.services import config_service
    # 旧配置：只有共享密钥，且进程内生效配置与磁盘一致（服务刚以该配置启动）。
    path = settings._get_config_file_path()
    content = knowledge_base.settings.model_dump()
    content["mcp"]["api_key"] = "legacy-key"
    content["mcp"]["index_api_key"] = None
    path.write_text(json.dumps(content), encoding="utf-8")
    settings.reload_settings()
    notify = Mock()
    monkeypatch.setattr(settings_handlers.ui, "notify", notify)
    form = {}
    handler = settings_handlers.SettingsHandlers(form)
    handler.init_settings_form()
    assert form["mcp_api_key"] == form["mcp_index_api_key"] == "legacy-key"
    handler.save_settings_form()
    saved = config_service.get_saved_settings()
    assert saved.mcp.get_api_key("index") == "legacy-key"
    assert notify.call_args.kwargs["type"] == "positive"
    monkeypatch.setattr(settings_handlers, "generate_api_key", lambda: "new-key")
    handler.regenerate_mcp_api_key(Mock(), "index")
    handler.save_settings_form()
    saved = config_service.get_saved_settings()
    assert saved.mcp.get_api_key("index") == "new-key" and saved.mcp.get_api_key("retrieval") == "legacy-key"
    assert settings.get_settings().mcp.get_api_key("index") == "new-key"
    assert notify.call_args.kwargs["type"] == "positive"
