"""在实际编辑器验证 Unicode 选区、格式按钮与草稿同步。"""
import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401
from app.i18n import t


def test_markdown_toolbar_keeps_selection_and_live_draft(gui_service):
    service = gui_service
    expect = playwright.expect
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960},
                                http_credentials={"username": "piece", "password": service["key"]})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(service["url"])
            expect(page.locator(".resource-collection-row").first).to_be_visible(timeout=30000)
            page.locator(".workspace-primary").get_by_role("button", name="Wiki", exact=True).click()
            page.get_by_role("button", name=t("knowledge.create"), exact=True).click()
            editor = page.locator(".wiki-editor")
            body = editor.get_by_label(t("knowledge.body"), exact=True)
            toolbar = editor.locator(".markdown-toolbar")
            body.fill("🙂 目标\n第二行")
            body.evaluate("el => el.setSelectionRange(3, 5)")
            toolbar.get_by_role("button", name=t("knowledge.md_link"), exact=True).click()
            expect(body).to_have_value("🙂 [目标](url)\n第二行")
            expect(body).to_be_focused()
            assert body.evaluate("el => el.value.slice(el.selectionStart, el.selectionEnd)") == "url"

            # 选区止于换行时，不给下一行添加前缀。
            body.fill("第一行\n第二行")
            body.evaluate("el => el.setSelectionRange(0, 4)")
            toolbar.get_by_role("button", name=t("knowledge.md_list"), exact=True).click()
            expect(body).to_have_value("- 第一行\n第二行")
            toolbar.get_by_role("button", name=t("knowledge.md_list"), exact=True).click()
            expect(body).to_have_value("第一行\n第二行")

            body.fill("输入中")
            body.evaluate("el => { el.dataset.composing = 'true'; }")
            toolbar.get_by_role("button", name=t("knowledge.md_heading"), exact=True).click()
            expect(body).to_have_value("输入中")
            body.evaluate("el => { delete el.dataset.composing; el.setSelectionRange(0, 0); }")
            toolbar.get_by_role("button", name=t("knowledge.md_heading"), exact=True).click()
            expect(body).to_have_value("# 输入中")

            # 切换预览重建工作区，验证变换已通过原输入事件同步到草稿。
            editor.get_by_role("button", name=t("wiki.preview"), exact=True).click()
            expect(editor.locator(".knowledge-prose h1")).to_have_text("输入中")
            editor.get_by_role("button", name=t("wiki.edit_source"), exact=True).click()
            expect(body).to_have_value("# 输入中")
            page.screenshot(path=str(service["path"] / "markdown-toolbar.png"))
            assert not errors, errors
        finally:
            browser.close()
