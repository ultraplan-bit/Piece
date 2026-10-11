"""真实 Edge 中验收 Zotero 工作区；仅操作临时库与 MockTransport Zotero。"""

import re

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401
from test_settings_tools_browser import app_page, _setting, _tool, _saved  # noqa: F401
from app.i18n import t


@pytest.mark.parametrize("gui_service", ["zotero"], indirect=True)
def test_zotero_browser_read_import_and_confirmed_note(app_page):
    page, service = app_page
    expect = playwright.expect
    _tool(page, "zotero")
    expect(page.locator("[data-zotero-browser]")).to_be_visible()
    expect(page.locator("[data-zotero-detail]")).to_be_visible()
    page.get_by_role("button", name=t("zotero.connect"), exact=True).click()
    row = page.locator(".zotero-item").filter(has_text="Zotero 浏览验收")
    expect(row).to_be_visible()
    expect(page.locator(".zotero-item").filter(has_text="没有 PDF 的文献")).to_be_visible()
    row.get_by_role("button").click()
    detail = page.locator("[data-zotero-detail]")
    expect(detail.get_by_text("书目摘要不是论文全文", exact=True)).to_be_visible()
    expect(detail.get_by_role("button", name=t("zotero.edit_metadata"), exact=True)).to_be_disabled()

    detail.get_by_role("button", name=t("zotero.children"), exact=True).click()
    detail.get_by_role("button", name="用户的阅读笔记", exact=True).click()
    expect(detail.get_by_text("用户的阅读笔记", exact=True).last).to_be_visible()
    assert page.evaluate("typeof window.zoteroUnsafe === 'undefined'")
    detail.get_by_role("button", name=t("zotero.parent"), exact=True).click()
    detail.get_by_role("button", name=t("zotero.children"), exact=True).click()
    detail.get_by_role("button", name="主文档 PDF", exact=True).click()
    detail.get_by_role("button", name=t("zotero.children"), exact=True).click()
    expect(detail.get_by_role("button", name="选定高亮文本", exact=True)).to_be_visible()
    detail.get_by_role("button", name=t("zotero.fulltext"), exact=True).click()
    expect(detail.get_by_text("可用的索引片段", exact=True)).to_be_visible()
    expect(detail.get_by_text(re.compile(t("zotero.incomplete")))).to_be_visible()
    page.screenshot(path=str(service["path"] / "zotero-read.png"))

    row.get_by_role("checkbox").click()
    page.locator("[data-zotero-browser]").get_by_role("button", name=t("zotero.preview_import"), exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_role("checkbox")).to_be_checked()
    dialog.get_by_role("button", name=t("zotero.import_selected"), exact=True).click()
    expect(dialog.get_by_text(t("zotero.accepted_hint"), exact=True)).to_be_visible()
    for _ in range(40):
        dialog.get_by_role("button", name=t("zotero.refresh_status"), exact=True).click()
        if t("file_status.indexed") in dialog.inner_text():
            break
        page.wait_for_timeout(250)
    expect(dialog).to_contain_text(t("file_status.indexed"))
    page.screenshot(path=str(service["path"] / "zotero-import-indexed.png"))
    dialog.get_by_role("button", name=t("knowledge.close"), exact=True).click()

    page.get_by_role("button", name=t("zotero.settings"), exact=True).click()
    expect(page.locator(".settings-nav-active")).to_contain_text(t("zotero.title"))
    original = _saved(service)
    page.get_by_role("switch", name=t("zotero.enable_write"), exact=True).click()
    page.get_by_role("switch", name=t("zotero.enable_mcp"), exact=True).click()
    page.locator("[data-settings-save]").click()
    expect(dialog).to_contain_text(t("zotero.settings_confirm"))
    assert not _saved(service)["zotero_write_enabled"]
    dialog.get_by_role("button", name=t("settings.save_group"), exact=True).click()
    expect(page.locator("[data-settings-save]")).to_be_disabled()
    saved = _saved(service)
    assert saved["zotero_write_enabled"] and saved["mcp"]["zotero_enabled"]
    assert saved["mcp"]["port"] == original["mcp"]["port"]
    assert saved["mcp"]["api_key"] == original["mcp"]["api_key"]

    _tool(page, "zotero")
    page.get_by_role("button", name=t("zotero.actions"), exact=True).click()
    page.get_by_role("menuitem", name=t("zotero.create_note"), exact=True).click()
    dialog.get_by_role("textbox", name=t("zotero.note_content"), exact=True).fill("浏览器验收总结")
    dialog.get_by_role("button", name=t("zotero.preview_changes"), exact=True).click()
    dialog = page.get_by_role("dialog").filter(has_text=t("zotero.plan_hint"))
    expect(dialog).to_contain_text(t("zotero.plan_hint"))
    expect(dialog.get_by_role("button", name=t("zotero.apply"), exact=True)).to_be_disabled()
    dialog.get_by_role("button", name=t("zotero.authorize"), exact=True).click()
    expect(page.get_by_text(t("zotero.authorized"), exact=True)).to_be_visible()
    dialog.get_by_role("checkbox", name=t("zotero.confirm_plan"), exact=True).click()
    dialog.get_by_role("button", name=t("zotero.apply"), exact=True).click()
    expect(dialog.get_by_text(t("zotero.write_counts", success=1, unchanged=0, failed=0, pending=0), exact=True)).to_be_visible()
    page.screenshot(path=str(service["path"] / "zotero-write-verified.png"))
    dialog.get_by_role("button", name=t("knowledge.close"), exact=True).click()

    _tool(page, "mcp_config")
    page.get_by_role("button", name="Cursor", exact=True).click()
    read = page.get_by_role("checkbox", name=t("mcp_config.service_kb"), exact=True)
    zotero = page.get_by_role("checkbox", name=t("mcp_config.service_zotero"), exact=True)
    read.click()
    expect(read).not_to_be_checked()
    zotero.click()
    expect(zotero).to_be_checked()
    expect(page.locator("pre").first).to_contain_text("piece-zotero")
    expect(page.locator("pre").first).not_to_contain_text("piece-kb")
    expect(page.locator("pre").first).not_to_contain_text("piece-index")
    _tool(page, "skills")
    expect(page.get_by_text("piece-zotero", exact=True).first).to_be_visible()

    page.locator("[data-open-settings]").click()
    _setting(page, t("settings.appearance"))
    page.get_by_role("button", name=t("settings_appearance.theme_dark"), exact=True).click()
    _tool(page, "zotero")
    expect(page.locator("body")).to_have_class(re.compile("body--dark"))
    page.set_viewport_size({"width": 1000, "height": 760})
    assert page.locator("[data-zotero-browser]").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    assert page.locator("[data-zotero-detail]").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    page.screenshot(path=str(service["path"] / "zotero-dark.png"))
