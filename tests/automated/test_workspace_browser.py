"""使用隔离资料库与真实 Edge 验证目录、快捷键和任务入口。"""
import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401
from app.i18n import t
from tree_browser_helpers import browse_mode


def test_catalog_keyboard_selection_search_and_restore(gui_service):
    service = gui_service
    expect = playwright.expect
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900},
                                http_credentials={"username": "piece", "password": service["key"]})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(service["url"])
            catalog = page.locator('[data-catalog="files"]')
            modes = page.get_by_role("group", name=t("files.view"), exact=True)
            mode = modes.get_by_role("button", name=t("files.all_files"), exact=True)
            expect(mode).to_be_enabled()
            mode_id = mode.get_attribute("id")
            row_id = catalog.locator(".library-row").first.get_attribute("id")
            mode.focus()
            catalog.get_by_role("button", name=t("files.refresh"), exact=True).evaluate("button => button.click()")
            expect(catalog.locator(".library-row").first).not_to_have_attribute("id", row_id)
            expect(mode).to_have_attribute("id", mode_id)
            expect(mode).to_be_focused()
            expect(modes.get_by_role("button")).to_have_count(3)
            page.screenshot(path=str(service["path"] / "browse-modes-refresh.png"))
            mode.press("Enter")
            expect(catalog).to_have_attribute("data-library-mode", "all")
            rows = catalog.locator("[data-catalog-row]")
            expect(rows).to_have_count(50, timeout=30000)
            rows.nth(0).click()
            expect(rows.nth(0)).to_have_attribute("aria-pressed", "true")
            rows.nth(0).press("ArrowDown")
            expect(rows.nth(1)).to_be_focused()
            expect(rows.nth(1)).to_have_attribute("aria-pressed", "true")
            title = rows.nth(1).get_attribute("aria-label").split(" — ")[0]
            expect(page.locator(".library-reader-title")).to_have_text(title)

            rows.nth(2).click(modifiers=["Control"])
            expect(catalog).to_have_attribute("data-multiselect", "true")
            expect(catalog.locator('[data-catalog-row][aria-pressed="true"]')).to_have_count(2)
            rows.nth(2).press("Shift+ArrowDown")
            expect(catalog.locator('[data-catalog-row][aria-pressed="true"]')).to_have_count(3)
            expect(page.locator(".library-reader-title")).to_have_text(title)
            identities = rows.evaluate_all("items => items.map(item => item.id)")
            rows.nth(2).press("Space")
            expect(catalog.locator('[data-catalog-row][aria-pressed="true"]')).to_have_count(2)
            assert rows.evaluate_all("items => items.map(item => item.id)") == identities

            # 本页全选不抹掉其他页的选择。
            select_page = catalog.get_by_role("checkbox", name="本页全选", exact=True)
            select_page.click()
            expect(select_page).to_be_checked()
            expect(catalog.locator('[data-catalog-row][aria-pressed="true"]')).to_have_count(50)
            page.get_by_role("button", name="下一页", exact=True).click()
            expect(rows).to_have_count(2)
            rows.nth(0).click()
            page.get_by_role("button", name="上一页", exact=True).click()
            expect(rows).to_have_count(50)
            select_page = catalog.get_by_role("checkbox", name="本页全选", exact=True)
            expect(select_page).to_be_checked()
            select_page.click()
            expect(select_page).not_to_be_checked()
            expect(catalog).to_contain_text(t("workspace.selected_cross_page", count=1))
            rows.nth(0).press("Escape")
            expect(catalog).to_have_attribute("data-multiselect", "false")
            expect(page.locator(".results-splitter > .q-splitter__before")).to_have_css("width", "320px")

            # 页面根聚焦时也能搜索；清除只影响目录，不卸下正在阅读的文件。
            page.evaluate("document.activeElement.blur()")
            page.keyboard.press("Control+k")
            search = catalog.get_by_role("textbox", name=t("files.search"), exact=True)
            expect(search).to_be_focused()
            search.fill("研究方法")
            expect(rows).to_have_count(1)
            rows.first.click()
            expect(page.locator(".library-reader-title")).to_have_text("研究方法.pdf")
            page.wait_for_function("""expectedFile => Object.keys(localStorage).some(key => {
                if (!key.startsWith('piece.workspace.v1.')) return false;
                const saved = JSON.parse(localStorage[key]);
                return saved.files.search_keyword === '研究方法' && saved.files.selected_file_id === expectedFile;
            })""", arg=service["file_id"])
            page.reload()
            expect(search).to_have_value("研究方法", timeout=30000)
            expect(search).to_be_enabled()
            expect(rows).to_have_count(1)
            expect(page.locator(".library-reader-title")).to_have_text("研究方法.pdf")
            search.press("Escape")
            expect(search).to_have_value("")
            expect(rows).to_have_count(50)
            expect(page.locator(".library-reader-title")).to_have_text("研究方法.pdf")
            assert not errors, errors
            page.screenshot(path=str(service["path"] / "workspace-keyboard.png"))
        finally:
            page.screenshot(path=str(service["path"] / "workspace-last.png"))
            print("Workspace screenshots:", service["path"])
            browser.close()


