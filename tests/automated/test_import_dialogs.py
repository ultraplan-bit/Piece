"""外部知识库导入对话框：无浏览器渲染，验证参数收集与错误指引。"""

import asyncio

import pytest

from indexing.services.errors import BusinessError


def _find(container, kind):
    """按文档顺序收集指定类型的元素。"""
    found = []

    def walk(element):
        if type(element).__name__ == kind:
            found.append(element)
        for slot in element.slots.values():
            for child in slot.children:
                walk(child)

    walk(container)
    return found


def _labels(container):
    return [el.text for el in _find(container, "Label")]


def _buttons(container):
    return {b.text: b for b in _find(container, "Button") if b.text}


def _run(handler):
    """在事件循环内执行处理器并等其派生的后台任务结束。

    NiceGUI 的事件处理与 refreshable.refresh 都把协程交给 background_tasks，
    要求 core.loop 已设置。
    """
    from nicegui import background_tasks, core

    async def wrapped():
        core.loop = asyncio.get_running_loop()
        try:
            await handler()
            while background_tasks.running_tasks:
                await asyncio.gather(*background_tasks.running_tasks, return_exceptions=True)
        finally:
            core.loop = None

    asyncio.run(wrapped())


def _click(button):
    """触发按钮点击：NiceGUI 的 on_click 包了一层接收事件参数的 lambda。"""
    listener = next(item for item in button._event_listeners.values() if item.type == "click")

    async def fire():
        listener.handler(None)

    return fire


@pytest.fixture
def page(knowledge_base):
    from nicegui import ui
    with ui.column() as container:
        yield container
    container.delete()


def test_folder_dialog_collects_options_and_previews(page, tmp_path):
    from app.ui import import_dialogs
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "templates").mkdir()
    (vault / "templates" / "t.md").write_text("{{date}}", encoding="utf-8")
    (vault / "note.md").write_text("正文", encoding="utf-8")
    seen = {}

    def preview(options):
        seen["preview"] = options
        return {"candidates": [vault / "note.md"], "skipped": [], "excluded": [{"path": str(vault / "templates"), "reason": "pattern: templates"}]}

    async def start(options):
        seen["import"] = options

    dialog = import_dialogs.folder_import_dialog(preview, start)
    inputs = _find(dialog, "Input")
    assert len(inputs) == 2
    inputs[0].value = f'"{vault}"'
    inputs[1].value = "templates，attachments/*"
    checkboxes = _find(dialog, "Checkbox")
    assert [c.value for c in checkboxes] == [True, True]
    checkboxes[0].value = False
    _run(_click(_buttons(dialog)["预览"]))
    assert seen["preview"] == {"path": vault, "recursive": False, "skip_link_notes": True, "exclude": ["templates", "attachments/*"]}
    assert any("将导入 1 个文件，排除 1 个" in text for text in _labels(dialog))
    _run(_click(_buttons(dialog)["开始导入"]))
    assert seen["import"] == seen["preview"]


def test_folder_dialog_rejects_empty_path(page):
    from app.ui import import_dialogs
    calls = []
    dialog = import_dialogs.folder_import_dialog(lambda o: calls.append(o), lambda o: calls.append(o))
    _run(_click(_buttons(dialog)["开始导入"]))
    assert calls == []


def test_zotero_dialog_shows_enable_hint_then_previews(page):
    from app.ui import import_dialogs
    state = {"enabled": False, "calls": []}

    def preview(keys, mode):
        state["calls"].append((keys, mode))
        if not state["enabled"]:
            raise BusinessError("ZOTERO_DISABLED", "Zotero 拒绝了本机 API 访问")
        return {"zotero": {"zotero_version": "10.0.2"}, "items_total": 3,
                "collections": [{"key": "AAAA", "name": "顶层", "path": "顶层", "item_count": 2},
                                {"key": "BBBB", "name": "子集", "path": "顶层/子集", "item_count": 1}],
                "importable_count": 2, "skipped_count": 1,
                "items": [{"filename": "Lin - 2024 - A.pdf"}, {"filename": "Wu - 2023 - B.pdf"}], "skipped": []}

    imported = []
    dialog = import_dialogs.zotero_import_dialog(preview, lambda keys, mode: imported.append((keys, mode)))
    _run(dialog.refresh_preview)
    assert state["calls"] == [(None, "path")]
    assert any("允许本机其他应用程序" in text for text in _labels(dialog))
    assert not _find(dialog, "Checkbox")

    state["enabled"] = True
    _run(_click(_buttons(dialog)["重试"]))
    labels = _labels(dialog)
    assert any("10.0.2" in text and "3" in text for text in labels)
    assert any("Lin - 2024 - A.pdf" in text for text in labels)
    checkboxes = _find(dialog, "Checkbox")
    assert [c.text for c in checkboxes] == ["顶层  (2)", "顶层/子集  (1)"]
    checkboxes[1].value = True
    _find(dialog, "Select")[0].value = "top"
    _run(_click(_buttons(dialog)["开始导入"]))
    # 导入前按当前勾选重新预览一次，再把同样的范围交给导入回调
    assert state["calls"][-1] == (["BBBB"], "top")
    assert imported == [(["BBBB"], "top")]
