"""工作台交互回归：退出保护、目录选中态与双语界面文案。"""
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.i18n import t


def test_edit_guard_tracks_values_and_requires_explicit_discard(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui import components

    confirmations = []
    monkeypatch.setattr(components, "confirm_dialog", lambda **kwargs: confirmations.append(kwargs))
    with ui.dialog() as dialog, ui.card():
        title = ui.input(value="原标题")
        body = ui.textarea(value="原正文")
        close = components.guard_unsaved(dialog)
    status = next(element for element in dialog.descendants()
                  if isinstance(element, ui.label) and element.text == t("workspace.unsaved"))
    dialog.open()
    body.value = "未保存正文"
    assert status.visible
    close()
    assert dialog.value and body.value == "未保存正文"
    assert confirmations[-1]["cancel_text"] == t("workspace.keep_editing")
    # 不执行确认回调即继续编辑；把值还原后取消不应再提示。
    body.value = "原正文"
    assert not status.visible
    close()
    assert not dialog.value and len(confirmations) == 1
    dialog.open()
    title.value = "另一个标题"
    close()
    confirmations[-1]["on_confirm"]()
    assert not dialog.value
    assert dialog._props["persistent"]


def test_edit_guard_does_not_close_busy_or_unconfirmed_writes(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui import components

    state = {"busy": True, "pending": True}
    confirmations = []
    monkeypatch.setattr(components, "confirm_dialog", lambda **kwargs: confirmations.append(kwargs))
    with ui.dialog() as dialog, ui.card():
        ui.input(value="未改动")
        close = components.guard_unsaved(dialog, busy=lambda: state["busy"], pending=lambda: state["pending"])
    dialog.open()
    close()
    assert dialog.value and not confirmations
    state["busy"] = False
    close()
    assert dialog.value and len(confirmations) == 1


@pytest.mark.parametrize("feature,record_kind,result_key", [("wiki", "page", "pages"), ("graph", "object", "objects")])
def test_catalog_preserves_selection_and_accepts_cleared_search(knowledge_base, monkeypatch, feature, record_kind, result_key):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench
    from app.ui.views.graph_view import GraphWorkbench

    workbench = WikiWorkbench() if feature == "wiki" else GraphWorkbench()
    result = workbench.service.apply({"request_key": uuid4().hex, "reason": "test", result_key: [
        {"ref": "first", "kind": "concept", "title": "工作台条目", "summary": "摘要"},
        {"ref": "other", "kind": "concept", "title": "其他条目"},
    ]})
    ident = result["refs"]["first"]["id"]
    asyncio.run(workbench.search())
    with ui.column() as container:
        workbench.render_middle()
    asyncio.run(workbench.select(record_kind, ident))
    row = workbench._list_rows[(record_kind, ident)]
    assert "theme-selected" in row.classes
    assert sum("theme-selected" in element.classes for element in workbench._list_rows.values()) == 1
    search = next(element for element in container.descendants()
                  if isinstance(element, ui.input) and element._props.get("label") == t("knowledge.search"))
    search.value = "工作台"
    assert workbench.query == "工作台"
    search.value = None
    assert workbench.query == ""
    # 此处验证查询参数；真实事件循环中的列表重绘由浏览器用例覆盖。
    monkeypatch.setattr(workbench, "refresh_list", lambda: None)
    asyncio.run(workbench.search())
    assert workbench.results["total"] == 2


def test_workspace_translations_match():
    root = Path(__file__).resolve().parents[2] / "app" / "i18n" / "locales"
    zh = json.loads((root / "zh.json").read_text(encoding="utf-8"))
    en = json.loads((root / "en.json").read_text(encoding="utf-8"))
    for section in ("workspace", "sidebar", "knowledge", "library", "files", "collections", "task_activity", "settings_ocr", "settings_performance"):
        assert zh[section].keys() == en[section].keys()
        assert all(zh[section].values()) and all(en[section].values())


def test_contextual_help_supports_focus_and_escape(knowledge_base):
    from nicegui import ui
    from app.ui.components import help_hint

    with ui.column() as container:
        button = help_hint("低频说明", label="字段帮助")
    tooltip = next(element for element in container.descendants() if isinstance(element, ui.tooltip))
    assert button._props["aria-describedby"] == f"c{tooltip.id}"
    assert button._props["aria-label"] == "字段帮助"
    events = {listener.type for listener in button._event_listeners.values()}
    assert {"focus", "click", "blur", "keydown.escape.stop"} <= events
    assert not any(isinstance(element, ui.label) and element.text == "低频说明" for element in container.descendants())


def test_settings_help_does_not_hide_upload_warning(knowledge_base):
    from nicegui import ui
    from app.ui.views.settings_view import _render_ocr_mineru_fields

    with ui.column() as container:
        _render_ocr_mineru_fields({})
    labels = [element.text for element in container.descendants() if isinstance(element, ui.label)]
    assert t("settings_ocr.third_party_warning") in labels
    assert t("settings_ocr.mineru_hint") not in labels
    assert any(isinstance(element, ui.tooltip) and element.text == t("settings_ocr.mineru_hint")
               for element in container.descendants())


def test_confirmation_blocks_duplicate_submission_and_defaults_to_cancel(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.components import confirm_dialog

    calls = []
    gate = None
    callbacks = {}
    on_click = ui.button.on_click

    def capture(button, callback):
        callbacks[button.text] = callback
        return on_click(button, callback)

    monkeypatch.setattr(ui.button, "on_click", capture)

    async def save():
        calls.append(True)
        await gate.wait()

    dialog = confirm_dialog("确认操作", "第一行\n第二行", save, danger=True)
    buttons = [element for element in dialog.descendants() if isinstance(element, ui.button)]
    confirm = next(button for button in buttons if button.text == t("confirm_dialog.btn_confirm"))
    cancel = next(button for button in buttons if button.text == t("confirm_dialog.btn_cancel"))
    callback = callbacks[t("confirm_dialog.btn_confirm")]
    assert cancel._props["autofocus"]

    async def scenario():
        nonlocal gate
        gate = asyncio.Event()
        submitted = asyncio.create_task(callback())
        await asyncio.sleep(0)
        assert not confirm.enabled and not cancel.enabled and dialog._props["persistent"]
        await callback()
        assert calls == [True]
        gate.set()
        await submitted
        assert not dialog.value
        assert confirm.enabled and cancel.enabled

    asyncio.run(scenario())
    dialog.delete()


@pytest.mark.parametrize("feature", ["wiki", "graph"])
@pytest.mark.parametrize("failure", [False, True])
def test_async_notification_survives_deleted_event_origin(knowledge_base, monkeypatch, feature, failure):
    import gc
    from nicegui import context, run, ui
    from app.ui.views.wiki_view import WikiWorkbench
    from app.ui.views.graph_view import GraphWorkbench
    from indexing.services.errors import BusinessError

    workbench = WikiWorkbench() if feature == "wiki" else GraphWorkbench()
    with ui.column() as catalog:
        workbench.render_middle()
    workbench.refresh_list = lambda: None
    notifications = []
    monkeypatch.setattr(ui, "notify", lambda message, **options:
                        notifications.append((context.client, message, options["type"])))

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def call(function, *args, **kwargs):
            if function == workbench.service.rebuild_index:
                started.set()
                await release.wait()
                if failure:
                    raise BusinessError("NOT_FOUND", "测试服务错误")
            return {}

        monkeypatch.setattr(run, "io_bound", call)
        with catalog.client:
            origin = ui.column()
        slot = origin.default_slot

        async def rebuild():
            with slot:
                await workbench.rebuild_index()

        pending = asyncio.create_task(rebuild())
        await started.wait()
        # 只保留事件 slot，模拟用户切走后原按钮父元素已被回收。
        origin.delete()
        del origin
        gc.collect()
        release.set()
        await pending

    try:
        asyncio.run(scenario())
        message = workbench.text("error", code="NOT_FOUND") if failure else workbench.text("rebuild_done")
        assert notifications == [(catalog.client, message, "warning" if failure else "positive")]
    finally:
        catalog.delete()
