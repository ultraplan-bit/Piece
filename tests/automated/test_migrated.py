"""由旧回归迁入的有效合同：新任务模型、受控路径与临时配置，不保留旧负载兼容。"""

import asyncio
import copy
import json
import shlex
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from indexing import database, settings
from indexing.services import chunk_service, file_service, task_service
from indexing.services.errors import BusinessError


def _settle():
    time.sleep(0.003)


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
                           notify=notify, files=files, file_id=file_id)


def test_task_from_any_source_shows_up_and_finishes_once(subscription):
    s = subscription
    asyncio.run(s.handlers.init_active_tasks())
    _settle()
    task_id = file_service.reindex_file(s.file_id)["task_id"]
    asyncio.run(s.handlers.poll())
    assert s.state["task_progress"][task_id]["status"] == "pending"
    assert s.ui_refs["file_list_container"].refreshes == 1 and s.files.loads == 0
    _settle()
    task_service.update_page_progress(task_id, 3, 10, 5, 40, stage="parsing")
    asyncio.run(s.handlers.poll())
    snapshot = s.state["task_progress"][task_id]
    assert snapshot["progress"] == 40 and snapshot["stage"] == "parsing"
    _settle()
    task_service.update_task_status(task_id, "completed", progress=100)
    for _ in range(4):
        asyncio.run(s.handlers.poll())
    assert task_id not in s.state["task_progress"]
    assert s.notify == [(s.module.t("task.completed"), "positive")]
    assert s.files.loads == 1


def test_fast_failed_task_reports_real_error_only(subscription):
    s = subscription
    asyncio.run(s.handlers.init_active_tasks())
    _settle()
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
    _settle()
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
    _settle()
    file_service.reindex_file(s.file_id)
    asyncio.run(s.handlers.poll())
    assert s.files.loads == 1
    s.state["files_data"] = [{"id": s.file_id}]
    asyncio.run(s.handlers.poll())
    assert s.files.loads == 1


def test_safe_cancel_is_silent_and_deleted_row_is_dropped(subscription):
    s = subscription
    asyncio.run(s.handlers.init_active_tasks())
    _settle()
    task_id = chunk_service.create_chunk_add_task(s.file_id, "a", "b")
    asyncio.run(s.handlers.poll())
    assert task_id in s.state["task_progress"]
    _settle()
    task_service.cancel_task(task_id)
    asyncio.run(s.handlers.poll())
    assert task_id not in s.state["task_progress"] and s.notify == [] and s.files.loads == 1
    _settle()
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
    _settle()
    task_id = file_service.reindex_file(s.file_id)["task_id"]
    asyncio.run(s.handlers.poll())
    with database.get_db_cursor(write=True) as cursor:
        cursor.execute("UPDATE tasks SET status='completed', progress=100, updated_at='2000-01-01T00:00:00' WHERE id=?", (task_id,))
    asyncio.run(s.handlers.poll())
    assert task_id not in s.state["task_progress"]
    assert s.notify == [(s.module.t("task.completed"), "positive")]


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
    from indexing.services.chunking.utils import build_chunk, heading_path_from_doc_title
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
        expected = original if source_page else heading_path_from_doc_title(title)
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


# ---- MCP 密钥、配置模板与端口 ---------------------------------------------------


@pytest.fixture
def mcp_settings(knowledge_base):
    return knowledge_base.settings


@pytest.mark.parametrize("payload, expected", [
    ({"api_key": "legacy"}, ("legacy", "legacy")),
    ({"api_key": "legacy", "index_api_key": None}, ("legacy", "legacy")),
    ({"api_key": "read", "index_api_key": "write"}, ("read", "write")),
    ({"api_key": "read", "index_api_key": ""}, ("read", "")),
    ({"api_key": "", "index_api_key": "write"}, ("", "write")),
    ({}, ("", "")),
])
def test_key_selection_and_empty_key_semantics(mcp_settings, payload, expected):
    mcp_settings.mcp = settings.McpSettings(**payload)
    for service, key in zip(("retrieval", "index"), expected):
        assert settings.get_mcp_api_key(service) == key
        assert settings.is_mcp_auth_enabled(service) == bool(key)


def test_new_install_generates_independent_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DEFAULT_DATA_PATH", tmp_path / "fresh")
    monkeypatch.setattr(settings, "_settings", None)
    config = settings.load_settings()
    assert len(config.mcp.api_key) == 32 and len(config.mcp.index_api_key) == 32
    assert config.mcp.api_key != config.mcp.index_api_key != config.api.admin_key
    stored = json.loads((tmp_path / "fresh" / "config.json").read_text(encoding="utf-8"))
    assert stored["mcp"]["index_api_key"] == config.mcp.index_api_key


def test_unknown_service_is_rejected():
    with pytest.raises(ValueError, match="未知 MCP 服务"):
        settings.McpSettings(api_key="secret").get_api_key("typo")


