"""实机验收：编辑器草稿状态标签与 Ctrl/Cmd+S 快捷键（本机 Edge，无外部请求）。

uv run --no-sync --with playwright pytest -q -s tests/automated/test_wiki_editor_browser.py
"""

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401
from app.i18n import t


def k(name):
    return t("knowledge." + name)


def test_wiki_editor_shortcut_and_status(gui_service):
    service = gui_service
    expect = playwright.expect
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960},
                                http_credentials={"username": "piece", "password": service["key"]})
        errors, downloads = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("download", lambda item: downloads.append(item))
        try:
            page.goto(service["url"])
            expect(page.locator(".resource-collection-row").first).to_be_visible(timeout=30000)
            page.locator(".workspace-primary").get_by_role("button", name=t("sidebar.wiki"), exact=True).click()
            page.get_by_role("button", name=k("create"), exact=True).click()
            editor = page.locator(".wiki-editor")
            body = editor.get_by_label(k("body"), exact=True)
            status = editor.locator(".editor-draft-status")

            # 新建页无改动：状态标签为 unchanged。
            expect(status).to_have_attribute("data-state", "unchanged")

            # 打字触发状态更新（服务端往返），但不得重建输入控件，焦点与光标原位保留。
            body.click()
            body.type("草稿正文")
            expect(status).to_have_attribute("data-state", "unsaved")
            expect(body).to_be_focused()
            assert body.evaluate("el => el.selectionStart") == len("草稿正文")
            assert body.evaluate("el => el.selectionEnd") == len("草稿正文")

            editor.get_by_label(k("title_field"), exact=True).fill("快捷键页面")
            editor.get_by_label(k("reason"), exact=True).fill("浏览器快捷键")

            # Ctrl+S 在编辑器内保存：不触发浏览器“保存网页”，编辑器关闭后页面进入列表。
            body.click()
            body.press("Control+s")
            expect(editor).not_to_be_visible()
            expect(page.get_by_role("button", name="快捷键页面", exact=True).first).to_be_visible(timeout=15000)
            assert downloads == []
            assert not errors, errors
        finally:
            browser.close()
