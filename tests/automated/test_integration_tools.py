"""工具页（MCP 配置 / Skill 导出）交互合同。

覆盖：
- MCP：客户端搜索与状态保留、服务勾选与应用服务跨客户端保留、未选服务禁用复制、
  官方文档入口、复制真实反馈、打开 MCP 服务设置回调、服务端点/认证与三者区分、
  列表为可键盘操作按钮。
- Skill：去大卡片、默认导出目录显示与保存一致、busy 防重复提交（含输入禁用）、
  导出结果持久可见（含目标/数量/复制路径）、覆盖确认冻结目录且由持久入口触发、
  面板刷新指向最新渲染、复制前缀真实反馈、超时按失败处理。

沿用 test_import_dialogs 的无浏览器渲染套路，依赖 knowledge_base 隔离设置。
"""

import asyncio
import json
import sys
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# 优先导入本仓库源码（避免 editable 安装指向其他工作树）
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from test_import_dialogs import _buttons, _click, _find, _labels, _run


@pytest.fixture
def page(knowledge_base):
    from nicegui import ui
    with ui.column() as container:
        yield container
    container.delete()


def _set_value(element, value):
    """设置控件值并触发其 on_value_change（在事件循环内执行）。"""
    async def fire():
        element.value = value
    return fire


# ==================== MCP 配置 ====================


def test_mcp_client_search_filters_list(page):
    from app.ui.views import mcp_config_view as mcp

    refs = {}
    mcp.render_mcp_config_middle({"value": None}, refs, lambda client_id: None)
    assert {"Cursor", "Zed"} <= set(_labels(page))

    search = _find(page, "Input")[0]
    _run(_set_value(search, "zed"))
    labels = _labels(page)
    assert "Zed" in labels
    assert "Cursor" not in labels and "VS Code" not in labels

    _run(_set_value(search, "zzz-no-such-client"))
    assert mcp.t("mcp_config.no_match") in _labels(page)


def test_mcp_client_search_state_persists_across_rebuild(page):
    from app.ui.views import mcp_config_view as mcp

    refs = {}
    mcp.render_mcp_config_middle({"value": None}, refs, lambda client_id: None)
    _run(_set_value(_find(page, "Input")[0], "zed"))
    assert refs["client_search"]["value"] == "zed"

    page.clear()
    mcp.render_mcp_config_middle({"value": None}, refs, lambda client_id: None)
    assert _find(page, "Input")[0].value == "zed"
    assert "Zed" in _labels(page) and "Cursor" not in _labels(page)


def test_mcp_client_list_items_are_keyboard_buttons(page):
    from app.ui.views import mcp_config_view as mcp

    calls = []
    mcp.render_mcp_config_middle({"value": "cursor"}, {}, calls.append)

    zed_button = next(b for b in _find(page, "Button") if "Zed" in _labels(b))
    _run(_click(zed_button))
    assert calls == ["zed"]


def test_mcp_service_selection_prunes_json_config(knowledge_base):
    from app.ui.views import mcp_config_view as mcp

    cursor = next(c for c in mcp.MCP_CLIENTS if c["id"] == "cursor")

    both = json.loads(mcp._get_config_json(cursor))["mcpServers"]
    assert set(both) == {"piece-kb", "piece-index"}

    only_kb = json.loads(mcp._get_config_json(cursor, {"piece-kb"}))["mcpServers"]
    assert set(only_kb) == {"piece-kb"}

    only_index = json.loads(mcp._get_config_json(cursor, {"piece-index"}))["mcpServers"]
    assert set(only_index) == {"piece-index"}


def test_mcp_service_selection_prunes_bash_and_toml(knowledge_base):
    from app.ui.views import mcp_config_view as mcp

    bash_client = next(c for c in mcp.MCP_CLIENTS if c["id"] == "claude_code")
    text = mcp._get_config_json(bash_client, {"piece-index"})
    assert "piece-index" in text and "piece-kb" not in text

    toml_client = next(c for c in mcp.MCP_CLIENTS if c["id"] == "openai_codex")
    servers = tomllib.loads(mcp._get_config_json(toml_client, {"piece-kb"}))["mcp_servers"]
    assert set(servers) == {"piece-kb"}


