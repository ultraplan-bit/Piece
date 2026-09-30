"""后台启动合同：模拟异常路径，以及临时目录里的真实服务起停。"""

import subprocess
import sys
from unittest.mock import Mock

import pytest

from app import background
from app.client import ClientError, ConnectionFailure, PieceClient, make_envelope


@pytest.fixture
def startup(tmp_path, monkeypatch):
    client = PieceClient(port=8689, config_dir=tmp_path, db_path=tmp_path / "kb.db", api_key="test-key")
    status = {"ready": True, "with_gui": False, "with_mcp": False, "components": {}}
    monkeypatch.setattr(client, "handshake", Mock(return_value=make_envelope(True, "identity", {})))
    monkeypatch.setattr(client, "get", Mock(side_effect=lambda *a: make_envelope(True, "status", status)))
    monkeypatch.setattr(client, "post", Mock(return_value=make_envelope(True, "ok")))
    process = Mock(pid=321, returncode=None)
    process.poll.return_value = None
    process.wait.return_value = 0
    spawn = Mock(return_value=process)
    monkeypatch.setattr(background, "_spawn", spawn)
    monkeypatch.setattr(background, "_port_occupied", lambda client: False)
    return client, status, process, spawn


def test_existing_service_is_reused_without_spawning(startup):
    client, status, process, spawn = startup
    result = background.start_service(client)
    assert result["success"] and result["data"]["already_running"]
    spawn.assert_not_called()
    client.post.assert_not_called()
    assert client.timeout == 10


@pytest.mark.parametrize("code", ["NOT_PIECE", "TARGET_MISMATCH", "VERSION_MISMATCH"])
def test_wrong_peer_never_receives_credentials_or_starts_process(startup, code):
    client, status, process, spawn = startup
    client.handshake.side_effect = ClientError("wrong peer", code=code)
    result = background.start_service(client)
    assert not result["success"] and result["error"]["code"] == code
    spawn.assert_not_called()
    client.get.assert_not_called()
    client.post.assert_not_called()


def test_occupied_unresponsive_port_is_not_replaced(startup, monkeypatch):
    client, status, process, spawn = startup
    client.handshake.side_effect = ConnectionFailure("unavailable")
    monkeypatch.setattr(background, "_port_occupied", lambda client: True)
    result = background.start_service(client)
    assert result["error"]["code"] == "PORT_IN_USE"
    spawn.assert_not_called()


def test_spawn_waits_for_readiness_and_returns_pid(startup):
    client, status, process, spawn = startup
    client.handshake.side_effect = [ConnectionFailure("not started"), {}]
    result = background.start_service(client)
    assert result["success"] and not result["data"]["already_running"]
    assert result["data"]["pid"] == process.pid
    assert result["data"]["log_path"].endswith("background-8689.log")
    assert spawn.call_args.kwargs == {"with_gui": False, "with_mcp": False, "open_ui": False}


def test_failed_child_returns_log_location(startup):
    client, status, process, spawn = startup
    client.handshake.side_effect = ConnectionFailure("not started")
    process.poll.return_value = process.returncode = 1
    result = background.start_service(client)
    assert result["error"]["code"] == "SERVICE_START_FAILED"
    assert result["data"]["exit_code"] == 1 and result["data"]["log_path"]
    process.kill.assert_not_called()


def test_timeout_keeps_owned_child_and_reports_recovery(startup):
    client, status, process, spawn = startup
    client.handshake.side_effect = ConnectionFailure("not started")
    result = background.start_service(client, timeout=0.02)
    assert result["error"]["code"] == "START_TIMEOUT"
    assert result["data"]["process_running"] and result["data"]["pid"] == process.pid
    assert "piece status / piece stop" in result["message"]
    process.kill.assert_not_called()
    process.terminate.assert_not_called()
    client.post.assert_not_called()


