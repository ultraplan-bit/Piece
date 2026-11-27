"""Real Canvas exploration: dense groups, full pagination, source direction, and cycles."""
import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_graph_browser import apply, chart_state, gk, graph_gui, k  # noqa: F401
from test_collection_browser import gui_service  # noqa: F401
from app.i18n import t


def click_canvas_node(page, node_id):
    page.wait_for_function("""id => {
        const el = document.querySelector('.nicegui-echart');
        const chart = el && getElement(el.id.slice(1))?.chart;
        const data = chart?.getModel()?.getSeriesByIndex(0)?.getData();
        if (!data) return false;
        for (let i = 0; i < data.count(); i++) {
            if (data.getId(i) !== id || !data.getItemGraphicEl(i)) continue;
            const path = data.getItemGraphicEl(i).getSymbolPath();
            const box = path.getBoundingRect();
            const point = path.transformCoordToGlobal(box.x + box.width / 2, box.y + box.height / 2);
            const previous = window.graphClickGeometry;
            if (!previous || previous.id !== id || Math.hypot(point[0]-previous.x,point[1]-previous.y)>0.1) {
                window.graphClickGeometry = {id,x:point[0],y:point[1],time:performance.now()};
                return false;
            }
            return performance.now()-previous.time > 100;
        }
        return false;
    }""", arg=node_id)
    position = page.locator(".nicegui-echart").evaluate("""(el, id) => {
        const chart = getElement(el.id.slice(1)).chart;
        const data = chart.getModel().getSeriesByIndex(0).getData();
        const index = Array.from({length: data.count()}, (_, i) => i).find(i => data.getId(i) === id);
        const graphic = data.getItemGraphicEl(index);
        const path = graphic.getSymbolPath();
        const box = path.getBoundingRect();
        const point = path.transformCoordToGlobal(box.x + box.width / 2, box.y + box.height / 2);
        const bounds = el.getBoundingClientRect();
        return {x: bounds.left + point[0], y: bounds.top + point[1]};
    }""", node_id)
    page.mouse.click(position["x"], position["y"])


def seed_large_graph(graph_id):
    ids = []
    for start in range(0, 420, 20):
        result = apply(graph_id=graph_id, objects=[
            {"ref": str(index), "kind": "concept" if index % 2 else "entity", "title": f"Graph node {index:03}"}
            for index in range(start, start + 20)])
        ids.extend(result["refs"][str(index)]["id"] for index in range(start, start + 20))
    relations = [
        {"ref": f"{index}-{step}", "source": {"id": ids[index]}, "target": {"id": ids[(index + step) % 400]},
         "predicate": predicate, "description": f"Relation {index}-{step}", "basis": "explicit"}
        for index in range(400) for step, predicate in ((1, "depends_on"), (7, "supports"), (29, "contradicts"))]
    for start in range(0, len(relations), 100):
        apply(graph_id=graph_id, relations=relations[start:start + 100])
    return ids


