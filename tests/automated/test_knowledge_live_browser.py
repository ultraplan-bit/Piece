"""真实 GUI 子进程：外部更新不重启页面，空间/画布/辅助面板均自动同步。"""
from uuid import uuid4

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service
from tree_browser_helpers import browse_mode, document_row
from app.i18n import t
from indexing.services import knowledge_service as graph_service, wiki_service


@pytest.fixture
def live_gui(gui_service):
    from indexing import database
    database.init_connection_pool()
    yield gui_service
    database.close_connection_pool()


def text(feature, name, **kwargs):
    value = t(f"{feature}.{name}", **kwargs)
    return t(f"knowledge.{name}", **kwargs) if value == f"{feature}.{name}" else value


def apply(service, **parts):
    return service.apply({"request_key": uuid4().hex, "reason": "实时覆盖浏览器", **parts})


def remove(service, kind, record):
    payload = {"kind": kind, "id": record["id"], "expected_revision": record["revision"]}
    if service is wiki_service:
        payload["expected_content_hash"] = record["content_hash"]
    preview = service.delete(payload)
    service.delete({**payload, "request_key": uuid4().hex, "impact_token": preview["impact_token"],
                    "dry_run": False, "confirmed": True})


def chart_state(page):
    return page.locator(".nicegui-echart").evaluate("el => getElement(el.id.slice(1)).chart.getOption()")


def wait_chart(page, node_count, edge_count=None):
    page.wait_for_function("""([nodes, edges]) => {
        const el = document.querySelector('.nicegui-echart');
        const series = el && getElement(el.id.slice(1))?.chart?.getOption()?.series;
        if (!series?.length) return false;
        const count = series[0].type === 'graph' ? series[0].data.length
            : series.slice(1).reduce((total, item) => total + item.data.length, 0);
        const links = series[0].type === 'graph' ? series[0].links : series[0].data;
        return count === nodes && (edges === null || links.length === edges);
    }""", arg=[node_count, edge_count], timeout=20000)


def test_graph_spaces_and_canvas_refresh_without_reload(live_gui):
    expect = playwright.expect
    root = apply(graph_service, objects=[{"ref": "a", "kind": "concept", "title": "实时图谱根节点"}])["objects"][0]["id"]
    default = next(item for item in graph_service.list_graphs()["graphs"] if item["is_default"])
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1540, "height": 1080},
                                http_credentials={"username": "piece", "password": live_gui["key"]})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(live_gui["url"])
            expect(page.locator(".resource-collection-row").first).to_be_visible(timeout=30000)
            page.get_by_role("button", name=t("sidebar.graph"), exact=True).click()
            picker = page.locator(".graph-space-picker")
            expect(picker).to_contain_text(default["name"], timeout=15000)
            select = picker.get_by_role("combobox")
            expect(select).to_be_enabled()
            select.click()
            expect(page.get_by_role("option", name=default["name"], exact=True)).to_be_visible()
            created = graph_service.create_graph("外部实时空间")
            option = page.get_by_role("option", name="外部实时空间", exact=True)
            expect(option).to_be_visible(timeout=15000)
            option.click()
            graph_service.rename_graph(created["id"], "外部实时改名")
            expect(picker).to_contain_text("外部实时改名", timeout=15000)
            preview = graph_service.delete_graph(created["id"])
            graph_service.delete_graph(created["id"], confirmed=True, impact_token=preview["impact_token"])
            expect(picker).to_contain_text(default["name"], timeout=15000)
            page.locator(f'[data-catalog-row="{root}"]').click()
            page.locator(".graph-inspector").get_by_role("button", name=text("graph", "center"), exact=True).click()
            wait_chart(page, 1, 0)
            canvas_id = page.locator(".nicegui-echart").get_attribute("id")
            page.locator(".nicegui-echart").evaluate("""el => {
                getElement(el.id.slice(1)).chart.setOption({series: [{id: 'knowledge', zoom: 1.5, center: [0, 0]}]});
            }""")
            linked = apply(graph_service, objects=[{"ref": "b", "kind": "entity", "title": "实时相邻节点"}],
                           relations=[{"ref": "r", "source": {"id": root}, "target": {"ref": "b"},
                                       "predicate": "related_to", "description": "实时关系", "basis": "inference"}])
            wait_chart(page, 2, 1)
            assert page.locator(".nicegui-echart").get_attribute("id") == canvas_id
            state = chart_state(page)
            assert state["series"][0]["zoom"] == pytest.approx(1.5)
            assert state["series"][0]["center"] == [0, 0]
            remove(graph_service, "relation", graph_service.get_record(kind="relation", id=linked["relations"][0]["id"])["record"])
            wait_chart(page, 1, 0)
            page.get_by_role("button", name=text("graph", "global_graph"), exact=True).click()
            wait_chart(page, 2, 0)
            page.locator(".graph-toolbar").get_by_role("button", name=text("graph", "more"), exact=True).click()
            page.get_by_text(text("graph", "layout_3d"), exact=True).click()
            page.wait_for_function("""() => {
                const el = document.querySelector('.nicegui-echart');
                return el && getElement(el.id.slice(1))?.chart?.getOption()?.series?.[0]?.type === 'lines3D';
            }""")
            canvas_id = page.locator(".nicegui-echart").get_attribute("id")
            page.locator(".nicegui-echart").evaluate("""el => {
                getElement(el.id.slice(1)).chart.setOption({globe: {viewControl: {alpha: 36, beta: 51, distance: 195}}});
            }""")
            apply(graph_service, objects=[{"ref": "c", "kind": "entity", "title": "实时全局节点"}])
            wait_chart(page, 3, 0)
            assert page.locator(".nicegui-echart").get_attribute("id") == canvas_id
            camera = chart_state(page)["globe"][0]["viewControl"]
            assert camera["alpha"] == pytest.approx(36)
            assert camera["beta"] == pytest.approx(51)
            assert camera["distance"] == pytest.approx(195)
            assert not errors, errors
        finally:
            page.screenshot(path=str(live_gui["path"] / "live-graph.png"))
            print("Live graph screenshot:", live_gui["path"] / "live-graph.png")
            browser.close()


