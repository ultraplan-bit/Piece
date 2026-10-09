"""真实分栏下的长文件名、元数据与批量模式布局回归；使用临时库和本机 Edge。"""
import json

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401
from app.i18n import t
from tree_browser_helpers import collection_row


def test_file_list_long_titles_and_responsive_metadata(knowledge_base, request):
    from indexing import database
    from indexing.services import collection_service, file_service

    collection_id = collection_service.create_collection("列表验收")["collection_id"]
    long_collection = collection_service.create_collection("很长的集合名称用于验证列表截断" * 5,
                                                            parent_id=collection_id)["collection_id"]
    filenames = [
        "阳台菜园：专家手把手教你学种菜（方淑华）(z-library.sk, 1lib.sk, z-lib.sk).md",
        "ST-STM32F103C8.md",
        "LongUnbrokenDocumentFilename" * 12 + ".md",
    ]
    file_ids = []
    for index, filename in enumerate(filenames):
        file_id = file_service.create_empty_file(f"layout-{index}")["file_id"]
        file_ids.append(file_id)
        collection_service.set_file_collections(file_id, [long_collection if index == 2 else collection_id])
        metadata = {"author": "方淑华"} if index == 0 else ({"author": "LongAuthorName" * 12} if index == 2 else {})
        with database.get_db_cursor(write=True) as cursor:
            cursor.execute("UPDATE files SET filename=?, original_file_type=?, status='indexed', metadata=?, created_at=? WHERE id=?",
                           (filename, "epub" if index == 0 else "pdf", json.dumps(metadata),
                            f"2026-10-{8 - index:02} 12:34:56", file_id))

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
            group = collection_row(page, collection_id)
            expect(group).to_be_visible(timeout=30000)
            group.click()
            expect(group).to_have_attribute("aria-selected", "true")
            catalog = page.locator(".library-catalog")
            catalog.get_by_role("textbox", name=t("files.search"), exact=True).fill(".md")
            rows = catalog.locator("[data-catalog-row]")
            expect(rows).to_have_count(3)
            before = page.locator(".results-splitter > .q-splitter__before")
            first_row = catalog.locator(f'[data-document-id="{file_ids[0]}"]')
            first_row.click()
            expect(first_row).to_have_attribute("aria-pressed", "true")

            for batch in (False, True):
                if batch:
                    catalog.get_by_role("button", name="批量选择", exact=True).click()
                    expect(catalog).to_have_attribute("data-multiselect", "true")
                for width in (220, 320, 550, 640, 760):
                    separator = page.locator(".results-splitter > .q-splitter__separator").bounding_box()
                    current_width = before.bounding_box()["width"]
                    x, y = separator["x"] + separator["width"] / 2, separator["y"] + 100
                    page.mouse.move(x, y)
                    page.mouse.down()
                    page.mouse.move(x + width - current_width, y, steps=5)
                    page.mouse.up()
                    expect(before).to_have_css("width", f"{width}px")
                    narrow = width <= 640
                    header = catalog.locator(".library-table-header")
                    if narrow:
                        expect(header).to_be_hidden()
                    else:
                        expect(header).to_be_visible()

                    for file_id, filename in zip(file_ids, filenames):
                        row = catalog.locator(f'[data-document-id="{file_id}"]')
                        name = row.locator(".library-file-name")
                        expect(name).to_have_text(filename)
                        for column in ("type", "status", "date"):
                            expect(row.locator(f".library-col-{column}")).to_be_visible()
                        layout = row.evaluate("""(row, narrow) => {
                            const rect = el => {
                                const r = el.getBoundingClientRect();
                                return {left: r.left, right: r.right, top: r.top, bottom: r.bottom};
                            };
                            const bounds = rect(row);
                            const title = row.querySelector('.library-title-cell');
                            const titleBox = rect(title);
                            const name = row.querySelector('.library-file-name');
                            const style = getComputedStyle(name);
                            const cells = [...row.querySelector('.library-row-meta').children].map(rect);
                            const titleParts = [...title.querySelectorAll('.library-file-name, .truncate, .q-icon')].map(rect);
                            return {
                                bounds, titleBox, cells,
                                contained: [...titleParts, ...cells].every(r => r.left >= bounds.left && r.right <= bounds.right + 1),
                                titleContained: titleParts.every(r => r.left >= titleBox.left && r.right <= titleBox.right + 1),
                                separated: cells.every(r => narrow ? r.top >= titleBox.bottom : r.left >= titleBox.right),
                                metadataSeparated: cells.every((r, i) => cells.slice(i + 1).every(other =>
                                    r.right <= other.left || other.right <= r.left || r.bottom <= other.top || other.bottom <= r.top)),
                                clipped: style.overflow === 'hidden' && (narrow
                                    ? style.webkitLineClamp === '2' && name.clientHeight <= parseFloat(style.lineHeight) * 2 + 1
                                    : style.whiteSpace === 'nowrap' && style.textOverflow === 'ellipsis'),
                            };
                        }""", narrow)
                        assert all(layout[key] for key in ("contained", "titleContained", "separated", "metadataSeparated", "clipped")), (width, batch, filename, layout)
                        if not narrow:
                            for column in ("type", "date"):
                                label_x = header.locator(f".library-col-{column}").bounding_box()["x"]
                                assert row.locator(f".library-col-{column}").bounding_box()["x"] == pytest.approx(label_x, abs=1)

                    if width == 550:
                        first_row.locator(".library-file-name").hover()
                        expect(page.locator(".q-tooltip").filter(has_text=filenames[0])).to_be_visible()
                    page.mouse.move(1400, 20)
                    if width in (320, 550, 760):
                        for dark in (False, True):
                            page.evaluate("dark => Quasar.Dark.set(dark)", dark)
                            muted_color = first_row.locator(".library-col-date").evaluate("el => getComputedStyle(el).color")
                            expect(first_row.locator(".q-badge")).to_have_css("color", muted_color)
                            catalog.screenshot(path=str(service["path"] / f"file-list-{width}-{'batch' if batch else 'normal'}-{'dark' if dark else 'light'}.png"))
                        page.evaluate("Quasar.Dark.set(false)")
            first_row.click()
            expect(first_row).to_have_attribute("aria-pressed", "true")
            first_row.press("Escape")
            expect(catalog).to_have_attribute("data-multiselect", "false")
            assert not errors, errors
        finally:
            page.screenshot(path=str(service["path"] / "file-list-last.png"))
            print("File list screenshots:", service["path"])
            browser.close()
