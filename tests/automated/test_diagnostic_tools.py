"""诊断工具（召回测试 / 日志 / 云同步）的离线回归。

只用临时知识库与元素树渲染，不触网、不跑真实同步/重索引；纯函数直接断言，
渲染断言用与视图一致的 ``t()`` 文案，避免和翻译文件耦合。
"""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("nicegui")


# ==================== 通用工具 ====================

def _labels_text(container):
    from nicegui import ui
    return [element.text for element in container.descendants() if isinstance(element, ui.label)]


def _buttons(container):
    from nicegui import ui
    return [element for element in container.descendants() if isinstance(element, ui.button)]


def _button_labels(container):
    return [button._props.get("label") for button in _buttons(container)]


def _button_aria(container):
    return [button._props.get("aria-label") for button in _buttons(container)]


# ==================== 召回测试：条件快照与陈旧标注 ====================

def test_recall_stale_detects_condition_changes():
    from app.ui.views import recall_test_view as module

    assert module.is_stale({}) is False
    base = {
        "query": " 问题 ", "filenames": "a, b", "collection_names": ["B", "A"],
        "include_descendants": True,
        "last_run": {
            "conditions": {"query": "问题", "filenames": ["a", "b"],
                           "collections": ["A", "B"], "include_descendants": True},
            "at": "09:00:00",
        },
    }
    # 集合顺序不影响判定；首尾空白归一
    assert module.is_stale(base) is False
    base["query"] = "另一个问题"
    assert module.is_stale(base) is True
    base["query"] = "问题"
    base["include_descendants"] = False
    assert module.is_stale(base) is True


def test_locate_prefers_injected_callback_then_falls_back():
    from app.ui.views import recall_test_view as module

    item = {"doc": {"file_id": 7, "chunk_id": 9}}
    seen = []
    handler = module._locate_handler(
        {"open_recall_result": lambda file_id, chunk_id: seen.append((file_id, chunk_id))}, item
    )
    asyncio.run(handler())
    assert seen == [(7, 9)]

    opened = []
    handler = module._locate_handler({"open_task_file": lambda file_id: opened.append(file_id)}, item)
    asyncio.run(handler())
    assert opened == [7]


def test_recall_middle_shows_last_run_snapshot(knowledge_base):
    from nicegui import ui
    from app.i18n import t
    from app.ui.views import recall_test_view as module

    state = {
        "query": "当前问题", "filenames": "a, b", "collection_names": ["X"],
        "include_descendants": False,
        "last_run": {
            "conditions": {"query": "上次问题", "filenames": ["a", "b"],
                           "collections": ["X"], "include_descendants": False},
            "at": "09:30:00",
        },
    }
    refs: dict = {}
    with ui.column() as container:
        module.render_recall_test_middle(state, refs)

    texts = _labels_text(container)
    assert t("recall_test.last_run") in texts
    assert "上次问题" in texts
    assert t("recall_test.last_run_at", time="09:30:00") in texts
    assert "recall_last_run" in refs
    # 不提供取消按钮：后端链路没有取消能力
    icons = [button._props.get("icon") for button in _buttons(container)]
    assert "search" in icons
    assert not {"cancel", "stop", "close"} & set(icons)
    container.delete()


def test_recall_results_mark_stale_and_offer_locate(knowledge_base):
    from nicegui import ui
    from app.i18n import t
    from app.ui.views import recall_test_view as module
    from indexing.services.file_service import get_working_dir

    file_path = str(get_working_dir() / "doc.md")
    state = {
        "has_run": True, "error": None, "stats": None,
        "query": "新查询", "filenames": "", "collection_names": [], "include_descendants": True,
        "last_run": {
            "conditions": {"query": "旧查询", "filenames": [],
                           "collections": [], "include_descendants": True},
            "at": "10:00:00",
        },
        "results": [{
            "chunk_id": 2, "doc_title": "命中的卡片", "score": 0.9, "routes": ["exact_rank"],
            "doc": {"file_id": 1, "chunk_id": 2, "filename": "doc.md", "file_path": file_path,
                    "chunk_text": "正文内容", "heading_path": "第一章",
                    "chunk_index_in_file": 1, "total_chunks_in_file": 2, "collections": ["集合A"]},
        }],
    }
    refs = {"open_recall_result": lambda file_id, chunk_id: None}
    with ui.column() as container:
        module.render_recall_test_right(state, refs)

    texts = _labels_text(container)
    assert t("recall_test.stale") in texts
    assert "命中的卡片" in texts
    assert t("recall_test.locate") in _button_aria(container)
    container.delete()


