"""统一浏览树的真实 Edge 验收：行内操作、归属拖拽、阅读/对照不被卸载。"""

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from app.i18n import t
from test_collection_browser import gui_service
from test_file_drop_browser import _drop, _ready
from tree_browser_helpers import browse_mode, collection_row, document_row, expand_collection


def _reader(page, service):
    expect = playwright.expect
    expand_collection(page, service["root"])
    expand_collection(page, service["rag"])
    document_row(page, service["file_id"], collection_id=service["rag"]).dblclick(delay=100)
    page.locator(".reader-toolbar").get_by_role("button", name=t("library.view_compare"), exact=True).click()
    source = page.locator(".source-pane")
    expect(source.locator("img")).to_be_visible(timeout=30000)
    source.get_by_role("button", name=t("library.next_source_page"), exact=True).click()
    expect(source.get_by_role("textbox", name=t("library.source_page_number"), exact=True)).to_have_value("2")
    return source


def test_resource_tree_inline_collection_actions(gui_service):
    service = gui_service
    expect = playwright.expect
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 960},
                                      http_credentials={"username": "piece", "password": service["key"]})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(service["url"])
            expect(page.get_by_role("tree", name=t("files.tree_view"), exact=True)).to_be_visible(timeout=30000)
            expect(page.locator(".workspace-sidebar [data-collection-id]")).to_have_count(0)
            source = _reader(page, service)
            rag = collection_row(page, service["rag"])
            # 重命名就在原来的资料集行中；重复名称保留输入，不关掉编辑器。
            rag.focus()
            rag.press("F2")
            editor = page.locator("[data-collection-editor]")
            name = editor.get_by_role("textbox", name=t("collections.name"), exact=True)
            name.fill("知识图谱")
            name.press("Enter")
            expect(editor.locator('[role="alert"]')).to_contain_text("已存在")
            expect(name).to_have_value("知识图谱")
            name.fill("研究方法资料")
            name.press("Enter")
            expect(editor).to_have_count(0)
            expect(rag).to_have_attribute("aria-label", "研究方法资料")
            expect(page.locator(".library-reader-title")).to_have_text("研究方法.pdf")
            expect(source.get_by_role("textbox", name=t("library.source_page_number"), exact=True)).to_have_value("2")

            rag.click(button="right")
            page.get_by_role("menuitem", name=t("collections.create_child"), exact=True).click()
            name.fill("就地创建的子资料集")
            name.press("Enter")
            expect(editor).to_have_count(0)
            result = service["cli"]("collection", "list")
            child = next(item for item in result.data["data"]["collections"] if item["name"] == "就地创建的子资料集")
            assert child["parent_id"] == service["rag"]
            child_row = collection_row(page, child["id"])
            expect(child_row).to_have_attribute("data-parent-row", f'c:{service["rag"]}')

            # 非叶删除仍受服务端契约约束；叶子删除使用明确影响确认。
            rag.click(button="right")
            expect(page.get_by_role("menuitem", name=t("collections.delete"), exact=True)).to_be_disabled()
            page.keyboard.press("Escape")
            child_row.click(button="right")
            page.get_by_role("menuitem", name=t("collections.delete"), exact=True).click()
            dialog = page.get_by_role("dialog")
            expect(dialog).to_contain_text("不会删除文档或原件")
            dialog.get_by_role("button", name=t("confirm_dialog.btn_delete"), exact=True).click()
            expect(child_row).to_have_count(0)
            expect(page.locator(".library-reader-title")).to_have_text("研究方法.pdf")
            expect(source.get_by_role("textbox", name=t("library.source_page_number"), exact=True)).to_have_value("2")
            page.screenshot(path=str(service["path"] / "resource-tree-light.png"))
            page.evaluate("Quasar.Dark.set(true)")
            page.screenshot(path=str(service["path"] / "resource-tree-dark.png"))
            assert not errors, errors
        finally:
            page.screenshot(path=str(service["path"] / "resource-tree-last.png"))
            print("Resource tree screenshots:", service["path"])
            context.close()
            browser.close()


def test_resource_tree_drag_memberships_import_and_restore(gui_service):
    service = gui_service
    expect = playwright.expect
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
            source = _reader(page, service)
            original = document_row(page, service["file_id"], collection_id=service["rag"])
            loose = page.locator('.resource-file-row[data-parent-row=""]').first
            loose_id = int(loose.get_attribute("data-document-id"))
            loose.click(modifiers=["Control"])
            expect(original).to_have_attribute("aria-selected", "true")
            expect(loose).to_have_attribute("aria-selected", "true")
            other = collection_row(page, service["other"])
            original.drag_to(other)
            expect(other.locator(".collection-tree-count")).to_have_text("2")
            expect(page.locator(".library-reader-title")).to_have_text("研究方法.pdf")
            expect(source.get_by_role("textbox", name=t("library.source_page_number"), exact=True)).to_have_value("2")
            expand_collection(page, service["other"])
            alias = document_row(page, service["file_id"], collection_id=service["other"])
            expect(alias).to_be_visible()
            expect(document_row(page, loose_id, collection_id=service["other"])).to_be_visible()
            expect(original).to_be_visible()

            alias.click(button="right")
            page.get_by_role("menuitem", name=t("collections.remove_here"), exact=True).click()
            expect(alias).to_have_count(0)
            expect(original).to_be_visible()
            expect(other.locator(".collection-tree-count")).to_have_text("1")

            # 外部拖入复用原件查重；不会复制文档，也不会替换原有归属。
            _drop(page, other, [("source-copy.pdf", "application/pdf", service["original"].read_bytes())],
                  label=t("files.drop_target", count=1, name="其他资料"))
            expect(page.get_by_text(t("files.upload_exists", filename="source-copy.pdf"), exact=True)).to_be_visible(timeout=30000)
            _ready(page)
            expect(alias).to_be_visible()
            expect(original).to_be_visible()
            browse_mode(page, "all")
            expect(page.get_by_text(t("files.pagination", page=1, pages=2, total=52), exact=True)).to_be_visible()
            browse_mode(page, "uncategorized")
            expect(page.get_by_text(t("files.pagination", page=1, pages=1, total=50), exact=True)).to_be_visible()
            browse_mode(page, "tree")
            expect(alias).to_be_visible()
            expect(source.get_by_role("textbox", name=t("library.source_page_number"), exact=True)).to_have_value("2")
            page.wait_for_function("""() => Object.keys(localStorage).some(key =>
                key.startsWith('piece.workspace.v1.') && JSON.parse(localStorage[key]).files.library_mode === 'tree')""")
            page.reload()
            expect(alias).to_be_visible(timeout=30000)
            expect(page.locator(".library-reader-title")).to_have_text("研究方法.pdf")
            expect(page.locator(".source-pane img")).to_be_visible()
            assert not errors, errors
        finally:
            page.screenshot(path=str(service["path"] / "resource-tree-drag.png"))
            context.close()
            browser.close()