def test_mcp_service_selection_persists_across_clients(page, knowledge_base):
    from app.ui.views import mcp_config_view as mcp

    selected = {"value": "cursor"}
    mcp.render_mcp_config_right(selected)
    assert selected["services"] == {"piece-kb"}

    _run(_set_value(_find(page, "Checkbox")[1], True))
    assert selected["services"] == {"piece-kb", "piece-index"}
    _run(_set_value(_find(page, "Checkbox")[1], False))
    assert selected["services"] == {"piece-kb"}

    # 切换客户端后仍保留用户选择；写入服务不是默认权限。
    page.clear()
    selected["value"] = "zed"
    mcp.render_mcp_config_right(selected)
    content = _find(page, "Code")[0].content
    assert "piece-kb" in content
    assert "piece-index" not in content


def test_mcp_empty_service_selection_disables_copy(page, knowledge_base):
    from app.ui.views import mcp_config_view as mcp

    selected = {"value": "cursor"}
    mcp.render_mcp_config_right(selected)
    _run(_set_value(_find(page, "Checkbox")[0], False))
    _run(_set_value(_find(page, "Checkbox")[1], False))
    assert selected["services"] == set()

    copy_button = next(b for b in _find(page, "Button") if b._props.get("icon") == "content_copy")
    assert copy_button._props.get("disable")
    assert mcp.t("mcp_config.no_service_selected") in _labels(page)


def test_mcp_copy_reports_real_success_and_failure(page, monkeypatch):
    from nicegui import ui
    from app.ui.views import mcp_config_view as mcp

    notices = []
    monkeypatch.setattr(ui, "notify", lambda *a, **k: notices.append(k.get("type")))

    monkeypatch.setattr(mcp.ui, "run_javascript", AsyncMock(return_value=True))
    _run(lambda: mcp._copy_config_text("{}"))
    assert notices[-1] == "positive"

    monkeypatch.setattr(mcp.ui, "run_javascript", AsyncMock(return_value=False))
    _run(lambda: mcp._copy_config_text("{}"))
    assert notices[-1] == "negative"


def test_mcp_write_clipboard_timeout_is_failure(monkeypatch):
    from app.ui.views import mcp_config_view as mcp

    monkeypatch.setattr(mcp.ui, "run_javascript", AsyncMock(side_effect=TimeoutError("no client")))
    assert asyncio.run(mcp._write_clipboard("x")) is False


def test_mcp_docs_map_only_verified_vendor_domains():
    from app.ui.views import mcp_config_view as mcp

    ids = {c["id"] for c in mcp.MCP_CLIENTS}
    assert set(mcp.MCP_CLIENT_DOCS) <= ids
    assert all(url.startswith("https://") for url in mcp.MCP_CLIENT_DOCS.values())

    cursor = next(c for c in mcp.MCP_CLIENTS if c["id"] == "cursor")
    assert mcp._client_docs(cursor) == "https://cursor.com/docs/mcp"

    # 未收录的客户端不猜路径，返回 None 走通用模板说明
    workbuddy = next(c for c in mcp.MCP_CLIENTS if c["id"] == "workbuddy")
    assert mcp._client_docs(workbuddy) is None


def test_mcp_right_docs_link_only_when_known(page, knowledge_base):
    from app.ui.views import mcp_config_view as mcp

    mcp.render_mcp_config_right({"value": "cursor"})
    hrefs = [el._props.get("href") for el in _find(page, "Link")]
    assert "https://cursor.com/docs/mcp" in hrefs
    assert mcp.t("mcp_config.entry_json") in _labels(page)

    page.clear()
    mcp.render_mcp_config_right({"value": "workbuddy"})
    assert _find(page, "Link") == []


def test_mcp_open_settings_callback_sync_and_async(page, knowledge_base):
    from app.ui.views import mcp_config_view as mcp

    calls = []

    def sync_cb():
        calls.append("sync")

    mcp.render_mcp_config_right({"value": "cursor"}, on_open_settings=sync_cb)
    assert mcp.t("mcp_config.open_settings") in _buttons(page)
    _run(_click(_buttons(page)[mcp.t("mcp_config.open_settings")]))
    assert calls == ["sync"]

    page.clear()

    async def async_cb():
        calls.append("async")

    mcp.render_mcp_config_right({"value": "cursor"}, on_open_settings=async_cb)
    _run(_click(_buttons(page)[mcp.t("mcp_config.open_settings")]))
    assert calls == ["sync", "async"]

    # 未提供回调时不渲染入口
    page.clear()
    mcp.render_mcp_config_right({"value": "cursor"})
    assert mcp.t("mcp_config.open_settings") not in _buttons(page)