# ==================== 日志：脱敏、正序、滚动跟随 ====================

def test_mask_sensitive_hides_credentials_by_default():
    from app.ui.views.logs_view import display_message, mask_sensitive

    assert mask_sensitive("password=secret123") == "password=***"
    assert mask_sensitive("https://user:pass@host/path") == "https://***:***@host/path"
    assert "abcd1234" not in mask_sensitive("Authorization: Bearer abcd1234")
    # JSON 带引号的键，以及含空格的引号值
    assert mask_sensitive('"api_key": "secret123"') == '"api_key": "***"'
    assert mask_sensitive('password="a b"') == 'password="***"'
    assert mask_sensitive("普通日志没有敏感内容") == "普通日志没有敏感内容"
    # 默认脱敏，只有显式取原文才给原文
    assert display_message("token=abc") == "token=***"
    assert display_message("token=abc", reveal=True) == "token=abc"


def test_should_tail_only_when_near_bottom():
    from app.ui.views.logs_view import should_tail

    assert should_tail({"position": None}) is True
    assert should_tail({"position": 980, "size": 1000, "container": 300}) is True
    # 已向上滚动：刷新后保持位置，不强行拉到底
    assert should_tail({"position": 100, "size": 1000, "container": 300}) is False


def test_is_expandable_for_multiline_and_long_messages():
    from app.ui.views.logs_view import is_expandable

    assert is_expandable({"message": "Traceback ...\n  File x"}) is True
    assert is_expandable({"message": "x" * 300}) is True
    assert is_expandable({"message": "short info"}) is False


def test_format_copy_text_is_chronological_and_masked():
    from app.ui.views.logs_view import format_copy_text

    logs = [
        {"timestamp": "2026-10-09T10:00:00", "level": "ERROR", "module": "m", "message": "password=secret123"},
    ]
    masked = format_copy_text(logs, reveal=False)
    assert "secret123" not in masked
    assert "10:00:00 [ERROR] m: password=***" == masked
    assert "secret123" in format_copy_text(logs, reveal=True)


