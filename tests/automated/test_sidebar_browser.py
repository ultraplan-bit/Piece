"""窄栏下的统一树、长名称和全局图标栏恢复；临时库 + 真实 Edge。"""
import re

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401
from app.i18n import t
from tree_browser_helpers import collection_row, document_row, expand_collection


def test_sidebar_shrinking_keeps_labels_and_controls_contained(knowledge_base, request):
    from indexing.services import collection_service, file_service

    names = ["侧栏宽度检查", "很长的集合名称用于窄栏显示验证" * 4, "LongUnbrokenCollectionName" * 3]
    parent = None
    ids = []
    for name in names:
        parent = collection_service.create_collection(name, parent_id=parent)["collection_id"]
        ids.append(parent)
    file_id = file_service.create_empty_file("侧栏验收")["file_id"]
    collection_service.set_file_collections(file_id, [ids[-1]])
    service = request.getfixturevalue("gui_service")
    expect = playwright.expect
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900},
                                http_credentials={"username": "piece", "password": service["key"]})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(service["url"])
            expect(collection_row(page, ids[0])).to_be_visible(timeout=30000)
            sidebar = page.locator(".workspace-sidebar")
            catalog = page.locator(".library-catalog")
            expect(sidebar.locator("[data-collection-id]")).to_have_count(0)
            for cid in ids:
                expand_collection(page, cid)
            leaf = collection_row(page, ids[-1])
            document_row(page, file_id, collection_id=ids[-1]).click()
            expect(page.locator(".library-reader-title")).to_have_text("侧栏验收.md")

            def resize(splitter, width):
                before = splitter.locator(":scope > .q-splitter__before")
                separator = splitter.locator(":scope > .q-splitter__separator").bounding_box()
                current = before.bounding_box()["width"]
                x, y = separator["x"] + separator["width"] / 2, separator["y"] + 100
                page.mouse.move(x, y)
                page.mouse.down()
                page.mouse.move(x + width - current, y, steps=5)
                page.mouse.up()
                expect(before).to_have_css("width", f"{width}px")

            for width in (380, 320, 260, 220):
                resize(page.locator(".results-splitter"), width)
                page.mouse.move(1400, 20)
                colors = set()
                for dark in (False, True):
                    page.evaluate("dark => Quasar.Dark.set(dark)", dark)
                    colors.add(leaf.locator(".collection-tree-name").evaluate("el => getComputedStyle(el).color"))
                    catalog.screenshot(path=str(service["path"] / f"tree-{width}-{'dark' if dark else 'light'}.png"))
                assert len(colors) == 2
                page.evaluate("Quasar.Dark.set(false)")
                overflow = catalog.evaluate("""catalog => {
                    const bounds = catalog.getBoundingClientRect();
                    return [...catalog.querySelectorAll('.resource-collection-row, .collection-tree-name, .collection-tree-count, .collection-tree-icon, .tree-expander, .collection-row-action, .resource-file-row .library-file-name')]
                        .filter(el => el.getBoundingClientRect().width > 0)
                        .flatMap(el => {
                            const rect = el.getBoundingClientRect();
                            return rect.left >= bounds.left && rect.right <= bounds.right + 1 ? []
                                : [{text: el.textContent, left: rect.left, right: rect.right, bounds: bounds.toJSON()}];
                        });
                }""")
                assert not overflow, (width, overflow)
            expect(leaf).to_have_attribute("aria-label", names[-1])
            expect(leaf.locator(".collection-tree-count")).to_have_text("1")
            footer = sidebar.locator(".library-footer")
            stats = footer.locator(".navigation-label").inner_text()
            footer.hover()
            expect(page.locator(".q-tooltip").filter(has_text=stats)).to_be_visible()

            # 全局导航可收成图标栏，但文件浏览树不会因此关闭。
            splitter = page.locator(".workspace-splitter").first
            before = splitter.locator(":scope > .q-splitter__before")
            resize(splitter, 120)
            expect(sidebar).to_have_class(re.compile(".*navigation-collapsed.*"))
            expect(catalog).to_be_visible()
            expect(leaf).to_be_visible()
            sidebar.get_by_role("button", name=t("sidebar.wiki"), exact=True).click()
            expect(sidebar.locator("[aria-current=page]")).to_have_attribute("aria-label", t("sidebar.wiki"))
            expect(before).to_have_css("width", "52px")
            sidebar.get_by_role("button", name=t("sidebar.files"), exact=True).click()
            expect(leaf).to_be_visible()
            page.locator(".app-header").get_by_role("button", name=t("workspace.navigation"), exact=True).click()
            expect(before).to_have_css("width", "160px")
            expect(document_row(page, file_id, collection_id=ids[-1])).to_be_visible()
            page.wait_for_function("""() => Object.keys(localStorage).some(key =>
                key.startsWith('piece.workspace.v1.') && JSON.parse(localStorage[key]).navigation_width === 160)""")
            page.reload()
            expect(before).to_have_css("width", "160px", timeout=30000)
            expect(leaf).to_be_visible()
            expect(document_row(page, file_id, collection_id=ids[-1])).to_be_visible()
            assert not errors, errors
        finally:
            page.screenshot(path=str(service["path"] / "sidebar-last.png"))
            print("Sidebar screenshots:", service["path"])
            browser.close()
