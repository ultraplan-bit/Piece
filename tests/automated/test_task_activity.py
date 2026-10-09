"""任务活动入口的离线回归：状态归类、操作可用性与面板刷新节流。

不使用网络与真实嵌入；渲染检查复用 NiceGUI 的元素树，不依赖浏览器。
"""

import asyncio

import pytest

pytest.importorskip("nicegui")


# ==================== 纯函数：状态归类与操作可用性 ====================

def test_activity_status_distinguishes_cancelling_and_terminal():
    from app.ui.views import task_activity as module

    assert module.activity_status({"status": "pending"}) == "pending"
    assert module.activity_status({"status": "processing", "stage": "parsing"}) == "processing"
    # 处理中且已请求取消：单独归为一态，区别于一般处理中
    assert module.activity_status({"status": "processing", "stage": "cancelling"}) == "cancelling"
    assert module.activity_status({"status": "completed"}) == "completed"
    assert module.activity_status({"status": "failed"}) == "failed"
    assert module.activity_status({"status": "cancelled"}) == "cancelled"


def test_cancel_only_for_real_cancellable_tasks():
    from app.ui.views import task_activity as module

    assert module.is_cancellable({"status": "pending"})
    assert module.is_cancellable({"status": "processing", "stage": "parsing"})
    # 正在取消中、以及任何终态都不可再取消
    assert not module.is_cancellable({"status": "processing", "stage": "cancelling"})
    assert not module.is_cancellable({"status": "completed"})
    assert not module.is_cancellable({"status": "failed"})


def test_retry_only_for_failed_or_cancelled_without_published_result():
    from app.ui.views import task_activity as module

    assert module.is_retryable({"status": "failed", "result": None})
    assert module.is_retryable({"status": "cancelled", "result": None})
    # 已有已发布结果的任务不可重试（与仓储 TASK_NOT_RETRYABLE 约束一致）
    assert not module.is_retryable({"status": "failed", "result": {"chunks": 3}})
    assert not module.is_retryable({"status": "completed", "result": None})


def test_split_activity_orders_newest_first_and_groups():
    from app.ui.views import task_activity as module

    tasks = [
        {"id": 1, "status": "pending"},
        {"id": 2, "status": "processing", "stage": "cancelling"},
        {"id": 3, "status": "pending"},
        {"id": 4, "status": "completed"},
        {"id": 5, "status": "failed", "result": None},
    ]
    active, recent = module.split_activity(tasks)
    assert [task["id"] for task in active] == [3, 2, 1]
    assert [task["id"] for task in recent] == [5, 4]


def test_open_file_prefers_injected_callback_then_file_handlers():
    from app.ui.views import task_activity as module

    opened = []
    asyncio.run(module._open_file({"open_task_file": lambda fid: opened.append(fid)}, None, 5))
    assert opened == [5]

    class _FileHandlers:
        def __init__(self):
            self.calls = []

        async def open_reader(self, file_id):
            self.calls.append(file_id)

    handlers = _FileHandlers()
    asyncio.run(module._open_file({}, handlers, 7))
    assert handlers.calls == [7]


# ==================== 渲染：只给可操作的任务显示取消/重试 ====================

def _seed_tasks(file_id):
    """写入四种任务状态，返回最新任务行列表。"""
    from indexing import database
    from indexing.services import task_service

    pending = task_service.create_task("待处理", file_id, task_type="chunk_add",
                                       input_data={"doc_title": "t", "chunk_text": "c"})
    cancelling = task_service.create_task("取消中", file_id, task_type="chunk_add",
                                          input_data={"doc_title": "t", "chunk_text": "c"})
    failed = task_service.create_task("失败", file_id, task_type="chunk_add",
                                      input_data={"doc_title": "t", "chunk_text": "c"})
    completed = task_service.create_task("完成", file_id, task_type="chunk_add",
                                         input_data={"doc_title": "t", "chunk_text": "c"})
    with database.get_db_cursor(write=True) as cursor:
        cursor.execute("UPDATE tasks SET status='processing', stage='cancelling' WHERE id=?",
                       (cancelling,))
    task_service.update_task_status(failed, "failed", error_message="嵌入失败")
    task_service.update_task_status(completed, "completed", progress=100)
    return pending, cancelling, failed, completed


def test_render_marks_states_and_offers_actions(knowledge_base):
    from nicegui import ui
    from app.i18n import t
    from app.ui.views import task_activity as module
    from indexing.services import file_service, task_service

    file_id = file_service.create_empty_file("活动源")["file_id"]
    _seed_tasks(file_id)

    state = {"task_activity": task_service.list_tasks(limit=20)["tasks"],
             "task_progress": {"1": {}}}
    refs: dict = {}
    with ui.column() as container:
        module.render_task_activity(state, refs, None)

    rows = [element for element in container.descendants()
            if "task-activity-row" in element._classes]
    assert {row._props.get("data-status") for row in rows} == {
        "pending", "cancelling", "failed", "completed",
    }

    buttons = [element for element in container.descendants() if isinstance(element, ui.button)]
    labels = [button._props.get("aria-label") for button in buttons]
    # 入口按钮 + 一个取消（仅 pending）+ 一个重试（仅 failed）
    assert labels.count(t("task.cancel")) == 1
    assert labels.count(t("task.retry")) == 1
    assert t("task_activity.open") in labels
    # 进度活动数徽标
    assert any("task-activity-badge" in element._classes for element in container.descendants())

    # 失败原因可读
    assert any(getattr(element, "text", None) == "嵌入失败"
               for element in container.descendants())
    # 加载回调注册到 ui_refs，供面板打开时调用
    assert "task_activity_panel" in refs
    container.delete()


