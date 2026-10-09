"""切片图片的真实浏览器回归；使用隔离资料库与本机 Edge。"""
import html
from urllib.parse import quote

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401
from app.i18n import t
from tree_browser_helpers import browse_mode, document_row


def test_chunk_images_load_without_root_relative_requests(knowledge_base, request):
    from PIL import Image
    from indexing import database
    from indexing.services import file_service

    file_id = file_service.create_empty_file("图片路径验证")["file_id"]
    working = file_service.get_working_dir()
    generation = working / ".generations" / "task-image-test"
    reference = "报告 #1 (草稿(50%)) & 附录/图 a.jpg"
    image_path = generation / reference
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (160, 120), color="#56866b").save(image_path)
    body = (
        f'![正文插图]({reference})\n\n'
        f'![带图注](<{reference}> "图注")\n\n'
        f"<img alt='HTML 插图' src='{html.escape(reference, quote=True)}'>"
    )
    file_path = generation / "图片路径验证.md"
    file_path.write_text(body, encoding="utf-8")
    with database.get_db_cursor(write=True) as cursor:
        cursor.execute("UPDATE files SET file_path=?, status='indexed' WHERE id=?", (str(file_path), file_id))
        cursor.execute("INSERT INTO chunks(file_id, doc_title, chunk_text) VALUES (?, ?, ?)",
                       (file_id, "图片路径验证", body))

    service = request.getfixturevalue("gui_service")
    expected_url = f'{service["url"]}/working/{quote(image_path.relative_to(working).as_posix(), safe="/")}'
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000},
                                http_credentials={"username": "piece", "password": service["key"]})
        image_responses = []
        image_requests = []
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda req: image_requests.append(req.url)
                if req.resource_type == "image" else None)
        page.on("response", lambda response: image_responses.append((response.url, response.status))
                if response.request.resource_type == "image" else None)
        try:
            page.goto(service["url"])
            browse_mode(page, "all")
            page.get_by_role("textbox", name=t("files.search"), exact=True).fill("图片路径验证")
            document_row(page, file_id).click()
            images = page.locator(".chunk-content img")
            playwright.expect(images).to_have_count(3)
            page.wait_for_function("""() => [...document.querySelectorAll('.chunk-content img')]
                .every(image => image.complete && image.naturalWidth === 160)""")
            assert images.evaluate_all("images => images.map(image => image.currentSrc)") == [expected_url] * 3
            assert image_requests and set(image_requests) == {expected_url}
            assert image_responses and all(status == 200 for _, status in image_responses)
            assert not errors
            screenshot = service["path"] / "chunk-images.png"
            page.screenshot(path=str(screenshot))
            print(f"图片浏览器验收截图：{screenshot}")
        finally:
            browser.close()
