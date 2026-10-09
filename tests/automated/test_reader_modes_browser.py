"""用隔离资料库和真实 Edge 验证正文形态与原页对照的独立切换。"""
from urllib.parse import urlsplit

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401
from app.i18n import t
from tree_browser_helpers import browse_mode, collection_row, document_row, expand_collection


def test_reader_modes_and_source_comparison_are_independent(gui_service):
    service = gui_service
    expect = playwright.expect
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 960},
                                      http_credentials={"username": "piece", "password": service["key"]})
        context.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).hostname in
                      {"127.0.0.1", "localhost"} else route.abort())
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(service["url"])
            expect(collection_row(page, service["root"])).to_be_visible(timeout=30000)
            toolbar = page.locator(".reader-toolbar")
            modes = toolbar.get_by_role("group", name=t("library.view_mode"), exact=True)
            text = modes.get_by_role("button", name=t("library.mode_reading"), exact=True)
            cards = modes.get_by_role("button", name=t("library.mode_cards"), exact=True)
            compare = toolbar.get_by_role("button", name=t("library.view_compare"), exact=True)
            source = page.locator(".source-pane")
            expect(text).to_be_disabled()
            expect(cards).to_be_disabled()
            expect(compare).to_be_disabled()

            expand_collection(page, service["root"])
            expand_collection(page, service["rag"])
            document_row(page, service["file_id"], collection_id=service["rag"]).dblclick(delay=100)
            expect(page.locator(".library-reading-block")).to_be_visible()
            expect(text).to_have_attribute("aria-pressed", "true")
            expect(cards).to_have_attribute("aria-pressed", "false")
            expect(compare).to_be_enabled()

            # 键盘可切到卡片，原页对照不会顺带切回正文。
            cards.focus()
            cards.press("Enter")
            expect(page.locator(".library-cards .chunk-sheet")).to_be_visible()
            expect(cards).to_have_attribute("aria-pressed", "true")
            expect(text).to_have_attribute("aria-pressed", "false")
            compare.click()
            expect(source.locator("img")).to_be_visible(timeout=30000)
            expect(compare).to_have_attribute("aria-pressed", "true")
            expect(page.locator(".library-cards .chunk-sheet")).to_be_visible()
            source.get_by_role("button", name=t("library.next_source_page"), exact=True).click()
            page_number = source.get_by_role("textbox", name=t("library.source_page_number"), exact=True)
            expect(page_number).to_have_value("2")

            # 双向切换正文形态，保留已打开原页的位置与控件。
            for button, content in ((text, ".library-reading-block"), (cards, ".library-cards .chunk-sheet")):
                button.click()
                expect(page.locator(content)).to_be_visible()
                expect(button).to_have_attribute("aria-pressed", "true")
                expect(compare).to_have_attribute("aria-pressed", "true")
                expect(page_number).to_have_value("2")

            # 顶栏开关与对照栏自身的关闭按钮都不改变卡片形态。
            compare.click()
            expect(source).to_have_count(0)
            expect(cards).to_have_attribute("aria-pressed", "true")
            expect(compare).to_have_attribute("aria-pressed", "false")
            compare.click()
            expect(source.locator("img")).to_be_visible(timeout=30000)
            source.get_by_role("button", name=t("chunks.source_page_close"), exact=True).click()
            expect(source).to_have_count(0)
            expect(cards).to_have_attribute("aria-pressed", "true")
            expect(compare).to_have_attribute("aria-pressed", "false")

            compare.click()
            expect(source.locator("img")).to_be_visible(timeout=30000)
            page.wait_for_function("""() => Object.keys(localStorage).some(key => {
                if (!key.startsWith('piece.workspace.v1.')) return false;
                const saved = JSON.parse(localStorage[key]).files;
                return saved.file_reading_mode === 'cards' && saved.library_view === 'compare';
            })""")
            page.reload()
            expect(source.locator("img")).to_be_visible(timeout=30000)
            expect(page.locator(".library-cards .chunk-sheet")).to_be_visible()
            expect(cards).to_have_attribute("aria-pressed", "true")
            expect(compare).to_have_attribute("aria-pressed", "true")
            for width in (1440, 850):
                page.set_viewport_size({"width": width, "height": 960})
                assert toolbar.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
                page.screenshot(path=str(service["path"] / f"reader-cards-compare-{width}.png"))
            page.set_viewport_size({"width": 1440, "height": 960})

            # 批量选择临时强制卡片；退出仍回到用户选择的正文，不关闭对照。
            text.click()
            expect(page.locator(".library-reading-block")).to_be_visible()
            toolbar.get_by_role("button", name=t("workspace.more"), exact=True).click()
            menu = page.locator(".q-menu")
            expect(menu).to_be_visible()
            expect(menu.get_by_text(t("library.mode_cards"), exact=True)).to_have_count(0)
            menu.get_by_text(t("workspace.select"), exact=True).click()
            expect(modes).to_have_count(0)
            expect(page.locator(".library-cards .chunk-sheet")).to_be_visible()
            expect(source.locator("img")).to_be_visible()
            toolbar.get_by_role("button", name=t("chunks.batch_cancel"), exact=True).click()
            expect(text).to_have_attribute("aria-pressed", "true")
            expect(page.locator(".library-reading-block")).to_be_visible()
            expect(compare).to_have_attribute("aria-pressed", "true")

            # 换到无原页文档只收起对照，不重置正文形态，并给出禁用原因。
            cards.click()
            expect(cards).to_have_attribute("aria-pressed", "true")
            browse_mode(page, "all")
            catalog = page.locator('[data-catalog="files"]')
            catalog.get_by_role("textbox", name=t("files.search"), exact=True).fill("分页样本 00")
            expect(catalog.locator(".library-row")).to_have_count(1)
            catalog.locator(".library-row").click()
            expect(page.locator(".library-reader-title")).to_contain_text("分页样本 00")
            expect(source).to_have_count(0)
            expect(cards).to_have_attribute("aria-pressed", "true")
            expect(compare).to_be_disabled()
            compare.locator("..").hover()
            expect(page.get_by_text(t("library.compare_unavailable"), exact=True)).to_be_visible()
            assert not errors, errors
        finally:
            page.screenshot(path=str(service["path"] / "reader-modes-last.png"))
            print("Reader mode screenshots:", service["path"])
            browser.close()
