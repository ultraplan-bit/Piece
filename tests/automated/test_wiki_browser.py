"""阶段 C 实机验收：独立 Wiki 流程，临时新库 + 本机 Edge，无 CDN / 模型请求。

uv run --no-sync --with playwright pytest -q -s tests/automated/test_wiki_browser.py
"""
from uuid import uuid4

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401
from app.i18n import t


def k(name):
    return t("knowledge." + name)


def wk(name):
    return t("wiki." + name)


@pytest.fixture
def wiki_gui(gui_service):
    # 服务进程持有自己的连接池；测试进程要用同一临时库做并发写入，需自行初始化。
    from indexing import database
    database.init_connection_pool()
    yield gui_service
    database.close_connection_pool()


def apply(**parts):
    """测试进程与 GUI 使用同一临时数据目录，直接调用同一 Wiki 服务写入。"""
    from indexing.services import wiki_service
    return wiki_service.apply({"request_key": uuid4().hex, "reason": "浏览器验收", **parts})


def get_page(page_id):
    from indexing.services import wiki_service
    return wiki_service.get_record(kind="page", id=page_id)


def test_wiki_browser_independent_roundtrip(wiki_gui):
    service = wiki_gui
    expect = playwright.expect
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1540, "height": 1080},
                                http_credentials={"username": "piece", "password": service["key"]})
        errors = []
        external_requests = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(request):
            if request.request.url.startswith(service["url"] + "/"):
                request.continue_()
            else:
                external_requests.append(request.request.url)
                request.abort()
        page.route("**/*", route)
        try:
            page.goto(service["url"])
            expect(page.get_by_text("人工智能 (1)", exact=True)).to_be_visible(timeout=30000)
            page.get_by_role("button", name='Expand "文件库"', exact=True).click()
            page.get_by_role("button", name=t("sidebar.wiki"), exact=True).click()
            expect(page.get_by_text(k("empty"), exact=True)).to_be_visible()

            # GUI 新建页面，正文含表格与公式。
            page.get_by_role("button", name=k("create"), exact=True).click()
            dialog = page.get_by_role("dialog")
            dialog.get_by_label(k("title_field"), exact=True).fill("Wiki 浏览页")
            dialog.get_by_label(k("body"), exact=True).fill("# 阅读\n\n| 项目 | 内容 |\n| --- | --- |\n| 范围 | 本地 |\n\n公式 $a^2+b^2=c^2$。")
            dialog.get_by_label(k("aliases_text"), exact=True).fill("浏览别名")
            dialog.get_by_label(k("reason"), exact=True).fill("界面创建")
            dialog.get_by_role("button", name=k("save"), exact=True).click()
            expect(dialog).not_to_be_visible()
            prose = page.locator(".knowledge-prose")
            expect(prose).to_contain_text("阅读")
            expect(prose.locator("table")).to_be_visible()
            expect(prose.locator("math")).to_be_visible()

            from indexing.services import wiki_service
            page1 = wiki_service.search_pages(query="浏览别名")["pages"][0]
            assert page1["content_hash"]
            # 外部调用者新建目标页，并把 MD 页面链接写进正文。
            target = apply(pages=[{"ref": "target", "kind": "concept", "title": "目标页", "body": "目标正文"}]
                           )["refs"]["target"]["id"]
            current = get_page(page1["id"])["record"]
            apply(pages=[{"id": page1["id"], "expected_revision": current["revision"],
                          "expected_content_hash": current["content_hash"],
                          "body": f"## 原有正文\n\n[目标页](piece://wiki/{target})"}])
            expect(prose).to_contain_text("目标页", timeout=15000)

            # 点击 MD 派生的 Wiki 内链，在本功能内跳转，不进入图谱。
            prose.get_by_text("目标页", exact=True).click()
            expect(page.locator(".knowledge-prose")).to_contain_text("目标正文")
            page.get_by_role("button", name="Wiki 浏览页", exact=True).first.click()

            # 只在 Wiki 出现的证据：Piece 来源定位、比对与打开原页。
            library = wiki_service.list_pages()["library_id"]
            from indexing.services import file_service
            chunk = file_service.get_chunks_paginated(service["file_id"])["chunks"][0]
            record = get_page(page1["id"])["record"]
            apply(pages=[{"id": page1["id"], "expected_revision": record["revision"],
                          "expected_content_hash": record["content_hash"]}],
                  evidence=[{"page": {"id": page1["id"]}, "source_kind": "piece",
                             "source_library_id": library, "source_file_id": service["file_id"],
                             "source_chunk_id": chunk["id"], "quote": chunk["chunk_text"]}])
            expect(page.get_by_text(k("location_current"), exact=True)).to_be_visible(timeout=15000)

            # 结构检查与从 Markdown 重建派生索引，都不重写正文。
            page.get_by_role("button", name=k("review"), exact=True).click()
            page.get_by_role("button", name=k("run_checks"), exact=True).click()
            page.get_by_role("button", name=wk("rebuild_index"), exact=True).click()
            # 重建不重写正文；按钮与页面保持可用。
            expect(page.get_by_role("button", name=wk("rebuild_index"), exact=True)).to_be_visible()
            page.get_by_role("button", name=k("pages"), exact=True).first.click()

            # 删除证据只清理证据；页面与历史保留（预览由服务声明）。
            page.get_by_role("button", name=k("delete_evidence"), exact=True).first.click()
            expect(page.get_by_text(k("delete_preview"), exact=True)).to_be_visible()
            expect(page.get_by_text(k("delete_retains_history"), exact=True)).to_be_visible()
            dialog.get_by_role("button", name=k("confirm_delete"), exact=True).click()
            expect(dialog).not_to_be_visible()
            page.get_by_role("button", name="Wiki 浏览页", exact=True).first.click()
            expect(page.get_by_text(k("no_evidence"), exact=True)).to_be_visible(timeout=10000)

            # 删除页面后服务不再返回该页面，历史快照仍可读。
            page.get_by_role("button", name="Wiki 浏览页", exact=True).first.click()
            page.get_by_role("button", name=k("delete"), exact=True).click()
            expect(page.get_by_text(k("delete_preview"), exact=True)).to_be_visible()
            dialog.get_by_role("button", name=k("confirm_delete"), exact=True).click()
            expect(dialog).not_to_be_visible()
            from indexing.services.errors import BusinessError
            with pytest.raises(BusinessError):
                get_page(page1["id"])
            history = wiki_service.history(kind="page", id=page1["id"])
            assert history["total"] >= 1
            assert not errors, errors
            assert not external_requests, external_requests
        finally:
            page.screenshot(path=str(service["path"] / "wiki-final.png"))
            browser.close()
