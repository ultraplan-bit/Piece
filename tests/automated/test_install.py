"""仓库外安装验证：wheel 精简安装（基础依赖）与 desktop extras 完整安装。

所有被安装入口都在仓库目录之外运行，且子进程环境不含 PYTHONPATH、仓库相关环境变量，
确保精简安装不是从源码目录偷加载缺失模块。需要 `uv` 命令和依赖下载（有 uv 缓存时较快）；
不接触默认知识库，全部使用临时数据目录和动态端口。
"""

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
IGNORED_ENV = {"PIECE_DATA_DIR", "PIECE_API_KEY", "PIECE_INDEX_MCP_PORT",
               "NICEGUI_STORAGE_PATH", "PYTHONPATH", "VIRTUAL_ENV"}

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="需要 uv 命令构建与安装")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _child_env():
    env = {k: v for k, v in os.environ.items() if k.upper() not in IGNORED_ENV}
    env["PYTHONUTF8"] = "1"
    return env


def _piece(venv, *args, timeout=180):
    """在仓库外的临时目录运行已安装 venv 中的 piece。"""
    executable = venv / ("Scripts/piece.exe" if os.name == "nt" else "bin/piece")
    result = subprocess.run([str(executable), *args], cwd=str(venv.parent), env=_child_env(),
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    return SimpleNamespace(code=result.returncode, stdout=result.stdout.decode("utf-8"),
                           stderr=result.stderr.decode("utf-8"))


def _python(venv, code):
    executable = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    result = subprocess.run([str(executable), "-c", code], cwd=str(venv.parent), env=_child_env(),
                            capture_output=True, timeout=120)
    return result.returncode


def _venv_python(venv):
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    output = tmp_path_factory.mktemp("wheelhouse")
    subprocess.run(["uv", "build", "--wheel", "-o", str(output)], cwd=ROOT,
                   check=True, capture_output=True, timeout=600)
    built = list(output.glob("*.whl"))
    assert built, "wheel 构建产物缺失"
    return built[0]


@pytest.fixture(scope="module")
def slim_venv(tmp_path_factory, wheel):
    venv = tmp_path_factory.mktemp("install") / "venv-slim"
    subprocess.run(["uv", "venv", str(venv)], check=True, capture_output=True)
    subprocess.run(["uv", "pip", "install", "--python", str(_venv_python(venv)), str(wheel)],
                   check=True, capture_output=True, timeout=900)
    return venv


@pytest.fixture(scope="module")
def full_venv(tmp_path_factory, wheel):
    venv = tmp_path_factory.mktemp("install") / "venv-full"
    subprocess.run(["uv", "venv", str(venv)], check=True, capture_output=True)
    subprocess.run(["uv", "pip", "install", "--python", str(_venv_python(venv)), f"{wheel}[desktop]"],
                   check=True, capture_output=True, timeout=900)
    return venv


def test_slim_install_runs_cli_without_initializing(slim_venv):
    version = _piece(slim_venv, "--version")
    assert version.code == 0 and version.stdout.strip()
    help_text = _piece(slim_venv, "--help")
    assert help_text.code == 0 and "serve" in help_text.stdout


def test_slim_install_has_no_gui_or_mcp_packages(slim_venv):
    assert _python(slim_venv, "import nicegui") != 0, "精简安装不应包含 nicegui"
    assert _python(slim_venv, "import fastmcp") != 0, "精简安装不应包含 fastmcp"


def test_slim_install_doctor_healthy(slim_venv):
    doctor = _piece(slim_venv, "doctor", "--json")
    assert doctor.code == 0, doctor.stdout + doctor.stderr
    data = json.loads(doctor.stdout)["data"]
    assert data["optional_dependencies"]["nicegui"] is False
    assert data["optional_dependencies"]["fastmcp"] is False


def test_slim_install_lists_and_exports_skills(tmp_path, slim_venv):
    """wheel 安装层能拿到内置 Skill，导出前缀指向该 venv 的控制台脚本。"""
    listing = _piece(slim_venv, "skill", "list", "--json")
    assert listing.code == 0, listing.stdout + listing.stderr
    assert [s["id"] for s in json.loads(listing.stdout)["data"]["skills"]] == [
        "piece-index", "piece-search",
    ]

    out_dir = tmp_path / "skills"
    exported = _piece(slim_venv, "skill", "export", "--dir", str(out_dir), "--json")
    assert exported.code == 0, exported.stdout + exported.stderr
    content = (out_dir / "piece-search" / "SKILL.md").read_text(encoding="utf-8")
    console = slim_venv / ("Scripts/piece.exe" if os.name == "nt" else "bin/piece")
    assert f'"{console.as_posix()}"' in content
    assert "--data-dir" in content and "--port 8689" in content
    assert "{{PIECE_CLI}}" not in content


def test_slim_install_serves_core_without_gui_mcp(tmp_path_factory, slim_venv):
    data_dir = tmp_path_factory.mktemp("slim-data")
    port = _free_port()
    assert _piece(slim_venv, "config", "init", "--data-dir", str(data_dir),
                  "--port", str(port), "--json").code == 0
    executable = str(slim_venv / ("Scripts/piece.exe" if os.name == "nt" else "bin/piece"))
    log = open(data_dir.parent / "slim-serve.log", "wb")
    process = subprocess.Popen([executable, "serve", "--no-gui", "--no-mcp", "--no-tray",
                                "--data-dir", str(data_dir), "--port", str(port)],
                               cwd=str(data_dir.parent), env=_child_env(), stdout=log,
                               stderr=subprocess.STDOUT,
                               creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        status = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            assert process.poll() is None, (data_dir.parent / "slim-serve.log").read_text("utf-8", "replace")
            probe = _piece(slim_venv, "status", "--data-dir", str(data_dir), "--port", str(port), "--json")
            if probe.code == 0:
                status = json.loads(probe.stdout)["data"]
                break
            time.sleep(0.5)
        else:
            pytest.fail("精简安装服务未就绪：" + (data_dir.parent / "slim-serve.log").read_text("utf-8", "replace"))
        assert status["ready"] is True
        assert status["with_gui"] is False and status["with_mcp"] is False
        assert _piece(slim_venv, "stop", "--data-dir", str(data_dir), "--port", str(port), "--json").code == 0
        process.wait(timeout=60)
        assert process.returncode == 0, (data_dir.parent / "slim-serve.log").read_text("utf-8", "replace")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        log.close()


def test_full_install_has_gui_and_mcp(full_venv):
    assert _python(full_venv, "import nicegui") == 0, "完整安装应包含 nicegui"
    assert _python(full_venv, "import fastmcp") == 0, "完整安装应包含 fastmcp"
    version = _piece(full_venv, "--version")
    assert version.code == 0 and version.stdout.strip()
    doctor = _piece(full_venv, "doctor", "--json")
    assert doctor.code == 0, doctor.stdout + doctor.stderr
