"""Wide sibling layers with long Chinese titles remain readable on the real canvas."""
import json

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401
from test_graph_browser import apply, gk, graph_gui  # noqa: F401
from test_graph_exploration_browser import click_canvas_node
from app.i18n import t


@pytest.fixture
def broad_hierarchy(graph_gui):
    from indexing.services import knowledge_service

    graph = knowledge_service.create_graph("长标题组成层级可读性")
    titles = ["复杂知识系统的总体架构与组成关系", "资料处理与内容组织的核心子系统", "知识关联与检索分析的核心子系统"]
    titles.extend(f"研究主题{index:02}的资料采集整理与证据关联模块" for index in range(31))
    titles.extend(f"尚未加入组成关系的独立资料主题{index:02}" for index in range(13))
    ids = []
    for start in range(0, len(titles), 20):
        result = apply(graph_id=graph["id"], objects=[
            {"ref": str(index), "kind": "concept" if index < 3 else "entity", "title": titles[index]}
            for index in range(start, min(start + 20, len(titles)))])
        ids.extend(result["refs"][str(index)]["id"] for index in range(start, min(start + 20, len(titles))))
    part_pairs = [(1, 0), (2, 0)] + [(index, 1 if index < 19 else 2) for index in range(3, 34)]
    dependency_pairs = [(index, index - 1) for index in range(1, 9)]
    apply(graph_id=graph["id"], relations=[
        {"ref": f"{predicate}-{source}-{target}", "source": {"id": ids[source]}, "target": {"id": ids[target]},
         "predicate": predicate, "description": "用于核验关系方向、层级和长中文标题", "basis": "explicit"}
        for predicate, pairs in (("part_of", part_pairs), ("depends_on", dependency_pairs)) for source, target in pairs])
    return {"graph": graph, "ids": ids, "titles": titles, "part_pairs": part_pairs,
            "dependency_pairs": dependency_pairs, "service": graph_gui}


def hierarchy_geometry(page):
    return page.locator(".nicegui-echart").evaluate("""el => {
        const chart = getElement(el.id.slice(1)).chart;
        const series = chart.getOption().series[0];
        const data = chart.getModel().getSeriesByIndex(0).getData();
        const nodes = [];
        data.eachItemGraphicEl((graphic, index) => {
            const path = graphic.getSymbolPath();
            const symbol = path.getBoundingRect().clone();
            symbol.applyTransform(path.getComputedTransform());
            const text = path.getTextContent();
            const label = text?.getBoundingRect().clone();
            if (label) label.applyTransform(text.getComputedTransform());
            nodes.push({id:data.getId(index), x:series.data[index].x, y:series.data[index].y,
                symbol:{x:symbol.x,y:symbol.y,width:symbol.width,height:symbol.height},
                label:label ? {x:label.x,y:label.y,width:label.width,height:label.height,text:text.style.text,
                    visible:!text.ignore && !text.invisible} : null});
        });
        return {width:chart.getWidth(),height:chart.getHeight(),nodes,links:series.links || series.edges,
            options:series.data,pieceHierarchy:series.pieceHierarchy};
    }""")


def wait_hierarchy(page, count, predicate):
    page.wait_for_function("""({count,predicate}) => {
        const el = document.querySelector('.nicegui-echart');
        const chart = el && getElement(el.id.slice(1))?.chart;
        const series = chart?.getOption()?.series?.[0];
        if (!series?.pieceHierarchy || series.data.length !== count ||
            !(series.links || series.edges).every(edge => edge.predicate === predicate)) return false;
        if (!series.pieceHierarchySize || Math.abs(series.pieceHierarchySize[0]-chart.getWidth())>1 ||
            Math.abs(series.pieceHierarchySize[1]-chart.getHeight())>1) return false;
        const model = chart.getModel().getSeriesByIndex(0).getData();
        const geometry = series.data.map((node,index) => {
            const path = model.getItemGraphicEl(index)?.getSymbolPath();
            if (!path) return null;
            return [node.id, node.x, node.y, node.symbolSize, path.getComputedTransform()];
        });
        if (geometry.some(item => !item)) return false;
        const signature = JSON.stringify([chart.getWidth(),chart.getHeight(),geometry]);
        if (window.hierarchyGeometry?.signature !== signature) {
            window.hierarchyGeometry = {signature,time:performance.now()};
            return false;
        }
        return performance.now() - window.hierarchyGeometry.time > 150;
    }""", arg={"count": count, "predicate": predicate})


