"""Wiki 写入对话框：以真实服务验证版本+内容哈希冲突、响应丢失与删除边界。"""
import asyncio
import inspect
from copy import deepcopy
from uuid import uuid4

from app.i18n import t


def k(name):
    return t("knowledge." + name)


def create_page(service):
    result = service.apply({"request_key": str(uuid4()), "reason": "test", "pages": [
        {"ref": "a", "kind": "concept", "title": "编辑页面", "body": "原始正文"}]})
    return result["refs"]["a"]["id"]


def newest_button(ui, caption):
    from nicegui import context
    return next(element for element in reversed(list(context.client.elements.values()))
                if isinstance(element, ui.button) and element.text == caption)


async def click(button):
    listener = next(iter(button._event_listeners.values()))
    callback = inspect.getclosurevars(listener.handler).nonlocals["callback"]
    with button.parent_slot:
        value = callback()
        if value is not None:
            await value


def test_conflict_adopts_revision_and_content_hash_together(knowledge_base, monkeypatch):
    from nicegui import context, ui
    from app.ui.views.wiki_view import WikiWorkbench
    from indexing.services import wiki_service as service
    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    oid = create_page(service)
    original = service.get_record(kind="page", id=oid)["record"]
    workbench = WikiWorkbench()
    asyncio.run(workbench.select("page", oid))
    workbench.render_right()
    workbench.edit_page(deepcopy(original))
    body = next(element for element in reversed(list(context.client.elements.values()))
                if isinstance(element, ui.textarea) and element._props.get("label") == k("body"))
    body.value = "用户未保存的草稿"
    service.apply({"request_key": str(uuid4()), "reason": "external", "pages": [
        {"id": oid, "expected_revision": 1, "expected_content_hash": original["content_hash"],
         "body": "外部修改"}]})
    asyncio.run(workbench.poll())
    assert workbench.detail["record"]["body"] == "外部修改"
    assert body.value == "用户未保存的草稿"
    save = newest_button(ui, k("save"))
    reason = next(element for element in reversed(list(context.client.elements.values()))
                  if isinstance(element, ui.input) and element._props.get("label") == k("reason"))
    reason.value = "manual"
    asyncio.run(click(save))
    assert body.value == "用户未保存的草稿"
    assert service.get_record(kind="page", id=oid)["record"]["body"] == "外部修改"
    # 显式采用最新 revision 与 content_hash 后才能继续保存。
    adopt = newest_button(ui, k("use_revision"))
    asyncio.run(click(adopt))
    asyncio.run(click(save))
    assert service.get_record(kind="page", id=oid)["record"]["body"] == "用户未保存的草稿"
    assert service.get_record(kind="page", id=oid)["record"]["revision"] == 3


def test_timeout_queries_original_request_without_duplicate_write(knowledge_base, monkeypatch):
    from nicegui import context, ui
    from app.ui.views.wiki_view import WikiWorkbench
    from indexing.services import wiki_service as service
    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    oid = create_page(service)
    original = service.get_record(kind="page", id=oid)["record"]
    workbench = WikiWorkbench()
    workbench.render_right()
    workbench.edit_page(deepcopy(original))
    reason = next(element for element in reversed(list(context.client.elements.values()))
                  if isinstance(element, ui.input) and element._props.get("label") == k("reason"))
    reason.value = "manual"
    apply = service.apply
    keys = []

    def lose_response(payload, **kwargs):
        keys.append(payload["request_key"])
        apply(payload, **kwargs)
        raise TimeoutError("response lost")

    monkeypatch.setattr(service, "apply", lose_response)
    save = newest_button(ui, k("save"))
    asyncio.run(click(save))
    assert service.get_record(kind="page", id=oid)["record"]["revision"] == 2
    pending_key = workbench.editor.pending.key
    workbench.render_right()
    assert workbench.editor.pending.key == pending_key
    save = newest_button(ui, k("save"))
    asyncio.run(click(save))
    assert len(keys) == 1
    assert service.get_record(kind="page", id=oid)["record"]["revision"] == 2


def test_evidence_delete_uses_page_content_hash(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench
    from indexing.services import wiki_service as service
    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    oid = create_page(service)
    record = service.get_record(kind="page", id=oid)["record"]
    made = service.apply({"request_key": str(uuid4()), "reason": "evidence", "pages": [
        {"id": oid, "expected_revision": record["revision"], "expected_content_hash": record["content_hash"]}],
        "evidence": [{"page": {"id": oid}, "source_kind": "user", "quote": "页面证据"}]})
    eid = made["evidence"][0]["id"]
    evidence = service.get_record(kind="evidence", id=eid)["record"]
    assert evidence["page_content_hash"]
    workbench = WikiWorkbench()
    captured = {}
    real = service.delete

    def spy(payload, **kwargs):
        captured.update(payload)
        return real(payload, **kwargs)

    monkeypatch.setattr(service, "delete", spy)
    container = ui.column()

    async def preview():
        with container:
            await workbench.preview_delete("evidence", evidence)
    asyncio.run(preview())
    assert captured["kind"] == "evidence"
    assert captured["expected_content_hash"] == evidence["page_content_hash"]
    assert "expected_revision" not in captured


def test_evidence_delete_prefers_page_hash_over_source_hash(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench
    from indexing.services import wiki_service as service
    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    workbench = WikiWorkbench()
    record = {"id": str(uuid4()), "title": "证据", "page_id": str(uuid4()),
              "page_content_hash": "a" * 64, "content_hash": "b" * 64}
    captured = {}

    def fake_delete(payload, **kwargs):
        captured.update(payload)
        return {"dry_run": True, "counts": {"evidence": 1}, "impact_token": "c" * 64}

    monkeypatch.setattr(service, "delete", fake_delete)
    container = ui.column()

    async def preview():
        with container:
            await workbench.preview_delete("evidence", record)
    asyncio.run(preview())
    # 证据同时带来源卡片哈希与页面哈希；删除用页面哈希，不能用来源快照哈希。
    assert captured["expected_content_hash"] == "a" * 64
    assert "expected_revision" not in captured


def test_page_delete_preview_declares_history_retained(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench
    from indexing.services import wiki_service as service
    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    workbench = WikiWorkbench()
    record = {"id": str(uuid4()), "title": "页面", "revision": 2, "content_hash": "a" * 64}
    monkeypatch.setattr(service, "delete", lambda payload, **kwargs: {
        "dry_run": True, "committed": False, "kind": "page", "id": record["id"], "revision": 2,
        "counts": {"page": 1, "evidence": 0, "history": 3}, "impact_token": "b" * 64,
        "clears_online_history": False, "retains_history": True,
        "deletes_files": False, "backups_affected": False})
    container = ui.column()

    async def preview():
        with container:
            await workbench.preview_delete("page", record)
    asyncio.run(preview())
    # 删除影响范围由服务边界声明，页面删除保留在线历史。
    from nicegui import context
    labels = [getattr(element, "text", "") for element in context.client.elements.values()
              if isinstance(element, ui.label)]
    assert t("knowledge.delete_retains_history") in labels