def test_mcp_right_shows_endpoints_and_distinguishes_states(page, knowledge_base):
    from app.ui.views import mcp_config_view as mcp

    mcp.render_mcp_config_right({"value": "cursor"})
    labels = _labels(page)
    # 服务状态（端点 + 认证）与「不检测客户端连接」的说明都要出现
    assert any("http://localhost:" in text for text in labels)
    assert mcp.t("mcp_config.auth_state", state=mcp.t("mcp_config.auth_on")) in labels
    assert mcp.t("mcp_config.notice") in labels
    assert mcp.t("mcp_config.settings_restart_hint") in labels


def test_mcp_right_has_no_card(page, knowledge_base):
    from app.ui.views import mcp_config_view as mcp

    mcp.render_mcp_config_right({"value": "cursor"})
    assert _find(page, "Card") == []


# ==================== Skill 导出 ====================


def _render_skill(page, skill_id, export_state, monkeypatch, tmp_path):
    from app.ui.views import skill_view

    monkeypatch.setattr(skill_view, "get_default_data_dir", lambda: tmp_path)
    skill_view.render_skill_middle({"value": skill_id}, {}, lambda value: None)
    skill_view.render_skill_right({"value": skill_id}, export_state, port=1234)


def test_skill_panel_has_no_card(page, knowledge_base, monkeypatch, tmp_path):
    _render_skill(page, "piece-index", {"export_dir": ""}, monkeypatch, tmp_path)
    assert _find(page, "Card") == []


def test_skill_list_items_are_keyboard_buttons(page, knowledge_base):
    from app.ui.views import skill_view

    calls = []
    skill_view.render_skill_middle({"value": None}, {}, calls.append)
    _run(_click(_find(page, "Button")[0]))
    assert calls == [skill_view.SKILLS[0]["id"]]


def test_skill_default_export_dir_is_displayed_and_persisted(
    page, knowledge_base, monkeypatch, tmp_path
):
    from app.ui.views import skill_view

    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)

    export_state = {}
    _render_skill(page, "piece-index", export_state, monkeypatch, tmp_path)
    # 显示的值就是保存的值，导出不会再报“未填目录”
    assert export_state["export_dir"] == "~/.claude/skills"
    assert _find(page, "Input")[0].value == "~/.claude/skills"


def test_skill_busy_blocks_duplicate_export(page, knowledge_base, monkeypatch, tmp_path):
    from nicegui import ui
    from app.ui.views import skill_view

    monkeypatch.setattr(ui, "notify", lambda *a, **k: None)
    target = tmp_path / "导出"
    export_state = {"export_dir": str(target), "busy": True}
    _render_skill(page, "piece-index", export_state, monkeypatch, tmp_path)

    button = _buttons(page)[skill_view.t("skills.export_current")]
    assert button._props.get("disable")
    assert button._props.get("loading")
    # 运行中目标输入禁用，防止改到未经确认的目录
    assert _find(page, "Input")[0]._props.get("disable")

    _run(_click(button))
    assert not (target / "piece-index").exists()


def test_skill_export_result_persists_with_target_and_count(
    page, knowledge_base, monkeypatch, tmp_path
):
    from nicegui import ui
    from app.ui.views import skill_view

    monkeypatch.setattr(ui, "notify", lambda *a, **k: None)
    target = tmp_path / "导出"
    export_state = {"export_dir": str(target)}
    _render_skill(page, "piece-index", export_state, monkeypatch, tmp_path)

    _run(_click(_buttons(page)[skill_view.t("skills.export_current")]))

    result = export_state["last_result"]
    assert result and result["ok"] and result["count"] == 1
    assert result["dir"] == str(target)
    exported_text = skill_view.t("skills.exported", count=1, dir=result["dir"])
    assert exported_text in _labels(page)

    # 右栏重建（新容器、同一 export_state）后结果仍然可见
    with ui.column() as second:
        skill_view.render_skill_right({"value": "piece-index"}, export_state, port=1234)
    try:
        assert exported_text in _labels(second)
    finally:
        second.delete()


