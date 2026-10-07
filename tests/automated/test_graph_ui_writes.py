"""图谱写入对话框：以真实服务验证冲突、响应丢失与删除范围。"""
import asyncio
import inspect
from copy import deepcopy
from uuid import uuid4

from app.i18n import t


def k(name):
    return t("knowledge." + name)


def create_object(service):
    result = service.apply({"request_key": str(uuid4()), "reason": "test", "objects": [
        {"ref": "a", "kind": "concept", "title": "编辑对象", "summary": "原始摘要"}]})
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


def test_conflict_keeps_draft_until_explicit_revision_adoption(knowledge_base, monkeypatch):
    from nicegui import context, ui
    from app.ui.views.graph_view import GraphWorkbench
    from indexing.services import knowledge_service as service
    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    oid = create_object(service)
    original = service.get_record(kind="object", id=oid)["record"]
    workbench = GraphWorkbench()
    asyncio.run(workbench.select("object", oid))
    workbench.edit_object(deepcopy(original))
    summary = next(element for element in reversed(list(context.client.elements.values()))
                   if isinstance(element, ui.textarea) and element._props.get("label") == k("summary"))
    summary.value = "用户未保存的草稿"
    service.apply({"request_key": str(uuid4()), "reason": "external", "objects": [
        {"id": oid, "expected_revision": 1, "summary": "外部修改"}]})
    asyncio.run(workbench.poll())
    assert workbench.detail["record"]["summary"] == "外部修改"
    assert summary.value == "用户未保存的草稿"
    save = newest_button(ui, k("save"))
    reason = next(element for element in reversed(list(context.client.elements.values()))
                  if isinstance(element, ui.input) and element._props.get("label") == k("reason"))
    reason.value = "manual"
    asyncio.run(click(save))
    assert summary.value == "用户未保存的草稿"
    assert service.get_record(kind="object", id=oid)["record"]["summary"] == "外部修改"
    adopt = newest_button(ui, k("use_revision"))
    asyncio.run(click(adopt))
    asyncio.run(click(save))
    assert service.get_record(kind="object", id=oid)["record"]["summary"] == "用户未保存的草稿"
    assert service.get_record(kind="object", id=oid)["record"]["revision"] == 3


def test_timeout_queries_original_request_without_duplicate_update(knowledge_base, monkeypatch):
    from nicegui import context, ui
    from app.ui.views.graph_view import GraphWorkbench
    from indexing.services import knowledge_service as service
    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    oid = create_object(service)
    original = service.get_record(kind="object", id=oid)["record"]
    workbench = GraphWorkbench()
    workbench.edit_object(deepcopy(original))
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
    assert service.get_record(kind="object", id=oid)["record"]["revision"] == 2
    asyncio.run(click(save))
    assert len(keys) == 1
    assert service.get_record(kind="object", id=oid)["record"]["revision"] == 2


def test_delete_confirmation_cannot_remove_new_dependencies(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.graph_view import GraphWorkbench
    from indexing.services import knowledge_service as service
    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    oid = create_object(service)
    record = service.get_record(kind="object", id=oid)["record"]
    workbench = GraphWorkbench()
    container = ui.column()

    async def preview():
        with container:
            await workbench.preview_delete("object", record)
    asyncio.run(preview())
    confirm = newest_button(ui, k("confirm_delete"))
    service.apply({"request_key": str(uuid4()), "reason": "new evidence", "evidence": [
        {"object": {"id": oid}, "source_kind": "user", "quote": "新增证据"}]})
    asyncio.run(click(confirm))
    assert service.get_record(kind="object", id=oid)["evidence"]["total"] == 1
    assert confirm._props["disable"]
