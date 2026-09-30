"""显式后台启动：复用服务独占锁与客户端握手，不维护易过期的 PID 文件。"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.client import ClientError, ConnectionFailure, PieceClient, make_envelope
from app.platform import subprocess_options


def _spawn(config_dir: Path, port: int, log_path: Path, *, with_gui: bool,
           with_mcp: bool, open_ui: bool) -> subprocess.Popen:
    prefix = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "app.cli"]
    command = [*prefix, "serve", "--data-dir", str(config_dir), "--port", str(port), "--no-tray"]
    if not with_gui:
        command.append("--no-gui")
    if not with_mcp:
        command.append("--no-mcp")
    if open_ui:
        command.append("--open")
    options = subprocess_options()
    if sys.platform == "win32":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    log_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "ab") as log:
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONUTF8": "1"}, **options)


def _port_occupied(client: PieceClient) -> bool:
    try:
        with socket.create_connection((client.host, client.port), timeout=0.2):
            return True
    except OSError:
        return False


def start_service(client: PieceClient, *, timeout: float = 60, with_gui: bool = False,
                  with_mcp: bool = False, open_ui: bool = False) -> dict:
    """等待就绪有总时限；超时或中断只停止等待，不杀可能已经开始处理任务的服务。"""
    with_gui = with_gui or open_ui
    log_path = client.config_dir / "logs" / f"background-{client.port}.log"
    process = None
    original_timeout = client.timeout
    client.timeout = min(original_timeout, 1.0)
    details = {"port": client.port, "config_dir": str(client.config_dir),
               "log_path": str(log_path), "already_running": False}
    deadline = time.monotonic() + timeout
    try:
        with client.time_budget(timeout):
            try:
                client.handshake()
            except ConnectionFailure:
                # 端口有监听却不能完成握手时，不向该服务发送凭据，也不另起进程。
                if _port_occupied(client):
                    raise ClientError("端口已被占用且未能完成 Piece 握手，请先检查端口上的服务",
                                      code="PORT_IN_USE") from None
                process = _spawn(client.config_dir, client.port, log_path,
                                 with_gui=with_gui, with_mcp=with_mcp, open_ui=open_ui)
                details["pid"] = process.pid
            else:
                details["already_running"] = True

            while time.monotonic() < deadline:
                if process is not None and process.poll() is not None:
                    details["exit_code"] = process.returncode
                    raise ClientError("后台服务启动失败，请查看日志", code="SERVICE_START_FAILED")
                try:
                    # 每轮重新校验身份，避免端口被其他进程接管后沿用已完成的握手。
                    client.handshake()
                    result = client.get("/api/v1/status")
                    if not result["success"]:
                        raise ClientError(result["message"], code=result["error"]["code"], data=result["data"])
                    status = result["data"]
                    if not isinstance(status, dict) or not isinstance(status.get("ready"), bool):
                        raise ClientError("服务状态格式无效", code="PROTOCOL_ERROR")
                    details["status"] = status
                    if status["ready"]:
                        if ((with_gui and not status.get("with_gui"))
                                or (with_mcp and not status.get("with_mcp"))):
                            raise ClientError("服务已在运行，但未启用请求的入口；请先安全停止再用所需参数启动",
                                              code="START_OPTIONS_MISMATCH")
                        components = status.get("components") or {}
                        degraded = [name for name, item in components.items()
                                    if isinstance(item, dict) and item.get("status") in {"degraded", "failed"}]
                        if degraded:
                            raise ClientError(f"核心可用，但可选入口降级：{', '.join(degraded)}", code="DEGRADED")
                        if open_ui and process is None:
                            opened = client.post("/api/v1/window/open", {})
                            if not opened["success"]:
                                raise ClientError(opened["message"], code=opened["error"]["code"])
                        message = "服务已在运行，未修改入口配置" if process is None else "后台服务已就绪"
                        return make_envelope(True, message, details)
                except ConnectionFailure:
                    pass
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            raise ClientError("等待后台服务就绪超时", code="START_TIMEOUT")
    except (ClientError, KeyboardInterrupt, OSError) as exc:
        if isinstance(exc, ClientError):
            code, message = exc.code, exc.message
            if isinstance(exc.data, dict):
                details["last_error"] = exc.data
            if code == "WAIT_TIMEOUT":
                code, message = "START_TIMEOUT", "等待后台服务就绪超时"
        elif isinstance(exc, KeyboardInterrupt):
            code, message = "START_INTERRUPTED", "已中断等待后台服务就绪"
        else:
            code, message = "SERVICE_START_FAILED", f"无法启动后台服务：{exc}"
        if process is not None and process.poll() is None:
            details["process_running"] = True
            message += "；后台进程可能仍在启动，未强制终止，请查看日志并用 piece status / piece stop 管理同一目标"
        return make_envelope(False, message, details, {"code": code, "message": message})
    finally:
        client.timeout = original_timeout
        if process is not None:
            # CLI 可立即退出；被其他 Python 程序调用时也能回收子进程，避免僵尸进程。
            threading.Thread(target=process.wait, daemon=True).start()
