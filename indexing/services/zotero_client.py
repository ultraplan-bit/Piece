"""Zotero Local API 协议：只访问回环地址，读写都绑定同一数据库实例。"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx

from .errors import BusinessError

DEFAULT_BASE_URL = "http://127.0.0.1:23119/api"
USER_PREFIX = "/users/0"
PERSONAL_LIBRARY = {"type": "user", "id": 0}
PAGE_SIZE = 100
MAX_KEYS = 50
_ENABLE_HINT = "请在 Zotero 的「设置 → 高级 → 允许本机其他应用程序与 Zotero 通信」中开启"
_KEY = re.compile(r"[A-Z0-9]{8}\Z")


def item_key(value: str) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise BusinessError("INVALID_INPUT", "Zotero key 必须是 8 位大写字母或数字")
    return value


def item_keys(values: list[str], *, name: str = "keys") -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_KEYS:
        raise BusinessError("INVALID_INPUT", f"{name} 需要 1 至 {MAX_KEYS} 个 Zotero key")
    return list(dict.fromkeys(item_key(value) for value in values))


def pagination(limit: int, offset: int, *, maximum: int = PAGE_SIZE) -> None:
    if type(limit) is not int or not 1 <= limit <= maximum or type(offset) is not int or offset < 0:
        raise BusinessError("INVALID_INPUT", f"limit 必须为 1 至 {maximum}，offset 必须是非负整数")


class ZoteroClient:
    """阻塞客户端，由 HTTP/MCP 入口放在线程池运行；不自动重试写请求。"""

    def __init__(self, base_url: str | None = None, *, timeout: float = 30.0,
                 transport=None, server_id: str | None = None):
        address = DEFAULT_BASE_URL if base_url is None else base_url
        if not isinstance(address, str) or len(address) > 512 or any(ord(char) < 32 for char in address):
            raise BusinessError("INVALID_INPUT", "无效的 Zotero Local API 地址")
        self.base_url = address.rstrip("/")
        try:
            parsed = urlparse(self.base_url)
            port = parsed.port
        except ValueError:
            raise BusinessError("INVALID_INPUT", "无效的 Zotero Local API 地址或端口") from None
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or port == 0 or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != "/api"):
            raise BusinessError("INVALID_INPUT", "Zotero Local API 地址必须是本机回环 HTTP 地址，路径为 /api")
        self.server_id: str | None = None
        self.bind(server_id)
        # 本地 key 和请求不能被环境代理转发；不跟随 HTTP 或 file:// 重定向。
        self._client = httpx.Client(timeout=timeout, transport=transport, trust_env=False, follow_redirects=False)

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def bind(self, server_id: str | None) -> None:
        if server_id is None:
            return
        if not isinstance(server_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", server_id):
            raise BusinessError("INVALID_INPUT", "无效的 Zotero-Server-ID，请使用状态接口返回的实例标识")
        if self.server_id is not None and self.server_id != server_id:
            raise BusinessError("ZOTERO_INSTANCE_CHANGED", "Zotero 数据库实例已改变，请重新查询并预览")
        self.server_id = server_id

    def _request(self, method: str, path: str, *, params=None, headers=None, json=None) -> httpx.Response:
        request_headers = {"Zotero-API-Version": "3", **(headers or {})}
        if self.server_id:
            request_headers["Zotero-Server-ID"] = self.server_id
        writing = method != "GET"
        if writing and not self.server_id:
            raise BusinessError("ZOTERO_UNSUPPORTED", "写入需要 Zotero 10+ 的数据库实例标识")
        try:
            response = self._client.request(method, f"{self.base_url}{path}", params=params,
                                            headers=request_headers, json=json)
        except httpx.HTTPError as exc:
            if path == "/local/authorize":
                # 授权框一直等用户点击；超时只说明用户还没决定，Zotero 没有写任何条目。
                # 但用户稍后点「允许」时 Zotero 仍会签发 key，而本次请求已放弃，所以单次 key 会白白消耗。
                raise BusinessError("ZOTERO_AUTH_TIMEOUT",
                                    "等待 Zotero 授权弹窗超时，本次授权作废；请在 Zotero 中尽快点击「允许」或「始终允许」，再重新请求授权"
                                    if isinstance(exc, httpx.TimeoutException) else
                                    "请求 Zotero 授权时连接中断，本次授权作废；请重新请求授权") from None
            if writing:
                raise BusinessError("ZOTERO_WRITE_UNCERTAIN", "Zotero 写请求连接中断，结果待核对；不要重新创建相同内容",
                                    data={"outcome": "unknown"}) from None
            raise BusinessError("ZOTERO_UNAVAILABLE", f"无法连接 Zotero（{self.base_url}）；请确认 Zotero 7 及以上版本正在运行") from None
        actual_id = response.headers.get("Zotero-Server-ID")
        if self.server_id and (actual_id and actual_id != self.server_id
                               or not actual_id and response.is_success):
            raise BusinessError("ZOTERO_INSTANCE_CHANGED", "Zotero 数据库实例已改变或不再支持实例校验，请重新查询并预览")
        if actual_id and self.server_id is None:
            self.bind(actual_id)
        code = response.status_code
        if 200 <= code < 300:
            return response
        if code == 401:
            raise BusinessError("ZOTERO_AUTH_REQUIRED", "Zotero 写授权已失效或未授权；请明确请求重新授权")
        if code == 403:
            if path == "/local/authorize":
                raise BusinessError("ZOTERO_AUTH_DENIED", "用户拒绝了 Zotero 写授权；不会自动再次弹窗")
            if writing:
                raise BusinessError("ZOTERO_WRITE_FORBIDDEN", "Zotero 拒绝了本次写入，请检查库权限与本机 API 设置")
            raise BusinessError("ZOTERO_DISABLED", f"Zotero 拒绝了本机 API 访问；{_ENABLE_HINT}")
        if code == 404:
            raise BusinessError("NOT_FOUND", f"Zotero 中不存在该对象：{path}")
        if code == 412:
            raise BusinessError("VERSION_CONFLICT", "Zotero 对象或写请求状态已改变，请读回核对，不能盲目覆盖")
        if code in {405, 501}:
            raise BusinessError("ZOTERO_UNSUPPORTED", "当前 Zotero 版本不支持此接口")
        if code == 428:
            raise BusinessError("ZOTERO_UNSUPPORTED", "Zotero 写入缺少实例或版本前置条件")
        if code == 429:
            raise BusinessError("ZOTERO_RATE_LIMITED", "Zotero 请求过于频繁，请等待后再操作",
                                data={"retry_after": response.headers.get("Retry-After")})
        # 不返回任意服务器错误正文，避免响应回显授权 key 或请求中的笔记。
        raise BusinessError("ZOTERO_ERROR", f"Zotero 返回 HTTP {code}", data={"http_status": code})

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        return self._request("GET", path, params=params)

    @staticmethod
    def json(response: httpx.Response):
        try:
            return response.json()
        except ValueError:
            raise BusinessError("ZOTERO_ERROR", "Zotero 返回了无效的 JSON") from None

    def probe(self) -> dict[str, Any]:
        response = self._get("/")
        api_version = response.headers.get("Zotero-API-Version")
        if api_version and api_version != "3":
            raise BusinessError("ZOTERO_UNSUPPORTED", f"仅支持 Zotero Local API v3，当前为 {api_version}")
        version = response.headers.get("X-Zotero-Version")
        parsed_version = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", version or "")
        release = tuple(int(part or 0) for part in parsed_version.groups()) if parsed_version else (0, 0, 0)
        native_write = bool(self.server_id and release >= (10, 0, 0) and api_version == "3")
        return {
            "base_url": self.base_url,
            "zotero_version": version,
            "api_version": api_version,
            "schema_version": response.headers.get("Zotero-Schema-Version"),
            "server_id": self.server_id,
            "library": dict(PERSONAL_LIBRARY),
            "identity_verified": self.server_id is not None,
            "native_write_supported": native_write,
            # 10.0.5 修复客户端预指定对象 key，创建笔记才可安全核对和重试。
            "write_supported": native_write and release >= (10, 0, 5),
            "minimum_write_version": "10.0.5",
        }

    def page(self, path: str, params=None, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        pagination(limit, offset)
        response = self._get(path, {**(params or {}), "limit": limit, "start": offset})
        values = self.json(response)
        if not isinstance(values, list):
            raise BusinessError("ZOTERO_ERROR", "Zotero 列表响应格式异常")
        total_header = response.headers.get("Total-Results")
        try:
            total = int(total_header) if total_header is not None else None
        except ValueError:
            raise BusinessError("ZOTERO_ERROR", "Zotero 分页计数无效") from None
        has_more = bool(values) and (offset + len(values) < total if total is not None else len(values) == limit)
        return {"items": values, "total": total, "limit": limit, "offset": offset,
                "has_more": has_more, "next_offset": offset + len(values) if has_more else None}

    def _paged(self, path: str, params=None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.page(path, params, limit=PAGE_SIZE, offset=offset)
            items.extend(page["items"])
            if not page["has_more"]:
                return items
            offset = page["next_offset"]

    def top_items(self) -> list[dict[str, Any]]:
        return self._paged(f"{USER_PREFIX}/items/top")

    def attachments(self) -> list[dict[str, Any]]:
        return self._paged(f"{USER_PREFIX}/items", {"itemType": "attachment"})

    def collections(self) -> list[dict[str, Any]]:
        return self._paged(f"{USER_PREFIX}/collections")

    def item(self, key: str) -> dict[str, Any]:
        value = self.json(self._get(f"{USER_PREFIX}/items/{item_key(key)}"))
        if not isinstance(value, dict) or not isinstance(value.get("data"), dict) or value.get("key") != key:
            raise BusinessError("ZOTERO_ERROR", "Zotero 条目响应格式异常")
        return value

    def collection(self, key: str) -> dict[str, Any]:
        value = self.json(self._get(f"{USER_PREFIX}/collections/{item_key(key)}"))
        if not isinstance(value, dict) or not isinstance(value.get("data"), dict) or value.get("key") != key:
            raise BusinessError("ZOTERO_ERROR", "Zotero 集合响应格式异常")
        return value

    def children(self, key: str, item_type: str | None = None) -> list[dict[str, Any]]:
        return self._paged(f"{USER_PREFIX}/items/{item_key(key)}/children",
                           {"itemType": item_type} if item_type else None)

    def attachment_file_url(self, key: str) -> str:
        return self._get(f"{USER_PREFIX}/items/{item_key(key)}/file/view/url").text.strip()
