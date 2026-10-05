"""阶段 C 实机验收：临时新库 + 本机 Edge，无 CDN / 模型请求。

uv run --no-sync --with playwright pytest -q -s tests/automated/test_knowledge_browser.py
截图、日志保留在 pytest 临时目录；复用阶段 A 的真实 GUI 服务启动/清理。
"""
import json
import time
from uuid import uuid4

import httpx
import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401
from app.i18n import t


def k(name):
    return t("knowledge." + name)


@pytest.fixture
def wiki_gui(gui_service):
    with httpx.Client(base_url=gui_service["url"], headers={"Authorization": "Bearer " + gui_service["key"]}, timeout=30) as client:
        client.headers["X-Piece-Target"] = gui_service["cli"]("status").data["data"]["target_id"]
        def post(operation, **payload):
            response = client.post("/api/v1/" + operation, json=payload)
            data = response.json()
            assert data["success"], data
            return data["data"]

        def apply(**parts):
            return post("knowledge/apply", request_key=uuid4().hex, reason="浏览器验收", **parts)

        yield {**gui_service, "post": post, "apply": apply}


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
                return {id: nodes.getId(i), x: rect.x + rect.width / 2, y: rect.y + rect.height / 2,
                        width: rect.width, height: rect.height, layout: nodes.getItemLayout(i)};
            }),
            edges: Array.from({length: edges.count()}, (_, i) => {
                const line = edges.getItemGraphicEl(i).childAt(0);
                return {id: edges.getId(i), point: line.transformCoordToGlobal(...line.pointAt(0.5)),
                        symbols: [edges.getItemVisual(i, 'fromSymbol'), edges.getItemVisual(i, 'toSymbol')]};
            }),
            zoom: chart.getOption().series[0].zoom,
        };
    }""")


def wait_graph(page, nodes, edges):
    page.wait_for_function("""expected => {
        const el = document.querySelector('.nicegui-echart');
        const series = el && getElement(el.id.slice(1))?.chart?.getModel()?.getSeriesByIndex(0);
        return series?.getData().count() === expected.nodes && series?.getEdgeData().count() === expected.edges;
    }""", arg={"nodes": nodes, "edges": edges})


def click_chart(page, point):
    canvas = page.locator(".nicegui-echart")
    canvas.scroll_into_view_if_needed()
    canvas.click(position={"x": point[0], "y": point[1]})


def test_knowledge_browser_roundtrip(wiki_gui):
    service = wiki_gui
    post, apply = service["post"], service["apply"]
    expect = playwright.expect
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1540, "height": 1080},
                                      http_credentials={"username": "piece", "password": service["key"]})
        # 外部地址不得成为渲染知识正文/图谱的资源依赖。
        external_requests = []
        def route(request):
            if request.request.url.startswith(service["url"] + "/"):
                request.continue_()
            else:
                external_requests.append(request.request.url)
                request.abort()
        context.route("**/*", route)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(service["url"])
            expect(page.get_by_text("人工智能 (1)", exact=True)).to_be_visible(timeout=30000)
            page.get_by_role("button", name='Expand "文件库"', exact=True).click()
            page.get_by_role("button", name=k("title"), exact=True).click()
            expect(page.get_by_text(k("empty"), exact=True)).to_be_visible()
            page.screenshot(path=str(service["path"] / "knowledge-empty.png"))

            page.get_by_role("button", name=k("create"), exact=True).click()
            dialog = page.get_by_role("dialog")
            dialog.get_by_label(k("title_field"), exact=True).fill("浏览器知识页")
            dialog.get_by_label(k("body"), exact=True).fill("# 阅读与核查\n\n保留人工正文。\n\n| 项目 | 内容 |\n| --- | --- |\n| 范围 | 本地知识 |\n\n公式 $a^2+b^2=c^2$。")
            dialog.get_by_label(k("aliases_text"), exact=True).fill("浏览器别名")
            dialog.get_by_label(k("reason"), exact=True).fill("用户在界面创建")
            dialog.get_by_role("button", name=k("save"), exact=True).click()
            expect(dialog).not_to_be_visible()
            expect(page.locator(".knowledge-prose")).to_contain_text("保留人工正文")
            expect(page.locator(".knowledge-prose table")).to_be_visible()
            expect(page.locator(".knowledge-prose math")).to_be_visible()
            page.screenshot(path=str(service["path"] / "knowledge-page.png"))
            root = post("knowledge/search", query="浏览器别名")["objects"][0]["id"]
            assert post("knowledge/get", kind="object", id=root)["record"]["revision"] == 1
            page.get_by_role("button", name=k("view_graph"), exact=True).click()
            wait_graph(page, 1, 0)
            expect(page.get_by_text(k("no_edges"), exact=True).first).to_be_visible()
            page.get_by_role("button", name=k("pages"), exact=True).first.click()

            # 外部调用者并发修改：GUI 草稿必须保留，不能自动提升版本重试。
            page.get_by_role("button", name=k("edit"), exact=True).click()
            dialog.get_by_label(k("body"), exact=True).fill("未保存的人工草稿")
            dialog.get_by_label(k("reason"), exact=True).fill("比较后修订")
            apply(objects=[{"id": root, "expected_revision": 1, "summary": "外部调用者的新摘要"}])
            dialog.get_by_role("button", name=k("save"), exact=True).click()
            expect(dialog.get_by_text(k("conflict_draft"), exact=True)).to_be_visible()
            expect(dialog.get_by_label(k("body"), exact=True)).to_have_value("未保存的人工草稿")
            assert post("knowledge/get", kind="object", id=root)["record"]["revision"] == 2
            page.screenshot(path=str(service["path"] / "knowledge-conflict.png"))
            dialog.get_by_role("button", name=k("close"), exact=True).click()

            made = apply(objects=[{"ref": "target", "kind": "entity", "title": "关联目标：长中文名称用于检查标签布局"}],
                         relations=[{"ref": "edge", "source": {"id": root}, "target": {"ref": "target"},
                                     "predicate": "related_to", "description": "用于核对局部关系与证据", "basis": "inference"}])
            target = made["refs"]["target"]["id"]
            relation = made["refs"]["edge"]["id"]
            chunk = post("chunk/list", file_id=service["file_id"])["chunks"][0]
            library = post("knowledge/list")["library_id"]
            evidence = apply(objects=[{"id": root, "expected_revision": 2,
                                      "body": f'[目标页](piece://knowledge/{target.upper()})\n\n<script>window.__wiki_xss=1</script>\n\n[危险链接](javascript:alert(1))\n\n![外部图片](https://example.invalid/tracker.png)'}],
                             links=[{"source": {"id": root}, "target": {"id": target}}],
                             evidence=[{"relation": {"id": relation}, "source_kind": "piece", "source_library_id": library,
                                        "source_file_id": service["file_id"], "source_chunk_id": chunk["id"], "quote": "原页核验正文"}])
            eid = evidence["evidence"][0]["id"]
            page.get_by_role("button", name=k("search"), exact=True).click()
            page.get_by_role("button", name="浏览器知识页", exact=True).first.click()
            expect(page.locator(".knowledge-prose")).to_contain_text("目标页")
            assert page.evaluate("window.__wiki_xss") is None
            assert page.locator('.knowledge-prose a[href^="javascript:"]').count() == 0
            page.locator(".knowledge-prose").get_by_text("目标页", exact=True).click()
            expect(page.get_by_text(k("no_body"), exact=True)).to_be_visible()
            page.get_by_role("button", name="浏览器知识页", exact=True).first.click()
            page.get_by_role("button", name=k("view_graph"), exact=True).click()
            expect(page.locator(".nicegui-echart canvas")).to_be_visible(timeout=30000)
            expect(page.get_by_text(k("table"), exact=True)).to_be_visible()
            state = chart_state(page)
            assert all(5 <= node["width"] <= 35 and 5 <= node["height"] <= 35 for node in state["nodes"]), state
            node = next(node for node in state["nodes"] if node["id"] == target)
            click_chart(page, (node["x"], node["y"]))
            expect(page.get_by_text(k("no_body"), exact=True)).to_be_visible()
            # 高亮允许放大符号，但不能移动节点或重排整图。
            assert [(n["id"], n["x"], n["y"], n["layout"]) for n in chart_state(page)["nodes"]] == [
                (n["id"], n["x"], n["y"], n["layout"]) for n in state["nodes"]]
            edge = next(edge for edge in state["edges"] if edge["id"] == relation)
            assert edge["symbols"] == ["none", "none"]
            assert any(e["symbols"] == ["none", "arrow"] for e in state["edges"])
            click_chart(page, edge["point"])
            expect(page.get_by_text("用于核对局部关系与证据", exact=False)).to_be_visible()
            page.screenshot(path=str(service["path"] / "knowledge-graph-light.png"))
            expect(page.get_by_text(k("location_current"), exact=True)).to_be_visible()
            page.get_by_role("button", name=k("compare_source"), exact=True).click()
            expect(dialog.get_by_text(chunk["chunk_text"], exact=True)).to_be_visible()
            page.screenshot(path=str(service["path"] / "knowledge-evidence.png"))
            dialog.get_by_role("button", name=k("open_file"), exact=True).click()
            expect(page.locator(".source-pane img")).to_be_visible(timeout=30000)
            expect(page.get_by_text(chunk["chunk_text"], exact=True)).to_be_visible()
            page.screenshot(path=str(service["path"] / "knowledge-source.png"))
            page.get_by_role("button", name=k("return_knowledge"), exact=True).click()
            expect(page.get_by_text(k("location_current"), exact=True)).to_be_visible()
            assert [(n["id"], n["layout"]) for n in chart_state(page)["nodes"]] == [
                (n["id"], n["layout"]) for n in state["nodes"]]

            # 键盘表格与画布打开同一条关系；文件反查也使用同一个知识入口。
            page.get_by_role("button", name=k("related_to"), exact=True).focus()
            page.keyboard.press("Enter")
            expect(page.get_by_text(k("location_current"), exact=True)).to_be_visible()
            page.get_by_role("button", name=k("return_file"), exact=True).click()
            page.get_by_text("研究方法.pdf", exact=True).last.click()
            page.get_by_role("button", name=k("references"), exact=True).click()
            expect(page.get_by_role("button", name="用于核对局部关系与证据", exact=True)).to_be_visible()
            page.get_by_role("button", name="用于核对局部关系与证据", exact=True).click()
            expect(page.get_by_text(k("location_current"), exact=True)).to_be_visible()
            page.get_by_role("button", name=k("close"), exact=True).click()

            # 删除原件不删除知识和引用快照；已失效卡片不能通过旧 ID 再跳转。
            preview = post("file/delete", file_ids=[service["file_id"]], dry_run=True)
            assert preview["knowledge_evidence_count"] == 1
            post("file/delete", file_ids=[service["file_id"]], confirmed=True)
            page.get_by_role("button", name=k("related_to"), exact=True).click()
            expect(page.get_by_text(k("location_missing"), exact=True)).to_be_visible()
            expect(page.get_by_text("原页核验正文", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name=k("compare_source"), exact=True)).to_have_count(0)
            assert post("knowledge/get", kind="evidence", id=eid)["record"]["location_status"] == "missing"

            # 证据删除只清理证据，保留知识页和关系。
            page.get_by_role("button", name=k("delete_evidence"), exact=True).click()
            expect(dialog.get_by_text(k("delete_preview"), exact=True)).to_be_visible()
            dialog.get_by_role("button", name=k("confirm_delete"), exact=True).click()
            expect(dialog).not_to_be_visible()
            assert post("knowledge/get", kind="relation", id=relation)["evidence"]["total"] == 0
            assert post("knowledge/list")["total"] == 2
            assert post("knowledge/references", source_library_id=library, source_file_id=service["file_id"])["total"] == 0

            page.get_by_role("button", name="浏览器知识页", exact=True).first.click()
            page.get_by_role("button", name=k("view_graph"), exact=True).click()
            page.set_viewport_size({"width": 850, "height": 780})
            navigation = page.locator(".workspace-splitter > .q-splitter__before").first
            expect(navigation).to_have_js_property("clientWidth", 0)
            page.get_by_role("button", name=k("navigation"), exact=True).click()
            expect(navigation).not_to_have_js_property("clientWidth", 0)
            page.get_by_role("button", name=k("navigation"), exact=True).click()
            expect(navigation).to_have_js_property("clientWidth", 0)
            page.screenshot(path=str(service["path"] / "knowledge-narrow.png"))
            page.set_viewport_size({"width": 1540, "height": 1080})
            expect(navigation).not_to_have_js_property("clientWidth", 0)
            page.get_by_role("button", name="设置", exact=True).click()
            page.get_by_text("基础设置", exact=True).click()
            page.get_by_text("深色", exact=True).click()
            page.get_by_role("button", name=k("title"), exact=True).click()
            expect(page.locator(".nicegui-echart canvas")).to_be_visible()
            page.screenshot(path=str(service["path"] / "knowledge-graph-dark.png"))
            page.evaluate("document.documentElement.style.fontSize = '24px'")
            page.get_by_role("button", name=k("reading"), exact=True).click()
            expect(navigation).to_have_js_property("clientWidth", 0)
            expect(page.locator(".nicegui-echart canvas")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(service["path"] / "knowledge-large-text.png"))
            assert not errors, errors
            assert not external_requests, external_requests
            print("知识库浏览器截图：", service["path"])
        finally:
            page.screenshot(path=str(service["path"] / "knowledge-final.png"))
            browser.close()


def test_knowledge_browser_cycles_filters_and_manual_relations(wiki_gui):
    service = wiki_gui
    made = service["apply"](objects=[
        {"ref": "a", "kind": "concept", "title": "循环中心"},
        {"ref": "b", "kind": "entity", "title": "循环第二页", "aliases": ["循环中心"], "status": "disputed"},
        {"ref": "c", "kind": "topic", "title": "循环第三页"}],
        links=[{"source": {"ref": a}, "target": {"ref": b}} for a, b in (("a", "b"), ("b", "a"), ("b", "c"), ("c", "b"))],
        relations=[{"ref": predicate, "source": {"ref": "a"}, "target": {"ref": "b"}, "predicate": predicate,
                    "description": "同一端点的不同关系", "basis": "inference"} for predicate in ("depends_on", "supports")])
    ids = {key: value["id"] for key, value in made["refs"].items()}
    expect = playwright.expect
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1540, "height": 1080},
                                http_credentials={"username": "piece", "password": service["key"]})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(service["url"])
            expect(page.get_by_text("人工智能 (1)", exact=True)).to_be_visible(timeout=30000)
            page.get_by_role("button", name='Expand "文件库"', exact=True).click()
            page.get_by_role("button", name=k("title"), exact=True).click()
            page.get_by_role("button", name="循环中心", exact=True).click()
            page.get_by_role("button", name=k("view_graph"), exact=True).click()
            expect(page.locator(".nicegui-echart canvas")).to_be_visible()
            assert len(chart_state(page)["nodes"]) == 2 and len(chart_state(page)["edges"]) == 4
            page.get_by_role("combobox", name=k("one_hop"), exact=True).click()
            page.get_by_role("option", name=k("two_hops"), exact=True).click()
            page.get_by_role("button", name=k("refresh"), exact=True).click()
            wait_graph(page, 3, 6)
            assert len(chart_state(page)["edges"]) == 6
            page.screenshot(path=str(service["path"] / "knowledge-cycle.png"))

            page.get_by_role("combobox", name=k("predicate"), exact=True).click()
            page.get_by_role("option", name=k("depends_on"), exact=True).click()
            page.keyboard.press("Escape")
            page.get_by_role("combobox", name=k("link") + ", " + k("relation"), exact=True).click()
            page.get_by_role("option", name=k("link"), exact=True).click()
            page.keyboard.press("Escape")
            page.get_by_role("button", name=k("refresh"), exact=True).click()
            wait_graph(page, 2, 1)
            filtered = chart_state(page)
            assert {node["id"] for node in filtered["nodes"]} == {ids["a"], ids["b"]}
            assert filtered["edges"][0]["id"] == ids["depends_on"]

            page.get_by_role("button", name=k("review"), exact=True).click()
            page.get_by_role("button", name=k("run_checks"), exact=True).click()
            expect(page.get_by_text(k("lint_AMBIGUOUS_NAME"), exact=True).first).to_be_visible()
            expect(page.get_by_role("button", name="循环第二页 · " + k("entity") + " · " + ids["b"][:8], exact=True)).to_be_visible()
            page.get_by_role("button", name=k("disputed"), exact=True).click()
            expect(page.get_by_role("button", name=k("run_checks"), exact=True)).to_have_count(0)
            page.get_by_role("button", name="循环第二页", exact=True).first.click()
            expect(page.locator(".knowledge-detail > .text-2xl")).to_have_text("循环第二页")

            page.get_by_role("button", name=k("create_relation"), exact=True).click()
            dialog = page.get_by_role("dialog")
            dialog.get_by_label(k("target_search"), exact=True).fill("循环第三页")
            dialog.get_by_role("button", name=k("search"), exact=True).click()
            dialog.get_by_label(k("target_object"), exact=True).click()
            page.get_by_role("option").filter(has_text="循环第三页").click()
            dialog.get_by_label(k("description"), exact=True).fill("人工创建的关系")
            dialog.get_by_label(k("reason"), exact=True).fill("界面维护验收")
            dialog.get_by_role("button", name=k("save"), exact=True).click()
            expect(dialog).not_to_be_visible()
            expect(page.get_by_text(k("description") + " · 人工创建的关系", exact=True)).to_be_visible()
            page.get_by_role("button", name=k("edit"), exact=True).click()
            dialog.get_by_label(k("description"), exact=True).fill("人工修订的关系")
            dialog.get_by_label(k("reason"), exact=True).fill("核查后修订")
            dialog.get_by_role("button", name=k("save"), exact=True).click()
            expect(dialog).not_to_be_visible()
            page.get_by_role("button", name=k("add_evidence"), exact=True).click()
            dialog.get_by_label(k("quote"), exact=True).fill("人工补充的背景说明")
            dialog.get_by_label(k("reason"), exact=True).fill("补充用户证据")
            dialog.get_by_role("button", name=k("save"), exact=True).click()
            expect(dialog).not_to_be_visible()
            expect(page.get_by_text("人工补充的背景说明", exact=True)).to_be_visible()
            expect(page.get_by_text(k("location_unverified"), exact=True)).to_be_visible()
            graph = service["post"]("knowledge/graph", root_id=ids["b"])
            relation = next(e for e in graph["edges"] if e.get("description") == "人工修订的关系")
            assert relation["revision"] == 2 and relation["evidence_count"] == 1
            page.screenshot(path=str(service["path"] / "knowledge-manual.png"))
            assert not errors, errors
        finally:
            page.screenshot(path=str(service["path"] / "knowledge-cycle-final.png"))
            browser.close()


def test_knowledge_browser_updates_after_cli_without_manual_search(wiki_gui):
    service = wiki_gui
    expect = playwright.expect
    title = "CLI 自动更新知识页"
    body = "## 原有正文\n\n" + "\n\n".join(f"阅读段落 {i}：保留阅读位置。" for i in range(80))
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1540, "height": 1080},
                                http_credentials={"username": "piece", "password": service["key"]})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(service["url"])
            expect(page.get_by_text("人工智能 (1)", exact=True)).to_be_visible(timeout=30000)
            page.get_by_role("button", name='Expand "文件库"', exact=True).click()
            page.get_by_role("button", name=k("title"), exact=True).click()
            expect(page.get_by_text(k("empty"), exact=True)).to_be_visible()
            batch = service["path"] / "auto-refresh.json"
            batch.write_text(json.dumps({"reason": "CLI 自动更新验收", "objects": [
                {"ref": "new", "kind": "topic", "title": title, "body": body}]}, ensure_ascii=False), encoding="utf-8")
            response = service["cli"]("wiki", "apply", "--input", str(batch), "--request-id", "browser-auto-refresh")
            assert response.code == 0, response.stdout
            oid = response.data["data"]["refs"]["new"]["id"]
            item = page.get_by_role("button", name=title, exact=True)
            expect(item).to_be_visible(timeout=10000)
            item.click()
            prose = page.locator(".knowledge-prose")
            expect(prose).to_contain_text("原有正文")
            prose.evaluate("el => el.dataset.sameNode = 'retained'")
            page.wait_for_timeout(2400)  # 覆盖无变化的一轮自动检查，DOM 不应重建。
            expect(prose).to_have_attribute("data-same-node", "retained")

            prose.evaluate("el => el.closest('.q-scrollarea').querySelector('.q-scrollarea__container').scrollTop = 450")
            page.wait_for_function("document.querySelector('.knowledge-prose').closest('.q-scrollarea').querySelector('.q-scrollarea__container').scrollTop > 400")
            service["apply"](objects=[{"id": oid, "expected_revision": 1, "body": body.replace("原有正文", "自动同步正文")}])
            expect(prose).to_contain_text("自动同步正文", timeout=10000)
            page.wait_for_function("Math.abs(document.querySelector('.knowledge-prose').closest('.q-scrollarea').querySelector('.q-scrollarea__container').scrollTop - 450) < 5")

            page.get_by_role("button", name=k("edit"), exact=True).click()
            dialog = page.get_by_role("dialog")
            dialog.get_by_label(k("body"), exact=True).fill("未保存的个人草稿")
            dialog.get_by_label(k("reason"), exact=True).fill("草稿保护验证")
            service["apply"](objects=[{"id": oid, "expected_revision": 2, "body": "另一个调用者的正文"}])
            expect(prose).to_contain_text("另一个调用者的正文", timeout=10000)
            expect(dialog.get_by_label(k("body"), exact=True)).to_have_value("未保存的个人草稿")
            dialog.get_by_role("button", name=k("save"), exact=True).click()
            expect(dialog.get_by_text(k("conflict_draft"), exact=True)).to_be_visible()
            dialog.get_by_role("button", name=k("close"), exact=True).click()

            service["apply"](evidence=[{"object": {"id": oid}, "source_kind": "user", "quote": "外部增加的证据"}])
            expect(page.get_by_text("外部增加的证据", exact=True)).to_be_visible(timeout=10000)
            page.screenshot(path=str(service["path"] / "knowledge-auto-refresh.png"))
            preview = service["post"]("knowledge/delete", kind="object", id=oid, expected_revision=3)
            service["post"]("knowledge/delete", kind="object", id=oid, expected_revision=3,
                            impact_token=preview["impact_token"], request_key="browser-auto-delete", dry_run=False, confirmed=True)
            expect(item).to_have_count(0, timeout=10000)
            expect(page.get_by_text(k("broken_link"), exact=True)).to_be_visible(timeout=10000)
            expect(prose).to_have_count(0)
            assert not errors, errors
        finally:
            page.screenshot(path=str(service["path"] / "knowledge-auto-refresh-last.png"))
            browser.close()


def test_knowledge_browser_high_degree_budget(wiki_gui):
    service = wiki_gui
    apply = service["apply"]
    root = apply(objects=[{"ref": "root", "kind": "topic", "title": "高出度验收中心"}])["refs"]["root"]["id"]
    targets = []
    kinds = ("concept", "entity", "topic", "synthesis", "source_summary")
    for start in range(0, 99, 20):
        result = apply(objects=[{"ref": str(i), "kind": kinds[i % 5], "title": f"节点 {i} · 中文标题"}
                                for i in range(start, min(start + 20, 99))])
        targets.extend(item["id"] for item in result["objects"])
    relations = [{"ref": f"r{i}-{variant}", "source": {"id": root}, "target": {"id": target},
                  "predicate": "related_to" if variant == 0 else "depends_on", "description": "预算测试关系",
                  "qualifier": str(variant), "basis": "inference"}
                 for i, target in enumerate(targets) for variant in range(3)]
    relations.extend({"ref": f"extra{i}", "source": {"id": root}, "target": {"id": target}, "predicate": "applies_to",
                      "description": "预算边界", "basis": "inference"} for i, target in enumerate(targets[:4]))
    for start in range(0, len(relations), 100):
        apply(relations=relations[start:start + 100])
    graph = service["post"]("knowledge/graph", root_id=root)
    assert graph["truncated"] and len(graph["nodes"]) == 100 and len(graph["edges"]) == 300
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1540, "height": 1080},
                                http_credentials={"username": "piece", "password": service["key"]})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(service["url"])
            playwright.expect(page.get_by_text("人工智能 (1)", exact=True)).to_be_visible(timeout=30000)
            page.get_by_role("button", name='Expand "文件库"', exact=True).click()
            page.get_by_role("button", name=k("title"), exact=True).click()
            page.get_by_label(k("search"), exact=True).fill("高出度验收中心")
            page.get_by_label(k("search"), exact=True).press("Enter")
            page.get_by_role("button", name="高出度验收中心", exact=True).click()
            start = time.monotonic()
            page.get_by_role("button", name=k("view_graph"), exact=True).click()
            playwright.expect(page.locator(".nicegui-echart canvas")).to_be_visible(timeout=30000)
            playwright.expect(page.get_by_text(k("truncated"), exact=True).first).to_be_visible()
            elapsed = time.monotonic() - start
            state = chart_state(page)
            assert len(state["nodes"]) == 100 and len(state["edges"]) == 300
            node = next(n for n in state["nodes"] if n["id"] != root)
            start = time.monotonic()
            click_chart(page, (node["x"], node["y"]))
            selected_title = next(n["title"] for n in graph["nodes"] if n["id"] == node["id"])
            playwright.expect(page.locator(".knowledge-detail > .text-2xl")).to_have_text(selected_title)
            selection_elapsed = time.monotonic() - start
            canvas = page.locator(".nicegui-echart")
            canvas.hover()
            page.mouse.wheel(0, -120)
            page.wait_for_function("getElement(document.querySelector('.nicegui-echart').id.slice(1)).chart.getOption().series[0].zoom !== 1")
            versions = page.evaluate("async () => ({echarts: (await import('nicegui-echart')).echarts.version, browser: navigator.userAgent})")
            metrics = {**versions, "nodes": 100, "edges": 300, "first_render_seconds": round(elapsed, 3),
                       "selection_seconds": round(selection_elapsed, 3), "zoom": chart_state(page)["zoom"]}
            (service["path"] / "knowledge-metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
            page.screenshot(path=str(service["path"] / "knowledge-budget.png"))
            print(f"局部图实测：{metrics}；截图：{service['path']}")
            assert not errors, errors
        finally:
            browser.close()