def test_skill_export_panel_tracks_latest_render(page, knowledge_base, monkeypatch, tmp_path):
    from nicegui import ui
    from app.ui.views import skill_view

    monkeypatch.setattr(ui, "notify", lambda *a, **k: None)
    monkeypatch.setattr(skill_view, "get_default_data_dir", lambda: tmp_path)
    export_state = {"export_dir": str(tmp_path / "out")}
    skill_view.render_skill_right({"value": "piece-index"}, export_state, port=1234)
    first = export_state["_panel"]

    with ui.column() as second:
        skill_view.render_skill_right({"value": "piece-index"}, export_state, port=1234)
    try:
        # 在途导出完成时刷新的应是“最新”面板，而不是旧 closure 的已删目标
        assert export_state["_panel"] is not first
        assert export_state["_panel"].targets
    finally:
        second.delete()


def test_skill_overwrite_entry_freezes_confirmed_dir(
    page, knowledge_base, monkeypatch, tmp_path
):
    from nicegui import ui
    from app.ui.views import skill_view

    monkeypatch.setattr(ui, "notify", lambda *a, **k: None)
    confirm_calls = []
    monkeypatch.setattr(skill_view, "confirm_dialog", lambda **kwargs: confirm_calls.append(kwargs))

    target = tmp_path / "out"
    (target / "piece-index").mkdir(parents=True)
    existing = target / "piece-index" / "SKILL.md"
    existing.write_text("原有内容", encoding="utf-8")

    export_state = {"export_dir": str(target)}
    _render_skill(page, "piece-index", export_state, monkeypatch, tmp_path)
    _run(_click(_buttons(page)[skill_view.t("skills.export_current")]))

    result = export_state["last_result"]
    assert result["exported"] == [] and result["exists"] == ["piece-index"]
    assert existing.read_text(encoding="utf-8") == "原有内容"
    # 覆盖不在导出事件里自动弹窗，而是冻结目录+清单，交由结果区按钮确认
    assert confirm_calls == []
    assert export_state["pending_overwrite"] == {
        "dir": str(target), "exists": ["piece-index"],
    }

    # 用户改了输入目录后再确认，也必须覆盖“最初确认”的目录
    export_state["export_dir"] = str(tmp_path / "other")
    _run(_click(_buttons(page)[skill_view.t("skills.overwrite_action")]))
    expected_message = skill_view.t(
        "skills.overwrite_confirm_message", dir=str(target), names="piece-index"
    )
    assert confirm_calls and confirm_calls[0]["message"] == expected_message

    _run(confirm_calls[0]["on_confirm"])
    assert existing.read_text(encoding="utf-8") != "原有内容"
    assert not (tmp_path / "other" / "piece-index").exists()
    assert export_state["pending_overwrite"] is None


def test_skill_prefix_copy_reports_result(page, knowledge_base, monkeypatch, tmp_path):
    from nicegui import ui
    from app.ui.views import skill_view

    notices = []
    monkeypatch.setattr(ui, "notify", lambda *a, **k: notices.append(k.get("type")))
    monkeypatch.setattr(ui, "run_javascript", AsyncMock(return_value=True))
    _render_skill(page, "piece-index", {"export_dir": ""}, monkeypatch, tmp_path)

    copy_button = next(
        b for b in _find(page, "Button") if b._props.get("icon") == "content_copy"
    )
    _run(_click(copy_button))
    assert notices[-1] == "positive"

    monkeypatch.setattr(ui, "run_javascript", AsyncMock(return_value=False))
    _run(_click(copy_button))
    assert notices[-1] == "negative"


def test_skill_write_clipboard_timeout_is_failure(monkeypatch):
    from app.ui.views import skill_view

    monkeypatch.setattr(skill_view.ui, "run_javascript", AsyncMock(side_effect=TimeoutError))
    assert asyncio.run(skill_view._write_clipboard("x")) is False
