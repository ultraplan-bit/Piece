"""在真实 Edge 中验证工具探索细节；服务、知识库与嵌入请求均为临时本机数据。"""

import re
import threading

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401
from test_settings_tools_browser import app_page, _tool  # noqa: F401
from app.i18n import t


def test_recall_keyboard_activity_stale_and_reader_return(app_page, embedding_stub, monkeypatch):
    page, service = app_page
    expect = playwright.expect
    release = threading.Event()
    original_post = embedding_stub.handler.do_POST

    def delayed_post(request):
        release.wait(15)
        original_post(request)

    monkeypatch.setattr(embedding_stub.handler, "do_POST", delayed_post)
    try:
        _tool(page, "recall_test")
        query = page.get_by_role("textbox", name=t("recall_test.query"), exact=True)
        query.fill("研究方法")
        query.press("Control+Enter")
        expect(page.get_by_text(re.compile(re.escape(t("recall_test.stage_search")) + r" ·"))).to_be_visible(timeout=10000)
        expect(page.locator(".task-activity-badge")).to_be_visible()
        _tool(page, "logs")
        page.get_by_role("button", name=t("task_activity.open"), exact=True).click()
        menu = page.locator(".task-activity-menu")
        expect(menu.get_by_text("研究方法", exact=True)).to_be_visible()
        menu.get_by_role("button", name=t("recall_test.title"), exact=True).click()
        expect(query).to_have_value("研究方法")
        release.set()
        result = page.locator(".recall-result").first
        expect(result).to_be_visible(timeout=30000)
        expect(page.locator(".task-activity-badge")).not_to_be_visible(timeout=10000)
        result.locator(".q-item").first.click()
        expect(result.locator(".q-item").first).to_have_attribute("aria-expanded", "true")
        result.get_by_role("button", name=t("recall_test.toggle_view"), exact=True).click()
        result_id = result.get_attribute("id")
        query.fill("修改后的查询")
        expect(page.get_by_text(t("recall_test.stale"), exact=True)).to_be_visible()
        expect(result).to_have_attribute("id", result_id)
        result.get_by_role("button", name=t("recall_test.locate"), exact=True).click()
        expect(page.locator(".library-reader-title")).to_have_text("研究方法.pdf")
        page.get_by_role("button", name=t("recall_test.return"), exact=True).click()
        result = page.locator(".recall-result").first
        expect(result.locator(".q-item").first).to_have_attribute("aria-expanded", "true")
        expect(query).to_have_value("修改后的查询")
        expect(result.get_by_role("button", name=t("recall_test.toggle_view"), exact=True).locator("i")).to_have_text("article")
        expect(result.locator(".q-item").first).to_be_focused()
        page.screenshot(path=str(service["path"] / "recall-return-stale.png"))
    finally:
        release.set()


def test_mcp_pin_copy_steps_and_native_skill_fallback(app_page):
    page, service = app_page
    expect = playwright.expect
    _tool(page, "mcp_config")
    page.get_by_role("button", name=t("mcp_config.pin") + " Zed", exact=True).click()
    expect(page.get_by_role("button", name=t("mcp_config.unpin") + " Zed", exact=True)).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="Zed", exact=True).click()
    expect(page.locator(".tool-steps")).to_contain_text(t("mcp_config.step_connect"))
    page.evaluate("""Object.defineProperty(navigator, 'clipboard', {configurable:true,
        value:{writeText: async value => { window.copiedConfig = value; }}})""")
    page.get_by_role("button", name=t("mcp_config.copy_btn"), exact=True).click()
    expect(page.get_by_text(t("mcp_config.after_copy"), exact=True)).to_be_visible()
    assert "piece-kb" in page.evaluate("window.copiedConfig")
    _tool(page, "skills")
    page.get_by_role("button", name=re.compile("^piece-search")).click()
    expect(page.get_by_text(t("skills.export_target", target="piece-search"), exact=True)).to_be_visible()
    expect(page.get_by_role("button", name=t("skills.choose_directory"), exact=True)).not_to_be_visible()
    target = service["path"] / "tool-detail-export"
    page.get_by_role("textbox", name=t("skills.export_dir"), exact=True).fill(str(target))
    page.get_by_role("button", name=t("skills.export_current"), exact=True).click()
    expect(page.locator(".skill-export-result")).to_contain_text("piece-search/SKILL.md")
    expect(page.get_by_role("button", name=t("skills.open_directory"), exact=True)).not_to_be_visible()
    assert (target / "piece-search" / "SKILL.md").is_file()
    _tool(page, "mcp_config")
    expect(page.get_by_role("button", name=t("mcp_config.unpin") + " Zed", exact=True)).to_be_visible()
    expect(page.get_by_text(t("mcp_config.after_copy"), exact=True)).to_be_visible()
    page.screenshot(path=str(service["path"] / "mcp-pinned-client.png"))

    # 浏览器中只模拟桥返回；真正的路径与同源校验由离线 native API 用例覆盖。
    chosen = str(service["path"] / "native-picked-export")
    page.evaluate("""chosen => {window.pywebview = {api:{
        choose_directory: async () => chosen,
        open_directory: async (path, exported) => { window.openedExport = {path, exported}; return true; }
    }};}""", chosen)
    _tool(page, "skills")
    page.get_by_role("button", name=t("skills.choose_directory"), exact=True).click()
    expect(page.get_by_role("textbox", name=t("skills.export_dir"), exact=True)).to_have_value(chosen)
    page.get_by_role("button", name=t("skills.export_current"), exact=True).click()
    expect(page.locator(".skill-export-result")).to_contain_text(t("skills.exported", count=1, dir=chosen))
    page.get_by_role("button", name=t("skills.open_directory"), exact=True).click()
    page.wait_for_function("window.openedExport !== undefined")
    assert page.evaluate("window.openedExport") == {"path": chosen, "exported": ["piece-search"]}


def test_logs_text_filters_copy_and_navigation_memory(app_page):
    page, service = app_page
    expect = playwright.expect
    _tool(page, "logs")
    query = page.get_by_role("textbox", name=t("logs.search"), exact=True)
    module = page.get_by_role("textbox", name=t("logs.module_filter"), exact=True)
    query.fill("no-log-can-match-this-value")
    expect(page.get_by_text(t("logs.no_match"), exact=True)).to_be_visible()
    query.fill("")
    expect(page.get_by_role("button", name=t("logs.copy_entry"), exact=True).first).to_be_visible()
    page.evaluate("""Object.defineProperty(navigator, 'clipboard', {configurable:true,
        value:{writeText: async value => { window.copiedLog = value; }}})""")
    page.get_by_role("button", name=t("logs.copy_entry"), exact=True).first.click()
    expect(page.get_by_text(t("logs.copied", count=1), exact=True)).to_be_visible()
    assert page.evaluate("window.copiedLog.length") > 0
    module.fill("app")
    _tool(page, "mcp_config")
    _tool(page, "logs")
    expect(module).to_have_value("app")
    page.set_viewport_size({"width": 1000, "height": 760})
    assert page.locator(".workspace-content").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    page.screenshot(path=str(service["path"] / "logs-filtered-narrow.png"))
