"""各视图须填满可调分栏，不能保留旧固定宽度或把内容裁掉。"""
import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401


def test_right_panel_layout(gui_service):
    service = gui_service
    expect = playwright.expect
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900},
                                http_credentials={"username": "piece", "password": service["key"]})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(service["url"])
            expect(page.get_by_text("人工智能 (1)", exact=True)).to_be_visible(timeout=30000)
            page.get_by_role("button", name='Expand "文件库"', exact=True).click()
            expect(page.get_by_role("button", name='Collapse "文件库"', exact=True)).to_be_visible()
            for width in (1440, 850):
                page.set_viewport_size({"width": width, "height": 900})
                for view in ("文件库", "知识库", "设置", "MCP 配置", "Skill 导出", "召回测试", "云同步", "日志"):
                    heading = page.get_by_role("button", name=f'Collapse "{view}"', exact=True, include_hidden=True)
                    if heading.count() == 0:
                        page.get_by_role("button", name=view, exact=True).click()
                    expect(heading).to_have_count(1)
                    if view == "设置":
                        page.get_by_text("基础设置", exact=True).click()
                        expect(page.get_by_text("主题模式", exact=True)).to_be_visible()
                    layout = page.evaluate("""() => {
                        const splitter = [...document.querySelectorAll('.workspace-splitter')].at(-1);
                        const before = splitter.querySelector(':scope > .q-splitter__before');
                        const after = splitter.querySelector(':scope > .q-splitter__after');
                        const left = before.firstElementChild.getBoundingClientRect();
                        const right = after.firstElementChild.firstElementChild.getBoundingClientRect();
                        return {middle: before.clientWidth, middleContent: left.width,
                                right: after.clientWidth, rightContent: right.width,
                                height: after.clientHeight, contentHeight: right.height};
                    }""")
                    assert abs(layout["middle"] - layout["middleContent"]) <= 1, (view, width, layout)
                    assert abs(layout["right"] - layout["rightContent"]) <= 1, (view, width, layout)
                    assert abs(layout["height"] - layout["contentHeight"]) <= 1, (view, width, layout)
                    page.screenshot(path=str(service["path"] / f"layout-{width}-{view}.png"))
                    if view == "知识库" and width < 1000:
                        page.get_by_role("button", name="导航", exact=True).click()
            assert not errors, errors
        finally:
            page.screenshot(path=str(service["path"] / "right-final.png"))
            print("Right panel screenshots:", service["path"])
            browser.close()
