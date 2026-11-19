"""可选浏览器回归：QUploader 传输失败、取消及上传新文档后的索引检索。"""

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service
from app.i18n import t
from tree_browser_helpers import browse_mode


def choose_upload(page, files):
    page.locator(".workspace-toolbar").get_by_role("button", name=t("files.create_menu"), exact=True).click()
    with page.expect_file_chooser() as chooser:
        page.get_by_text(t("files.upload"), exact=True).click()
    chooser.value.set_files(files)


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

        payload = {"name": "upload-test.md", "mimeType": "text/markdown", "buffer": b"# upload test\nbody"}
        failure = page.get_by_text(t("files.upload_failed"), exact=True)
        try:
            page.goto(service["url"])
            browse_mode(page, "all")
            expect(page.get_by_text(t("files.pagination", page=1, pages=2, total=52), exact=True)).to_be_visible(timeout=30000)
            choose_upload(page, [])
            expect(page.get_by_role("button", name=t("files.upload_cancel"), exact=True)).to_have_count(0)
            choose_upload(page, [payload])
            expect(failure).to_be_visible()
            expect(page.get_by_role("button", name=t("files.upload_cancel"), exact=True)).to_have_count(0)
            # 关闭永久通知，稍后的失败断言必须看到新通知。
            page.locator(".q-notification").filter(has=failure).get_by_role("button").click()
            expect(failure).to_have_count(0)
            hold = True
            choose_upload(page, [payload, {**payload, "name": "second.md"}])
            expect(page.get_by_text(t("files.uploading", count=2), exact=True)).to_be_visible()
            page.get_by_role("button", name=t("files.upload_cancel"), exact=True).click()
            expect(page.get_by_text(t("files.upload_cancelled"), exact=True)).to_be_visible()
            expect(page.get_by_role("button", name=t("files.upload_cancel"), exact=True)).to_have_count(0)
            for route in pending:
                route.abort()
            pending.clear()
            hold = False
            choose_upload(page, [payload])
            expect(failure).to_be_visible()
            expect(page.get_by_role("button", name=t("files.upload_cancel"), exact=True)).to_have_count(0)
            assert not errors, errors
            assert service["cli"]("file", "list", "--name", "upload-test").data["data"]["total"] == 0

            # 不拦截真实上传：命中已有原件，验证服务端受理后的真实通知，不调用外部解析服务。
            page.unroute("**/_nicegui/client/*/upload/*", intercept)
            choose_upload(page, [str(service["original"])])
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


def test_upload_new_markdown_indexes_and_searches_in_browser(gui_service):
    service = gui_service
    expect = playwright.expect
    filename = "browser-fresh-upload.md"
    query = "蓝铜罗盘"
    body = f"{query}记录了潮汐校准步骤。"
    payload = {"name": filename, "mimeType": "text/markdown", "buffer": f"# 航海观测记录\n\n{body}\n".encode("utf-8")}
    before = service["cli"]("file", "list", "--name", filename)
    assert before.code == 0, before.stdout
    assert before.data["data"]["total"] == 0, before.stdout

    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(http_credentials={"username": "piece", "password": service["key"]})
        page = context.new_page()
        try:
            page.goto(service["url"])
            browse_mode(page, "all")
            row = page.locator(".library-row").filter(has=page.get_by_text(filename, exact=True))
            choose_upload(page, [payload])
            expect(page.get_by_text(t("files.upload_processing", filename=filename), exact=True)).to_be_visible(timeout=30000)
            expect(row).to_be_visible(timeout=60000)
            expect(row.locator(".library-col-status")).to_have_text(t("file_status.indexed"), timeout=60000)
            file_id = int(row.get_attribute("data-document-id"))

            found = service["cli"]("search", query)
            assert found.code == 0, found.stdout
            hits = [candidate for candidate in found.data["data"]["candidates"] if candidate["file_id"] == file_id]
            assert hits, found.stdout
            chunk = service["cli"]("chunk", "get", str(hits[0]["chunk_id"]))
            assert chunk.code == 0, chunk.stdout
            assert body in chunk.data["data"]["chunk_text"], chunk.stdout
        finally:
            context.close()
            browser.close()
