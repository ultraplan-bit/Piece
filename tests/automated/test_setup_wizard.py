"""交互初始化只操作临时配置，不连接模型服务。"""

import io
import json
import sys

import pytest

from app import cli, setup_wizard
from app.client import ClientError
from app.platform import InstanceBusyError, database_lock
from indexing import settings
from indexing.services import config_service
from indexing.services.errors import BusinessError


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    directory = tmp_path / "初始化 配置"
    monkeypatch.setenv("PIECE_DATA_DIR", str(directory))
    monkeypatch.setattr(settings, "DEFAULT_DATA_PATH", directory)
    monkeypatch.setattr(settings, "_settings", None)
    return directory / "config.json"


def terminal(monkeypatch, text, key=""):
    stdin = io.StringIO(text)
    monkeypatch.setattr(stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(setup_wizard.getpass, "getpass", lambda *a, **kw: key)


def test_interactive_cli_saves_atomically_and_redacts(config_path, monkeypatch, capsys):
    terminal(monkeypatch, "\n\n2\nyes\n", "never-print-this-key")
    code = cli.main(["config", "init", "--interactive", "--data-dir", str(config_path.parent), "--json"])
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert code == 0 and result["success"]
    assert "never-print-this-key" not in output.out + output.err
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["embedding"]["api_key"] == "never-print-this-key"
    assert saved["embedding"]["vector_dim"] == 2
    keys = {saved["api"]["admin_key"], saved["mcp"]["api_key"], saved["mcp"]["index_api_key"]}
    assert len(keys) == 3 and all(len(key) == 32 for key in keys)
    assert not (config_path.parent / "kb.db").exists()


def test_non_tty_is_rejected_without_writes(config_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("yes\n"))
    with pytest.raises(ClientError) as caught:
        setup_wizard.initialize_interactively()
    assert caught.value.code == "INTERACTIVE_REQUIRED"
    assert not config_path.parent.exists()


@pytest.mark.parametrize("text", ["", "\n\n\nno\n", "\n\n\n"])
def test_cancellation_and_eof_do_not_initialize(config_path, monkeypatch, text):
    terminal(monkeypatch, text)
    with pytest.raises(ClientError) as caught:
        setup_wizard.initialize_interactively()
    assert caught.value.code == "CONFIG_CANCELLED"
    assert not config_path.parent.exists()


def test_hidden_input_unavailable_does_not_save(config_path, monkeypatch):
    terminal(monkeypatch, "\n\n\nyes\n")
    def unavailable(*args, **kwargs):
        raise setup_wizard.getpass.GetPassWarning("no hidden terminal")
    monkeypatch.setattr(setup_wizard.getpass, "getpass", unavailable)
    with pytest.raises(ClientError) as caught:
        setup_wizard.initialize_interactively()
    assert caught.value.code == "INTERACTIVE_REQUIRED" and not config_path.exists()


def test_existing_settings_and_credentials_are_preserved(config_path, monkeypatch):
    config_service.initialize_config()
    config_service.update_config({"embedding": {"api_key": "keep-secret"}, "appearance": {"theme": "dark"}}, offline=True)
    before = json.loads(config_path.read_text(encoding="utf-8"))
    terminal(monkeypatch, "\n\n\nyes\n")
    setup_wizard.initialize_interactively()
    assert json.loads(config_path.read_text(encoding="utf-8")) == before


def test_invalid_url_and_dimension_can_be_corrected(config_path, monkeypatch, capsys):
    terminal(monkeypatch, "invalid-url\nhttps://example.invalid/v1\n\n0\n2\nyes\n")
    setup_wizard.initialize_interactively()
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["embedding"]["base_url"] == "https://example.invalid/v1"
    assert saved["embedding"]["vector_dim"] == 2
    assert capsys.readouterr().err.count("输入无效") == 2


def test_concurrent_update_is_not_overwritten(config_path, monkeypatch):
    config_service.initialize_config()
    terminal(monkeypatch, "\n\n\nyes\n")
    real_readline = setup_wizard._readline
    def concurrent(prompt):
        if prompt.startswith("保存"):
            config_service.update_config({"appearance": {"theme": "dark"}}, offline=True)
        return real_readline(prompt)
    monkeypatch.setattr(setup_wizard, "_readline", concurrent)
    with pytest.raises(BusinessError) as caught:
        setup_wizard.initialize_interactively()
    assert caught.value.code == "CONFIG_CHANGED"
    assert json.loads(config_path.read_text(encoding="utf-8"))["appearance"]["theme"] == "dark"


def test_live_config_lock_rejects_interactive_save(config_path, monkeypatch):
    config_service.initialize_config()
    before = config_path.read_bytes()
    terminal(monkeypatch, "\n\n\nyes\n", "new-secret")
    with database_lock(config_path):
        with pytest.raises(InstanceBusyError) as caught:
            setup_wizard.initialize_interactively()
    assert caught.value.code == "INSTANCE_BUSY"
    assert config_path.read_bytes() == before


def test_interactive_setup_keeps_existing_model_guard(knowledge_base, monkeypatch):
    from indexing.services import chunk_service, file_service
    file_id = file_service.create_empty_file("已有知识")["file_id"]
    chunk_service.create_chunk_add_task(file_id, "标题", "正文")
    knowledge_base.drain()
    path = settings._get_config_file_path()
    before = path.read_bytes()
    terminal(monkeypatch, "https://example.invalid/v1\n\n\nyes\n")
    with pytest.raises(BusinessError) as caught:
        setup_wizard.initialize_interactively()
    assert caught.value.code == "REINDEX_REQUIRED"
    assert path.read_bytes() == before
