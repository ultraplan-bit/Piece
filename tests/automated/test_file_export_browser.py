"""用真实 Edge 下载验证文档栏导出，并检查目录切换、选中样式和窄栏布局。"""

from pathlib import Path
from zipfile import ZipFile

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from app.i18n import t
from test_collection_browser import gui_service  # noqa: F401
from tree_browser_helpers import browse_mode, collection_row, document_row, expand_collection


def test_catalog_export_downloads_and_tree_controls(knowledge_base, request):
    from indexing.services import file_service

    documents = [file_service.create_empty_file(f"导出样例{name}") for name in ("甲", "乙")]
    for document in documents:
        Path(document["file_path"]).write_text(f'# {document["filename"]}\n\n导出正文。', encoding="utf-8")
    service = request.getfixturevalue("gui_service")
    expect = playwright.expect
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, accept_downloads=True,
                                http_credentials={"username": "piece", "password": service["key"]})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(service["url"])
            catalog = page.locator(".library-catalog")
            export = catalog.get_by_role("button", name=t("files.export"), exact=True)
            expect(export).to_be_disabled(timeout=30000)
            browse_mode(page, "all")
            search = catalog.get_by_role("textbox", name=t("files.search"), exact=True)
            search.fill("导出样例")
            first = document_row(page, documents[0]["file_id"])
            second = document_row(page, documents[1]["file_id"])
            first.click()
            expect(export).to_be_enabled()
            for dark in (False, True):
                page.evaluate("dark => Quasar.Dark.set(dark)", dark)
                expect(first).to_have_css("box-shadow", "none")
                expect(first).to_have_css("background-color", "rgb(52, 56, 63)" if dark else "rgb(233, 236, 239)")
                icon = first.locator(".library-title-cell > .q-icon")
                muted_color = first.locator(".library-col-date").evaluate("el => getComputedStyle(el).color")
                expect(icon).to_have_css("color", muted_color)
            page.evaluate("Quasar.Dark.set(false)")
            export.click()
            expect(page.get_by_role("menuitem", name=t("files.export_original"), exact=True)).to_be_disabled()
            with page.expect_download() as single:
                page.get_by_role("menuitem", name=t("files.export_working"), exact=True).click()
            download = single.value
            assert download.suggested_filename == documents[0]["filename"]
            assert Path(download.path()).read_text(encoding="utf-8") == Path(documents[0]["file_path"]).read_text(encoding="utf-8")

            second.click(modifiers=["Control"])
            expect(catalog).to_have_attribute("data-multiselect", "true")
            expect(catalog).to_contain_text(t("workspace.selected", count=2))
            export.click()
            with page.expect_download() as batch:
                page.get_by_role("menuitem", name=t("files.export_working"), exact=True).click()
            assert batch.value.suggested_filename == "piece-markdown.zip"
            with ZipFile(batch.value.path()) as archive:
                assert set(archive.namelist()) == {f'{document["file_id"]}-{document["filename"]}' for document in documents}
                assert all("导出正文" in archive.read(name).decode("utf-8") for name in archive.namelist())
            page.mouse.move(1400, 20)
            expect(page.locator(".q-tooltip")).to_have_count(0)
            catalog.screenshot(path=str(service["path"] / "catalog-batch-export.png"))

            # 名称筛选隐藏的选择仍参与导出；没有原件的笔记会明确提示跳过。
            search.fill("研究方法")
            original_row = document_row(page, service["file_id"])
            original_row.click()
            expect(catalog).to_contain_text(t("workspace.selected_cross_page", count=3))
            export.click()
            with page.expect_download() as originals:
                page.get_by_role("menuitem", name=t("files.export_original"), exact=True).click()
            expect(page.get_by_text(t("files.export_originals_skipped", count=2), exact=True)).to_be_visible()
            with ZipFile(originals.value.path()) as archive:
                assert archive.namelist() == [f'{service["file_id"]}-{service["original"].name}']
                assert archive.read(archive.namelist()[0]) == service["original"].read_bytes()
            expect(catalog).to_contain_text(t("workspace.selected_cross_page", count=3))
            catalog.get_by_role("button", name=t("files.batch_cancel"), exact=True).click()
            original_row.click()
            page.locator(".reader-toolbar").get_by_role("button", name=t("workspace.more"), exact=True).click()
            expect(page.get_by_role("menuitem", name=t("files.export_working"), exact=True)).to_have_count(0)
            expect(page.get_by_role("menuitem", name=t("files.export_original"), exact=True)).to_have_count(0)
            page.keyboard.press("Escape")

            browse_mode(page, "tree")
            expand_collection(page, service["root"])
            expand_collection(page, service["rag"])
            original_row = document_row(page, service["file_id"], collection_id=service["rag"])
            original_row.click()
            original_row.click(button="right")
            with page.expect_download() as original:
                page.get_by_role("menuitem", name=t("files.export_original"), exact=True).click()
            assert Path(original.value.path()).read_bytes() == service["original"].read_bytes()
            page.mouse.move(1400, 20)
            expect(page.locator(".q-tooltip")).to_have_count(0)
            for dark in (False, True):
                page.evaluate("dark => Quasar.Dark.set(dark)", dark)
                expect(original_row).to_have_css("box-shadow", "none")
                catalog.screenshot(path=str(service["path"] / f"catalog-tree-{'dark' if dark else 'light'}.png"))
            page.evaluate("Quasar.Dark.set(false)")

            before = page.locator(".results-splitter > .q-splitter__before")
            separator = page.locator(".results-splitter > .q-splitter__separator").bounding_box()
            x, y = separator["x"] + separator["width"] / 2, separator["y"] + 100
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x + 220 - before.bounding_box()["width"], y, steps=5)
            page.mouse.up()
            expect(before).to_have_css("width", "220px")
            assert catalog.evaluate("""el => {
                const box = el.getBoundingClientRect();
                return [...el.querySelectorAll('.catalog-heading button, .library-scope button')].every(button => {
                    const rect = button.getBoundingClientRect();
                    return !rect.width || rect.left >= box.left && rect.right <= box.right + 1;
                });
            }""")
            assert catalog.locator(".library-view-button").evaluate_all("""buttons => buttons.every(button => {
                const label = button.querySelector('.block');
                return label.scrollWidth <= label.clientWidth + 1;
            })""")
            page.mouse.move(1400, 20)
            catalog.screenshot(path=str(service["path"] / "catalog-tree-narrow.png"))
            catalog.get_by_role("button", name=t("files.collapse_all"), exact=True).click()
            expect(collection_row(page, service["root"])).to_have_attribute("aria-expanded", "false")
            expect(collection_row(page, service["rag"])).to_have_count(0)
            expect(page.locator(".library-reader-title")).to_have_text("研究方法.pdf")
            assert not errors, errors
        finally:
            page.screenshot(path=str(service["path"] / "catalog-export-last.png"))
            print("Catalog export screenshots:", service["path"])
            browser.close()
