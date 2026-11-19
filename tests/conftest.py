"""回归测试的公共隔离、解析资源和可选测试入口。"""

import importlib.util

import pytest


def pytest_addoption(parser):
    parser.addoption("--run-slow", action="store_true", help="运行大数据量分页和性能回归")
    parser.addoption("--run-browser", action="store_true", help="收集浏览器验收（需要 Playwright 和 Windows Edge）")
    parser.addoption("--run-external", action="store_true", help="收集需要联网构建和安装的验收")


def pytest_configure(config):
    if config.getoption("--run-browser") and importlib.util.find_spec("playwright") is None:
        raise pytest.UsageError("浏览器验收需要先运行 uv sync --frozen --extra desktop --group browser")


def pytest_ignore_collect(collection_path, config):
    name = collection_path.name
    if name.endswith("_browser.py") and not config.getoption("--run-browser"):
        return True
    if name == "test_install.py" and not config.getoption("--run-external"):
        return True
    return None


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.path.name.endswith("_browser.py"):
            item.add_marker(pytest.mark.browser)
            if not config.getoption("--run-browser"):
                item.add_marker(pytest.mark.skip(reason="需要 --run-browser"))
        if item.path.name == "test_install.py":
            item.add_marker(pytest.mark.external)
            if not config.getoption("--run-external"):
                item.add_marker(pytest.mark.skip(reason="需要 --run-external"))
        if "slow" in item.keywords and not config.getoption("--run-slow"):
            item.add_marker(pytest.mark.skip(reason="需要 --run-slow"))


@pytest.fixture(autouse=True)
def isolated_entry_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PIECE_DATA_DIR", str(tmp_path / "uninitialized"))
    monkeypatch.setenv("NICEGUI_STORAGE_PATH", str(tmp_path / "gui-storage"))
    monkeypatch.delenv("PIECE_API_KEY", raising=False)


@pytest.fixture
def parser_helpers():
    from indexing.services import parser_helper

    parser_helper.start_parser_helpers(2)
    try:
        yield parser_helper
    finally:
        parser_helper.stop_parser_helpers()
