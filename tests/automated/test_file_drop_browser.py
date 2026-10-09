"""可选 Edge 回归：真实 DataTransfer 落点、上传通道、取消与阅读上下文。"""

import base64

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from app.i18n import t
from test_collection_browser import gui_service
from tree_browser_helpers import browse_mode, collection_row, expand_collection


def _transfer(page, files):
    return page.evaluate_handle("""files => {
        const transfer = new DataTransfer();
        for (const file of files) {
            const bytes = Uint8Array.from(atob(file.body), char => char.charCodeAt(0));
            transfer.items.add(new File([bytes], file.name, {type: file.type}));
        }
        return transfer;
    }""", [{"name": name, "type": mime, "body": base64.b64encode(body).decode("ascii")}
           for name, mime, body in files])


def _ready(page):
    page.wait_for_function("""() => {
        const input = document.querySelector('[data-workspace-upload]');
        return input && !input._pieceUploadBusy;
    }""")


def _memberships(service):
    # 测试库只有研究方法.pdf 有归属；通过运行中的服务读取，不在测试进程重开数据库。
    result = service["cli"]("collection", "list")
    assert result.code == 0, result.stdout
    return {item["id"] for item in result.data["data"]["collections"] if item["direct_file_count"]}


def _drop(page, target, files, *, label=None, screenshot=None):
    _ready(page)
    transfer = _transfer(page, files)
    try:
        target.dispatch_event("dragenter", {"dataTransfer": transfer})
        target.dispatch_event("dragover", {"dataTransfer": transfer})
        if label:
            playwright.expect(page.locator(".file-drop-hint")).to_have_text(label)
        if screenshot:
            page.screenshot(path=str(screenshot))
        target.dispatch_event("drop", {"dataTransfer": transfer})
        playwright.expect(page.locator(".file-drop-hint")).not_to_be_visible()
    finally:
        transfer.dispose()


def test_drop_targets_reuse_documents_and_keep_reading_context(gui_service):
    service = gui_service
    expect = playwright.expect
    body = service["original"].read_bytes()
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 960},
                                      http_credentials={"username": "piece", "password": service["key"]})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(service["url"])
            expect(collection_row(page, service["root"])).to_be_visible(timeout=30000)
            expand_collection(page, service["root"])
            expand_collection(page, service["rag"])
            page.locator(".library-row").filter(has_text="研究方法.pdf").dblclick(delay=100)
            page.locator(".reader-toolbar").get_by_role("button", name=t("library.view_compare"), exact=True).click()
            source = page.locator(".source-pane")
            expect(source.locator("img")).to_be_visible(timeout=30000)
            source.get_by_role("button", name=t("library.next_source_page"), exact=True).click()
            source_page = source.get_by_role("textbox", name=t("library.source_page_number"), exact=True)
            expect(source_page).to_have_value("2")
            width = page.locator(".results-splitter > .q-splitter__before").bounding_box()["width"]

            # 实际传输两份文件，内容查重只复用已有文档，不触发外部解析或嵌入。
            catalog = page.locator('[data-catalog="files"]')
            _drop(page, collection_row(page, service["rag"]), [("first.pdf", "application/pdf", body), ("second.pdf", "application/pdf", body)],
                  label=t("files.drop_target", count=2, name="人工智能 › 检索增强"),
                  screenshot=service["path"] / "file-drop-hover.png")
            expect(page.get_by_text(t("files.upload_exists", filename="first.pdf"), exact=True)).to_be_visible(timeout=30000)
            expect(page.get_by_text(t("files.upload_exists", filename="second.pdf"), exact=True)).to_be_visible()
            _ready(page)
            expect(page.locator(".library-reader-title")).to_have_text("研究方法.pdf")
            expect(source_page).to_have_value("2")

            # 拖到未选中的资料集，只增加直接归属，不切换当前列表或阅读内容。
            other = collection_row(page, service["other"])
            _drop(page, other, [("another.pdf", "application/pdf", body)],
                  label=t("files.drop_target", count=1, name="其他资料"))
            expect(other.locator(".collection-tree-count")).to_have_text("1", timeout=30000)
            _ready(page)
            assert _memberships(service) == {
                service["rag"], service["graph"], service["other"],
            }
            expect(catalog).to_have_attribute("data-import-collection", "all")
            expect(page.locator(".library-reader-title")).to_have_text("研究方法.pdf")
            expect(source_page).to_have_value("2")
            assert page.locator(".results-splitter > .q-splitter__before").bounding_box()["width"] == width

            # 全部文档是未指定归类的入口，重复文档的既有归属不会被清空。
            all_documents = page.locator('.workspace-primary [data-import-collection="all"]')
            _drop(page, all_documents, [("root.pdf", "application/pdf", body)],
                  label=t("files.drop_target", count=1, name=t("files.drop_library")))
            expect(page.get_by_text(t("files.upload_exists", filename="root.pdf"), exact=True)).to_be_visible(timeout=30000)
            _ready(page)
            assert len(_memberships(service)) == 3

            # 混合格式：逐文件说明拒绝原因，其余文档继续导入。
            _drop(page, catalog, [("invalid.exe", "application/octet-stream", b"unsupported"),
                                   ("valid.pdf", "application/pdf", body)])
            expect(page.get_by_text(t("files.upload_file_failed", filename="invalid.exe",
                                     error=t("files.reject_format")), exact=True)).to_be_visible()
            expect(page.get_by_text(t("files.upload_exists", filename="valid.pdf"), exact=True)).to_be_visible(timeout=30000)
            _ready(page)

            # Wiki 中可经全局资料库入口导入，不跳回资料库或关闭当前工作区。
            page.get_by_role("button", name=t("sidebar.wiki"), exact=True).click()
            _drop(page, page.locator('.workspace-primary [data-import-collection="all"]'), [("from-wiki.pdf", "application/pdf", body)])
            expect(page.get_by_text(t("files.upload_exists", filename="from-wiki.pdf"), exact=True)).to_be_visible(timeout=30000)
            expect(page.locator(".workspace-primary [aria-current=page]")).to_have_attribute("aria-label", t("sidebar.wiki"))
            _ready(page)
            assert not errors, errors
        finally:
            page.screenshot(path=str(service["path"] / "file-drop-context.png"))
            context.close()
            browser.close()


