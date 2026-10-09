"""真实 Edge 验收设置和工具；只使用临时服务与目录，不连接外部服务。"""
import json
import re
from urllib.parse import urlsplit

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401
from app.i18n import t


@pytest.fixture
def app_page(gui_service):
    service = gui_service
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 960},
                                      http_credentials={"username": "piece", "password": service["key"]})
        context.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).hostname in
                      {"127.0.0.1", "localhost"} else route.abort())
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(service["url"])
            playwright.expect(page.locator(".library-row").first).to_be_visible(timeout=30000)
            yield page, service
            assert not errors, errors
        finally:
            page.screenshot(path=str(service["path"] / "tools-last.png"))
            print("Settings/tools screenshots:", service["path"])
            browser.close()


def _setting(page, label):
    page.locator(".settings-nav-item").filter(has_text=label).click()


def _tool(page, view):
    page.locator(".app-tools").click()
    page.get_by_role("menuitem", name=t(f"sidebar.{view}"), exact=True).click()
    playwright.expect(page.locator(".q-menu")).not_to_be_visible()


def _saved(service):
    return json.loads((service["path"] / "config.json").read_text(encoding="utf-8"))


def test_settings_drafts_group_save_restart_and_keyboard(app_page):
    page, service = app_page
    expect = playwright.expect
    original = _saved(service)
    page.keyboard.press("Control+,")
    expect(page.locator(".settings-workspace")).to_be_visible()
    expect(page.locator(".settings-nav-active")).to_contain_text(t("settings.appearance"))

    _setting(page, t("settings.embedding"))
    address = page.get_by_role("textbox", name=t("settings_embedding.base_url"), exact=True)
    address.fill("unfinished-address")
    address.press("Tab")
    expect(page.get_by_text(t("settings.invalid_url"), exact=True)).to_be_visible()
    expect(page.locator("[data-settings-save]")).to_be_enabled()
    assert page.evaluate("""() => {
        const event = new Event('beforeunload', {cancelable: true});
        window.dispatchEvent(event); return event.defaultPrevented;
    }""")

    # 另一页保存只提交它自己的字段，既不校验也不保存嵌入页的半成品。
    _setting(page, t("settings.mcp"))
    page.get_by_role("switch", name=t("settings_mcp.include_images"), exact=True).click()
    page.keyboard.press("Control+s")
    expect(page.locator(".settings-save-bar").get_by_text(t("settings.saved_group"), exact=True)).to_be_visible()
    expect(page.locator("[data-settings-save]")).to_be_disabled()
    assert _saved(service)["mcp"]["include_images_default"] is True
    assert _saved(service)["embedding"] == original["embedding"]
    expect(page.get_by_text(t("settings.other_drafts", count=1), exact=True)).to_be_visible()

    # 通用偏好立即保存，但不会顺带提交其他分类。
    _setting(page, t("settings.appearance"))
    page.get_by_role("button", name=t("settings_appearance.theme_dark"), exact=True).click()
    expect(page.locator("body")).to_have_class(re.compile("body--dark"))
    assert _saved(service)["appearance"]["theme"] == "dark"
    assert _saved(service)["embedding"] == original["embedding"]

    page.locator(".workspace-primary").get_by_role("button", name=t("sidebar.files"), exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text(t("settings.discard_title"))
    dialog.get_by_role("button", name=t("settings.keep_editing"), exact=True).click()
    expect(page.locator(".settings-workspace")).to_be_visible()
    _setting(page, t("settings.embedding"))
    expect(address).to_have_value("unfinished-address")
    page.get_by_role("button", name=t("settings.revert_group"), exact=True).click()
    expect(address).to_have_value(original["embedding"]["base_url"])
    expect(page.locator("[data-settings-save]")).to_be_disabled()

    # 密钥/端口的提交有确认；保存不假装当前进程已经生效。
    _setting(page, t("settings.mcp"))
    port = page.get_by_role("spinbutton", name=t("settings_mcp.retrieval_port"), exact=True)
    port.fill(str(original["mcp"]["port"] + 10))
    page.locator("[data-settings-save]").click()
    expect(dialog).to_contain_text(t("settings_mcp.apply_title"))
    dialog.get_by_role("button", name=t("settings.save_group"), exact=True).click()
    expect(page.locator(".settings-effect-note")).to_contain_text(t("settings.effect_mcp"))
    expect(page.locator("[data-settings-save]")).to_be_disabled()
    page.screenshot(path=str(service["path"] / "settings-dark-mcp.png"))

    _tool(page, "logs")
    page.locator("[data-open-settings]").click()
    expect(page.locator(".settings-effect-note")).to_contain_text(t("settings.effect_mcp"))
    expect(port).to_have_value(str(original["mcp"]["port"] + 10))

    _setting(page, t("settings_ocr.title"))
    page.get_by_role("button", name=t("settings_ocr.provider_mineru"), exact=True).click()
    page.set_viewport_size({"width": 1000, "height": 760})
    expect(page.get_by_text(t("settings_ocr.third_party_warning"), exact=True)).to_be_visible()
    assert page.locator(".settings-workspace").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    page.screenshot(path=str(service["path"] / "settings-narrow-parser.png"))
    page.get_by_role("button", name=t("settings.revert_group"), exact=True).click()

    # 放弃整页草稿是明确动作；普通分类切换本身不会丢草稿。
    _setting(page, t("settings.embedding"))
    address.fill("discard-this-draft")
    page.locator(".workspace-primary").get_by_role("button", name=t("sidebar.files"), exact=True).click()
    dialog.get_by_role("button", name=t("settings.discard"), exact=True).click()
    expect(page.locator(".settings-workspace")).to_have_count(0)
    page.locator("[data-open-settings]").click()
    expect(address).to_have_value(original["embedding"]["base_url"])
    page.set_viewport_size({"width": 1440, "height": 960})
    page.screenshot(path=str(service["path"] / "settings-embedding.png"))


def test_integration_export_logs_and_sync_navigation(app_page):
    page, service = app_page
    expect = playwright.expect
    _tool(page, "mcp_config")
    search = page.get_by_placeholder(t("mcp_config.search_placeholder"), exact=True)
    search.fill("cursor")
    page.get_by_role("button", name="Cursor", exact=True).click()
    read = page.get_by_role("checkbox", name=t("mcp_config.service_kb"), exact=True)
    write = page.get_by_role("checkbox", name=t("mcp_config.service_index"), exact=True)
    expect(read).to_be_checked()
    expect(write).not_to_be_checked()
    expect(page.locator("pre").first).to_contain_text("piece-kb")
    expect(page.locator("pre").first).not_to_contain_text("piece-index")
    read.click()
    expect(page.get_by_role("button", name=t("mcp_config.copy_btn"), exact=True)).to_be_disabled()
    read.click()
    write.click()
    expect(page.locator("pre").first).to_contain_text("piece-index")
    page.screenshot(path=str(service["path"] / "mcp-client.png"))

    # 浏览器拒绝剪贴板时显示失败，不产生未处理的 JS 异常或成功提示。
    page.evaluate("""Object.defineProperty(navigator, 'clipboard', {configurable: true,
        value: {writeText: () => Promise.reject(new Error('test clipboard denied'))}})""")
    page.get_by_role("button", name=t("mcp_config.copy_btn"), exact=True).click()
    expect(page.get_by_text(t("mcp_config.copy_failed"), exact=True)).to_be_visible()
    page.get_by_role("button", name=t("mcp_config.open_settings"), exact=True).click()
    expect(page.locator(".settings-nav-active")).to_contain_text(t("settings.mcp"))
    _tool(page, "mcp_config")
    expect(search).to_have_value("cursor")
    expect(write).to_be_checked()

    _tool(page, "skills")
    page.get_by_role("button", name=re.compile("^piece-search")).click()
    expect(page.get_by_text(t("skills.export_target", target="piece-search"), exact=True)).to_be_visible()
    directory = page.get_by_role("textbox", name=t("skills.export_dir"), exact=True)
    target = service["path"] / "exported-skills"
    directory.fill(str(target))
    page.get_by_role("button", name=t("skills.export_current"), exact=True).click()
    result = page.locator(".skill-export-result")
    expect(result).to_contain_text(t("skills.exported", count=1, dir=str(target)))
    exported = target / "piece-search" / "SKILL.md"
    assert exported.is_file()
    exported.write_text("user-edited test content", encoding="utf-8")
    page.get_by_role("button", name=t("skills.export_current"), exact=True).click()
    expect(result).to_contain_text(t("skills.result_exists", count=1))
    other = service["path"] / "not-confirmed"
    directory.fill(str(other))
    page.get_by_role("button", name=t("skills.overwrite_action"), exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text(str(target))
    dialog.get_by_role("button", name=t("skills.overwrite_btn"), exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(result).to_contain_text(t("skills.exported", count=1, dir=str(target)))
    assert exported.read_text(encoding="utf-8") != "user-edited test content"
    assert not other.exists()
    page.screenshot(path=str(service["path"] / "skill-export.png"))

    _tool(page, "logs")
    expect(page.locator(".results-splitter > .q-splitter__before")).not_to_be_visible()
    auto_refresh = page.get_by_role("switch", name=t("logs.auto_refresh"), exact=True)
    auto_refresh.click()
    expect(auto_refresh).to_be_checked()
    page.locator("[data-open-settings]").click()
    _tool(page, "logs")
    expect(auto_refresh).to_be_checked()
    page.screenshot(path=str(service["path"] / "logs-workspace.png"))

    page.locator(".app-header").get_by_role("button", name=t("sidebar.cloud_sync"), exact=True).click()
    expect(page.get_by_text(t("cloud_sync.strategy"), exact=True)).to_be_visible()
    page.screenshot(path=str(service["path"] / "sync-workspace.png"))
    page.get_by_role("button", name=t("cloud_sync.go_settings"), exact=True).click()
    expect(page.locator(".settings-nav-active")).to_contain_text(t("settings.webdav"))

    _tool(page, "recall_test")
    query = page.get_by_role("textbox", name=t("recall_test.query"), exact=True)
    query.fill("保留这次诊断查询")
    _tool(page, "logs")
    _tool(page, "recall_test")
    expect(query).to_have_value("保留这次诊断查询")
    page.screenshot(path=str(service["path"] / "recall-workspace.png"))


def test_english_settings_are_saved_and_render_after_refresh(app_page):
    page, service = app_page
    expect = playwright.expect
    page.locator("[data-open-settings]").click()
    page.get_by_role("combobox", name=t("settings_appearance.language"), exact=True).click()
    page.get_by_role("option", name="English", exact=True).click()
    refresh = page.get_by_role("button", name=t("settings.refresh_interface"), exact=True)
    expect(refresh).to_be_visible()
    assert _saved(service)["appearance"]["language"] == "en"
    refresh.click()
    expect(page.locator(".library-row").first).to_be_visible(timeout=30000)
    page.locator("[data-open-settings]").click()
    expect(page.locator(".settings-nav-active")).to_contain_text("General")
    page.screenshot(path=str(service["path"] / "settings-english-general.png"))
    _setting(page, "Embedding & Indexing")
    expect(page.get_by_role("button", name="Test current input", exact=True)).to_be_visible()
    expect(page.get_by_role("textbox", name="API Base URL", exact=True)).to_be_visible()
    assert not re.search(r"\bsettings[._]\w+", page.locator(".settings-workspace").inner_text())
    page.screenshot(path=str(service["path"] / "settings-english-embedding.png"))
