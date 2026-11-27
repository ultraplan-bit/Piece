"""阶段 C 实机验收：独立知识图谱流程，临时新库 + 本机 Edge，无 CDN / 模型请求。

uv run --no-sync --with playwright pytest -q -s tests/automated/test_graph_browser.py
"""
from pathlib import Path
from uuid import uuid4

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service
from app.i18n import t


def k(name):
    return t("knowledge." + name)


def gk(name):
    key = "graph." + name
    value = t(key)
    return k(name) if value == key else value


@pytest.fixture
def graph_gui(gui_service):
    from indexing import database
    database.init_connection_pool()
    yield gui_service
    database.close_connection_pool()


def apply(**parts):
    from indexing.services import knowledge_service
    return knowledge_service.apply({"request_key": uuid4().hex, "reason": "浏览器验收", **parts})


def chart_state(page, expected_type):
    return page.wait_for_function("""expected => {
        const el = document.querySelector('.nicegui-echart');
        const series = el && getElement(el.id.slice(1))?.chart?.getOption()?.series;
        if (!series?.length || series[0].type !== expected) return false;
        return series.map(item => ({type: item.type,
            count: (item.data || item.edges || []).length,
            data: item.data || item.edges || []}));
    }""", arg=expected_type).json_value()


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
            expect(page.locator(".resource-collection-row").first).to_be_visible(timeout=30000)
            page.get_by_role("button", name=t("sidebar.graph"), exact=True).click()
            expect(page.locator(".graph-stage").get_by_text(gk("empty"), exact=True)).to_be_visible()

            # GUI 新建图节点：没有 Wiki 正文，只有种类、标题与摘要。
            page.locator(".knowledge-catalog").get_by_role("button", name=gk("create"), exact=True).click()
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
            expect(page.locator(".knowledge-catalog").get_by_role("button", name="目标图节点", exact=True)).to_be_visible(timeout=15000)
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
            page.locator(".knowledge-detail").get_by_role("button", name=k("more"), exact=True).click()
            page.locator(".q-menu").get_by_text(k("add_evidence"), exact=True).click()
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
            page.locator(".graph-inspector").get_by_role("button", name=gk("center"), exact=True).click()
            expect(page.get_by_role("button", name=gk("local_graph"), exact=True)).to_have_attribute("aria-pressed", "true")
            expect(page.locator(".nicegui-echart canvas")).to_be_visible(timeout=30000)
            middle = page.locator(".workspace-splitter").last.locator(":scope > .q-splitter__before")
            expect(middle).to_have_css("width", "260px")
            # 画布铺满右栏：高度跟随栏高，不再被写死成固定像素而在下方留白。
            sizes = page.evaluate("""() => {
                const el = document.querySelector('.nicegui-echart');
                const pane = el.closest('.theme-content');
                return {chart: el.offsetHeight, pane: pane.offsetHeight};
            }""")
            assert sizes["chart"] > sizes["pane"] * 0.7, sizes

            expect(middle.get_by_role("button", name="目标图节点", exact=True)).to_be_visible()
            # 筛选控件要有可用宽度：多选框空值时没有值撑宽，只给 max-width 会被挤成窄缝。
            page.locator(".graph-toolbar").get_by_role("button", name=gk("filters"), exact=True).click()
            expect(page.locator(".graph-filter-menu")).to_be_visible()
            widths = page.evaluate("""() => [...document.querySelectorAll('.graph-filter-menu .q-select')]
                .map(s => s.offsetWidth)""")
            assert widths and min(widths) >= 100, widths
            page.keyboard.press("Escape")
            # 关系表格默认收进底部抽屉，展开后才渲染关系行。
            table_title = page.get_by_text(k("table"), exact=True)
            expect(table_title).to_be_visible()
            expect(page.locator(".graph-drawer").get_by_role("button", name=k("related_to"), exact=True)).not_to_be_visible()
            table_title.click()
            expect(page.locator(".graph-drawer").get_by_role("button", name=k("related_to"), exact=True)).to_be_visible()
            state = chart_state(page, "graph")
            assert state[0]["type"] == "graph"
            assert {node["id"] for node in state[0]["data"]} == {root, target}
            assert state[0]["count"] == 2
            # 检查真实 Canvas 上的关系文字，而不只检查传入的 ECharts 配置。
            page.wait_for_function("""() => {
                const el = document.querySelector('.nicegui-echart');
                const data = getElement(el.id.slice(1)).chart.getModel().getSeriesByIndex(0).getEdgeData();
                const line = data.getItemGraphicEl(0);
                const text = line?.getTextContent();
                const transform = text?.getComputedTransform();
                const path = line?.getLinePath();
                if (!transform || path?.shape.percent !== 1) return false;
                const middle = path.transformCoordToGlobal(...path.pointAt(0.5));
                return Math.abs(Math.hypot(transform[0], transform[1])-1) < 0.000001 &&
                    Math.hypot(transform[4]-middle[0], transform[5]-middle[1]) < 15;
            }""")
            labels = page.locator(".nicegui-echart").evaluate("""el => {
                const data = getElement(el.id.slice(1)).chart.getModel().getSeriesByIndex(0).getEdgeData();
                const labels = [];
                data.eachItemGraphicEl(line => {
                    const label = line.getTextContent();
                    const transform = label.getComputedTransform();
                    const path = line.getLinePath();
                    const point = path.pointAt(0.5);
                    const middle = path.transformCoordToGlobal(...point);
                    labels.push({text: label.style.text, visible: !label.ignore,
                        scale: Math.hypot(transform[0], transform[1]),
                        middle_distance: Math.hypot(transform[4] - middle[0], transform[5] - middle[1])});
                });
                return labels;
            }""")
            assert len(labels) == 1 and labels[0]["text"] == k("related_to") and labels[0]["visible"], labels
            assert labels[0]["scale"] == pytest.approx(1), labels
            assert labels[0]["middle_distance"] < 15, labels
            page.locator(".graph-stage").screenshot(path=str(service["path"] / "graph-local-labels.png"))
            page.locator(".graph-drawer").get_by_role("button", name=k("related_to"), exact=True).focus()
            page.keyboard.press("Enter")
            expect(page.get_by_text(k("description") + " · 人工创建的关系", exact=True)).to_be_visible()

            # 关系表选择留在画布，并在检查器读取真实端点名称。
            expect(page.get_by_role("button", name=gk("graph"), exact=True)).to_have_attribute("aria-pressed", "true")
            expect(page.locator(".graph-inspector .graph-relation-path").get_by_role("button", name="图谱中心", exact=True)).to_be_visible()
            page.get_by_role("button", name="图谱中心", exact=True).first.click()
            expect(page.locator(".graph-inspector .graph-detail-title")).to_have_text("图谱中心")
            # 全局默认二维，三维球面成为明确选择的辅助展示。
            page.get_by_role("button", name=gk("global_graph"), exact=True).click()
            expect(page.get_by_text(t("graph.overview_count", nodes=2, edges=1,
                                      total_nodes=2, total_edges=1), exact=True)).to_be_visible()
            global_state = chart_state(page, "graph")
            assert {node["id"] for node in global_state[0]["data"]} == {root, target}
            page.locator(".graph-toolbar").get_by_role("button", name=gk("more"), exact=True).click()
            page.get_by_text(gk("layout_3d"), exact=True).click()
            global_state = chart_state(page, "lines3D")
            assert [series["type"] for series in global_state] == ["lines3D", "scatter3D", "scatter3D"]
            global_nodes = [node for series in global_state[1:] for node in series["data"]]
            assert {node["id"] for node in global_nodes} == {root, target}
            focus = next(node for node in global_nodes if node["id"] == root)
            assert focus["value"] == [0, 0, 1]
            assert any(focus["value"] in edge["coords"] for edge in global_state[0]["data"])
            globe_visible = page.locator(".nicegui-echart").evaluate("""el => {
                const chart = getElement(el.id.slice(1)).chart;
                return chart.getOption().globe[0].show;
            }""")
            assert globe_visible is False
            page.screenshot(path=str(service["path"] / "graph-overview.png"))
            page.get_by_role("button", name="目标图节点", exact=True).first.click()
            expect(page.locator(".graph-inspector .graph-detail-title")).to_have_text("目标图节点")
            assert chart_state(page, "lines3D")[0]["type"] == "lines3D"
            page.locator(".graph-inspector").get_by_role("button", name=gk("center"), exact=True).click()
            expect(page.get_by_role("button", name=gk("local_graph"), exact=True)).to_have_attribute("aria-pressed", "true")
            assert chart_state(page, "graph")[0]["type"] == "graph"

            # 实体页内展开只读结构检查（不改实体、关系、证据）。
            page.get_by_role("button", name=gk("objects"), exact=True).click()
            page.get_by_role("button", name='Expand "' + k("review") + '"').click()
            page.get_by_role("button", name=k("run_checks"), exact=True).click()
            expect(page.get_by_text("本次检查 2 个对象", exact=True)).to_be_visible()

            # 删除节点会影响其关系与证据，预览范围由服务给出。
            page.get_by_role("button", name="图谱中心", exact=True).first.click()
            page.locator(".knowledge-detail").get_by_role("button", name=k("more"), exact=True).click()
            page.locator(".q-menu").get_by_text(k("delete"), exact=True).click()
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
            print("Graph screenshots:", service["path"])
            browser.close()