def test_interrupt_does_not_kill_background_service(startup):
    client, status, process, spawn = startup
    client.handshake.side_effect = [ConnectionFailure("not started"), KeyboardInterrupt()]
    result = background.start_service(client)
    assert result["error"]["code"] == "START_INTERRUPTED" and result["data"]["process_running"]
    process.kill.assert_not_called()
    client.post.assert_not_called()


def test_existing_stopping_service_is_not_restarted(startup):
    client, status, process, spawn = startup
    status["ready"] = False
    result = background.start_service(client, timeout=0.02)
    assert result["error"]["code"] == "START_TIMEOUT" and result["data"]["already_running"]
    spawn.assert_not_called()


def test_existing_options_must_not_be_silently_changed(startup):
    client, status, process, spawn = startup
    result = background.start_service(client, with_gui=True)
    assert result["error"]["code"] == "START_OPTIONS_MISMATCH"
    spawn.assert_not_called()
    client.post.assert_not_called()


def test_existing_gui_can_be_opened_and_degraded_is_not_success(startup):
    client, status, process, spawn = startup
    status["with_gui"] = True
    assert background.start_service(client, open_ui=True)["success"]
    client.post.assert_called_once_with("/api/v1/window/open", {})
    status["components"]["mcp"] = {"status": "degraded"}
    result = background.start_service(client)
    assert result["error"]["code"] == "DEGRADED"
    spawn.assert_not_called()


@pytest.mark.parametrize("platform,frozen", [("win32", False), ("win32", True), ("linux", False)])
def test_spawn_detaches_redirects_and_passes_explicit_target(tmp_path, monkeypatch, platform, frozen):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 8, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(background, "subprocess_options", lambda: {"cwd": tmp_path, "creationflags": 0})
    popen = Mock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    log = tmp_path / "logs" / "start.log"
    background._spawn(tmp_path, 12345, log, with_gui=False, with_mcp=False, open_ui=False)
    command = popen.call_args.args[0]
    options = popen.call_args.kwargs
    assert command[:2] == ([sys.executable, "serve"] if frozen else [sys.executable, "-m"])
    assert command[command.index("--data-dir") + 1] == str(tmp_path)
    assert command[command.index("--port") + 1] == "12345"
    assert all(flag in command for flag in ("--no-gui", "--no-mcp", "--no-tray"))
    assert options["stdin"] == subprocess.DEVNULL and options["stderr"] == subprocess.STDOUT
    assert options["stdout"].closed and log.is_file()
    if platform == "win32":
        assert options["creationflags"] == 520
    else:
        assert options["start_new_session"] is True


def test_real_background_service_lifecycle(tmp_path, monkeypatch):
    import socket
    from indexing import settings
    from indexing.services.config_service import initialize_config

    directory = tmp_path / "后台 知识库"
    monkeypatch.setenv("PIECE_DATA_DIR", str(directory))
    monkeypatch.setattr(settings, "DEFAULT_DATA_PATH", directory)
    monkeypatch.setattr(settings, "_settings", None)
    initialize_config()
    config = settings.get_settings()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    client = PieceClient(port=port, config_dir=directory, db_path=config.get_db_path(), api_key=config.api.admin_key)
    spawn = background._spawn
    processes = []
    def record(*args, **kwargs):
        process = spawn(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(background, "_spawn", record)
    try:
        result = background.start_service(client, timeout=60)
        assert result["success"], result
        assert result["data"]["pid"] == processes[0].pid
        assert result["data"]["status"]["ready"]
        assert not result["data"]["status"]["with_gui"] and not result["data"]["status"]["with_mcp"]
        reused = background.start_service(client)
        assert reused["success"] and reused["data"]["already_running"] and len(processes) == 1
        assert client.post("/api/v1/shutdown", {})["success"]
        assert processes[0].wait(timeout=60) == 0
    finally:
        for process in processes:
            if process.poll() is None:
                try:
                    client.post("/api/v1/shutdown", {})
                except ClientError:
                    pass
                try:
                    process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                    pytest.fail(f"本测试创建的后台进程 {process.pid} 未能安全停止")
