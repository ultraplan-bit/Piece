"""阶段 C 实机验收：独立知识图谱流程，临时新库 + 本机 Edge，无 CDN / 模型请求。

uv run --no-sync --with playwright pytest -q -s tests/automated/test_graph_browser.py
"""
from uuid import uuid4

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401
from app.i18n import t


def k(name):
    return t("knowledge." + name)


def gk(name):
    return t("graph." + name)


@pytest.fixture
def graph_gui(gui_service):
    from indexing import database
    database.init_connection_pool()
    yield gui_service
    database.close_connection_pool()


def apply(**parts):
    from indexing.services import knowledge_service
    return knowledge_service.apply({"request_key": uuid4().hex, "reason": "浏览器验收", **parts})


def chart_state(page):
    return page.locator(".nicegui-echart").evaluate("""el => {
        const chart = getElement(el.id.slice(1)).chart;
        const series = chart.getModel().getSeriesByIndex(0);
        const nodes = series.getData();
        const edges = series.getEdgeData();
        return {
            nodes: Array.from({length: nodes.count()}, (_, i) => {
                const symbol = nodes.getItemGraphicEl(i).childAt(0);
                const rect = symbol.getBoundingRect().clone();
                rect.applyTransform(symbol.getComputedTransform());
                return {id: nodes.getId(i), x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
            }),
            edges: Array.from({length: edges.count()}, (_, i) => {
                const line = edges.getItemGraphicEl(i).childAt(0);
                return {id: edges.getId(i), point: line.transformCoordToGlobal(...line.pointAt(0.5))};
            }),
        };
    }""")


def test_graph_browser_independent_roundtrip(graph_gui):
    service = graph_gui
    expect = playwright.expect
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1540, "height": 1080},
                                http_credentials={"username": "piece", "password": service["key"]})
        errors = []
        external_requests = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(request):
            if request.request.url.startswith(service["url"] + "/"):
                request.continue_()
            else:
                external_requests.append(request.request.url)
                request.abort()
        page.route("**/*", route)
        try:
            page.goto(service["url"])
            expect(page.get_by_text("人工智能 (1)", exact=True)).to_be_visible(timeout=30000)
            page.get_by_role("button", name='Expand "文件库"', exact=True).click()
            page.get_by_role("button", name=t("sidebar.graph"), exact=True).click()
            expect(page.get_by_text(k("empty"), exact=True)).to_be_visible()

            # GUI 新建图节点：没有 Wiki 正文，只有种类、标题与摘要。
            page.get_by_role("button", name=gk("create"), exact=True).click()
            dialog = page.get_by_role("dialog")
            dialog.get_by_label(k("title_field"), exact=True).fill("图谱中心")
            dialog.get_by_label(k("summary"), exact=True).fill("中心摘要")
            dialog.get_by_label(k("reason"), exact=True).fill("界面创建")
            dialog.get_by_role("button", name=k("save"), exact=True).click()
            expect(dialog).not_to_be_visible()

            from indexing.services import knowledge_service
            root = knowledge_service.search_objects(query="图谱中心")["objects"][0]["id"]
            # 外部调用者创建目标节点，GUI 自动列入。
            target = apply(objects=[{"ref": "target", "kind": "entity", "title": "目标图节点",
                                     "summary": "候选摘要"}])["refs"]["target"]["id"]

            # GUI 建立语义关系。
            expect(page.get_by_role("button", name="目标图节点", exact=True)).to_be_visible(timeout=15000)
            page.get_by_role("button", name=k("create_relation"), exact=True).click()
            dialog.get_by_label(k("target_search"), exact=True).fill("目标图节点")
            dialog.get_by_role("button", name=k("search"), exact=True).click()
            dialog.get_by_label(k("target_object"), exact=True).click()
            page.get_by_role("option").filter(has_text="目标图节点").click()
            dialog.get_by_label(k("description"), exact=True).fill("人工创建的关系")
            dialog.get_by_label(k("reason"), exact=True).fill("关系验收")
            dialog.get_by_role("button", name=k("save"), exact=True).click()
            expect(dialog).not_to_be_visible()
            expect(page.get_by_text(k("description") + " · 人工创建的关系", exact=True)).to_be_visible()

            # 给关系补证据。
            page.get_by_role("button", name=k("add_evidence"), exact=True).click()
            dialog.get_by_label(k("quote"), exact=True).fill("人工补充证据")
            dialog.get_by_label(k("reason"), exact=True).fill("证据验收")
            dialog.get_by_role("button", name=k("save"), exact=True).click()
            expect(dialog).not_to_be_visible()
            expect(page.get_by_text(k("location_unverified"), exact=True)).to_be_visible()
            relation = next(edge for edge in knowledge_service.graph(root_id=root)["edges"]
                            if edge.get("description") == "人工创建的关系")
            assert relation["evidence_count"] == 1

            # 局部图：节点、关系表与画布点击。
            page.get_by_role("button", name="图谱中心", exact=True).first.click()
            page.get_by_role("button", name=k("view_graph"), exact=True).click()
            expect(page.locator(".nicegui-echart canvas")).to_be_visible(timeout=30000)
            expect(page.get_by_text(k("table"), exact=True)).to_be_visible()
            state = chart_state(page)
            assert {node["id"] for node in state["nodes"]} == {root, target}
            assert len(state["edges"]) == 1
            page.get_by_role("button", name=k("related_to"), exact=True).focus()
            page.keyboard.press("Enter")
            expect(page.get_by_text(k("description") + " · 人工创建的关系", exact=True)).to_be_visible()

            # 只读结构检查与查询索引重建（不改实体、关系、证据）。
            page.get_by_role("button", name=k("review"), exact=True).click()
            page.get_by_role("button", name=k("run_checks"), exact=True).click()
            page.get_by_role("button", name=gk("rebuild_index"), exact=True).click()
            expect(page.get_by_role("button", name=gk("rebuild_index"), exact=True)).to_be_visible()
            page.get_by_role("button", name=gk("objects"), exact=True).click()

            # 删除节点会影响其关系与证据，预览范围由服务给出。
            page.get_by_role("button", name="图谱中心", exact=True).first.click()
            page.get_by_role("button", name=k("delete"), exact=True).click()
            expect(page.get_by_text(k("delete_preview"), exact=True)).to_be_visible()
            impact = t("knowledge.impact_count", kind=k("relations"), count=1)
            expect(page.get_by_text(impact, exact=True)).to_be_visible()
            dialog.get_by_role("button", name=k("confirm_delete"), exact=True).click()
            expect(dialog).not_to_be_visible()
            from indexing.services.errors import BusinessError
            with pytest.raises(BusinessError):
                knowledge_service.get_record(kind="object", id=root)
            assert not errors, errors
            assert not external_requests, external_requests
        finally:
            page.screenshot(path=str(service["path"] / "graph-final.png"))
            browser.close()