def test_graph_right_pane_themes_and_narrow_layout(graph_gui):
    """长内容、窄右栏与三种主题使用同一套工具栏和阅读布局。"""
    from app.ui.styles import PINK_PRIMARY
    from indexing.services import knowledge_service

    service = graph_gui
    expect = playwright.expect
    output = service["path"] / "graph-rightbar"
    output.mkdir(parents=True, exist_ok=True)
    default = next(graph for graph in knowledge_service.list_graphs()["graphs"] if graph["is_default"])
    empty = knowledge_service.create_graph("用于检查空状态的知识空间")
    preview = knowledge_service.create_graph("研究知识图谱")
    chinese_title = "面向复杂资料的知识图谱与证据关联阅读体验" * 3
    english_title = "KnowledgeGraphEvidenceRelationship" * 5
    apply(graph_id=default["id"], objects=[
        {"ref": "root", "kind": "concept", "title": chinese_title,
         "aliases": ["知识关联", english_title], "summary": "摘要应保持清晰的阅读宽度，并随主题变化。" * 5},
        {"ref": "target", "kind": "entity", "title": english_title, "summary": "英文长词也不能撑宽右栏。"}],
        relations=[{"ref": "edge", "source": {"ref": "root"}, "target": {"ref": "target"},
                    "predicate": "related_to", "description": "从概念连接到相关实体", "basis": "inference"}],
        evidence=[{"object": {"ref": "root"}, "source_kind": "user", "source_title": "阅读与布局核验",
                   "quote": "关联证据应保留完整内容：" + english_title}])
    apply(graph_id=preview["id"], objects=[
        {"ref": "knowledge", "kind": "concept", "title": "知识图谱", "aliases": ["Knowledge graph"],
         "summary": "用实体与语义关系组织知识，并为每个判断保留可追溯的证据。通过关联阅读，让分散的资料形成清晰的知识结构。"},
        {"ref": "entity", "kind": "concept", "title": "实体"},
        {"ref": "relation", "kind": "concept", "title": "语义关系"},
        {"ref": "evidence", "kind": "entity", "title": "证据溯源"},
        {"ref": "rag", "kind": "entity", "title": "检索增强生成"}],
        relations=[{"ref": f"edge-{target}", "source": {"ref": "knowledge"}, "target": {"ref": target},
                    "predicate": "related_to", "description": "关联知识", "basis": "inference"}
                   for target in ("entity", "relation", "evidence", "rag")],
        evidence=[{"object": {"ref": "knowledge"}, "source_kind": "user", "source_title": "知识整理笔记",
                   "quote": "关系描述实体之间的联系，证据保留这些联系的来源。图谱中的每次补充，都可以回到原始资料核验。"}])

    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1540, "height": 1000},
                                      http_credentials={"username": "piece", "password": service["key"]})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        context.route("**/*", lambda route: route.continue_()
                      if route.request.url.startswith(service["url"] + "/") else route.abort())

        def choose_space(name):
            page.locator(".graph-space-picker").get_by_role("combobox").click()
            page.get_by_role("option", name=name, exact=True).click()
            expect(page.locator(".graph-space-picker")).to_contain_text(name)

        def assert_widths():
            measure = """root => {
                const boundary = root.getBoundingClientRect();
                return [root, ...root.querySelectorAll(
                    '.workspace-toolbar, .graph-toolbar, .graph-detail, .graph-stage, .graph-canvas-tools')]
                    .filter(el => el.getBoundingClientRect().width > 0)
                    .filter(el => el.scrollWidth > el.clientWidth + 2 ||
                        el.getBoundingClientRect().right > boundary.right + 2 ||
                        el.getBoundingClientRect().left < boundary.left - 2)
                    .map(el => ({classes: el.className, width: el.clientWidth, scroll: el.scrollWidth}));
            }"""
            try:
                page.wait_for_function("() => (" + measure + ")(document.querySelector('.graph-workbench')).length === 0", timeout=5000)
            except playwright.TimeoutError:
                print("Persistent graph overflow:", page.locator(".graph-workbench").evaluate(measure))
                print("Toolbar children:", page.locator(".graph-toolbar").evaluate("root => [...root.children].map(el => ({tag:el.tagName,text:el.textContent,classes:el.className,width:el.clientWidth,scroll:el.scrollWidth,rect:el.getBoundingClientRect().toJSON()}))"))
                raise
            overflow = page.locator(".graph-workbench").evaluate(measure)
            assert not overflow, overflow

        def capture(name, *, graph=False):
            page.mouse.move(450, 20)
            if graph:
                page.wait_for_function("""() => {
                    const el = document.querySelector('.nicegui-echart');
                    const data = el && getElement(el.id.slice(1))?.chart?.getModel()?.getSeriesByIndex(0)?.getEdgeData();
                    return data?.getItemGraphicEl(0)?.getLinePath()?.shape.percent === 1;
                }""")
            page.screenshot(path=str(output / name), animations="disabled")

        try:
            page.goto(service["url"])
            expect(page.locator(".library-catalog")).to_be_visible(timeout=30000)
            for theme in ("light", "dark", "pink"):
                page.set_viewport_size({"width": 1540, "height": 1000})
                page.locator("[data-open-settings]").click()
                page.get_by_role("button", name=t(f"settings_appearance.theme_{theme}"), exact=True).click()
                expect(page.locator("body")).to_have_css("--q-primary", PINK_PRIMARY if theme == "pink" else "#5898d4")
                page.locator(".workspace-primary").get_by_role("button", name=t("sidebar.graph"), exact=True).click()
                expect(page.locator(".graph-workbench")).to_be_visible()
                expect(page.locator(".graph-space-picker")).to_contain_text(
                    default["name"] if theme == "light" else preview["name"])
                choose_space(empty["name"])
                expect(page.locator(".graph-stage").get_by_text(gk("empty"), exact=True)).to_be_visible()
                page.locator(".graph-space-actions").click()
                menu = page.locator(".q-menu").filter(has_text=gk("create_graph"))
                for key in ("create_graph", "rename_graph", "delete_graph", "export_graph_json", "export_graphml", "import_graph"):
                    expect(menu.get_by_text(gk(key), exact=True)).to_be_visible()
                menu.get_by_text(gk("rename_graph"), exact=True).click()
                dialog = page.get_by_role("dialog")
                expect(dialog.get_by_label(gk("graph_name"), exact=True)).to_have_value(empty["name"])
                dialog.get_by_role("button", name=k("cancel"), exact=True).click()
                expect(dialog).not_to_be_visible()
                capture(f"empty-{theme}.png")
                choose_space(default["name"])
                page.locator(".graph-space-actions").click()
                expect(page.locator(".q-menu").get_by_text(gk("delete_graph"), exact=True)).to_have_count(0)
                page.keyboard.press("Escape")

                for width in (1540, 1024, 768):
                    page.set_viewport_size({"width": width, "height": 1000})
                    page.get_by_role("button", name=gk("graph"), exact=True).click()
                    for index, title in enumerate((chinese_title, english_title)):
                        page.get_by_role("button", name=title, exact=True).first.click()
                        expect(page.locator(".graph-detail").get_by_role("heading", name=title, exact=True)).to_be_visible()
                        assert_widths()
                        capture(f"detail-{theme}-{width}-{'zh' if index == 0 else 'en'}.png")
                    page.locator(".graph-inspector").get_by_role("button", name=gk("center"), exact=True).click()
                    expect(page.locator(".nicegui-echart canvas")).to_be_visible(timeout=30000)
                    expect(page.locator(".graph-canvas-tools")).to_be_visible()
                    assert_widths()
                    expect(page.locator(".graph-workbench")).to_have_css(
                        "background-color", "rgb(32, 34, 38)" if theme == "dark" else "rgb(255, 255, 255)")
                    capture(f"canvas-{theme}-{width}.png", graph=True)
                    page.get_by_role("button", name=gk("objects"), exact=True).click()
                    expect(page.get_by_role("button", name=gk("objects"), exact=True)).to_have_attribute("aria-pressed", "true")
                page.set_viewport_size({"width": 1540, "height": 1000})
                choose_space(preview["name"])
                page.locator(".knowledge-catalog").get_by_role("button", name="知识图谱", exact=True).click()
                expect(page.locator(".graph-detail").get_by_role("heading", name="知识图谱", exact=True)).to_be_visible()
                capture(f"preview-detail-{theme}.png")
                page.locator(".graph-inspector").get_by_role("button", name=gk("center"), exact=True).click()
                expect(page.locator(".nicegui-echart canvas")).to_be_visible(timeout=30000)
                capture(f"preview-canvas-{theme}.png", graph=True)
            assert not errors, errors
        finally:
            page.screenshot(path=str(output / "last.png"))
            print("Graph right pane screenshots:", output)
            context.close()
            browser.close()
