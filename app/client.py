"""只连接既有 Piece 服务的标准库客户端；不导入业务/GUI，不创建配置目录。"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

APPLICATION = "Piece"
API_VERSION = 1
DEFAULT_PORT = 8689
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class ClientError(Exception):
    def __init__(self, message: str, *, code: str = "CLIENT_ERROR", data: Any = None):
        super().__init__(message)
        self.message = str(message)
        self.code = str(code)
        self.data = data


class ConnectionFailure(ClientError):
    def __init__(self, message: str):
        super().__init__(message, code="SERVICE_UNAVAILABLE")


class ProtocolFailure(ClientError):
    def __init__(self, message: str):
        super().__init__(message, code="PROTOCOL_ERROR")


def make_envelope(success: bool, message: str, data: Any = None,
                  error: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"success": bool(success), "message": str(message), "data": data,
            "error": dict(error) if error is not None else None}


def target_id(config_dir: Path | str, db_path: Path | str) -> str:
    normalized = (os.path.normcase(str(Path(config_dir).expanduser().resolve())) + "\n"
                  + os.path.normcase(str(Path(db_path).expanduser().resolve())))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _content_disposition_filename(header: str) -> str | None:
    """email 会把 filename* 归一为 filename tuple；扩展参数优先，拒绝路径名。"""
    from email.message import Message
    from email.utils import collapse_rfc2231_value

    message = Message()
    message["content-disposition"] = header
    names = [value for key, value in message.get_params(header="content-disposition", failobj=[])
             if key.lower() == "filename"]
    extended = [value for value in names if isinstance(value, tuple)]
    name = collapse_rfc2231_value(extended[0]) if extended else next(iter(names), None)
    if (not isinstance(name, str) or not name or name in {".", ".."}
            or any(c in name for c in '/\\:*?"<>|') or any(ord(c) < 32 for c in name)
            or name.endswith((".", " ")) or PureWindowsPath(name).stem.upper() in _RESERVED_NAMES):
        return None
    return name


def decode_json(raw: bytes | bytearray) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolFailure("服务返回的 UTF-8 JSON 无效") from exc


def validate_envelope(value: Any, status: int = 200) -> dict[str, Any]:
    if not isinstance(value, dict) or not {"success", "message", "data", "error"} <= value.keys():
        raise ProtocolFailure("服务返回缺少标准 envelope 字段")
    if not isinstance(value["success"], bool) or not isinstance(value["message"], str):
        raise ProtocolFailure("服务返回的 success/message 类型无效")
    if value["error"] is not None and not isinstance(value["error"], dict):
        raise ProtocolFailure("服务返回的 error 类型无效")
    if not value["success"] and not isinstance((value["error"] or {}).get("code"), str):
        raise ProtocolFailure("失败响应缺少结构化错误码")
    if status >= 400 and value["success"]:
        return make_envelope(False, value["message"], value["data"],
                             {"code": f"HTTP_{status}", "message": value["message"]})
    return value


class PieceClient:
    def __init__(self, *, port: int = DEFAULT_PORT, config_dir: Path | str, db_path: Path | str,
                 api_key: str | None, timeout: float = 10.0, host: str = "127.0.0.1"):
        if not 1 <= int(port) <= 65535:
            raise ValueError("端口必须在 1-65535 之间")
        if timeout <= 0:
            raise ValueError("网络超时必须大于零")
        self.host, self.port = host, int(port)
        self.config_dir = Path(config_dir).expanduser().resolve()
        self.db_path = Path(db_path).expanduser().resolve()
        self.expected_target_id = target_id(self.config_dir, self.db_path)
        self.api_key = api_key
        self.timeout = float(timeout)
        self._handshaken = False
        self._deadline: float | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @contextmanager
    def time_budget(self, seconds: float):
        """握手、所有批次和响应读取共用一个总 deadline，不在每次请求时重置。"""
        previous = self._deadline
        self._deadline = min(previous or float("inf"), time.monotonic() + seconds)
        try:
            yield
        finally:
            self._deadline = previous

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise self._network_error("请求超时")
        return remaining

    def _network_error(self, detail: str) -> ClientError:
        if self._deadline is not None and time.monotonic() >= self._deadline:
            return ClientError("任务等待达到总超时；不会取消已受理任务", code="WAIT_TIMEOUT")
        return ConnectionFailure(f"{detail}：{self.base_url}；请确认目标并先显式启动 piece serve")

    @contextmanager
    def _response(self, method: str, path: str, *, body=None, headers=None):
        deadline = min(self._deadline or float("inf"), time.monotonic() + self.timeout)
        request_headers = {"Accept": "application/json", **(headers or {})}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json; charset=utf-8"
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self._remaining(deadline))
        timer = None
        response = None
        sent = False
        try:
            try:
                connection.connect()
                transport = connection.sock
                # socket 的单次 read timeout 不能约束缓慢滴流的响应头/正文；到期关闭
                # 本次连接，确保整个等待有界。每次请求只有一个定时器，finally 必回收。
                def expire():
                    try:
                        transport.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                timer = threading.Timer(self._remaining(deadline), expire)
                timer.daemon = True
                timer.start()
                sent = True  # request 可能只发送了一部分，结果仍须保守标记为未知。
                connection.request(method, path, body=payload, headers=request_headers)
                response = connection.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                raise self._network_error("无法连接或读取 Piece 服务") from exc
            yield response, deadline
        except ClientError as exc:
            exc.data = {**(exc.data if isinstance(exc.data, dict) else {}), "request_sent": sent}
            raise
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            if response is not None:
                response.close()
            connection.close()

    def _blocks(self, response, deadline):
        size = 0
        expected = response.length
        while True:
            self._remaining(deadline)
            try:
                block = response.read1(64 * 1024)
            except (OSError, http.client.HTTPException) as exc:
                raise self._network_error("读取 Piece 响应中断") from exc
            self._remaining(deadline)
            if not block:
                break
            size += len(block)
            yield block
        if expected is not None and size != expected:
            raise self._network_error("Piece 响应未完整传输")

    def _read_json(self, response, deadline):
        raw = bytearray()
        for block in self._blocks(response, deadline):
            raw.extend(block)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ProtocolFailure("JSON 响应过大，请缩小查询范围")
        return validate_envelope(decode_json(raw), response.status)

    def probe_handshake(self) -> dict[str, Any]:
        """无凭据探测，并统一校验 Piece/API 版本；不校验本地目标。"""
        try:
            with self._response("GET", "/api/v1/handshake") as (response, deadline):
                result = self._read_json(response, deadline)
        except ProtocolFailure as exc:
            raise ClientError("端口上的服务未返回 Piece 握手", code="NOT_PIECE") from exc
        if not result["success"]:
            raise ClientError(result["message"], code=result["error"]["code"], data=result["data"])
        data = result["data"]
        if not isinstance(data, dict) or data.get("application") != APPLICATION:
            raise ClientError("端口上的服务不是 Piece，已拒绝发送密钥", code="NOT_PIECE")
        if type(data.get("api_version")) is not int or data["api_version"] != API_VERSION:
            raise ClientError("Piece API 版本不兼容，已拒绝发送密钥", code="VERSION_MISMATCH")
        if not isinstance(data.get("ready"), bool):
            raise ProtocolFailure("握手 ready 类型无效")
        return result

    def handshake(self) -> dict[str, Any]:
        result = self.probe_handshake()
        if result["data"].get("target_id") != self.expected_target_id:
            raise ClientError("服务目标不匹配，已拒绝发送密钥", code="TARGET_MISMATCH")
        self._handshaken = True
        return result

    def _authorized_headers(self) -> dict[str, str]:
        if not self._handshaken:
            self.handshake()
        if not self.api_key:
            raise ClientError("未找到 API 密钥；请设置 PIECE_API_KEY 或显式初始化配置", code="AUTH_REQUIRED")
        return {"Authorization": f"Bearer {self.api_key}", "X-Piece-Target": self.expected_target_id}

    def request(self, method: str, path: str, payload: Any = None) -> dict[str, Any]:
        try:
            headers = self._authorized_headers()
        except ClientError as exc:
            exc.data = {**(exc.data if isinstance(exc.data, dict) else {}), "request_sent": False}
            raise
        with self._response(method, path, body=payload, headers=headers) as (response, deadline):
            return self._read_json(response, deadline)

    def get(self, path: str) -> dict[str, Any]:
        return self.request("GET", path)

    def post(self, path: str, payload: Any) -> dict[str, Any]:
        return self.request("POST", path, payload)

    def download(self, path: str, destination: Path | str, *, overwrite: bool = False) -> dict[str, Any]:
        """完整下载到同目录临时文件；非覆盖模式用硬链接原子发布，避免 exists 竞态。"""
        destination = Path(destination).expanduser()
        into_directory = destination.is_dir()
        parent = destination if into_directory else destination.parent
        if not parent.is_dir():
            raise ClientError(f"目标目录不存在：{parent}", code="INVALID_PATH")
        if not into_directory and destination.exists() and not overwrite:
            raise ClientError(f"目标已存在：{destination}；明确授权后使用 --yes 覆盖", code="ALREADY_EXISTS")
        headers = {**self._authorized_headers(), "Accept": "application/octet-stream, application/json"}
        temporary = None
        try:
            with self._response("GET", path, headers=headers) as (response, deadline):
                if response.status >= 400 or "json" in response.getheader("Content-Type", "").lower():
                    result = self._read_json(response, deadline)
                    if not result["success"]:
                        return result
                    raise ProtocolFailure("下载接口返回 JSON 而不是文件")
                if into_directory:
                    filename = _content_disposition_filename(response.getheader("Content-Disposition", ""))
                    if not filename:
                        raise ProtocolFailure("服务未返回安全文件名，请用 --output 指定完整路径")
                    destination = parent / filename
                descriptor, name = tempfile.mkstemp(prefix=".piece-download-", suffix=".tmp", dir=parent)
                temporary = Path(name)
                size = 0
                with os.fdopen(descriptor, "wb") as output:
                    for block in self._blocks(response, deadline):
                        output.write(block)
                        size += len(block)
                    output.flush()
                    os.fsync(output.fileno())
                if overwrite:
                    os.replace(temporary, destination)
                else:
                    try:
                        os.link(temporary, destination)
                    except FileExistsError:
                        raise ClientError(f"目标已存在：{destination}；未覆盖", code="ALREADY_EXISTS") from None
            return make_envelope(True, "文件已保存", {"path": str(destination), "bytes": size})
        except OSError as exc:
            raise ClientError(f"保存下载文件失败：{exc}", code="FILE_ERROR") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def read_json_file(path: Path | str) -> Any:
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ClientError(f"文件不存在：{path}", code="FILE_NOT_FOUND") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientError(f"JSON 文件无效：{path}", code="INVALID_JSON") from exc
