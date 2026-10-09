"""主题控制元素应跟随页面存活，而不是随设置视图销毁。"""

import pytest


@pytest.mark.parametrize("initial_theme", ["light", "dark", "pink"])
def test_body_theme_query_survives_settings_view_changes(initial_theme):
    from nicegui import Client, ui
    from app.ui.styles import apply_theme, init_theme

    client = Client(ui.page("/_test_theme_lifetime"))
    try:
        with client:
            dark = ui.dark_mode()
            init_theme(dark, initial_theme)
            for theme in ("pink", "light", "dark", "pink", "light"):
                with ui.column() as settings_view:
                    apply_theme(dark, theme)
                body = ui.query("body").element
                assert ("theme-pink" in body._props["classes"]) == (theme == "pink")
                assert dark.value == (theme == "dark")
                settings_view.delete()
                assert not body.is_deleted
                assert ui.query("body").element is body
    finally:
        client.delete()