def test_drop_transfer_cancel_and_non_file_drags(gui_service):
    service = gui_service
    expect = playwright.expect
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(http_credentials={"username": "piece", "password": service["key"]})
        page = context.new_page()
        errors, pending = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        hold = True

        def intercept(route):
            if hold:
                pending.append(route)
            else:
                route.fulfill(status=503, body="test transfer failure")

        page.route("**/_nicegui/client/*/upload/*", intercept)
        try:
            page.goto(service["url"])
            browse_mode(page, "all")
            expect(page.get_by_text(t("files.pagination", page=1, pages=2, total=52), exact=True)).to_be_visible(timeout=30000)
            catalog = page.locator('[data-catalog="files"]')
            body = service["original"].read_bytes()
            original_input = page.locator("[data-workspace-upload]").get_attribute("id")
            _drop(page, catalog, [("held.pdf", "application/pdf", body)])
            expect(page.get_by_text(t("files.uploading", count=1), exact=True)).to_be_visible()
            page.get_by_role("button", name=t("sidebar.wiki"), exact=True).click()
            expect(page.locator("[data-workspace-upload]")).to_have_attribute("id", original_input)
            busy_drop = _transfer(page, [("not-added.pdf", "application/pdf", body)])
            busy_target = page.locator('.workspace-primary [data-import-collection="all"]')
            busy_target.dispatch_event("dragover", {"dataTransfer": busy_drop})
            expect(page.locator(".file-drop-hint")).to_have_text(t("files.upload_busy"))
            busy_target.dispatch_event("drop", {"dataTransfer": busy_drop})
            busy_drop.dispose()
            expect(page.get_by_role("alert").filter(has_text=t("files.upload_busy"))).to_be_visible()
            expect(page.locator(".task-activity-badge")).to_have_text("1")
            page.get_by_role("button", name=t("task_activity.open"), exact=True).click()
            menu = page.locator(".task-activity-menu")
            expect(menu.get_by_text("held.pdf", exact=True)).to_be_visible()
            menu.get_by_role("button", name=t("files.upload_cancel"), exact=True).click()
            expect(page.get_by_text(t("files.upload_cancelled"), exact=True)).to_be_visible()
            page.keyboard.press("Escape")
            _ready(page)
            assert page.locator("[data-workspace-upload]").get_attribute("id") != original_input
            for route in pending:
                route.abort()
            pending.clear()

            # 取消后可再次拖入；网络失败有持久反馈，也能重新接收后续文件。
            hold = False
            target = page.locator('.workspace-primary [data-import-collection="all"]')
            _drop(page, target, [("failed.pdf", "application/pdf", body)])
            expect(page.get_by_text(t("files.upload_failed"), exact=True)).to_be_visible()
            _ready(page)

            # 普通文本拖动不被文件处理器拦截；外部文件落在非目标区不会打开或上传。
            default_prevented = page.evaluate("""() => {
                const text = new DataTransfer();
                text.setData('text/plain', 'editable text');
                const file = new DataTransfer();
                file.items.add(new File(['content'], 'outside.md', {type: 'text/markdown'}));
                return [text, file].map(dataTransfer => {
                    const event = new DragEvent('drop', {dataTransfer, bubbles: true, cancelable: true});
                    document.body.dispatchEvent(event);
                    return event.defaultPrevented;
                });
            }""")
            assert default_prevented == [False, True]
            expect(page).to_have_url(service["url"] + "/")

            # 文件夹只给出拒绝说明；退出拖动或 Escape 不遗留高亮。
            transfer = page.evaluate_handle("""() => {
                const data = new DataTransfer();
                data.items.add(new File([''], 'folder'));
                Object.defineProperty(data, 'items', {value: [{
                    kind: 'file', webkitGetAsEntry: () => ({isDirectory: true, name: 'folder'})
                }]});
                return data;
            }""")
            target.dispatch_event("dragover", {"dataTransfer": transfer})
            expect(page.locator(".file-drop-highlight")).to_have_count(1)
            page.keyboard.press("Escape")
            expect(page.locator(".file-drop-highlight")).to_have_count(0)
            target.dispatch_event("drop", {"dataTransfer": transfer})
            transfer.dispose()
            expect(page.get_by_text(t("files.upload_file_failed", filename="folder",
                                     error=t("files.reject_directory")), exact=True)).to_be_visible()
            _ready(page)
            assert not errors, errors
        finally:
            for route in pending:
                route.abort()
            page.screenshot(path=str(service["path"] / "file-drop-cancel.png"))
            context.close()
            browser.close()
