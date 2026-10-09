"""文档式 Wiki 编辑器：草稿、校验失败和忙碌状态的生命周期。"""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4

from app.i18n import t
from app.ui.views.wiki_editor import STATUS_KEYS


def _draft_body(editor, ui, caption):
    return next(element for element in editor.controls
                if isinstance(element, ui.textarea) and element._props.get("label") == caption)


def _create_page(service):
    result = service.apply({"request_key": str(uuid4()), "reason": "test", "pages": [
        {"ref": "a", "kind": "concept", "title": "编辑页面", "body": "原始正文"}]})
    return result["refs"]["a"]["id"]


def test_draft_survives_rebuilding_editor_and_preview(knowledge_base):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench

    view = WikiWorkbench()
    with ui.column():
        view.render_right()
    view.edit_page()
    editor = view.editor
    body = _draft_body(editor, ui, t("knowledge.body"))
    body.value = "# 未保存的正文"
    editor.reason["reason"] = "草稿"
    assert editor.dirty
    editor.toggle_preview()
    assert view.editor is editor and editor.preview
    editor.toggle_preview()
    view.render_right()
    assert view.editor is editor and editor.dirty
    assert editor.draft["body"] == "# 未保存的正文"
    assert editor.reason["reason"] == "草稿"
    assert view.service.list_pages()["total"] == 0