@pytest.mark.parametrize("feature", ["graph", "wiki"])
def test_reference_checks_and_open_history_refresh(live_gui, feature):
    from indexing.services import file_service
    expect = playwright.expect
    service = graph_service if feature == "graph" else wiki_service
    key, kind, record_kind = ("objects", "concept", "object") if feature == "graph" else ("pages", "topic", "page")
    chunk = file_service.get_chunks_paginated(live_gui["file_id"])["chunks"][0]
    library = service.library_id()

    def create(title):
        return apply(service, **{key: [{"ref": "a", "kind": kind, "title": title}],
                                 "evidence": [{record_kind: {"ref": "a"}, "source_kind": "piece", "source_library_id": library,
                                               "source_file_id": live_gui["file_id"], "source_chunk_id": chunk["id"],
                                               "quote": chunk["chunk_text"]}]})[key][0]["id"]

    first = create("实时引用首条")
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1540, "height": 1080},
                                http_credentials={"username": "piece", "password": live_gui["key"]})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(live_gui["url"])
            expect(page.locator(".resource-collection-row").first).to_be_visible(timeout=30000)
            browse_mode(page, "all")
            page.get_by_placeholder(t("files.search"), exact=True).fill("研究方法")
            document_row(page, live_gui["file_id"]).dblclick()
            page.get_by_role("button", name=t("files.info_title"), exact=True).click()
            page.get_by_role("button", name=t(f"knowledge.references_{feature}"), exact=True).click()
            references = page.locator(".q-expansion-item").filter(has=page.get_by_text(text(feature, "references"), exact=True))
            expect(references.get_by_role("button", name="实时引用首条", exact=True)).to_be_visible(timeout=15000)
            second = create("实时引用新增")
            expect(references.get_by_role("button", name="实时引用新增", exact=True)).to_be_visible(timeout=15000)
            remove(service, record_kind, service.get_record(kind=record_kind, id=first)["record"])
            expect(references.get_by_role("button", name="实时引用首条", exact=True)).to_have_count(0, timeout=15000)
            if feature == "wiki":
                page.get_by_role("button", name=text(feature, "review"), exact=True).click()
            else:
                page.get_by_role("button", name=text(feature, "objects"), exact=True).click()
                page.get_by_role("button", name='Expand "' + text(feature, "review") + '"').click()
            page.get_by_role("button", name=text(feature, "run_checks"), exact=True).click()
            expect(page.get_by_text(text(feature, "checked_count", count=1), exact=True)).to_be_visible()
            apply(service, **{key: [{"ref": "b", "kind": kind, "title": "实时检查新增"}]})
            expect(page.get_by_text(text(feature, "checked_count", count=2), exact=True)).to_be_visible(timeout=15000)
            if feature == "wiki":
                page.get_by_role("button", name=text(feature, "pages"), exact=True).click()
            page.locator(f'[data-catalog-row="{second}"]').click()
            actions = page.locator(".graph-detail-actions" if feature == "graph" else ".knowledge-detail")
            actions.get_by_role("button", name=text(feature, "more"), exact=True).click()
            page.locator(".q-menu").get_by_text(text(feature, "history"), exact=True).click()
            dialog = page.get_by_role("dialog")
            expect(dialog.locator(".q-expansion-item")).to_have_count(1)
            record = service.get_record(kind=record_kind, id=second)["record"]
            update = {"id": second, "expected_revision": record["revision"], "title": "实时历史新增"}
            if feature == "wiki":
                update["expected_content_hash"] = record["content_hash"]
            apply(service, **{key: [update]})
            expect(dialog.locator(".q-expansion-item")).to_have_count(2, timeout=15000)
            assert not errors, errors
        finally:
            page.screenshot(path=str(live_gui["path"] / f"live-{feature}-panels.png"))
            print("Live panel screenshot:", live_gui["path"] / f"live-{feature}-panels.png")
            browser.close()
