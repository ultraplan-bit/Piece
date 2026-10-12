"""真实 Edge 回归：跨工作区切换三种主题，不残留上一主题的选中底色。"""
import re
from urllib.parse import urlsplit

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from test_collection_browser import gui_service  # noqa: F401
from app.i18n import t
from app.ui.styles import PINK_PRIMARY


@pytest.mark.parametrize("initial_theme", ["light", "dark", "pink"])
def test_theme_changes_clear_previous_palette_after_navigation(gui_service, initial_theme):
    service = gui_service
    expect = playwright.expect
    selected_colors = {"light": "rgb(238, 241, 243)", "dark": "rgb(54, 59, 66)", "pink": "rgb(238, 241, 243)"}
    with playwright.sync_playwright() as automation:
        browser = automation.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900},
                                      http_credentials={"username": "piece", "password": service["key"]})
        context.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).hostname in
                      {"127.0.0.1", "localhost"} else route.abort())
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def choose_theme(theme):
            page.locator("[data-open-settings]").click()
            page.locator(".settings-nav-item").filter(has_text=t("settings.appearance")).click()
            page.get_by_role("button", name=t(f"settings_appearance.theme_{theme}"), exact=True).click()
            expect(page.locator("body")).to_have_css("--q-primary", PINK_PRIMARY if theme == "pink" else "#5898d4")
            page.wait_for_function("dark => document.body.classList.contains('body--dark') === dark", arg=theme == "dark")

        def assert_palette(theme):
            body = page.locator("body")
            if theme == "pink":
                expect(body).to_have_class(re.compile("theme-pink"))
            else:
                expect(body).not_to_have_class(re.compile("theme-pink"))
            if theme == "dark":
                expect(body).to_have_class(re.compile("body--dark"))
            else:
                expect(body).not_to_have_class(re.compile("body--dark"))
            expect(body).to_have_css("--q-primary", PINK_PRIMARY if theme == "pink" else "#5898d4")
            expect(page.locator(".wiki-workbench .mode-active")).to_have_css("background-color", selected_colors[theme])

        try:
            page.goto(service["url"])
            expect(page.locator(".library-catalog")).to_be_visible(timeout=30000)
            if initial_theme != "light":
                choose_theme(initial_theme)
                page.reload()
                expect(page.locator(".library-catalog")).to_be_visible(timeout=30000)

            for index, theme in enumerate(("pink", "light", "pink", "dark", "light")):
                choose_theme(theme)
                # 必须离开设置页：旧实现会连同设置子树删掉 body 的 query 控制元素。
                page.locator(".workspace-primary").get_by_role("button", name=t("sidebar.wiki"), exact=True).click()
                expect(page.locator(".wiki-workbench")).to_be_visible()
                assert_palette(theme)
                page.screenshot(path=str(service["path"] / f"theme-{index}-{theme}.png"))
                page.locator(".workspace-primary").get_by_role("button", name=t("sidebar.graph"), exact=True).click()
                expect(page.locator(".workspace-content .workspace-toolbar .mode-active").first).to_have_css(
                    "background-color", selected_colors[theme])
            assert not errors, errors
        finally:
            page.screenshot(path=str(service["path"] / "theme-last.png"))
            print("Theme screenshots:", service["path"])
            browser.close()