def test_render_empty_state_has_no_rows(knowledge_base):
    from nicegui import ui
    from app.i18n import t
    from app.ui.views import task_activity as module

    state = {"task_activity": [], "task_progress": {}}
    refs: dict = {}
    with ui.column() as container:
        module.render_task_activity(state, refs, None)

    assert not any("task-activity-row" in element._classes
                   for element in container.descendants())
    assert any(getattr(element, "text", None) == t("task_activity.empty")
               for element in container.descendants())
    # 面板重建即视为关闭，避免刷新已销毁的旧面板
    assert state["task_activity_open"] is False
    container.delete()


def test_opening_panel_refreshes_uploads_even_when_task_table_is_unchanged(knowledge_base, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from nicegui import ui
    from app.ui.views import task_activity as module

    state = {"task_activity": [], "task_progress": {}}
    reload = AsyncMock()
    refs = {"load_task_activity": reload}
    with ui.column() as container:
        module.render_task_activity(state, refs)
    refresh = Mock()
    monkeypatch.setattr(refs["task_activity_panel"], "refresh", refresh)
    state.update(uploading_count=1, uploading_files=["held.pdf"])
    refs["refresh_upload_activity"]()
    refresh.assert_not_called()
    on_show = next(listener.handler for listener in refs["task_activity_menu"]._event_listeners.values()
                   if listener.type == "show")
    asyncio.run(on_show())
    refresh.assert_called_once()
    reload.assert_awaited_once()
    assert state["task_activity_open"] is True
    container.delete()


# ==================== 轮询：仅在面板打开时加载与重绘 ====================

class _Refreshable:
    def __init__(self):
        self.refreshes = 0
        self.is_deleted = False

    def refresh(self):
        self.refreshes += 1


def test_poll_refreshes_activity_only_when_panel_open(knowledge_base, monkeypatch):
    from app.ui.handlers import task_handlers as module
    from app.ui.handlers.task_handlers import TaskHandlers
    from indexing.services import file_service, task_service

    monkeypatch.setattr(module, "ui", type("U", (), {"notify": staticmethod(lambda *a, **k: None)})())
    file_id = file_service.create_empty_file("订阅源")["file_id"]
    task_service.create_task("待处理", file_id, task_type="chunk_add",
                             input_data={"doc_title": "t", "chunk_text": "c"})

    state = {"files_data": [{"id": file_id}], "selected_file_id": None}
    refs = {"file_list_container": _Refreshable(), "task_activity_panel": _Refreshable()}
    handlers = TaskHandlers(state, refs)
    asyncio.run(handlers.init_active_tasks())

    # 面板关闭：不查询、不重绘
    asyncio.run(handlers.poll())
    assert refs["task_activity_panel"].refreshes == 0
    assert state["task_activity"] == []

    # 面板打开：跟随轮询载入最近任务并刷新一次
    state["task_activity_open"] = True
    asyncio.run(handlers.poll())
    assert refs["task_activity_panel"].refreshes == 1
    assert state["task_activity"] and state["task_activity"][0]["original_filename"] == "待处理"
    # 快照未变化：不再重建面板，避免打断键盘焦点与操作按钮
    asyncio.run(handlers.poll())
    assert refs["task_activity_panel"].refreshes == 1
    # 加载回调已注册，供顶栏按需触发
    assert refs["load_task_activity"] == handlers.refresh_activity


class _Badge:
    def __init__(self):
        self.is_deleted = False
        self.text = None
        self.visible = None

    def set_text(self, value):
        self.text = value

    def set_visibility(self, value):
        self.visible = value


def test_badge_updates_without_rebuilding_button(knowledge_base, monkeypatch):
    from app.ui.handlers import task_handlers as module
    from app.ui.handlers.task_handlers import TaskHandlers
    from indexing.services import file_service, task_service

    monkeypatch.setattr(module, "ui", type("U", (), {"notify": staticmethod(lambda *a, **k: None)})())
    file_id = file_service.create_empty_file("徽标源")["file_id"]
    task_id = task_service.create_task("待处理", file_id, task_type="chunk_add",
                                       input_data={"doc_title": "t", "chunk_text": "c"})

    state = {"files_data": [{"id": file_id}], "selected_file_id": None}
    badge = _Badge()
    refs = {"file_list_container": _Refreshable(), "task_activity_panel": _Refreshable(),
            "task_activity_badge": badge}
    handlers = TaskHandlers(state, refs)
    asyncio.run(handlers.init_active_tasks())

    # 面板关闭：计数仍随 task_progress 缓存原地更新
    asyncio.run(handlers.poll())
    assert state["task_activity_open"] is False
    assert badge.text == "1" and badge.visible is True

    # 任务终态后计数归零并隐藏，不重建按钮、不查询活动面板
    task_service.update_task_status(task_id, "completed", progress=100)
    asyncio.run(handlers.poll())
    asyncio.run(handlers.poll())
    assert badge.text == "" and badge.visible is False
    assert refs["task_activity_panel"].refreshes == 0