def test_activity_is_available_from_other_workspaces(knowledge_base, request):
    from indexing.services import file_service, task_service

    file_id = file_service.create_empty_file("活动来源")["file_id"]
    task_id = task_service.create_task("失败示例", file_id, task_type="chunk_add",
                                       input_data={"doc_title": "示例", "chunk_text": "正文"})
    task_service.update_task_status(task_id, "failed", error_message="测试失败原因，原资料保留")
    # 在启动服务前准备终态任务，不让真实后台访问任何解析或嵌入服务。
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
            expect(page.locator(".library-row").first).to_be_visible(timeout=30000)
            page.locator(".workspace-primary").get_by_role("button", name="Wiki", exact=True).click()
            page.locator(".app-header").get_by_role("button", name="任务活动", exact=True).click()
            panel = page.locator(".task-activity-panel")
            expect(panel).to_contain_text("测试失败原因，原资料保留")
            row = panel.locator(f'[data-task-id="{task_id}"]')
            expect(row.get_by_role("button", name="重试任务", exact=True)).to_be_enabled()
            page.screenshot(path=str(service["path"] / "task-activity.png"))
            row.get_by_role("button", name=t("task_activity.open_file"), exact=True).click()
            expect(panel).not_to_be_visible()
            expect(page.locator(".library-reader-title")).to_have_text("活动来源.md")
            expect(page.locator(".results-splitter > .q-splitter__before")).to_have_css("width", "320px")
            assert not errors, errors
        finally:
            print("Activity screenshots:", service["path"])
            browser.close()


def test_quick_navigation_keeps_the_last_click(knowledge_base, request):
    from uuid import uuid4
    from indexing.services import knowledge_service

    knowledge_service.apply({"request_key": uuid4().hex, "reason": "导航回归", "objects": [
        {"ref": "root", "kind": "concept", "title": "切换测试节点"},
    ]})
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
            browse_mode(page, "all")
            sidebar = page.locator(".workspace-primary")
            header = page.locator(".app-header")
            nav_ids = sidebar.locator("button").evaluate_all("buttons => buttons.map(button => button.id)")
            header_ids = header.locator("button").evaluate_all("buttons => buttons.map(button => button.id)")
            title = header.locator(".app-view-title")
            # 不在点击之间等待页面更新，顶栏与侧栏都不能销毁下一次点击的目标。
            sidebar.get_by_role("button", name=t("sidebar.wiki"), exact=True).click()
            header.get_by_role("button", name=t("sidebar.settings"), exact=True).click()
            sidebar.get_by_role("button", name=t("sidebar.files"), exact=True).click()
            header.get_by_role("button", name=t("sidebar.cloud_sync"), exact=True).click()
            sidebar.get_by_role("button", name=t("sidebar.graph"), exact=True).click()
            expect(title).to_have_text(t("sidebar.graph"))
            page.get_by_role("button", name="切换测试节点", exact=True).click()
            page.get_by_role("button", name=t("knowledge.view_graph"), exact=True).click()
            expect(page.locator(".nicegui-echart canvas")).to_be_visible(timeout=30000)

            # 只暂存图谱位置回传，按指定顺序放行，真实复现异步导航乱序。
            page.evaluate("""() => {
                const emit = window.socket.emit.bind(window.socket);
                window.__positionReplies = [];
                window.__restorePositionReplies = () => { window.socket.emit = emit; };
                window.socket.emit = (event, ...args) => {
                    if (event === 'javascript_response' && args[0]?.result?.points) {
                        window.__positionReplies.push(() => new Promise(resolve => emit(event, args[0], resolve)));
                        return window.socket;
                    }
                    return emit(event, ...args);
                };
            }""")
            sidebar.get_by_role("button", name=t("sidebar.files"), exact=True).click()
            page.wait_for_function("window.__positionReplies.length === 1")
            sidebar.get_by_role("button", name=t("sidebar.wiki"), exact=True).click()
            page.wait_for_function("window.__positionReplies.length === 2")
            page.evaluate("() => window.__positionReplies.pop()()")
            expect(title).to_have_text(t("sidebar.wiki"))
            page.evaluate("() => window.__positionReplies.pop()()")
            expect(title).to_have_text(t("sidebar.wiki"))
            expect(sidebar.locator("[aria-current=page]")).to_have_attribute("aria-label", t("sidebar.wiki"))

            # 点回当前工作区也算最后一次意图，应取消还未完成的离开请求。
            sidebar.get_by_role("button", name=t("sidebar.graph"), exact=True).click()
            expect(page.locator(".nicegui-echart canvas")).to_be_visible()
            sidebar.get_by_role("button", name=t("sidebar.files"), exact=True).click()
            page.wait_for_function("window.__positionReplies.length === 1")
            sidebar.get_by_role("button", name=t("sidebar.graph"), exact=True).click()
            page.evaluate("() => window.__positionReplies.pop()()")
            expect(title).to_have_text(t("sidebar.graph"))
            page.evaluate("window.__restorePositionReplies()")
            sidebar.get_by_role("button", name=t("sidebar.files"), exact=True).click()
            expect(title).to_have_text(t("sidebar.files"))
            expect(page.locator('[data-catalog="files"]')).to_have_attribute("data-library-mode", "all")
            assert sidebar.locator("button").evaluate_all("buttons => buttons.map(button => button.id)") == nav_ids
            assert header.locator("button").evaluate_all("buttons => buttons.map(button => button.id)") == header_ids
            assert not errors, errors
        finally:
            page.screenshot(path=str(service["path"] / "quick-navigation.png"))
            print("Navigation screenshots:", service["path"])
            browser.close()