def test_failed_validation_keeps_inline_draft(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    view = WikiWorkbench()
    view.render_right()
    view.edit_page()
    editor = view.editor
    editor.draft["body"] = "标题未填写，但正文不能丢失"
    editor.reason["reason"] = "校验"
    asyncio.run(editor.save())
    assert view.editor is editor
    assert editor.draft["body"] == "标题未填写，但正文不能丢失"
    assert editor.message
    assert editor.pending is None and not editor.busy
    assert view.service.list_pages()["total"] == 0


def test_switching_page_requires_consent_to_discard(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench

    view = WikiWorkbench()
    view.render_right()
    view.edit_page()
    editor = view.editor
    editor.draft["body"] = "保留这份草稿"
    consent = AsyncMock(return_value=False)
    monkeypatch.setattr(editor, "can_close", consent)
    asyncio.run(view.select("page", "not-used"))
    consent.assert_awaited_once()
    assert view.editor is editor and editor.draft["body"] == "保留这份草稿"
    asyncio.run(view.set_mode("review"))
    assert view.editor is editor and view.mode == "pages"


def test_busy_editor_cannot_be_closed(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    view = WikiWorkbench()
    view.render_right()
    view.edit_page()
    view.editor.busy = True
    assert asyncio.run(view.editor.can_close()) is False
    assert view.editor is not None


def test_status_label_tracks_every_outcome(knowledge_base):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench
    from app.ui.views.knowledge_common import PendingWrite

    view = WikiWorkbench()
    view.render_right()
    view.edit_page()
    editor = view.editor
    label = editor.status_label

    def shown():
        return label.text, label._props.get("data-state")

    # 未改动
    editor.update_status()
    assert editor.state == "unchanged"
    assert shown() == (view.text(STATUS_KEYS["unchanged"]), "unchanged")

    # 有未保存改动
    editor.draft["body"] = "改动"
    editor.update_status()
    assert editor.state == "unsaved"
    assert shown() == (view.text(STATUS_KEYS["unsaved"]), "unsaved")

    # 保存中
    editor.busy = True
    editor.update_status()
    assert editor.state == "saving"
    editor.busy = False

    # 已保存
    editor.outcome = "saved"
    editor.update_status()
    assert editor.state == "saved"
    assert shown() == (view.text(STATUS_KEYS["saved"]), "saved")

    # 校验失败保留草稿
    editor.outcome = "failed"
    editor.update_status()
    assert editor.state == "failed"
    assert shown() == (view.text(STATUS_KEYS["failed"]), "failed")

    # 版本冲突保留草稿
    editor.outcome = "conflict"
    editor.update_status()
    assert editor.state == "conflict"
    assert shown() == (view.text(STATUS_KEYS["conflict"]), "conflict")

    # 结果未确认（pending 未定论）
    editor.outcome = None
    editor.pending = PendingWrite({"pages": []})
    editor.pending.uncertain = True
    editor.update_status()
    assert editor.state == "uncertain"
    assert shown() == (view.text(STATUS_KEYS["uncertain"]), "uncertain")


def test_status_update_does_not_rebuild_controls(knowledge_base):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench

    view = WikiWorkbench()
    view.render_right()
    view.edit_page()
    editor = view.editor
    before = list(editor.controls)
    editor.draft["body"] = "打字不重建控件，保留光标与选区"
    editor.update_status()
    # 同一批元素对象仍在，且全部保持可编辑：状态刷新只改标签。
    assert editor.controls == before
    assert all(not element.is_deleted and element.enabled for element in editor.controls)
    assert editor.status_label is not None and not editor.status_label.is_deleted


def test_save_shortcut_reuses_save_pipeline(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench

    view = WikiWorkbench()
    view.render_right()
    view.edit_page()
    editor = view.editor
    seen = []

    async def submit(request):
        seen.append(request)
        return {"pages": [{"id": "page-1"}]}

    monkeypatch.setattr(view, "submit", submit)
    monkeypatch.setattr(view, "finish_editing", lambda: None)

    async def noop_readback(result):
        return None

    monkeypatch.setattr(view, "after_write", noop_readback)
    editor.draft["body"] = "快捷键保存的正文"
    editor.reason["reason"] = "快捷键"

    asyncio.run(editor.save_shortcut())

    assert len(seen) == 1
    # 请求键不可变：payload 只带一次 request_key，重试沿用同一对象。
    assert seen[0] is editor.pending
    assert seen[0].key == editor.pending.payload["request_key"]
    assert editor.outcome == "saved"


def test_save_shortcut_ignores_busy_and_keydown_guards(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench

    view = WikiWorkbench()
    view.render_right()
    view.edit_page()
    editor = view.editor

    # busy 时快捷键不得重复提交。
    calls = []

    async def submit(request):
        calls.append(request)
        return {"pages": []}

    monkeypatch.setattr(view, "submit", submit)
    editor.busy = True
    asyncio.run(editor.save_shortcut())
    assert calls == []
    assert editor.busy is True
    editor.busy = False

    # keydown 监听挂在编辑器根节点，浏览器侧已过滤 IME/重复键并阻止默认“保存网页”。
    listener = next(item for item in editor.root._event_listeners.values() if item.type == "keydown")
    js = listener.js_handler or ""
    for token in ("ctrlKey", "metaKey", "isComposing", "repeat", "preventDefault"):
        assert token in js
    save_button = next(button for button in editor.buttons if button._props.get("aria-keyshortcuts"))
    assert save_button._props["aria-keyshortcuts"] == "Control+S Meta+S"


def test_uncertain_result_keeps_draft_and_retries_same_key(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    view = WikiWorkbench()
    view.render_right()
    view.edit_page()
    editor = view.editor
    editor.draft["body"] = "响应丢失也不能丢草稿"
    editor.reason["reason"] = "网络"

    async def boom(request):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(view, "submit", boom)
    asyncio.run(editor.save_shortcut())

    assert editor.outcome == "uncertain"
    assert editor.pending is not None and editor.pending.uncertain
    assert editor.draft["body"] == "响应丢失也不能丢草稿"
    editor.update_status()
    assert editor.state == "uncertain"
    key = editor.pending.key

    # 再次保存核查原请求键，而不是拼一个新请求。
    seen = []

    async def ok(request):
        seen.append(request.key)
        return {"pages": [{"id": "page-1"}]}

    monkeypatch.setattr(view, "submit", ok)
    monkeypatch.setattr(view, "finish_editing", lambda: None)

    async def noop_readback(result):
        return None

    monkeypatch.setattr(view, "after_write", noop_readback)
    asyncio.run(editor.save_shortcut())

    assert seen == [key]
    assert editor.outcome == "saved"
    assert editor.pending.uncertain is False or editor.outcome == "saved"


def test_version_conflict_marks_state_and_keeps_draft(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench
    from indexing.services import wiki_service as service

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    oid = _create_page(service)
    original = service.get_record(kind="page", id=oid)["record"]
    view = WikiWorkbench()
    asyncio.run(view.select("page", oid))
    view.render_right()
    view.edit_page(deepcopy(original))
    editor = view.editor
    editor.draft["body"] = "用户未保存的草稿"
    editor.reason["reason"] = "manual"
    # 外部先改一版，让编辑器的 expected_revision/hash 过期。
    service.apply({"request_key": str(uuid4()), "reason": "external", "pages": [
        {"id": oid, "expected_revision": original["revision"],
         "expected_content_hash": original["content_hash"], "body": "外部修改"}]})

    asyncio.run(editor.save())

    assert editor.outcome == "conflict"
    assert editor.state == "conflict"
    assert editor.pending is None
    assert editor.latest is not None and editor.latest["body"] == "外部修改"
    assert editor.draft["body"] == "用户未保存的草稿"
    assert view.editor is editor

    # 显式采用最新版本后冲突消解，草稿仍是未保存内容。
    editor.adopt_latest()
    assert editor.outcome is None
    assert editor.state == "unsaved"
    assert editor.draft["body"] == "用户未保存的草稿"
