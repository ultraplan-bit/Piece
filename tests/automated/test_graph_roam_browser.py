"""Real wheel/pan gestures must not rebuild the graph or move it after release."""
import json

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401
from test_graph_browser import gk, graph_gui  # noqa: F401
from test_graph_hierarchy_layout_browser import broad_hierarchy, wait_overview, assert_overview_labels_readable  # noqa: F401
from app.i18n import t


def test_overview_wheel_and_pan_keep_continuous_view(broad_hierarchy):
    fixture, expect = broad_hierarchy, playwright.expect
    service = fixture["service"]
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1274, "height": 1000},
                                http_credentials={"username": "piece", "password": service["key"]})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(service["url"] + "/") else route.abort())
        try:
            page.goto(service["url"])
            expect(page.locator(".library-catalog")).to_be_visible(timeout=30000)
            page.get_by_role("button", name=t("sidebar.graph"), exact=True).click()
            page.locator(".graph-space-picker").get_by_role("combobox").click()
            page.get_by_role("option", name=fixture["graph"]["name"], exact=True).click()
            assert_overview_labels_readable(wait_overview(page))
            page.locator(".nicegui-echart").evaluate("""el => {
                const chart = getElement(el.id.slice(1)).chart;
                const probe = window.roamProbe = {chart, writes: [], roams: [], active: false};
                const setOption = chart.setOption.bind(chart);
                chart.setOption = (...args) => {
                    probe.writes.push({time:performance.now(), active:probe.active});
                    return setOption(...args);
                };
                probe.view = () => {
                    const model = chart.getModel().getSeriesByIndex(0);
                    const data = model.getData();
                    return {zoom:model.get('zoom'), center:model.get('center'),
                        points:Array.from({length:data.count()}, (_,i) => data.getItemLayout(i).slice()),
                        transform:model.coordinateSystem.transform.slice()};
                };
                chart.on('graphroam', () => probe.roams.push(probe.view()));
                probe.initial = probe.view();
            }""")
            box = page.locator(".nicegui-echart").bounding_box()
            anchor = [box["width"] * 0.62, box["height"] * 0.43]
            page.mouse.move(box["x"] + anchor[0], box["y"] + anchor[1])
            metrics = []
            for delta in (-70, 70):
                page.evaluate("() => {roamProbe.active=true; roamProbe.writes=[]; roamProbe.roams=[];}")
                for _ in range(12):
                    page.mouse.wheel(0, delta)
                    page.wait_for_timeout(25)
                during = page.evaluate("""async () => {
                    await new Promise(requestAnimationFrame);
                    roamProbe.active=false;
                    return {writes:roamProbe.writes.slice(), roams:roamProbe.roams.slice(), end:roamProbe.view()};
                }""")
                page.wait_for_timeout(650)
                after = page.evaluate("() => ({writes:roamProbe.writes, view:roamProbe.view()})")
                metrics.append({"delta":delta, "during":during, "after":after})
                (service["path"] / "roam-metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
                assert len(during["roams"]) >= 10
                assert not during["writes"], "A wheel gesture must use viewport transforms without setOption data rebuilds"
                assert len(after["writes"]) <= 1, "Label settling must not start a redraw loop"
                assert after["view"]["zoom"] == pytest.approx(during["end"]["zoom"])
                assert after["view"]["transform"] == pytest.approx(during["end"]["transform"], abs=0.01)
                assert after["view"]["points"] == during["end"]["points"]
                zooms = [state["zoom"] for state in during["roams"]]
                assert zooms == sorted(zooms, reverse=delta > 0)
            # A one-pixel trackpad input should not become ECharts' minimum 10% jump.
            before = page.evaluate("() => roamProbe.view()")
            page.mouse.wheel(0, -1)
            page.wait_for_function("zoom => roamProbe.view().zoom > zoom", arg=before["zoom"])
            after_small = page.evaluate("() => roamProbe.view()")
            assert 1 < after_small["zoom"] / before["zoom"] < 1.02
            # The world position below the cursor must stay at the same screen point.
            old, new = before["transform"], after_small["transform"]
            assert new[0] * (anchor[0] - old[4]) / old[0] + new[4] == pytest.approx(anchor[0], abs=0.01)
            assert new[3] * (anchor[1] - old[5]) / old[3] + new[5] == pytest.approx(anchor[1], abs=0.01)
            page.wait_for_timeout(350)
            # Line scrolling equals its pixel distance; multiple events in one frame are combined.
            batch = page.evaluate("""async () => {
                const el=roamProbe.chart.getDom(), rect=el.getBoundingClientRect();
                const send = (deltaY,deltaMode) => el.dispatchEvent(new WheelEvent('wheel', {
                    deltaY,deltaMode,bubbles:true,cancelable:true,clientX:rect.left+rect.width*.62,
                    clientY:rect.top+rect.height*.43}));
                const before=roamProbe.view().zoom, count=roamProbe.roams.length;
                for (let i=0;i<16;i++) send(-1,0);
                await new Promise(requestAnimationFrame);
                const pixels=roamProbe.view().zoom, events=roamProbe.roams.length-count;
                send(1,1);
                await new Promise(requestAnimationFrame);
                return {before,pixels,after:roamProbe.view().zoom,events};
            }""")
            assert batch["pixels"] > batch["before"]
            assert batch["events"] == 1
            assert batch["after"] == pytest.approx(batch["before"])
            # Saturation must not accumulate unconsumed scroll, delaying the reverse gesture.
            for _ in range(3):
                page.mouse.wheel(0, -10000)
                page.wait_for_timeout(30)
            page.wait_for_function("() => roamProbe.view().zoom === 5")
            page.mouse.wheel(0, 1)
            page.wait_for_function("() => roamProbe.view().zoom < 5")
            assert page.evaluate("() => roamProbe.view().zoom") > 4.9
            page.wait_for_timeout(350)
            page.get_by_role("button", name=gk("reset_view"), exact=True).click()
            assert_overview_labels_readable(wait_overview(page))
            page.get_by_role("button", name=gk("zoom_in"), exact=True).click()
            page.wait_for_function("() => Math.abs(roamProbe.view().zoom-1.25)<1e-6")
            page.get_by_role("button", name=gk("zoom_out"), exact=True).click()
            page.wait_for_function("() => Math.abs(roamProbe.view().zoom-1)<1e-6")
            page.wait_for_timeout(350)
            # Pan on blank canvas, then release: the view must not snap back during label settling.
            blank = page.evaluate("""() => {
                const chart=roamProbe.chart, coord=chart.getModel().getSeriesByIndex(0).coordinateSystem;
                for (let y=80;y<chart.getHeight()-80;y+=25) for (let x=100;x<chart.getWidth()-100;x+=25) {
                    if (coord.containPoint([x,y]) && !chart.getZr().findHover(x,y).target) return [x,y];
                }
            }""")
            assert blank, "The fixture must have a blank area for an actual pan gesture"
            page.mouse.move(box["x"] + blank[0], box["y"] + blank[1])
            start = page.evaluate("() => {roamProbe.active=true; roamProbe.writes=[]; return roamProbe.view();}")
            page.mouse.down()
            page.mouse.move(box["x"] + blank[0] + 70, box["y"] + blank[1] + 30, steps=12)
            page.mouse.up()
            end = page.evaluate("() => {roamProbe.active=false; return {view:roamProbe.view(), writes:roamProbe.writes.slice()};}")
            page.wait_for_timeout(650)
            after = page.evaluate("() => ({view:roamProbe.view(), writes:roamProbe.writes})")
            assert not end["writes"]
            assert end["view"]["transform"][4] - start["transform"][4] == pytest.approx(70, abs=0.01)
            assert end["view"]["transform"][5] - start["transform"][5] == pytest.approx(30, abs=0.01)
            assert end["view"]["points"] == start["points"]
            assert len(after["writes"]) <= 1
            assert after["view"]["transform"] == pytest.approx(end["view"]["transform"], abs=0.01)
            assert not errors, errors
        finally:
            page.screenshot(path=str(service["path"] / "roam-final.png"))
            browser.close()
