"""Zotero 协作读写离线契约：httpx.MockTransport 模拟本机 Zotero 10.0.6，不连真实服务。

覆盖：版本门控/只读降级、纯查询与分页、选定导入不扫全库、写授权与 key 不外泄、
元数据白名单、预览/确认/版本冲突、笔记固定 key 与批量部分失败、超时读回核对、幂等与过期。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx
import pytest

from indexing.services import zotero_service as zotero
from indexing.services import zotero_write_service as ws
from indexing.services.errors import BusinessError
from indexing.services.zotero_client import ZoteroClient

SERVER_ID = "SRVABCDEFGH1"
KEY_CHARS = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
_KEY_RE = re.compile(rf"[{KEY_CHARS}]{{8}}\Z")


# ---------------------------------------------------------------------------
# 模拟本机 Zotero 服务
# ---------------------------------------------------------------------------

def _item(key, item_type="journalArticle", version=5, **data):
    payload = {"key": key, "version": version, "links": {}, "meta": {},
               "data": {"key": key, "version": version, "itemType": item_type, "tags": [],
                        "collections": [], **data}}
    return payload


def _attachment(key, parent, path: Path | None, content_type="application/pdf",
                link_mode="imported_url", version=5, title="Full Text PDF"):
    links = {"enclosure": {"href": path.as_uri(), "type": content_type}} if path else {}
    return {"key": key, "version": version, "links": links, "meta": {},
            "data": {"key": key, "version": version, "itemType": "attachment", "parentItem": parent,
                     "contentType": content_type, "linkMode": link_mode, "title": title,
                     "filename": path.name if path else "", "tags": [], "collections": []}}


def _collection(key, name, parent=False, version=5):
    return {"key": key, "version": version, "meta": {"numItems": 1},
            "data": {"key": key, "version": version, "name": name, "parentCollection": parent}}


class FakeZotero:
    """一个有状态的假 Zotero 实例；默认 10.0.6 + server-id。"""

    def __init__(self, version="10.0.6", server_id=SERVER_ID, enabled=True):
        self.version = version
        self.server_id = server_id
        self.enabled = enabled
        self.items: dict[str, dict] = {}
        self.order: list[str] = []
        self.collections: dict[str, dict] = {}
        self.children: dict[str, list[str]] = {}
        self.fulltext: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.authorize_result = "key"  # key | deny | bad | timeout
        self.remember = True
        self.api_key = None
        self.post_behavior = None  # None | "timeout_before" | "timeout_after" | "401"
        self.fail_indices: set[int] = set()
        self.omit_success: set[int] = set()
        self._version_counter = 100
        self.item_fields = {"journalArticle": ["title", "date", "DOI", "url"],
                            "book": ["title", "date", "publisher"],
                            "note": ["title"]}
        self.creator_types = {"journalArticle": ["author", "editor"], "book": ["author"], "note": []}
        self.item_types = ["journalArticle", "book", "note", "attachment", "annotation"]

    # -- helpers ------------------------------------------------------------
    def add_item(self, item):
        self.items[item["key"]] = item
        self.order.append(item["key"])
        return item

    def _next_version(self):
        self._version_counter += 1
        return self._version_counter

    def _base_headers(self):
        headers = {"X-Zotero-Version": self.version, "Zotero-API-Version": "3",
                   "Zotero-Schema-Version": "44"}
        if self.server_id:
            headers["Zotero-Server-ID"] = self.server_id
        return headers

    def _resp(self, status, *, headers=None, json=None, text=None):
        merged = self._base_headers()
        if headers:
            merged.update(headers)
        if json is not None:
            return httpx.Response(status, headers=merged, json=json)
        return httpx.Response(status, headers=merged, text=text if text is not None else "")

    def _paged(self, values, params):
        start = int(params.get("start", 0) or 0)
        limit = int(params.get("limit", 100) or 100)
        return self._resp(200, headers={"Total-Results": str(len(values))},
                          json=values[start:start + limit])

    def paths(self):
        return [r.url.path for r in self.requests]

    def authorize_calls(self):
        return [r for r in self.requests if r.url.path == "/api/local/authorize"]

    def post_item_calls(self):
        return [r for r in self.requests
                if r.method == "POST" and r.url.path == "/api/users/0/items"]

    def post_collection_calls(self):
        return [r for r in self.requests
                if r.method == "POST" and r.url.path == "/api/users/0/collections"]

    # -- request handling ---------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.enabled:
            return httpx.Response(403, text="Forbidden")
        path = request.url.path
        params = request.url.params

        if path == "/api/":
            return self._resp(200, text="")
        if path == "/api/local/authorize" and request.method == "POST":
            return self._authorize()

        if path == "/api/itemTypes":
            return self._resp(200, json=[{"itemType": name, "localized": name} for name in self.item_types])
        if path == "/api/itemTypeFields":
            kind = params.get("itemType")
            return self._resp(200, json=[{"field": name, "localized": name} for name in self.item_fields.get(kind, [])])
        if path == "/api/itemTypeCreatorTypes":
            kind = params.get("itemType")
            return self._resp(200, json=[{"creatorType": name, "localized": name}
                                         for name in self.creator_types.get(kind, [])])

        if path == "/api/users/0/collections" and request.method == "POST":
            return self._write_objects(request, self.collections)
        if path == "/api/users/0/collections":
            return self._paged([c for c in self.collections.values() if not c["data"].get("deleted")], params)
        m = re.fullmatch(r"/api/users/0/collections/([^/]+)/collections", path)
        if m:
            subs = [c for c in self.collections.values()
                    if c["data"].get("parentCollection") == m.group(1)]
            return self._paged(subs, params)
        m = re.fullmatch(r"/api/users/0/collections/([^/]+)/items/tags", path)
        if m:
            # 真实 Local API 只有 /collections/{key}/items/tags，没有 /collections/{key}/tags。
            if m.group(1) not in self.collections:
                return self._resp(404, text="Not found")
            members = [k for k in self.order if m.group(1) in (self.items[k]["data"].get("collections") or [])]
            names = []
            for key in members + [c for k in members for c in self.children.get(k, [])]:
                for tag in self.items[key]["data"].get("tags") or []:
                    if tag["tag"] not in names:
                        names.append(tag["tag"])
            return self._paged([{"tag": name, "meta": {"type": 0, "numItems": 1}} for name in names], params)
        m = re.fullmatch(r"/api/users/0/collections/([^/]+)/items/top", path)
        if m:
            in_col = [self.items[k] for k in self.order
                      if m.group(1) in (self.items[k]["data"].get("collections") or [])]
            return self._paged(in_col, params)
        m = re.fullmatch(r"/api/users/0/collections/([^/]+)", path)
        if m:
            found = self.collections.get(m.group(1))
            return self._resp(200, json=found) if found else self._resp(404, text="Not found")

        if path == "/api/users/0/tags":
            names = []
            for item in self.items.values():
                for tag in item["data"].get("tags") or []:
                    if tag["tag"] not in names:
                        names.append(tag["tag"])
            return self._paged([{"tag": name, "meta": {"type": 0, "numItems": 1}} for name in names], params)
        if path == "/api/users/0/items/top":
            if params.get("itemKey"):
                values = [self.items[k] for k in params.get("itemKey").split(",")
                          if k in self.items and not self.items[k]["data"].get("parentItem")]
            else:
                values = [self.items[k] for k in self.order if not self.items[k]["data"].get("parentItem")]
            return self._paged(values, params)
        if path == "/api/users/0/items" and request.method == "GET":
            if params.get("itemType") == "attachment":
                values = [i for i in self.items.values() if i["data"].get("itemType") == "attachment"]
            elif params.get("itemKey"):
                # 真实 Zotero 的 /items?itemKey= 走 includeChildren 搜索：附件、笔记子项一并返回，并排在父项附近
                values = []
                for key in params.get("itemKey").split(","):
                    if key in self.items:
                        values.append(self.items[key])
                        values.extend(self.items[c] for c in self.children.get(key, []) if c in self.items)
            else:
                values = [self.items[k] for k in self.order]
            return self._paged(values, params)
        if path == "/api/users/0/items" and request.method == "POST":
            return self._write_objects(request, self.items, order=self.order)

        m = re.fullmatch(r"/api/users/0/items/([^/]+)/children", path)
        if m:
            keys = self.children.get(m.group(1), [])
            values = [self.items[k] for k in keys if k in self.items]
            wanted = params.get("itemType")
            if wanted:
                values = [v for v in values if v["data"].get("itemType") == wanted]
            return self._paged(values, params)
        m = re.fullmatch(r"/api/users/0/items/([^/]+)/fulltext", path)
        if m:
            record = self.fulltext.get(m.group(1))
            if not record:
                return self._resp(404, text="Not found")
            return self._resp(200, headers={"Last-Modified-Version": "7"},
                              json={"content": record["content"], **record["coverage"]})
        m = re.fullmatch(r"/api/users/0/items/([^/]+)", path)
        if m:
            found = self.items.get(m.group(1))
            return self._resp(200, json=found) if found else self._resp(404, text="Not found")

        return self._resp(404, text="Not found")

    def _authorize(self):
        if self.authorize_result == "timeout":
            raise httpx.ReadTimeout("authorization dialog still open")
        if self.authorize_result == "deny":
            return self._resp(403, json={"denied": True})
        if self.authorize_result == "bad":
            return self._resp(200, json={"key": "short", "remember": True})
        self.api_key = "K" * 32
        return self._resp(200, json={"key": self.api_key, "remember": self.remember})

    def _write_objects(self, request: httpx.Request, store: dict, order: list | None = None) -> httpx.Response:
        """条目与集合共用的批量写入：固定 key + version 0 新建，带版本更新，412 冲突。"""
        if self.post_behavior == "401":
            return self._resp(401, text="Unauthorized")
        supplied = request.headers.get("Zotero-API-Key")
        if self.api_key is not None and supplied != self.api_key:
            return self._resp(401, text="Unauthorized")
        body = json.loads(request.content)
        if self.post_behavior == "timeout_before":
            raise httpx.ReadTimeout("timed out")
        successful, success, unchanged, failed = {}, {}, {}, {}
        for index, entry in enumerate(body):
            key = entry.get("key")
            version = entry.get("version")
            if index in self.fail_indices:
                failed[str(index)] = {"key": key, "code": 412, "message": "forced failure"}
                continue
            if index in self.omit_success:
                # 模拟该下标既未落库也不出现在任何结果桶中（客户端需读回核对）
                continue
            existing = store.get(key)
            if existing is None:
                if version not in (0, None):
                    failed[str(index)] = {"key": key, "code": 412, "message": "does not exist"}
                    continue
                stored_version = self._next_version()
                data = {"key": key, "version": stored_version,
                        **{k: v for k, v in entry.items() if k not in ("key", "version")}}
                store[key] = {"key": key, "version": stored_version, "links": {}, "meta": {},
                              "data": data}
                if order is not None:
                    order.append(key)
            else:
                if version is not None and int(version) != existing["version"]:
                    failed[str(index)] = {"key": key, "code": 412, "message": "version mismatch"}
                    continue
                changed = False
                for k, v in entry.items():
                    if k in ("key", "version"):
                        continue
                    if existing["data"].get(k) != v:
                        existing["data"][k] = v
                        changed = True
                if not changed:
                    unchanged[str(index)] = key
                    continue
                existing["version"] = self._next_version()
            successful[str(index)] = store[key]
            success[str(index)] = key
        outcome = {"successful": successful, "success": success, "unchanged": unchanged, "failed": failed}
        if self.post_behavior == "timeout_after":
            raise httpx.ReadTimeout("timed out after write")
        return self._resp(200, headers={"Last-Modified-Version": str(self._version_counter)}, json=outcome)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture
def fake():
    return FakeZotero()


@pytest.fixture
def client(fake):
    value = ZoteroClient(transport=httpx.MockTransport(fake.handler))
    yield value
    value.close()


@pytest.fixture
def write_disabled(knowledge_base):
    ws._grants.clear()
    ws._plans.clear()
    yield ws
    ws._grants.clear()
    ws._plans.clear()


@pytest.fixture
def write_enabled(write_disabled, knowledge_base):
    knowledge_base.settings.zotero_write_enabled = True
    return ws


def _library(fake, tmp_path):
    pdf_a = tmp_path / "AAAA1111.pdf"
    pdf_a.write_bytes(b"%PDF-1.4 alpha")
    pdf_b = tmp_path / "BBBB2222.pdf"
    pdf_b.write_bytes(b"%PDF-1.4 beta")
    missing = tmp_path / "DDDD4444" / "gone.pdf"
    fake.add_item(_item("ITEMAAAA", title="Alpha", creators=[{"creatorType": "author", "lastName": "Lin"}],
                        collections=["COLLROOT"], date="2024-01-02"))
    fake.add_item(_item("ITEMBBBB", title="Beta", collections=["COLLCHLD"]))
    fake.add_item(_item("ITEMCCCC", title="No PDF"))
    att_a = fake.add_item(_attachment("AAAA1111", "ITEMAAAA", pdf_a))
    att_b = fake.add_item(_attachment("BBBB2222", "ITEMBBBB", pdf_b))
    att_missing = fake.add_item(_attachment("DDDD4444", "ITEMCCCC", missing))
    anno = fake.add_item(_item("ANNO0001", item_type="annotation", parentItem="AAAA1111",
                               annotationType="highlight", annotationText="重点", annotationComment="批注"))
    note = fake.add_item(_item("NOTE0001", item_type="note", parentItem="ITEMAAAA", note="<div>子笔记</div>"))
    fake.collections["COLLROOT"] = _collection("COLLROOT", "顶层")
    fake.collections["COLLCHLD"] = _collection("COLLCHLD", "子集", "COLLROOT")
    fake.children = {"ITEMAAAA": ["AAAA1111", "NOTE0001"],
                     "AAAA1111": ["ANNO0001"],
                     "ITEMBBBB": ["BBBB2222"],
                     "ITEMCCCC": ["DDDD4444"]}
    fake.fulltext["AAAA1111"] = {"content": "full text body", "coverage": {"indexedChars": 5, "totalChars": 10}}
    return {"pdf_a": pdf_a, "pdf_b": pdf_b, "att_a": att_a, "att_b": att_b,
            "att_missing": att_missing, "anno": anno, "note": note}


# ---------------------------------------------------------------------------
# 1. 版本门控：旧版只读降级，10.0.5+ 才允许安全写
# ---------------------------------------------------------------------------

def test_old_zotero_read_only_no_server_id(fake, client):
    fake.version = "9.0.0"
    fake.server_id = None
    info = client.probe()
    assert info["zotero_version"] == "9.0.0"
    assert info["server_id"] is None
    assert info["native_write_supported"] is False
    assert info["write_supported"] is False
    assert info["identity_verified"] is False


@pytest.mark.parametrize("version,expected", [
    ("10.0.0", False), ("10.0.2", False), ("10.0.4", False), ("10.0.5", True), ("10.0.6", True),
])
def test_write_supported_gate(fake, client, version, expected):
    fake.version = version
    info = client.probe()
    assert info["native_write_supported"] is True  # 10.0 起有实例标识
    assert info["write_supported"] is expected
    assert info["minimum_write_version"] == "10.0.5"


def test_write_info_rejects_pre_10_0_5(fake, client):
    fake.version = "10.0.2"
    with pytest.raises(BusinessError) as exc:
        client.probe() and ws._write_info(client)
    assert exc.value.code == "ZOTERO_UNSUPPORTED"


@pytest.mark.parametrize("address", [
    "https://example.com/api", "http://127.0.0.1:bad/api", "http://[::1/api",
    "http://secret@localhost:23119/api", "http://localhost:23119/api?key=secret", "",
])
def test_client_rejects_nonlocal_or_malformed_addresses(address):
    with pytest.raises(BusinessError) as error:
        ZoteroClient(address)
    assert error.value.code == "INVALID_INPUT"


def test_write_requires_server_id(fake):
    value = ZoteroClient(transport=httpx.MockTransport(fake.handler))
    try:
        with pytest.raises(BusinessError) as exc:
            value._request("POST", "/users/0/items", json=[])
        assert exc.value.code == "ZOTERO_UNSUPPORTED"
    finally:
        value.close()


def test_status_capabilities_reflect_version(fake, write_disabled):
    fake.version = "10.0.6"
    with ZoteroClient(transport=httpx.MockTransport(fake.handler)) as value:
        status = zotero.status(client=value)
    assert status["connected"] is True
    assert status["write_enabled"] is False
    assert status["capabilities"]["write"] is False
    assert status["capabilities"]["search_scope"] == "title_creator_year"


# ---------------------------------------------------------------------------
# 2. 纯查询：分页 / 子项 / 批注 / 全文，不触发导入与授权
# ---------------------------------------------------------------------------

def test_search_pagination_stays_read_only(fake, client, tmp_path):
    _library(fake, tmp_path)
    result = zotero.search(query="Alpha", limit=1, offset=0, client=client)
    assert result["zotero"]["write_supported"] is True
    assert result["search_scope"] == "title_creator_year"
    assert result["fulltext_searched"] is False and result["annotations_searched"] is False
    page_req = next(r for r in fake.requests if r.url.path == "/api/users/0/items/top")
    assert page_req.url.params["qmode"] == "titleCreatorYear"
    assert page_req.url.params["q"] == "Alpha"
    assert not fake.authorize_calls()
    assert all(r.method == "GET" for r in fake.requests)


def test_children_note_and_annotation(fake, client, tmp_path):
    _library(fake, tmp_path)
    notes = zotero.get_children("ITEMAAAA", item_type="note", client=client)
    assert [i["key"] for i in notes["items"]] == ["NOTE0001"]
    assert notes["parent"]["key"] == "ITEMAAAA"
    with pytest.raises(BusinessError) as exc:
        zotero.get_children("ITEMAAAA", item_type="annotation", client=client)
    assert exc.value.code == "INVALID_INPUT"
    annos = zotero.get_children("AAAA1111", item_type="annotation", client=client)
    assert [i["key"] for i in annos["items"]] == ["ANNO0001"]
    assert annos["items"][0]["source_kind"] == "user_annotation"


def test_fulltext_partial_coverage(fake, client, tmp_path):
    _library(fake, tmp_path)
    result = zotero.get_fulltext("AAAA1111", client=client)
    assert result["content"] == "full text body"
    assert result["coverage"] == {"indexedChars": 5, "totalChars": 10}
    assert result["complete"] is False
    assert result["source_kind"] == "zotero_indexed_fulltext"
    assert result["notice"]
    with pytest.raises(BusinessError) as exc:
        zotero.get_fulltext("ITEMAAAA", client=client)
    assert exc.value.code == "INVALID_INPUT"


def test_fulltext_unavailable_offers_import_preview_path(fake, client, tmp_path):
    _library(fake, tmp_path)
    fake.fulltext.pop("AAAA1111", None)
    with pytest.raises(BusinessError) as exc:
        zotero.get_fulltext("AAAA1111", server_id=SERVER_ID, client=client)
    error = exc.value
    assert error.code == "ZOTERO_FULLTEXT_UNAVAILABLE"
    assert error.data["attachment_key"] == "AAAA1111" and error.data["parent_item_key"] == "ITEMAAAA"
    assert error.data["next_tool"]["name"] == "zotero_preview_import"
    assert error.data["next_tool"]["arguments"] == {"attachment_keys": ["AAAA1111"], "server_id": SERVER_ID}
    assert error.data["next_command"] == f"zotero preview --attachment-key AAAA1111 --server-id {SERVER_ID} --json"
    assert "zotero_preview_import" in str(error)
    assert not fake.authorize_calls()


# ---------------------------------------------------------------------------
# 3. titleCreatorYear 限定、局部集合不存在不退全库、citation 格式
# ---------------------------------------------------------------------------

def test_search_unknown_collection_does_not_fall_back(fake, client, tmp_path):
    _library(fake, tmp_path)
    with pytest.raises(BusinessError) as exc:
        zotero.search(collection_key="NOPE0000", client=client)
    assert exc.value.code == "NOT_FOUND"
    assert "/api/users/0/items/top" not in fake.paths()


def test_export_citation_params(fake, client, tmp_path):
    _library(fake, tmp_path)
    result = zotero.export_citations(["ITEMAAAA"], format="citation", style="apa",
                                     locale="zh-CN", client=client)
    assert result["format"] == "citation"
    request = next(r for r in fake.requests if r.url.path == "/api/users/0/items/top" and r.method == "GET")
    assert request.url.params["include"] == "citation"
    assert request.url.params["format"] == "json"
    assert request.url.params["style"] == "apa" and request.url.params["locale"] == "zh-CN"
    with pytest.raises(BusinessError) as exc:
        zotero.export_citations(["AAAA1111"], client=client)
    assert exc.value.code == "INVALID_INPUT"


def test_export_citations_keeps_every_requested_item(fake, client, tmp_path):
    """/items?itemKey= 会连带返回附件、笔记子项，再按 limit 截断就会漏掉文献；必须走 /items/top。"""
    _library(fake, tmp_path)
    keys = ["ITEMAAAA", "ITEMBBBB", "ITEMCCCC"]  # ITEMAAAA 带附件和子笔记
    result = zotero.export_citations(keys, format="csljson", client=client)
    assert len(result["content"]) == 3 and "warning" not in result
    assert [entry["key"] for entry in result["content"]] == keys
    assert all(r.url.path != "/api/users/0/items" for r in fake.requests if r.method == "GET")


def test_list_tags_in_collection_uses_items_tags_route(fake, client, tmp_path):
    """Local API 没有 /collections/{key}/tags；带 collection_key 必须查 /collections/{key}/items/tags。"""
    _library(fake, tmp_path)
    fake.items["ITEMAAAA"]["data"]["tags"] = [{"tag": "已读"}]
    fake.items["NOTE0001"]["data"]["tags"] = [{"tag": "待核"}]
    fake.items["ITEMBBBB"]["data"]["tags"] = [{"tag": "其他集合"}]
    result = zotero.list_tags(collection_key="COLLROOT", client=client)
    assert [entry["tag"] for entry in result["items"]] == ["已读", "待核"]
    assert "/api/users/0/collections/COLLROOT/items/tags" in fake.paths()
    assert "/api/users/0/collections/COLLROOT/tags" not in fake.paths()
    whole = zotero.list_tags(client=client)
    assert {entry["tag"] for entry in whole["items"]} == {"已读", "待核", "其他集合"}
    with pytest.raises(BusinessError) as exc:
        zotero.list_tags(collection_key="NOPE0000", client=client)
    assert exc.value.code == "NOT_FOUND"


# ---------------------------------------------------------------------------
# 4. 选定导入：item_keys/attachment_keys 不扫全库，空/未知不退整库
# ---------------------------------------------------------------------------

def test_item_keys_select_without_scanning_library(knowledge_base, fake, client, tmp_path):
    _library(fake, tmp_path)
    preview = zotero.preview_import(item_keys=["ITEMAAAA"], client=client)
    assert [i["key"] for i in preview["items"]] == ["ITEMAAAA"]
    assert "/api/users/0/items/top" not in fake.paths()
    assert all(r.url.path != "/api/users/0/items" or r.url.params.get("itemType") != "attachment"
               for r in fake.requests)


def test_attachment_keys_select(knowledge_base, fake, client, tmp_path):
    _library(fake, tmp_path)
    preview = zotero.preview_import(attachment_keys=["AAAA1111"], client=client)
    assert [i["attachment_key"] for i in preview["items"]] == ["AAAA1111"]
    assert preview["items"][0]["key"] == "ITEMAAAA"
    mixed = zotero.preview_import(item_keys=["ITEMAAAA", "AAAA1111"], client=client)
    assert len(mixed["items"]) == 1


def test_empty_or_unknown_selector_never_returns_whole_library(fake, client, tmp_path):
    _library(fake, tmp_path)
    with pytest.raises(BusinessError) as empty:
        zotero.preview_import(item_keys=[], client=client)
    assert empty.value.code == "INVALID_INPUT"
    with pytest.raises(BusinessError) as both:
        zotero.preview_import(item_keys=["ITEMAAAA"], attachment_keys=["AAAA1111"], client=client)
    assert both.value.code == "INVALID_INPUT"
    with pytest.raises(BusinessError) as unknown:
        zotero.preview_import(item_keys=["ZZZZZZZZ"], client=client)
    assert unknown.value.code == "NOT_FOUND"
    assert "/api/users/0/items/top" not in fake.paths()


def test_preview_reports_missing_and_existing_hash(knowledge_base, fake, client, tmp_path):
    from indexing.services import file_service
    lib = _library(fake, tmp_path)
    fresh = zotero.preview_import(client=client)
    by_key = {i["attachment_key"]: i for i in fresh["items"]}
    assert by_key["DDDD4444"]["available"] is False and by_key["DDDD4444"]["reason"] == "file_missing"
    assert by_key["AAAA1111"]["available"] is True and by_key["AAAA1111"]["duplicate"] is False

    file_service.import_file(lib["pdf_a"], filename="alpha.pdf")
    again = zotero.preview_import(client=client)
    by_key = {i["attachment_key"]: i for i in again["items"]}
    assert by_key["AAAA1111"]["duplicate"] is True and by_key["AAAA1111"]["file_id"] is not None


def test_two_sources_same_hash_collapse_to_one_file(knowledge_base, fake, client, tmp_path):
    import pymupdf

    lib = _library(fake, tmp_path)
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Alpha research document")
        lib["pdf_a"].write_bytes(pdf.tobytes())
    # 两个不同条目、不同附件 key，指向同一份可解析的 PDF
    lib["pdf_b"].write_bytes(lib["pdf_a"].read_bytes())
    result = zotero.import_library(item_keys=["ITEMAAAA", "ITEMBBBB"], client=client)
    assert result["accepted_count"] == 1
    assert result["duplicate_count"] == 1
    assert len({i["file_id"] for i in result["items"]}) == 1
    links = zotero.get_links(item_keys=["ITEMAAAA", "ITEMBBBB"], limit=1, client=client)
    assert links["total"] == 2 and links["has_more"] and links["next_offset"] == 1
    second = zotero.get_links(item_keys=["ITEMAAAA", "ITEMBBBB"], offset=1, client=client)
    assert {links["links"][0]["source"]["item_key"], second["links"][0]["source"]["item_key"]} == {"ITEMAAAA", "ITEMBBBB"}
    assert links["links"][0]["file_id"] == second["links"][0]["file_id"]
    assert links["links"][0]["source"]["file_hash"] == links["links"][0]["file_hash"]
    knowledge_base.drain()
    from indexing.services import task_service
    assert all(task_service.get_task(task_id)["status"] == "completed" for task_id in result["task_ids"])


def test_import_batch_limit_is_checked_before_writing(knowledge_base, fake, client, tmp_path):
    _library(fake, tmp_path)
    with pytest.raises(BusinessError) as exc:
        zotero.import_library(item_keys=["ITEMAAAA", "ITEMBBBB"], max_items=1, client=client)
    # 超限是独立错误码并说明“范围有效、请分批”，不再混在 INVALID_INPUT 里让调用方误以为不支持该范围。
    assert exc.value.code == "IMPORT_LIMIT_EXCEEDED"
    assert "超过单批上限 1 个" in str(exc.value) and "范围本身有效" in str(exc.value)
    assert exc.value.data["max_items"] == 1 and exc.value.data["scope"] == "条目"
    from indexing.services import file_service
    assert file_service.get_files_list() == []
    with pytest.raises(BusinessError) as by_collection:
        zotero.preview_import(collection_keys=["COLLROOT"], max_items=1, client=client)
    assert by_collection.value.code == "IMPORT_LIMIT_EXCEEDED" and by_collection.value.data["scope"] == "集合"
    assert by_collection.value.data["collection_keys"] == ["COLLROOT"]


def test_search_scope_everything_uses_zotero_quicksearch(fake, client, tmp_path):
    _library(fake, tmp_path)
    result = zotero.search(query="Alpha", scope="everything", client=client)
    assert result["search_scope"] == "everything"
    assert result["fulltext_searched"] and result["notes_searched"] and result["annotations_searched"]
    page_req = next(r for r in fake.requests if r.url.path == "/api/users/0/items/top")
    assert page_req.url.params["qmode"] == "everything" and page_req.url.params["q"] == "Alpha"
    assert all(r.method == "GET" for r in fake.requests)
    with pytest.raises(BusinessError) as exc:
        zotero.search(query="Alpha", scope="fulltext", client=client)
    assert exc.value.code == "INVALID_INPUT"
    with ZoteroClient(transport=httpx.MockTransport(fake.handler)) as value:
        status = zotero.status(client=value)
    assert status["capabilities"]["search_scope"] == "title_creator_year"
    assert status["capabilities"]["search_scopes"] == ["everything", "title_creator_year"]


def test_api_reuses_scope_validation(api):
    result = api.post("/api/v1/zotero/preview", json={"collection_keys": ["COLLROOT"], "item_keys": ["ITEMAAAA"]})
    assert result.status_code == 400 and result.json()["error"]["code"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# 5. 写开关、授权 key 不外泄、拒绝不自动弹窗、单次批量
# ---------------------------------------------------------------------------

def test_write_disabled_blocks_authorize_and_apply(fake, client, write_disabled, tmp_path):
    _library(fake, tmp_path)
    with pytest.raises(BusinessError) as auth:
        ws.authorize(SERVER_ID, client=client)
    assert auth.value.code == "FORBIDDEN"
    with pytest.raises(BusinessError) as applied:
        ws.apply_changes("any-plan", confirmed=True, client=client)
    assert applied.value.code == "FORBIDDEN"
    assert not fake.authorize_calls()


def test_authorize_never_returns_key(fake, client, write_enabled):
    result = ws.authorize(SERVER_ID, client=client)
    assert result["authorized"] is True and result["single_use"] is False
    assert "key" not in result
    assert ws._grants[SERVER_ID]["key"] not in json.dumps(result)
    assert ws._grants[SERVER_ID]["key"] == "K" * 32


def test_authorize_rejects_bad_response(fake, client, write_enabled):
    fake.authorize_result = "bad"
    with pytest.raises(BusinessError) as exc:
        ws.authorize(SERVER_ID, client=client)
    assert exc.value.code == "ZOTERO_ERROR"
    assert SERVER_ID not in ws._grants


def test_denied_authorize_does_not_auto_reprompt(fake, client, write_enabled):
    fake.authorize_result = "deny"
    with pytest.raises(BusinessError) as exc:
        ws.authorize(SERVER_ID, client=client)
    assert exc.value.code == "ZOTERO_AUTH_DENIED"
    assert len(fake.authorize_calls()) == 1


def test_authorize_timeout_is_not_reported_as_uncertain_write(fake, client, write_enabled):
    """用户在弹窗上停留过久：不是写入不确定，而是本次授权作废，需要重新授权。"""
    fake.authorize_result = "timeout"
    with pytest.raises(BusinessError) as exc:
        ws.authorize(SERVER_ID, client=client)
    assert exc.value.code == "ZOTERO_AUTH_TIMEOUT"
    assert "重新请求授权" in str(exc.value)
    assert SERVER_ID not in ws._grants
    assert len(fake.authorize_calls()) == 1  # 不自动重试弹窗


def test_single_use_grant_explains_reauthorization(fake, client, write_enabled, tmp_path):
    """单次 key 在一次 apply 后消耗；下一个计划必须提示重新授权，而不是让用户困惑为何要弹两次。"""
    _library(fake, tmp_path)
    fake.remember = False
    first = ws.authorize(SERVER_ID, client=client)
    assert first["single_use"] is True and "始终允许" in first["notice"]
    plan_a = ws.preview_changes([{"action": "update_item", "item_key": "ITEMAAAA", "fields": {"title": "A2"}}],
                                SERVER_ID, client=client)
    assert ws.apply_changes(plan_a["plan_id"], confirmed=True, client=client)["completed"] is True
    plan_b = ws.preview_changes([{"action": "update_item", "item_key": "ITEMBBBB", "fields": {"title": "B2"}}],
                                SERVER_ID, client=client)
    with pytest.raises(BusinessError) as exc:
        ws.apply_changes(plan_b["plan_id"], confirmed=True, client=client)
    assert exc.value.code == "ZOTERO_AUTH_REQUIRED" and exc.value.data["needs_authorization"] is True
    assert len(fake.authorize_calls()) == 1  # 服务端不替用户再次弹窗
    assert fake.items["ITEMBBBB"]["data"]["title"] == "Beta"


def test_single_use_key_applies_batch_in_one_post(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    fake.remember = False
    ws.authorize(SERVER_ID, client=client)
    plan = ws.preview_changes([
        {"action": "update_item", "item_key": "ITEMAAAA", "fields": {"title": "Alpha v2"}},
        {"action": "update_item", "item_key": "ITEMBBBB", "fields": {"title": "Beta v2"}},
    ], SERVER_ID, client=client)
    result = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert result["successful_count"] == 2 and result["partial"] is False
    assert len(fake.post_item_calls()) == 1
    request = fake.post_item_calls()[0]
    assert request.headers["Zotero-Server-ID"] == SERVER_ID
    assert all(item["version"] == 5 for item in json.loads(request.content))
    assert SERVER_ID not in ws._grants  # 单次 key 用后即弃


# ---------------------------------------------------------------------------
# 6. 元数据白名单：拒绝受保护字段与未知字段
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"deleted": "true"}, {"itemType": "book"}, {"parentItem": "ITEMBBBB"},
    {"version": 9}, {"key": "ZZZZZZZZ"}, {"notAField": "x"},
])
def test_metadata_whitelist_rejects_protected_fields(fake, client, write_enabled, tmp_path, bad):
    _library(fake, tmp_path)
    with pytest.raises(BusinessError) as exc:
        ws.preview_changes([{"action": "update_item", "item_key": "ITEMAAAA", "fields": bad}],
                           SERVER_ID, client=client)
    assert exc.value.code == "INVALID_INPUT"


def test_metadata_rejects_unknown_action_and_extra_keys(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    with pytest.raises(BusinessError):
        ws.preview_changes([{"action": "purge_item", "item_key": "ITEMAAAA"}], SERVER_ID, client=client)
    with pytest.raises(BusinessError):
        ws.preview_changes([{"action": "update_item", "item_key": "ITEMAAAA", "extra": 1}],
                           SERVER_ID, client=client)


def test_metadata_allows_whitelisted_field(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "update_item", "item_key": "ITEMAAAA",
                                "fields": {"title": "新标题"}}], SERVER_ID, client=client)
    change = plan["changes"][0]
    assert change["after"] == {"title": "新标题"}
    assert change["before"] == {"title": "Alpha"}
    assert change["status"] == "pending"


# ---------------------------------------------------------------------------
# 7. 预览 before/after、apply 需 confirmed、版本与实例冲突
# ---------------------------------------------------------------------------

def test_apply_requires_confirmation(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "update_item", "item_key": "ITEMAAAA",
                                "fields": {"title": "X"}}], SERVER_ID, client=client)
    with pytest.raises(BusinessError) as exc:
        ws.apply_changes(plan["plan_id"], client=client)
    assert exc.value.code == "CONFIRMATION_REQUIRED"
    assert not fake.post_item_calls()


def test_apply_version_conflict_does_not_overwrite(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "update_item", "item_key": "ITEMAAAA",
                                "fields": {"title": "X"}}], SERVER_ID, client=client)
    fake.items["ITEMAAAA"]["version"] = 99
    fake.items["ITEMAAAA"]["data"]["title"] = "外部改动"
    result = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert result["failed_count"] == 1 and result["items"][0]["error"]["code"] == "VERSION_CONFLICT"
    assert fake.items["ITEMAAAA"]["data"]["title"] == "外部改动"
    assert not fake.post_item_calls()


def test_apply_instance_change_detected(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "update_item", "item_key": "ITEMAAAA",
                                "fields": {"title": "X"}}], SERVER_ID, client=client)
    fake.server_id = "OTHERSRV0001"
    with pytest.raises(BusinessError) as exc:
        ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert exc.value.code == "ZOTERO_INSTANCE_CHANGED"


# ---------------------------------------------------------------------------
# 8. 客户端固定 note key/version0、批量部分失败即使 200 也识别
# ---------------------------------------------------------------------------

def test_create_note_uses_client_key_and_version_zero(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "create_note", "content": "结论摘要",
                                "parent_item_key": "ITEMAAAA"}], SERVER_ID, client=client)
    change = plan["changes"][0]
    assert _KEY_RE.match(change["key"]) and change["version"] == 0
    assert change["after"]["itemType"] == "note" and change["after"]["parentItem"] == "ITEMAAAA"
    assert "zotero://select/library/items/ITEMAAAA" in change["after"]["note"]


def test_create_note_parent_and_collections_conflict(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    with pytest.raises(BusinessError):
        ws.preview_changes([{"action": "create_note", "content": "x", "parent_item_key": "ITEMAAAA",
                             "collection_keys": ["COLLROOT"]}], SERVER_ID, client=client)


def test_apply_partial_failure_even_on_http_200(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([
        {"action": "update_item", "item_key": "ITEMAAAA", "fields": {"title": "A2"}},
        {"action": "update_item", "item_key": "ITEMBBBB", "fields": {"title": "B2"}},
    ], SERVER_ID, client=client)
    fake.fail_indices = {1}
    ws.authorize(SERVER_ID, client=client)
    result = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert result["successful_count"] == 1 and result["failed_count"] == 1
    assert result["partial"] is True and result["completed"] is False
    failed = next(i for i in result["items"] if i["key"] == "ITEMBBBB")
    assert failed["error"]["code"] == "VERSION_CONFLICT"


# ---------------------------------------------------------------------------
# 9. 超时读回核对、同 key/token 重试、401 清 cache 不自动授权
# ---------------------------------------------------------------------------

def test_timeout_after_write_is_reconciled_by_read_back(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "create_note", "content": "超时笔记"}],
                              SERVER_ID, client=client)
    ws.authorize(SERVER_ID, client=client)
    fake.post_behavior = "timeout_after"  # 服务端已写入，但客户端超时
    result = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert result["successful_count"] == 1 and result["failed_count"] == 0
    assert len(fake.post_item_calls()) == 1  # 不重复创建


@pytest.mark.parametrize("code", ["ZOTERO_RATE_LIMITED", "ZOTERO_ERROR"])
def test_uncertain_write_survives_temporary_read_failure(fake, client, write_enabled, tmp_path, monkeypatch, code):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "create_note", "content": "只创建一次"}], SERVER_ID, client=client)
    ws.authorize(SERVER_ID, client=client)
    fake.post_behavior = "timeout_after"
    read_item = client.item
    unavailable = True

    def temporary_failure(key):
        if unavailable and fake.post_item_calls():
            raise BusinessError(code, "暂时无法核对")
        return read_item(key)

    monkeypatch.setattr(client, "item", temporary_failure)
    first = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    second = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert first["pending_count"] == second["pending_count"] == 1
    assert second["failed_count"] == 0 and second["items"][0]["status"] == "uncertain"
    unavailable = False
    recovered = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert recovered["successful_count"] == 1 and recovered["pending_count"] == 0
    assert len(fake.post_item_calls()) == 1


def test_retry_with_same_plan_reuses_key_and_token(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "create_note", "content": "重试笔记"}],
                              SERVER_ID, client=client)
    ws.authorize(SERVER_ID, client=client)
    fake.post_behavior = "timeout_before"  # 服务端未写入
    first = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert first["pending_count"] >= 1 and fake.post_item_calls()
    fake.post_behavior = None
    second = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert second["successful_count"] == 1
    tokens = [r.headers.get("Zotero-Write-Token") for r in fake.post_item_calls()]
    assert len(set(tokens)) == 1 and tokens[0]
    created = [v for v in fake.items.values() if v["data"].get("itemType") == "note"
               and v["key"] != "NOTE0001"]
    assert len(created) == 1  # 未重复创建


def test_partial_success_is_not_lost_on_retry(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([
        {"action": "create_note", "content": "一"},
        {"action": "create_note", "content": "二"},
    ], SERVER_ID, client=client)
    ws.authorize(SERVER_ID, client=client)
    fake.omit_success = {1}  # 只成功第 0 项
    first = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert first["successful_count"] == 1 and first["pending_count"] == 1
    fake.omit_success = set()
    second = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert second["successful_count"] == 2 and second["partial"] is False
    keys = [i["key"] for i in second["items"]]
    assert len(set(keys)) == 2
    assert all(v["key"] not in (None,) for v in fake.items.values())


def test_401_clears_grant_without_auto_authorize(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "update_item", "item_key": "ITEMAAAA",
                                "fields": {"title": "X"}}], SERVER_ID, client=client)
    ws.authorize(SERVER_ID, client=client)
    before = len(fake.authorize_calls())
    fake.post_behavior = "401"
    result = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert result["needs_authorization"] is True
    assert len(fake.authorize_calls()) == before  # 不自动重新弹授权
    assert ws.authorization_status(SERVER_ID)["state"] == "required"


# ---------------------------------------------------------------------------
# 10. 幂等、过期 plan、笔记父子关系
# ---------------------------------------------------------------------------

def test_reapply_is_idempotent(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "update_item", "item_key": "ITEMAAAA",
                                "fields": {"title": "A2"}}], SERVER_ID, client=client)
    ws.authorize(SERVER_ID, client=client)
    first = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert first["successful_count"] == 1
    posts = len(fake.post_item_calls())
    second = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert second["successful_count"] == 1 and second["failed_count"] == 0
    assert len(fake.post_item_calls()) == posts  # 不重做


def test_expired_plan_is_rejected(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "update_item", "item_key": "ITEMAAAA",
                                "fields": {"title": "X"}}], SERVER_ID, client=client)
    ws._plans[plan["plan_id"]]["created"] = time.monotonic() - ws.PLAN_TTL_SECONDS - 1
    with pytest.raises(BusinessError) as exc:
        ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert exc.value.code == "ZOTERO_PLAN_EXPIRED"


def test_update_note_requires_note_item(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    with pytest.raises(BusinessError) as exc:
        ws.preview_changes([{"action": "update_note", "item_key": "ITEMAAAA", "content": "x"}],
                           SERVER_ID, client=client)
    assert exc.value.code == "INVALID_INPUT"


def test_note_parent_relationship_recorded(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "create_note", "content": "父笔记",
                                "parent_item_key": "ITEMAAAA"}], SERVER_ID, client=client)
    ws.authorize(SERVER_ID, client=client)
    ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert fake.post_item_calls()
    note = next(v for v in fake.items.values() if v["data"].get("itemType") == "note"
                and v["key"] != "NOTE0001")
    assert note["data"]["parentItem"] == "ITEMAAAA"


# ---------------------------------------------------------------------------
# 11. create_item：按 item_type 校验字段、固定 key/version 0、集合与作者校验
# ---------------------------------------------------------------------------

def test_create_item_preview_and_apply(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{
        "action": "create_item", "item_type": "journalArticle",
        "fields": {"title": "新论文", "DOI": "10.1/x"},
        "creators": [{"creatorType": "author", "lastName": "Li"}],
        "tags": ["未读"], "collection_keys": ["COLLROOT"],
    }], SERVER_ID, client=client)
    change = plan["changes"][0]
    assert _KEY_RE.match(change["key"]) and change["version"] == 0
    assert change["before"] == {} and change["status"] == "pending"
    assert change["after"]["itemType"] == "journalArticle"
    assert change["after"]["title"] == "新论文" and change["after"]["DOI"] == "10.1/x"
    assert change["after"]["creators"] == [{"creatorType": "author", "lastName": "Li"}]
    assert change["after"]["tags"] == [{"tag": "未读"}]
    assert change["after"]["collections"] == ["COLLROOT"]
    ws.authorize(SERVER_ID, client=client)
    result = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert result["successful_count"] == 1 and result["completed"] is True
    created = fake.items[change["key"]]["data"]
    assert created["itemType"] == "journalArticle" and created["title"] == "新论文"
    assert len(fake.post_collection_calls()) == 0  # 新建条目走 /items


def test_create_item_rejects_unknown_item_type(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    with pytest.raises(BusinessError) as exc:
        ws.preview_changes([{"action": "create_item", "item_type": "noSuchType"}], SERVER_ID, client=client)
    assert exc.value.code == "INVALID_INPUT"


def test_create_item_rejects_note_like_type(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    with pytest.raises(BusinessError) as exc:
        ws.preview_changes([{"action": "create_item", "item_type": "note", "fields": {"title": "x"}}],
                           SERVER_ID, client=client)
    assert exc.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("change", [
    {"action": "create_item", "item_type": "journalArticle", "fields": {"notAField": "x"}},
    {"action": "create_item", "item_type": "journalArticle", "fields": {"itemType": "book"}},
    {"action": "create_item", "item_type": "journalArticle", "fields": {"deleted": "true"}},
    {"action": "create_item", "item_type": "journalArticle", "creators": [{"creatorType": "bogus", "lastName": "x"}]},
])
def test_create_item_field_and_creator_whitelist(fake, client, write_enabled, tmp_path, change):
    _library(fake, tmp_path)
    with pytest.raises(BusinessError) as exc:
        ws.preview_changes([change], SERVER_ID, client=client)
    assert exc.value.code == "INVALID_INPUT"


def test_create_item_rejects_unknown_collection(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    with pytest.raises(BusinessError) as exc:
        ws.preview_changes([{"action": "create_item", "item_type": "book",
                             "collection_keys": ["NOPE0000"]}], SERVER_ID, client=client)
    assert exc.value.code == "NOT_FOUND"


# ---------------------------------------------------------------------------
# 12. 集合操作：新建 / 改名 / 删除（进回收站，条目不删）
# ---------------------------------------------------------------------------

def test_create_collection_preview_and_apply(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "create_collection", "name": "新集合", "parent_key": "COLLROOT"}],
                              SERVER_ID, client=client)
    change = plan["changes"][0]
    assert _KEY_RE.match(change["key"]) and change["version"] == 0
    assert change["after"] == {"name": "新集合", "parentCollection": "COLLROOT"}
    ws.authorize(SERVER_ID, client=client)
    result = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert result["successful_count"] == 1
    assert len(fake.post_collection_calls()) == 1 and not fake.post_item_calls()
    assert fake.collections[change["key"]]["data"]["name"] == "新集合"


def test_create_collection_requires_existing_parent(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    with pytest.raises(BusinessError) as exc:
        ws.preview_changes([{"action": "create_collection", "name": "x", "parent_key": "NOPE0000"}],
                           SERVER_ID, client=client)
    assert exc.value.code == "NOT_FOUND"
    with pytest.raises(BusinessError) as empty:
        ws.preview_changes([{"action": "create_collection", "name": "  "}], SERVER_ID, client=client)
    assert empty.value.code == "INVALID_INPUT"


def test_rename_collection_uses_read_version(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "rename_collection", "collection_key": "COLLROOT", "name": "改名"}],
                              SERVER_ID, client=client)
    change = plan["changes"][0]
    assert change["key"] == "COLLROOT" and change["version"] == 5
    assert change["before"] == {"name": "顶层"} and change["after"] == {"name": "改名"}
    ws.authorize(SERVER_ID, client=client)
    result = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert result["successful_count"] == 1
    assert fake.collections["COLLROOT"]["data"]["name"] == "改名"


def test_rename_collection_version_conflict(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "rename_collection", "collection_key": "COLLROOT", "name": "改名"}],
                              SERVER_ID, client=client)
    fake.collections["COLLROOT"]["version"] = 99
    fake.collections["COLLROOT"]["data"]["name"] = "外部改动"
    result = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert result["failed_count"] == 1 and result["items"][0]["error"]["code"] == "VERSION_CONFLICT"
    assert fake.collections["COLLROOT"]["data"]["name"] == "外部改动"
    assert not fake.post_collection_calls()


def test_delete_collection_trashes_but_keeps_items(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "delete_collection", "collection_key": "COLLCHLD"}],
                              SERVER_ID, client=client)
    change = plan["changes"][0]
    assert change["version"] == 5 and change["after"] == {"deleted": True}
    ws.authorize(SERVER_ID, client=client)
    result = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert result["successful_count"] == 1
    assert fake.collections["COLLCHLD"]["data"]["deleted"] is True
    assert "ITEMBBBB" in fake.items  # 条目不随集合删除而消失


def test_delete_collection_rejects_trashed_collection(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    fake.collections["COLLCHLD"]["data"]["deleted"] = True
    with pytest.raises(BusinessError) as exc:
        ws.preview_changes([{"action": "delete_collection", "collection_key": "COLLCHLD"}],
                           SERVER_ID, client=client)
    assert exc.value.code == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# 13. delete_item：移入回收站，拒绝已删除条目
# ---------------------------------------------------------------------------

def test_delete_item_moves_to_trash(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "delete_item", "item_key": "ITEMAAAA"}], SERVER_ID, client=client)
    change = plan["changes"][0]
    assert change["key"] == "ITEMAAAA" and change["version"] == 5
    assert change["before"] == {"deleted": False} and change["after"] == {"deleted": True}
    ws.authorize(SERVER_ID, client=client)
    result = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert result["successful_count"] == 1 and result["completed"] is True
    assert fake.items["ITEMAAAA"]["data"]["deleted"] is True  # 只置 deleted，不物理删除


def test_delete_item_rejects_already_trashed(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    fake.items["ITEMAAAA"]["data"]["deleted"] = True
    with pytest.raises(BusinessError) as exc:
        ws.preview_changes([{"action": "delete_item", "item_key": "ITEMAAAA"}], SERVER_ID, client=client)
    assert exc.value.code == "INVALID_INPUT"


def test_delete_item_version_conflict(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([{"action": "delete_item", "item_key": "ITEMAAAA"}], SERVER_ID, client=client)
    fake.items["ITEMAAAA"]["version"] = 99
    result = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert result["failed_count"] == 1 and result["items"][0]["error"]["code"] == "VERSION_CONFLICT"
    assert not fake.items["ITEMAAAA"]["data"].get("deleted")


# ---------------------------------------------------------------------------
# 14. 去重与混合批量：同对象合并为一项，条目与集合分端点写入
# ---------------------------------------------------------------------------

def test_same_collection_duplicate_is_rejected(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    with pytest.raises(BusinessError) as exc:
        ws.preview_changes([{"action": "delete_collection", "collection_key": "COLLROOT"},
                            {"action": "rename_collection", "collection_key": "COLLROOT", "name": "x"}],
                           SERVER_ID, client=client)
    assert exc.value.code == "INVALID_INPUT"
    with pytest.raises(BusinessError):
        ws.preview_changes([{"action": "delete_item", "item_key": "ITEMAAAA"},
                            {"action": "delete_item", "item_key": "ITEMAAAA"}], SERVER_ID, client=client)


def test_mixed_batch_writes_items_and_collections_separately(fake, client, write_enabled, tmp_path):
    _library(fake, tmp_path)
    plan = ws.preview_changes([
        {"action": "update_item", "item_key": "ITEMAAAA", "fields": {"title": "改标题"}},
        {"action": "create_collection", "name": "新集合"},
    ], SERVER_ID, client=client)
    ws.authorize(SERVER_ID, client=client)  # 默认 remember=True，两类端点共用同一授权
    result = ws.apply_changes(plan["plan_id"], confirmed=True, client=client)
    assert result["successful_count"] == 2 and result["completed"] is True
    assert len(fake.post_item_calls()) == 1 and len(fake.post_collection_calls()) == 1
    assert fake.items["ITEMAAAA"]["data"]["title"] == "改标题"
    assert any(c["data"]["name"] == "新集合" for c in fake.collections.values())
