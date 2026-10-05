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
        pdf.new_page().insert_text((72, 72), "Piece collection hierarchy - source page 1")
        pdf.save(original)
    # 固定测试资料的索引与原页定位；不触发任何远端解析服务。
    with database.get_db_cursor(write=True) as cursor:
        cursor.execute("UPDATE files SET filename=?, original_file_path=?, file_hash=?, original_file_type='pdf', status='indexed' WHERE id=?",
                       ("研究方法.pdf", str(original), hashlib.sha256(original.read_bytes()).hexdigest(), file_id))
        cursor.execute("INSERT INTO chunks(file_id,doc_title,chunk_text,heading_path) VALUES(?,?,?,?)",
                       (file_id, "研究方法 · 第1页", "原页核验正文。父范围包含后代，直接归类不变。", "研究方法 / 第1页"))
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
            expect(page.get_by_text("人工智能 (1)", exact=True)).to_be_visible(timeout=30000)
            expect(page.get_by_text("第 1/2 页 · 52 个文件", exact=True)).to_be_visible()
            page.get_by_role("button", name="下一页", exact=True).click()
            expect(page.get_by_text("第 2/2 页 · 52 个文件", exact=True)).to_be_visible()
            page.get_by_text("人工智能 (1)", exact=True).click()
            expect(page.get_by_text("第 1/1 页 · 1 个文件", exact=True)).to_be_visible()
            page.get_by_role("switch", name="包含子集合").click()
            expect(page.get_by_text("第 1/1 页 · 0 个文件", exact=True)).to_be_visible()
            page.get_by_role("switch", name="包含子集合").click()
            expect(page.get_by_text("研究方法.pdf", exact=True)).to_be_visible()

            # 原生树组件支持键盘展开和选择。
            header = page.locator(".q-tree__node-header").filter(has_text="人工智能 (1)")
            header.focus()
            header.press("Space")
            expect(page.get_by_text("检索增强 (1)", exact=True)).to_be_visible()
            child = page.locator(".q-tree__node-header").filter(has_text="检索增强 (1)")
            child.focus()
            child.press("Enter")
            expect(page.get_by_role("button", name="检索增强", exact=True)).to_be_visible()
            page.get_by_text("研究方法.pdf", exact=True).click()
            expect(page.get_by_text("原页核验正文。父范围包含后代，直接归类不变。")).to_be_visible()
            page.get_by_role("button", name="查看原文第 1 页", exact=True).click()
            expect(page.locator(".source-pane img")).to_be_visible(timeout=30000)
            page.screenshot(path=str(service["path"] / "collections-light.png"))

            # 多归属定位必须让用户选择目标。
            page.get_by_text("研究方法.pdf", exact=True).last.click()
            page.get_by_role("button", name="在集合中定位", exact=True).click()
            expect(page.get_by_text("选择定位集合", exact=True)).to_be_visible()
            page.get_by_role("dialog").get_by_role("button", name="人工智能 › 知识图谱").click()
            expect(page.get_by_role("button", name="知识图谱", exact=True)).to_be_visible()

            # 实际 CLI 移动，再从 GUI 管理入口刷新验证稳定 ID 与路径。
            moved = service["cli"]("collection", "move", str(service["graph"]), "--parent-id", str(service["other"]))
            assert moved.code == 0, moved.stdout
            page.locator('button:has(i:text-is("refresh"))').first.click()
            expect(page.get_by_role("button", name="其他资料", exact=True)).to_be_visible()
            expect(page.get_by_text("原页核验正文。父范围包含后代，直接归类不变。")).to_be_visible()

            # 读模式不重建阅读区，宽度变化后恢复原来的分隔位置。
            page.get_by_role("button", name="专注阅读 / 恢复布局", exact=True).click()
            expect(page.locator(".source-pane")).to_be_visible()
            page.screenshot(path=str(service["path"] / "collections-reading.png"))
            page.get_by_role("button", name="专注阅读 / 恢复布局", exact=True).click()
            page.set_viewport_size({"width": 850, "height": 760})
            page.screenshot(path=str(service["path"] / "collections-narrow.png"))
            page.set_viewport_size({"width": 1440, "height": 960})

            # 使用真实设置入口切换深色，返回后应保留选择与原页。
            page.get_by_role("button", name='Expand "文件库"', exact=True).click()
            page.get_by_role("button", name="设置", exact=True).click()
            page.get_by_text("基础设置", exact=True).click()
            page.get_by_text("深色", exact=True).click()
            page.get_by_role("button", name="文件库", exact=True).click()
            expect(page.get_by_role("button", name="知识图谱", exact=True)).to_be_visible()
            expect(page.locator(".source-pane img")).to_be_visible()
            page.screenshot(path=str(service["path"] / "collections-dark.png"))

            # 管理入口走真实 UI：改名、新建子集合，以及叶子删除的影响确认。
            page.get_by_role("button", name="管理集合", exact=True).first.click()
            editor = page.get_by_role("dialog")
            editor.get_by_label("重命名", exact=True).fill("图谱改名")
            editor.get_by_label("重命名", exact=True).press("Enter")
            expect(page.get_by_role("button", name="图谱改名", exact=True)).to_be_visible()
            expect(editor.get_by_label("重命名", exact=True)).to_have_value("图谱改名")
            editor.get_by_label("新建集合", exact=True).fill("新增子集合")
            editor.get_by_role("button", name="新建子集合", exact=True).click()
            expect(editor.get_by_label("新建集合", exact=True)).to_have_value("")
            listed = service["cli"]("collection", "list")
            child = next(c for c in listed.data["data"]["collections"] if c["name"] == "新增子集合")
            assert child["parent_id"] == service["graph"]
            assert service["cli"]("collection", "delete", str(child["id"]), "--yes").code == 0
            editor.get_by_role("button", name="删除集合", exact=True).click()
            confirmation = page.get_by_role("dialog").filter(has_text="将解除 1 个文件的直接归类")
            expect(confirmation).to_be_visible()
            confirmation.get_by_role("button", name="确认", exact=True).click()
            expect(page.get_by_text("第 1/2 页 · 52 个文件", exact=True)).to_be_visible()
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