def test_logs_render_single_pane_with_masked_chronological_content(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.i18n import t
    from app.ui.views import logs_view as module

    # 渲染不触发真实刷新；仅检查元素树
    monkeypatch.setattr(module, "_refresh_logs", lambda ui_refs, **kwargs: None)
    logs = [
        {"timestamp": "2026-10-09T09:00:00", "level": "INFO", "module": "m1", "message": "第一条"},
        {"timestamp": "2026-10-09T09:00:01", "level": "ERROR", "module": "m2",
         "message": "password=secret123\nTraceback (most recent call last):"},
    ]
    refs = {"logs_data": logs}
    with ui.column() as container:
        module.render_logs_right(refs)

    texts = _labels_text(container)
    # 正序：较早的日志在前
    assert texts.index("第一条") < texts.index("password=***")
    # 多行堆栈只留首行摘要，完整内容进详情
    assert "Traceback (most recent call last):" not in texts
    assert all("secret123" not in (text or "") for text in texts)
    aria = _button_aria(container)
    assert t("logs.copy") in aria
    assert t("logs.clear") in aria
    assert t("logs.view_exception") in aria
    # 状态持久在 ui_refs 中，供跨视图切换复用
    assert refs["log_filter"] == {"level": None}
    assert refs["auto_refresh_state"] == {"enabled": False}
    container.delete()


def test_refresh_logs_restores_entry_position_and_only_tails_at_bottom(monkeypatch):
    from app.ui.views import logs_view as module

    monkeypatch.setattr(module, "get_log_buffer", lambda level=None, limit=0: [{"message": "m"}])
    monkeypatch.setattr(module, "count_log_buffer", lambda level=None: 1)

    class _Area:
        is_deleted = False

        def __init__(self):
            self.calls = []

        def scroll_to(self, **kwargs):
            self.calls.append(kwargs)

    class _Content:
        is_deleted = False

        def refresh(self):
            pass

    def make_refs(position):
        return {
            "log_filter": {"level": None}, "stats": {"total": 0},
            "logs_content": _Content(),
            "logs_scroll_area": _Area(),
            "log_scroll": {"position": position, "size": 1000, "container": 300},
        }

    # 入页：有历史位置就恢复，不被强制拉到底
    refs = make_refs(120)
    module._refresh_logs(refs, restore=True)
    assert refs["logs_scroll_area"].calls == [{"pixels": 120}]

    # 入页：从未滚动过则贴底看最新
    refs = make_refs(None)
    module._refresh_logs(refs, restore=True)
    assert refs["logs_scroll_area"].calls == [{"percent": 1.0}]

    # 自动刷新：向上阅读时保持原位
    refs = make_refs(100)
    module._refresh_logs(refs)
    assert refs["logs_scroll_area"].calls == [{"pixels": 100}]

    # 自动刷新：贴底时跟随最新
    refs = make_refs(980)
    module._refresh_logs(refs)
    assert refs["logs_scroll_area"].calls == [{"percent": 1.0}]


def test_logs_filter_and_switches_persist_across_rerender(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views import logs_view as module

    class _FakeTimer:
        is_deleted = False

        def __init__(self):
            self.canceled = False

        def cancel(self):
            self.canceled = True

    monkeypatch.setattr(module, "_refresh_logs", lambda ui_refs, **kwargs: None)
    monkeypatch.setattr(module.ui, "timer", lambda *args, **kwargs: _FakeTimer())
    refs = {"logs_data": []}
    with ui.column():
        module.render_logs_right(refs)

    refs["log_filter"]["level"] = "ERROR"
    refs["auto_refresh_state"]["enabled"] = True
    with ui.column() as container:
        module.render_logs_right(refs)

    selects = [element for element in container.descendants() if isinstance(element, ui.select)]
    assert selects and selects[0].value == "ERROR"
    switches = [element for element in container.descendants() if isinstance(element, ui.switch)]
    assert any(switch.value is True for switch in switches)
    container.delete()


# ==================== 云同步：设置入口、真实策略、上次结果 ====================

def test_open_webdav_settings_invokes_injected_callback():
    from app.ui.views import cloud_sync_view as module

    calls = []
    asyncio.run(module._open_webdav_settings({"open_webdav_settings": lambda: calls.append("open")}))
    assert calls == ["open"]
    asyncio.run(module._open_webdav_settings({}))  # 未注入时静默


def test_cloud_sync_middle_links_settings_and_shows_real_strategy(knowledge_base):
    from nicegui import ui
    from app.i18n import t
    from app.ui.views import cloud_sync_view as module

    state = {
        "is_syncing": False, "last_sync": None, "logs": [],
        "last_result": {
            "success": True, "uploaded": 2, "downloaded": 1, "skipped": 3, "errors": 0,
            "message": "同步完成: ↑2 ↓1，错误 0 项", "finished_at": "10:00:00",
        },
    }
    handlers = SimpleNamespace(is_enabled=lambda: False, do_sync=lambda: None, clear_logs=lambda: None)
    refs = {"open_webdav_settings": lambda: None}
    with ui.column() as container:
        module.render_cloud_sync_middle(state, refs, handlers)

    texts = _labels_text(container)
    assert t("cloud_sync.go_settings") in _button_labels(container)
    assert t("cloud_sync.sync_hint") in texts  # 展示现有实际策略
    assert t("cloud_sync.last_result") in texts
    assert "同步完成: ↑2 ↓1，错误 0 项" in texts
    assert t("cloud_sync.result_counts", uploaded=2, downloaded=1, skipped=3, errors=0) in texts
    container.delete()


def test_sync_handlers_mirror_real_last_result(monkeypatch):
    from datetime import datetime
    from app.ui.handlers import sync_handlers as module

    result = SimpleNamespace(success=False, uploaded=["a"], downloaded=["b", "c"],
                             skipped=[], errors=["e1"], message="部分失败")
    service = SimpleNamespace(
        is_enabled=lambda: True, is_running=lambda: False,
        get_log_version=lambda: 0, get_logs=lambda: [],
        last_finished_at=datetime(2026, 10, 9, 10, 0, 0), last_result=result,
    )
    monkeypatch.setattr(module, "get_sync_service", lambda: service)

    state = {"is_syncing": False, "last_sync": None, "logs": []}
    module.SyncHandlers(state, {})

    mirrored = state["last_result"]
    assert mirrored["success"] is False
    assert (mirrored["uploaded"], mirrored["downloaded"], mirrored["errors"]) == (1, 2, 1)
    assert mirrored["finished_at"] == "10:00:00"
    assert state["last_sync"] == "2026-10-09 10:00"
