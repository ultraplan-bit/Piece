"""各视图须填满可调分栏，不能保留旧固定宽度或把内容裁掉。"""
import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401
from app.i18n import t
from tree_browser_helpers import collection_row

FILES = t("sidebar.files")


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
            expect(collection_row(page, service["root"])).to_be_visible(timeout=30000)
            for view in (FILES, "Wiki", "知识图谱"):
                expect(page.locator(".workspace-nav-item").get_by_text(view, exact=True)).to_be_visible()
            for width in (1920, 1440, 850):
                page.set_viewport_size({"width": width, "height": 900})
                for view in (FILES, "Wiki", "知识图谱", "设置", "MCP 配置", "Skill 导出", "召回测试", "云同步", "日志"):
                    header = page.locator(".app-header")
                    if view in ("MCP 配置", "Skill 导出", "召回测试", "日志"):
                        header.get_by_role("button", name="工具与集成", exact=True).click()
                        page.locator(".q-menu").get_by_text(view, exact=True).click()
                    elif view in (FILES, "Wiki", "知识图谱"):
                        page.locator(".workspace-primary").get_by_role("button", name=view, exact=True).click()
                    else:
                        header.get_by_role("button", name=view, exact=True).click()
                    if view in (FILES, "Wiki", "知识图谱"):
                        expect(page.locator(".workspace-primary [aria-current=page]")).to_contain_text(view)
                    expect(header.locator(".app-view-title")).to_contain_text(view)
                    if view == "设置":
                        page.get_by_text("基础设置", exact=True).first.click()
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
                    expected_width = {FILES: 320, "Wiki": 260, "知识图谱": 260, "召回测试": 300}.get(view, 240)
                    assert abs(layout["middle"] - expected_width) <= 1, (view, width, layout)
                    navigation = page.locator(".workspace-splitter").first.locator(":scope > .q-splitter__before")
                    expect(navigation).to_have_css("width", "160px")
                    assert abs(layout["middle"] - layout["middleContent"]) <= 1, (view, width, layout)
                    assert abs(layout["right"] - layout["rightContent"]) <= 1, (view, width, layout)
                    assert abs(layout["height"] - layout["contentHeight"]) <= 1, (view, width, layout)
                    page.screenshot(path=str(service["path"] / f"layout-{width}-{view}.png"))
            # 像素初始宽度不影响拖动；切换工作区后恢复用户调整的宽度。
            page.locator(".workspace-primary").get_by_role("button", name="Wiki", exact=True).click()
            splitter = page.locator(".workspace-splitter").last
            before = splitter.locator(":scope > .q-splitter__before")
            expect(before).to_have_css("width", "260px")
            separator = splitter.locator(":scope > .q-splitter__separator").bounding_box()
            assert separator is not None
            x, y = separator["x"] + separator["width"] / 2, separator["y"] + separator["height"] / 2
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x + 60, y, steps=5)
            page.mouse.up()
            page.wait_for_function("""() => {
                const pane = [...document.querySelectorAll('.workspace-splitter')].at(-1)
                    .querySelector(':scope > .q-splitter__before');
                return Math.abs(pane.clientWidth - 320) <= 1;
            }""")
            page.locator(".workspace-primary").get_by_role("button", name="知识图谱", exact=True).click()
            expect(before).to_have_css("width", "260px")
            page.locator(".workspace-primary").get_by_role("button", name="Wiki", exact=True).click()
            expect(before).to_have_css("width", "320px")
            page.set_viewport_size({"width": 1920, "height": 900})
            expect(before).to_have_css("width", "320px")
            # 目录按钮只把焦点移回浏览区；阅读与切换工作区都不会收起目录。
            for view, other, width, other_width in (
                ("Wiki", "知识图谱", 320, 260),
                ("知识图谱", FILES, 260, 320),
                (FILES, "Wiki", 320, 320),
            ):
                page.locator(".workspace-primary").get_by_role("button", name=view, exact=True).click()
                focus = page.get_by_role("button", name="定位目录", exact=True)
                expect(focus).to_have_count(1)
                expect(before).to_have_css("width", f"{width}px")
                focus.click()
                expect(before).to_have_css("width", f"{width}px")
                assert page.evaluate("!!document.activeElement.closest('[data-catalog]')")
                page.locator(".workspace-primary").get_by_role("button", name=other, exact=True).click()
                expect(before).to_have_css("width", f"{other_width}px")
                page.locator(".workspace-primary").get_by_role("button", name=view, exact=True).click()
                expect(before).to_have_css("width", f"{width}px")
            # 重载页面也保留用户调过的目录宽度，而不是退回默认值或收起。
            page.wait_for_function("""() => Object.keys(localStorage).some(key =>
                key.startsWith('piece.workspace.v1.') && JSON.parse(localStorage[key]).widths.wiki === 320)""")
            page.reload()
            expect(page.locator(".library-catalog")).to_be_visible(timeout=30000)
            page.locator(".workspace-primary").get_by_role("button", name="Wiki", exact=True).click()
            expect(page.locator(".results-splitter > .q-splitter__before")).to_have_css("width", "320px")
            assert not errors, errors
        finally:
            page.screenshot(path=str(service["path"] / "right-final.png"))
            print("Right panel screenshots:", service["path"])
            browser.close()
