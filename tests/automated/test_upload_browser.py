"""可选 Edge 回归：真实 QUploader 传输失败、取消及再次选择文件。"""

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service
from app.i18n import t
from tree_browser_helpers import browse_mode


def test_upload_transfer_failure_and_cancel_in_browser(gui_service):
    service = gui_service
    expect = playwright.expect
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(http_credentials={"username": "piece", "password": service["key"]})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        pending = []
        hold = False

        def intercept(route):
            if hold:
                pending.append(route)
            else:
                route.fulfill(status=503, body="test transfer failure")

        page.route("**/_nicegui/client/*/upload/*", intercept)

        def choose(files):
            page.locator('button:has(i:text-is("add"))').first.click()
            with page.expect_file_chooser() as chooser:
                page.get_by_text(t("files.upload"), exact=True).click()
            chooser.value.set_files(files)

        payload = {"name": "upload-test.md", "mimeType": "text/markdown", "buffer": b"# upload test\nbody"}
        try:
            page.goto(service["url"])
            browse_mode(page, "all")
            expect(page.get_by_text(t("files.pagination", page=1, pages=2, total=52), exact=True)).to_be_visible(timeout=30000)
            choose([])
            expect(page.get_by_role("button", name=t("files.upload_cancel"), exact=True)).to_have_count(0)
            choose([payload])
            expect(page.get_by_text(t("files.upload_failed"), exact=True)).to_be_visible()
            expect(page.get_by_role("button", name=t("files.upload_cancel"), exact=True)).to_have_count(0)
            hold = True
            choose([payload, {**payload, "name": "second.md"}])
            expect(page.get_by_text(t("files.uploading", count=2), exact=True)).to_be_visible()
            page.get_by_role("button", name=t("files.upload_cancel"), exact=True).click()
            expect(page.get_by_text(t("files.upload_cancelled"), exact=True)).to_be_visible()
            expect(page.get_by_role("button", name=t("files.upload_cancel"), exact=True)).to_have_count(0)
            for route in pending:
                route.abort()
            pending.clear()
            hold = False
            choose([payload])
            expect(page.get_by_text(t("files.upload_failed"), exact=True).first).to_be_visible()
            expect(page.get_by_role("button", name=t("files.upload_cancel"), exact=True)).to_have_count(0)
            assert not errors, errors
            assert service["cli"]("file", "list", "--name", "upload-test").data["data"]["total"] == 0

            # 不拦截真实上传：命中已有原件，验证服务端受理后的真实通知，不调用外部解析服务。
            page.unroute("**/_nicegui/client/*/upload/*", intercept)
            choose([str(service["original"])])
            expect(page.get_by_text(t("files.upload_exists", filename=service["original"].name), exact=True)).to_be_visible(timeout=30000)
            expect(page.get_by_role("button", name=t("files.upload_cancel"), exact=True)).to_have_count(0)
            expect(page.get_by_text(t("files.pagination", page=1, pages=2, total=52), exact=True)).to_be_visible()
            assert not errors, errors
        finally:
            for route in pending:
                route.abort()
            page.screenshot(path=str(service["path"] / "upload-lifecycle.png"))
            context.close()
            browser.close()