def assert_cards_readable(geometry, child_ids):
    nodes = geometry["nodes"]
    assert len(nodes) == 34
    children = [node for node in nodes if node["id"] in child_ids]
    # The 31 siblings must wrap into rows rather than fit 6200 units into one strip.
    assert len({round(node["y"], 2) for node in children}) >= 3
    x0 = min(node["symbol"]["x"] for node in nodes)
    y0 = min(node["symbol"]["y"] for node in nodes)
    x1 = max(node["symbol"]["x"] + node["symbol"]["width"] for node in nodes)
    y1 = max(node["symbol"]["y"] + node["symbol"]["height"] for node in nodes)
    assert (x1 - x0) / max(1, y1 - y0) < 3, geometry
    assert y1 - y0 >= geometry["height"] * 0.35, geometry
    for node in nodes:
        box, label = node["symbol"], node["label"]
        assert box["x"] >= -1 and box["y"] >= -1, node
        assert box["x"] + box["width"] <= geometry["width"] + 1, node
        assert box["y"] + box["height"] <= geometry["height"] + 1, node
        assert label and label["visible"] and len(label["text"]) >= 6, node
        assert "\n" in label["text"], node
        assert label["x"] >= box["x"] - 2 and label["y"] >= box["y"] - 2, node
        assert label["x"] + label["width"] <= box["x"] + box["width"] + 2, node
        assert label["y"] + label["height"] <= box["y"] + box["height"] + 2, node
    for index, first in enumerate(nodes):
        for second in nodes[index + 1:]:
            a, b = first["symbol"], second["symbol"]
            dx = min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"])
            dy = min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"])
            assert dx < 2 or dy < 2, (first, second)


def wait_overview(page):
    page.mouse.move(450, 20)
    page.wait_for_function("""() => {
        const el = document.querySelector('.nicegui-echart');
        const chart = el && getElement(el.id.slice(1))?.chart;
        const series = chart?.getOption()?.series?.[0];
        if (!series || series.pieceHierarchy || series.data.length !== 47 ||
            (series.links || series.edges).length !== 41) return false;
        if (!chart.getZr().animation.isFinished()) return false;
        const data = chart.getModel().getSeriesByIndex(0).getData();
        const geometry = series.data.map((node,index) => {
            const path = data.getItemGraphicEl(index)?.getSymbolPath();
            if (!path) return null;
            return [node.id, node.x, node.y, path.getComputedTransform()];
        });
        if (geometry.some(item => !item)) return false;
        const signature = JSON.stringify([chart.getWidth(),chart.getHeight(),geometry]);
        if (window.overviewGeometry?.signature !== signature) {
            window.overviewGeometry = {signature,time:performance.now()};
            return false;
        }
        return performance.now() - window.overviewGeometry.time > 200;
    }""")
    return hierarchy_geometry(page)


def assert_overview_labels_readable(geometry):
    labels = [node["label"] for node in geometry["nodes"] if node["label"] and node["label"]["visible"]]
    assert labels
    for node in geometry["nodes"]:
        label = node["label"]
        if not label or not label["visible"]:
            continue
        assert label["x"] >= -1 and label["y"] >= -1, node
        assert label["x"] + label["width"] <= geometry["width"] + 1, node
        assert label["y"] + label["height"] <= geometry["height"] + 1, node
        for other in geometry["nodes"]:
            if other["id"] == node["id"]:
                continue
            box = other["symbol"]
            dx = min(label["x"] + label["width"], box["x"] + box["width"]) - max(label["x"], box["x"])
            dy = min(label["y"] + label["height"], box["y"] + box["height"]) - max(label["y"], box["y"])
            assert dx < 2 or dy < 2, (node, other)
    for index, first in enumerate(labels):
        for second in labels[index + 1:]:
            dx = min(first["x"] + first["width"], second["x"] + second["width"]) - max(first["x"], second["x"])
            dy = min(first["y"] + first["height"], second["y"] + second["height"]) - max(first["y"], second["y"])
            assert dx < 2 or dy < 2, (first, second)


