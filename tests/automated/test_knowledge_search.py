"""目录搜索：防抖刷新、回车立即、清空恢复与过滤触发。"""
import asyncio

from nicegui import ui

from app.i18n import t


def _workbench(feature):
    if feature == "wiki":
        from app.ui.views.wiki_view import WikiWorkbench
        return WikiWorkbench()
    from app.ui.views.graph_view import GraphWorkbench
    return GraphWorkbench()


def test_query_deduplicates_and_enter_forces_reexecution(knowledge_base, monkeypatch):
    workbench = _workbench("wiki")
    calls = []

    async def fake_search(reset=True):
        calls.append(workbench.query)

    monkeypatch.setattr(workbench, "search", fake_search)
    workbench.query_changed(type("E", (), {"value": "工作台"})())
    asyncio.run(workbench.query_committed())
    asyncio.run(workbench.query_committed())  # 同一查询串不重复请求
    assert calls == ["工作台"]
    asyncio.run(workbench.query_entered(type("E", (), {"args": "工作台"})()))
    assert calls == ["工作台", "工作台"]  # 回车显式重查
    asyncio.run(workbench.clear_query())
    assert workbench.query == "" and calls[-1] == ""
    asyncio.run(workbench.search_clicked())
    assert calls[-1] == ""


def test_filters_trigger_search(knowledge_base, monkeypatch):
    workbench = _workbench("graph")
    calls = []

    async def fake_search(reset=True):
        calls.append(True)

    monkeypatch.setattr(workbench, "search", fake_search)
    asyncio.run(workbench.filters_changed())
    assert calls == [True]


def test_catalog_wires_debounced_search_and_enter(knowledge_base):
    view = _workbench("graph")
    with ui.column() as container:
        view.render_middle()
    search = next(element for element in container.descendants()
                  if isinstance(element, ui.input) and element._props.get("label") == t("knowledge.search"))
    assert search._props.get("debounce") in (300, "300")
    event_types = {listener.type for listener in search._event_listeners.values()}
    assert "update:modelValue" in event_types and "keydown.enter" in event_types
    selects = [element for element in container.descendants() if isinstance(element, ui.select)]
    assert selects and all("update:modelValue" in {l.type for l in select._event_listeners.values()}
                           for select in selects)