@pytest.mark.parametrize("service, correct, wrong", [("retrieval", "read", "write"), ("index", "write", "read")])
@pytest.mark.parametrize("method", ["on_call_tool", "on_list_tools"])
def test_service_auth_rejects_cross_service_and_missing_keys(mcp_settings, monkeypatch, service, correct, wrong, method):
    pytest.importorskip("fastmcp")
    from indexing.mcp import auth
    mcp_settings.mcp = settings.McpSettings(api_key="read", index_api_key="write")
    middleware = []
    auth.apply_bearer_auth(SimpleNamespace(add_middleware=middleware.append), service)
    handler = getattr(middleware[0], method)
    call_next = AsyncMock(return_value="allowed")
    for header in ("", f"Bearer {wrong}", "Basic abc", f"Bearer {correct}extra"):
        monkeypatch.setattr(auth, "get_http_headers", lambda h=header: {"authorization": h})
        with pytest.raises(auth.ToolError, match="Unauthorized"):
            asyncio.run(handler(None, call_next))
    call_next.assert_not_awaited()
    monkeypatch.setattr(auth, "get_http_headers", lambda: {"authorization": f"Bearer {correct}"})
    assert asyncio.run(handler(None, call_next)) == "allowed"


@pytest.mark.parametrize("service", ["retrieval", "index"])
def test_auth_switch_disables_both_services(mcp_settings, service):
    pytest.importorskip("fastmcp")
    from indexing.mcp import auth
    mcp_settings.mcp = settings.McpSettings(api_key="read", index_api_key="write", auth_enabled=False)
    middleware = []
    auth.apply_bearer_auth(SimpleNamespace(add_middleware=middleware.append), service)
    assert not middleware and not settings.is_mcp_auth_enabled(service)


def _parse_config(client, text):
    if client.get("format") == "bash":
        servers = {}
        for command in text.split("\n\n"):
            args = shlex.split(command.replace("\\\n", ""))
            server = {"url": args[-1], "headers": {}}
            if "--header" in args:
                key, value = args[args.index("--header") + 1].split(": ", 1)
                server["headers"][key] = value
            servers[args[-2]] = server
        return servers
    if client.get("format") == "toml":
        servers = tomllib.loads(text)["mcp_servers"]
        for server in servers.values():
            server["headers"] = server.pop("http_headers", {})
        return servers

    def walk(config):
        servers = {}
        if isinstance(config, dict):
            for name in ("piece-kb", "piece-index"):
                if isinstance(config.get(name), dict):
                    servers[name] = config[name]
            if config.get("name") in ("piece-kb", "piece-index"):
                servers[config["name"]] = config
            for value in config.values():
                servers.update(walk(value))
        elif isinstance(config, list):
            for value in config:
                servers.update(walk(value))
        return servers
    return walk(json.loads(text))


def _clients():
    try:
        from app.ui.views import mcp_config_view
    except ImportError:
        return []
    return mcp_config_view.MCP_CLIENTS


@pytest.mark.parametrize("client", _clients(), ids=lambda c: c["id"])
@pytest.mark.parametrize("auth_enabled", [True, False])
def test_all_client_templates_use_service_specific_credentials(mcp_settings, monkeypatch, client, auth_enabled):
    from app.ui.views import mcp_config_view
    monkeypatch.delenv("PIECE_INDEX_MCP_PORT", raising=False)
    mcp_settings.mcp = settings.McpSettings(port=9230, api_key="read-key", index_api_key="write-key", auth_enabled=auth_enabled)
    original = copy.deepcopy(client)
    text = mcp_config_view._get_config_json(client)
    servers = _parse_config(client, text)
    assert set(servers) == {"piece-kb", "piece-index"}
    for name, port, key in (("piece-kb", 9230, "read-key"), ("piece-index", 9231, "write-key")):
        server = servers[name]
        assert (server.get("url") or server.get("serverUrl") or server.get("httpUrl")) == f"http://localhost:{port}/mcp"
        assert server.get("headers", {}).get("Authorization") == (f"Bearer {key}" if auth_enabled else None)
    if not auth_enabled:
        assert "read-key" not in text and "write-key" not in text
    assert client == original


def test_custom_keys_are_escaped_and_not_treated_as_templates(mcp_settings):
    from app.ui.views import mcp_config_view
    key = "key-'\"\\-{port}-{index_port}-密钥"
    mcp_settings.mcp = settings.McpSettings(api_key=key, index_api_key="index")
    for client_id in ("cursor", "claude_code", "openai_codex"):
        client = next(c for c in mcp_config_view.MCP_CLIENTS if c["id"] == client_id)
        assert _parse_config(client, mcp_config_view._get_config_json(client))["piece-kb"]["headers"]["Authorization"] == f"Bearer {key}"


def test_index_port_follows_environment_override(mcp_settings, monkeypatch):
    from indexing.mcp.config import get_mcp_port
    mcp_settings.mcp.port = 9120
    monkeypatch.setenv("PIECE_INDEX_MCP_PORT", "9457")
    assert get_mcp_port() == get_mcp_port(9220) == 9457
    monkeypatch.setenv("PIECE_INDEX_MCP_PORT", "invalid")
    assert get_mcp_port() == 9121 and get_mcp_port(9220) == 9221


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
    assert notify.call_args.kwargs["type"] == "warning"