def test_broad_chinese_hierarchy_cards_resize_reflow_and_keep_selection(broad_hierarchy):
    fixture, expect = broad_hierarchy, playwright.expect
    service, ids = fixture["service"], fixture["ids"]
    child_ids = set(ids[3:34])
    output = service["path"] / "hierarchy-layout"
    output.mkdir()
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1274, "height": 1000},
                                http_credentials={"username": "piece", "password": service["key"]})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(service["url"] + "/") else route.abort())

        def predicate(value, count):
            page.locator(".graph-hierarchy-select").get_by_role("combobox").click()
            page.get_by_role("option", name=gk(value), exact=True).click()
            wait_hierarchy(page, count, value)

        try:
            page.goto(service["url"])
            expect(page.locator(".library-catalog")).to_be_visible(timeout=30000)
            for theme in ("light", "dark"):
                page.set_viewport_size({"width": 1274, "height": 1000})
                page.locator("[data-open-settings]").click()
                page.get_by_role("button", name=t(f"settings_appearance.theme_{theme}"), exact=True).click()
                page.locator(".workspace-primary").get_by_role("button", name=t("sidebar.graph"), exact=True).click()
                page.locator(".graph-space-picker").get_by_role("combobox").click()
                page.get_by_role("option", name=fixture["graph"]["name"], exact=True).click()
                expect(page.locator(".graph-space-picker")).to_contain_text(fixture["graph"]["name"])
                if theme == "light":
                    expect(page.get_by_text(t("graph.overview_count", nodes=47, edges=41, total_nodes=47, total_edges=41), exact=True)).to_be_visible()
                initial = wait_overview(page)
                assert_overview_labels_readable(initial)
                original_positions = {node["id"]: (node["x"], node["y"]) for node in initial["nodes"]}
                page.get_by_role("button", name=gk("hierarchy"), exact=True).click()
                expect(page.locator(".graph-hierarchy-select")).to_be_visible()
                predicate("part_of", 34)
                # Leaving the hierarchy tab restores the filters and network from before entry.
                page.get_by_role("button", name=gk("global_graph"), exact=True).click()
                expect(page.get_by_role("button", name=gk("global_graph"), exact=True)).to_have_attribute("aria-pressed", "true")
                overview = wait_overview(page)
                assert len(overview["links"]) == 41
                assert {node["id"]: (node["x"], node["y"]) for node in overview["nodes"]} == original_positions
                expect(page.locator(".graph-filter-summary").nth(1)).not_to_be_visible()
                page.get_by_role("button", name=gk("hierarchy"), exact=True).click()
                expect(page.locator(".graph-hierarchy-select")).to_be_visible()
                predicate("part_of", 34)
                for width in (1274, 1000, 768):
                    page.set_viewport_size({"width": width, "height": 1000})
                    wait_hierarchy(page, 34, "part_of")
                    before = hierarchy_geometry(page)
                    (output / f"geometry-{theme}-{width}.json").write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
                    assert_cards_readable(before, child_ids)
                    assert {node["id"] for node in before["nodes"]} == set(ids[:34])
                    assert {(edge["source"], edge["target"]) for edge in before["links"]} == {
                        (ids[source], ids[target]) for source, target in fixture["part_pairs"]}
                    visible_nodes = {node["id"]: node for node in before["nodes"]}
                    for source, target in fixture["part_pairs"]:
                        child, parent = visible_nodes[ids[source]]["symbol"], visible_nodes[ids[target]]["symbol"]
                        assert child["y"] > parent["y"] + parent["height"], (child, parent)
                    assert all(node["label"]["position"] == "inside" for node in before["options"])
                    assert all(fixture["titles"][ids.index(node["id"])] in node["tooltip_text"] for node in before["options"])
                    page.mouse.move(450, 20)
                    page.screenshot(path=str(output / f"hierarchy-{theme}-{width}.png"))
                    click_canvas_node(page, ids[3])
                    expect(page.locator(".graph-inspector .graph-detail-title")).to_have_text(fixture["titles"][3])
                    after = hierarchy_geometry(page)
                    assert {node["id"]: (node["x"], node["y"]) for node in before["nodes"]} == {
                        node["id"]: (node["x"], node["y"]) for node in after["nodes"]}
                    page.locator(".graph-inspector").get_by_role("button", name=gk("close"), exact=True).click()
                    expect(page.locator(".graph-inspector")).not_to_be_visible()
                predicate("depends_on", 9)
                dependency = hierarchy_geometry(page)
                dependency_nodes = {node["id"]: node for node in dependency["nodes"]}
                assert all(dependency_nodes[ids[source]]["y"] > dependency_nodes[ids[target]]["y"]
                           for source, target in fixture["dependency_pairs"])
                assert any(dependency_nodes[node["id"]]["x"] != node["x"] or dependency_nodes[node["id"]]["y"] != node["y"]
                           for node in before["nodes"] if node["id"] in dependency_nodes)
                page.screenshot(path=str(output / f"dependency-{theme}-768.png"))
                predicate("part_of", 34)
                assert_cards_readable(hierarchy_geometry(page), child_ids)
                # 清除层级筛选会切回概览：不能把 34 张卡片的网格坐标套到全部 47 个节点上。
                page.locator(".graph-toolbar").get_by_role("button", name=gk("filters"), exact=True).click()
                page.locator(".graph-filter-menu").get_by_role("button", name=gk("clear_filters"), exact=True).click()
                page.keyboard.press("Escape")
                expect(page.get_by_role("button", name=gk("global_graph"), exact=True)).to_have_attribute("aria-pressed", "true")
                expect(page.locator(".graph-hierarchy-select")).not_to_be_visible()
                for width in (1274, 768):
                    page.set_viewport_size({"width": width, "height": 1000})
                    overview = wait_overview(page)
                    assert {node["id"]: (node["x"], node["y"]) for node in overview["nodes"]} == original_positions
                    assert_overview_labels_readable(overview)
                    assert all("hierarchy_label" not in node for node in overview["options"])
                    (output / f"overview-{theme}-{width}.json").write_text(json.dumps(overview, ensure_ascii=False, indent=2), encoding="utf-8")
                    page.screenshot(path=str(output / f"overview-{theme}-{width}.png"))
            assert not errors, errors
        finally:
            page.screenshot(path=str(output / "last.png"))
            print("Hierarchy layout screenshots:", output)
            browser.close()