def test_dense_groups_pagination_and_directed_hierarchy(graph_gui):
    from indexing.services import knowledge_service

    service = graph_gui
    large = knowledge_service.create_graph("Dense exploration")
    seed_large_graph(large["id"])
    hierarchy = knowledge_service.create_graph("Directed hierarchy")
    result = apply(graph_id=hierarchy["id"], objects=[
        {"ref": node, "kind": "concept", "title": f"Hierarchy {node}"} for node in "ABCDE"], relations=[
        {"ref": source + target, "source": {"ref": source}, "target": {"ref": target},
         "predicate": "is_a", "description": "Classification", "basis": "explicit"}
        for source, target in (("A", "B"), ("B", "A"), ("C", "A"), ("C", "D"), ("D", "E"))])
    hierarchy_ids = {node: result["refs"][node]["id"] for node in "ABCDE"}
    expect = playwright.expect
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1100},
                                http_credentials={"username": "piece", "password": service["key"]})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(service["url"] + "/") else route.abort())

        def choose_space(name):
            page.locator(".graph-space-picker").get_by_role("combobox").click()
            page.get_by_role("option", name=name, exact=True).click()
            expect(page.locator(".graph-space-picker")).to_contain_text(name)

        try:
            page.goto(service["url"])
            expect(page.locator(".library-catalog")).to_be_visible(timeout=30000)
            page.get_by_role("button", name=t("sidebar.graph"), exact=True).click()
            choose_space(large["name"])
            state = chart_state(page, "graph")[0]
            groups = [node for node in state["data"] if node.get("kind") == "group"]
            assert groups and state["count"] < 400
            group = next(node for node in groups if not node.get("isolated"))
            click_canvas_node(page, group["id"])
            expect(page.locator(".graph-inspector")).to_be_visible()
            expect(page.locator(".graph-inspector .graph-detail-title")).to_have_text(group["name"])
            expect(page.locator(".graph-inspector").get_by_text(gk("structure_group_hint"), exact=True)).to_be_visible()
            page.locator(".graph-inspector").get_by_role("button", name=gk("expand_group"), exact=True).click()
            page.wait_for_function("""ids => {
                const el = document.querySelector('.nicegui-echart');
                const nodes = getElement(el.id.slice(1)).chart.getOption().series[0].data;
                return ids.every(id => nodes.some(node => node.id === id));
            }""", arg=group["member_ids"])
            expect(page.locator(".graph-inspector").get_by_role("button", name=gk("collapse_group"), exact=True)).to_be_visible()
            page.locator(".graph-inspector").get_by_role("button", name=gk("collapse_group"), exact=True).click()
            page.wait_for_function("""id => {
                const el = document.querySelector('.nicegui-echart');
                return getElement(el.id.slice(1)).chart.getOption().series[0].data.some(node => node.id === id);
            }""", arg=group["id"])
            page.locator(".graph-inspector").get_by_role("button", name=gk("close"), exact=True).click()
            expect(page.locator(".graph-inspector")).not_to_be_visible()
            page.get_by_role("button", name=gk("load_more_nodes"), exact=True).click()
            expect(page.get_by_text(t("graph.overview_count", nodes=420, edges=1000, total_nodes=420, total_edges=1200), exact=True)).to_be_visible(timeout=15000)
            page.get_by_role("button", name=gk("load_more_edges"), exact=True).click()
            expect(page.get_by_text(t("graph.overview_count", nodes=420, edges=1200, total_nodes=420, total_edges=1200), exact=True)).to_be_visible(timeout=15000)
            expect(page.get_by_role("button", name=gk("load_more_edges"), exact=True)).to_have_count(0)
            page.mouse.move(20, 20)
            page.wait_for_function("""() => {
                const el = document.querySelector('.nicegui-echart');
                return getElement(el.id.slice(1)).chart.getOption().series[0].labelLayout?.hideOverlap;
            }""")
            labels = page.locator(".nicegui-echart").evaluate("""el => {
                const data = getElement(el.id.slice(1)).chart.getModel().getSeriesByIndex(0).getData();
                const boxes = [];
                data.eachItemGraphicEl(graphic => {
                    const text = graphic.getSymbolPath()?.getTextContent();
                    if (!text || text.ignore || text.invisible) return;
                    const box = text.getBoundingRect().clone();
                    box.applyTransform(text.getComputedTransform());
                    boxes.push({x: box.x, y: box.y, width: box.width, height: box.height, text: text.style.text});
                });
                return boxes;
            }""")
            assert labels
            overlapping = [(first["text"], second["text"]) for index, first in enumerate(labels) for second in labels[index + 1:]
                           if min(first["x"] + first["width"], second["x"] + second["width"]) - max(first["x"], second["x"]) > 2
                           and min(first["y"] + first["height"], second["y"] + second["height"]) - max(first["y"], second["y"]) > 2]
            assert not overlapping, overlapping
            page.screenshot(path=str(service["path"] / "dense-groups.png"))
            page.locator(".graph-toolbar").get_by_role("button", name=gk("filters"), exact=True).click()
            expect(page.locator(".graph-filter-menu")).to_be_visible()
            page.locator(".graph-filter-menu").get_by_role("switch", name=gk("isolated_only"), exact=True).click()
            page.keyboard.press("Escape")
            expect(page.get_by_text(t("graph.overview_count", nodes=20, edges=0, total_nodes=420, total_edges=1200), exact=True)).to_be_visible(timeout=15000)
            assert chart_state(page, "graph")[0]["count"] == 20

            choose_space(hierarchy["name"])
            expect(page.get_by_text(t("graph.overview_count", nodes=5, edges=5, total_nodes=5, total_edges=5), exact=True)).to_be_visible()
            page.get_by_role("button", name=gk("hierarchy"), exact=True).click()
            expect(page.locator(".graph-hierarchy-select")).to_be_visible()
            page.wait_for_function("""() => {
                const el = document.querySelector('.nicegui-echart');
                return getElement(el.id.slice(1)).chart.getOption().series[0].data.some(node => node.cycle_id);
            }""")
            state = chart_state(page, "graph")[0]
            nodes = {node["id"]: node for node in state["data"]}
            assert nodes[hierarchy_ids["A"]]["cycle_id"] == nodes[hierarchy_ids["B"]]["cycle_id"]
            assert nodes[hierarchy_ids["C"]]["y"] > nodes[hierarchy_ids["D"]]["y"] > nodes[hierarchy_ids["E"]]["y"]
            links = page.locator(".nicegui-echart").evaluate("el => getElement(el.id.slice(1)).chart.getOption().series[0].links")
            assert {(edge["source"], edge["target"]) for edge in links} == {
                (hierarchy_ids[source], hierarchy_ids[target]) for source, target in (("A", "B"), ("B", "A"), ("C", "A"), ("C", "D"), ("D", "E"))}
            assert all(edge["symbol"][1] == "arrow" for edge in links)
            expect(page.get_by_text(gk("cycle_detected"), exact=True)).to_be_visible()
            click_canvas_node(page, hierarchy_ids["C"])
            expect(page.locator(".graph-inspector .graph-detail-title")).to_have_text("Hierarchy C")
            expect(page.get_by_role("button", name=gk("hierarchy"), exact=True)).to_have_attribute("aria-pressed", "true")
            page.screenshot(path=str(service["path"] / "directed-hierarchy.png"))
            assert not errors, errors
        finally:
            page.screenshot(path=str(service["path"] / "exploration-final.png"))
            print("Graph exploration screenshots:", service["path"])
            browser.close()
