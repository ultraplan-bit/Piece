"""可选浏览器验收：uv run --no-sync --with playwright pytest -q -s tests/automated/test_collection_browser.py

使用本机 Edge 和临时新库，不下载浏览器、不访问解析/嵌入服务，不改变用户配置。
普通回归未安装 Playwright 时跳过；截图和服务日志留在 pytest 临时目录。
"""

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_service_process import ROOT, _cli, _free_port
from app.i18n import t
from tree_browser_helpers import browse_mode, collection_row, document_row, expand_collection


@pytest.fixture
def gui_service(knowledge_base, tmp_path):
    import fitz
    from indexing import database, settings
    from indexing.services import collection_service, file_service

    if sys.platform != "win32" or not Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe").is_file():
        pytest.skip("浏览器验收使用已安装的 Windows Edge")
    data_dir = knowledge_base.path
    config = knowledge_base.settings
    config.appearance.theme = "light"
    # 没有后台索引任务；即使发生意外请求，也只会连接本机不存在的服务。
    config.embedding.base_url = "http://127.0.0.1:1/v1"
    settings.save_settings(config)
    root = collection_service.create_collection("人工智能")["collection_id"]
    rag = collection_service.create_collection("检索增强", parent_id=root)["collection_id"]
    graph = collection_service.create_collection("知识图谱", parent_id=root)["collection_id"]
    other = collection_service.create_collection("其他资料")["collection_id"]
    collection_service.create_collection("很长的集合名称用于窄窗口换行检查" * 3, parent_id=other)
    file_id = file_service.create_empty_file("研究方法")["file_id"]
    collection_service.set_file_collections(file_id, [rag, graph])
    original = config.get_files_path() / "originals" / "browser-source.pdf"
    original.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open() as pdf:
        for number in range(1, 4):
            pdf.new_page().insert_text((72, 72), f"Piece collection hierarchy - source page {number}")
        pdf.save(original)
    body = (
        "原页核验正文。父范围包含后代，直接归类不变。\n\n"
        "| Timer | Counter resolution | Counter type | Prescaler factor | DMA request generation | Capture/compare channels | Complementary outputs |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| TIM1 | 16-bit | Up, down | Any integer between 1 and 65536 | Yes | 4 | Yes |"
    )
    # 固定测试资料的索引与原页定位；不触发任何远端解析服务。
    with database.get_db_cursor(write=True) as cursor:
        cursor.execute("UPDATE files SET filename=?, original_file_path=?, file_hash=?, original_file_type='pdf', status='indexed' WHERE id=?",
                       ("研究方法.pdf", str(original), hashlib.sha256(original.read_bytes()).hexdigest(), file_id))
        cursor.execute("INSERT INTO chunks(file_id,doc_title,chunk_text,heading_path) VALUES(?,?,?,?)",
                       (file_id, "研究方法 · 第1页", body, "研究方法 / 第1页"))
    for index in range(51):
        file_service.create_empty_file(f"分页样本 {index:02}")
    database.close_connection_pool()
    port = _free_port()
    log_path = tmp_path / "gui-serve.log"
    with log_path.open("wb") as log:
        executable = os.environ.get("PIECE_TEST_EXECUTABLE")
        command = [executable] if executable else [sys.executable, "-m", "app.cli"]
        process = subprocess.Popen([*command, "serve", "--no-mcp", "--no-tray",
                                    "--data-dir", str(data_dir), "--port", str(port)], cwd=ROOT,
                                   env={**os.environ, "PYTHONUTF8": "1"}, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status = _cli(data_dir, port, "status")
                if status.code == 0:
                    break
                assert process.poll() is None, log_path.read_text(encoding="utf-8", errors="replace")
                time.sleep(0.2)
            else:
                pytest.fail(log_path.read_text(encoding="utf-8", errors="replace"))
            yield {"url": f"http://127.0.0.1:{port}", "key": config.api.admin_key,
                   "root": root, "rag": rag, "graph": graph, "other": other, "file_id": file_id,
                   "cli": lambda *args: _cli(data_dir, port, *args), "path": tmp_path, "original": original}
        finally:
            if process.poll() is None:
                _cli(data_dir, port, "stop")
                try:
                    process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    pytest.fail(f"GUI 服务未正常退出：PID {process.pid}")
    service_log = log_path.read_text(encoding="utf-8", errors="replace")
    assert process.returncode == 0, service_log
    assert "[ERROR]" not in service_log, service_log


def test_collection_browser_roundtrip(gui_service):
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
            expect(page.locator(".workspace-sidebar [data-collection-id]")).to_have_count(0)
            browse_mode(page, "all")
            expect(page.locator(".app-header .workspace-nav-item")).to_have_count(0)
            expect(page.locator(".workspace-primary [aria-current=page]")).to_contain_text(t("sidebar.files"))
            # 说明默认不占空间；问号支持键盘查看、Esc 关闭和鼠标悬停。
            help_button = page.locator(".library-scope .help-hint")
            help_text = page.locator(".help-tooltip")
            expect(help_text).to_have_count(0)
            help_button.focus()
            expect(help_text).to_be_visible()
            help_button.press("Escape")
            expect(help_text).not_to_be_visible()
            help_button.hover()
            expect(help_text).to_be_visible()
            page.mouse.move(700, 20)
            help_button.evaluate("el => el.blur()")
            expect(help_text).not_to_be_visible()
            expect(page.get_by_text(t("files.pagination", page=1, pages=2, total=52), exact=True)).to_be_visible()
            # 统计文字和分页按钮不能撑出不同栏高；较矮窗口下也不应压缩底栏。
            footers = page.locator(".library-footer")
            expect(footers).to_have_count(2)
            for height in (960, 600):
                page.set_viewport_size({"width": 1440, "height": height})
                for footer in footers.all():
                    expect(footer).to_have_css("height", "44px")
                    expect(footer).to_have_css("align-items", "center")
                    assert footer.evaluate("""el => {
                        const box = el.getBoundingClientRect();
                        return [...el.children].every(child => {
                            const rect = child.getBoundingClientRect();
                            return Math.abs(rect.y + rect.height / 2 - box.y - box.height / 2) <= 1;
                        });
                    }""")
                assert footers.nth(0).bounding_box()["y"] == pytest.approx(footers.nth(1).bounding_box()["y"], abs=1)
            page.set_viewport_size({"width": 1440, "height": 960})
            page.get_by_role("button", name="下一页", exact=True).click()
            expect(page.get_by_text(t("files.pagination", page=2, pages=2, total=52), exact=True)).to_be_visible()
            browse_mode(page, "tree")
            expand_collection(page, service["root"])
            # 父资料集不会平铺后代的文档；展开子资料集才出现文档条目。
            expect(document_row(page, service["file_id"])).to_have_count(0)
            expand_collection(page, service["rag"])
            file_row = document_row(page, service["file_id"], collection_id=service["rag"])
            row_id = file_row.get_attribute("id")
            middle = page.locator(".workspace-splitter").last.locator(":scope > .q-splitter__before")
            file_row.dblclick(delay=100)
            expect(middle).to_have_css("width", "320px")
            expect(file_row).to_have_attribute("id", row_id)
            file_row.focus()
            file_row.press("Enter")
            expect(middle).to_have_css("width", "320px")
            expect(page.get_by_text("原页核验正文。父范围包含后代，直接归类不变。")).to_be_visible()
            expect(page.locator(".library-reader-title")).to_have_text("研究方法.pdf")
            expect(page.locator(".library-reading-block")).to_have_count(1)
            page.screenshot(path=str(service["path"] / "library-table.png"))
            toolbar = page.locator(".reader-toolbar")
            toolbar.get_by_role("button", name="更多操作", exact=True).click()
            page.locator(".q-menu").get_by_text("卡片管理", exact=True).click()
            expect(page.locator(".chunk-sheet")).to_have_count(1)
            toolbar.get_by_role("button", name="阅读", exact=True).click()
            expect(page.locator(".library-reading-block")).to_have_count(1)
            expect(page.locator(".workspace-splitter").first.locator(":scope > .q-splitter__before")).to_have_css("width", "160px")
            expect(middle).to_have_css("width", "320px")
            toolbar.get_by_role("button", name="对照", exact=True).click()
            expect(page.locator(".source-pane img")).to_be_visible(timeout=30000)
            expect(middle).to_have_css("width", "320px")
            expect(file_row).to_have_attribute("id", row_id)
            source = page.locator(".source-pane")
            source_input = source.get_by_role("textbox", name="原页页码", exact=True)
            expect(source_input).to_have_value("1")
            expect(source.get_by_text("/ 3", exact=True)).to_be_visible()
            image_id = source.locator(".source-image").get_attribute("id")
            source.get_by_role("button", name="下一原页", exact=True).click()
            expect(source_input).to_have_value("2")
            expect(source.get_by_role("switch", name="跟随", exact=True)).not_to_be_checked()
            source_input.fill("3")
            source_input.press("Enter")
            expect(source.get_by_role("button", name="下一原页", exact=True)).to_be_disabled()
            source.get_by_role("button", name="上一原页", exact=True).click()
            expect(source_input).to_have_value("2")
            source.get_by_role("switch", name="跟随", exact=True).click()
            expect(source_input).to_have_value("1")
            assert source.locator(".source-image").get_attribute("id") == image_id
            source_input.fill("99")
            source_input.press("Enter")
            expect(source_input).to_have_value("1")
            source_input.fill("invalid")
            source_input.press("Enter")
            expect(source_input).to_have_value("1")
            expect(source.get_by_role("switch", name="跟随", exact=True)).to_be_checked()
            source.get_by_role("button", name="放大原页", exact=True).click()
            expect(source.get_by_text("125%", exact=True)).to_be_visible()
            assert source.locator(".source-image").bounding_box()["width"] > source.locator(".source-scroll").bounding_box()["width"]
            source.get_by_role("button", name="适宽", exact=True).click()
            table = page.locator(".library-reading-body table")
            assert table.evaluate("el => el.scrollWidth > el.clientWidth")
            assert table.evaluate("el => { el.scrollLeft = 120; return el.scrollLeft > 0; }")
            table.evaluate("el => { el.scrollLeft = 0; }")
            assert table.evaluate("""el => {
                const text = el.querySelector('th:nth-child(2)').firstChild;
                const start = text.textContent.indexOf('resolution');
                const range = document.createRange();
                range.setStart(text, start); range.setEnd(text, start + 'resolution'.length);
                return range.getClientRects().length === 1;
            }""")
            page.screenshot(path=str(service["path"] / "collections-light.png"))

            # 多归属定位必须让用户选择目标；信息面板不再常驻正文顶部。
            toolbar.get_by_role("button", name="阅读", exact=True).click()
            expect(page.locator(".source-pane")).to_have_count(0)
            toolbar.get_by_role("button", name=t("files.info_title"), exact=True).click()
            page.get_by_role("button", name=t("files.locate"), exact=True).click()
            expect(page.get_by_text(t("files.locate_title"), exact=True)).to_be_visible()
            page.get_by_role("dialog").get_by_role("button", name="人工智能 › 知识图谱").click()
            expect(collection_row(page, service["graph"])).to_have_attribute("aria-selected", "true")
            page.get_by_role("button", name=t("library.close_info"), exact=True).click()

            # 实际 CLI 移动，再从 GUI 管理入口刷新验证稳定 ID 与路径。
            moved = service["cli"]("collection", "move", str(service["graph"]), "--parent-id", str(service["other"]))
            assert moved.code == 0, moved.stdout
            page.locator('button:has(i:text-is("refresh"))').first.click()
            expect(collection_row(page, service["graph"])).to_have_attribute("data-parent-row", f'c:{service["other"]}')
            expect(page.get_by_text("原页核验正文。父范围包含后代，直接归类不变。")).to_be_visible()

            # 阅读与对照均保留目录；窄窗口改为上下对照，不自动收起导航。
            toolbar.get_by_role("button", name="阅读", exact=True).click()
            expect(page.locator(".source-pane")).to_have_count(0)
            expect(middle).to_have_css("width", "320px")
            page.screenshot(path=str(service["path"] / "collections-reading.png"))
            toolbar.get_by_role("button", name="对照", exact=True).click()
            expect(page.locator(".source-pane img")).to_be_visible(timeout=30000)
            page.set_viewport_size({"width": 850, "height": 760})
            expect(page.locator(".workspace-splitter").first.locator(":scope > .q-splitter__before")).to_have_css("width", "160px")
            expect(middle).to_have_css("width", "320px")
            overflow = source.locator(".source-toolbar").evaluate("""el => {
                const bounds = el.getBoundingClientRect();
                return [...el.querySelectorAll('button, input, .q-toggle')].flatMap(control => {
                    const rect = control.getBoundingClientRect();
                    if (!rect.width || !rect.height) return [];
                    return rect.left >= bounds.left && rect.right <= bounds.right
                        && rect.top >= bounds.top && rect.bottom <= bounds.bottom ? [] : [{
                            control: control.outerHTML, rect: rect.toJSON(), bounds: bounds.toJSON(),
                        }];
                });
            }""")
            assert not overflow, overflow
            page.screenshot(path=str(service["path"] / "collections-narrow.png"))
            page.set_viewport_size({"width": 1440, "height": 960})

            # 切换工作区与主题后，仍保持对照模式及原页。
            page.get_by_role("button", name="设置", exact=True).click()
            page.get_by_text("基础设置", exact=True).click()
            page.get_by_text("深色", exact=True).click()
            page.get_by_role("button", name=t("sidebar.files"), exact=True).click()
            expect(page.locator(".source-pane img")).to_be_visible()
            expect(toolbar.get_by_role("button", name="对照", exact=True)).to_have_attribute("aria-pressed", "true")
            expect(middle).to_have_css("width", "320px")
            page.screenshot(path=str(service["path"] / "collections-dark.png"))
            toolbar.get_by_role("button", name="阅读", exact=True).click()
            expect(collection_row(page, service["graph"])).to_have_attribute("aria-selected", "true")

            # 在条目上就地改名、新建子资料集，删除仍展示影响确认。
            graph_row = collection_row(page, service["graph"])
            graph_row.focus()
            graph_row.press("F2")
            editor = page.locator("[data-collection-editor]")
            name = editor.get_by_role("textbox", name=t("collections.name"), exact=True)
            name.fill("图谱改名")
            name.press("Enter")
            expect(editor).to_have_count(0)
            expect(graph_row).to_have_attribute("aria-label", "图谱改名")
            graph_row.click(button="right")
            page.get_by_role("menuitem", name=t("collections.create_child"), exact=True).click()
            name.fill("新增子集合")
            name.press("Enter")
            expect(editor).to_have_count(0)
            listed = service["cli"]("collection", "list")
            child = next(c for c in listed.data["data"]["collections"] if c["name"] == "新增子集合")
            assert child["parent_id"] == service["graph"]
            assert service["cli"]("collection", "delete", str(child["id"]), "--yes").code == 0
            page.locator('.workspace-toolbar button:has(i:text-is("refresh"))').click()
            expect(collection_row(page, child["id"])).to_have_count(0)
            graph_row.click(button="right")
            page.get_by_role("menuitem", name=t("collections.delete"), exact=True).click()
            confirmation = page.get_by_role("dialog").filter(has_text="将解除 1 个文档的直接归类")
            expect(confirmation).to_be_visible()
            confirmation.get_by_role("button", name=t("confirm_dialog.btn_delete"), exact=True).click()
            expect(graph_row).to_have_count(0)
            browse_mode(page, "all")
            expect(page.get_by_text(t("files.pagination", page=1, pages=2, total=52), exact=True)).to_be_visible()
            verified = service["cli"]("file", "list", "--name", "研究方法")
            assert verified.code == 0 and verified.data["data"]["total"] == 1
            assert not errors, errors
            print(f"GUI screenshots: {service['path']}")
        finally:
            print(f"GUI screenshots: {service['path']}")
            page.screenshot(path=str(service["path"] / "collections-last.png"))
            (service["path"] / "browser-errors.txt").write_text("\n".join(errors), encoding="utf-8")
            context.close()
            browser.close()
