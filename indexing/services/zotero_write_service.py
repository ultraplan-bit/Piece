"""Zotero 有限写入：服务端保留授权与短时预览，只执行确认过的对象和字段。"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from ..settings import get_settings
from .errors import BusinessError
from .zotero_client import USER_PREFIX, item_key, item_keys
from .zotero_service import _client_scope

PLAN_TTL_SECONDS = 15 * 60
# 授权框等的是用户点击；请求超时后 Zotero 仍可能签发 key，但本次已放弃，单次 key 会白白消耗。
AUTHORIZE_TIMEOUT_SECONDS = 300.0
MAX_PLANS = 32
MAX_PLAN_BYTES = 512 * 1024
_KEY_CHARS = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
_PROTECTED_FIELDS = {
    "key", "version", "itemType", "parentItem", "deleted", "inPublications", "dateAdded", "dateModified",
    "note", "tags", "collections", "relations", "linkMode", "contentType", "path", "filename", "md5", "mtime",
}
# 新建对象预分配 key、version: 0；删除对象只置 deleted，不做物理删除（DELETE 不可恢复，本实现不用）。
_CREATE_ACTIONS = {"create_note", "create_item", "create_collection"}
_COLLECTION_ACTIONS = {"create_collection", "rename_collection", "delete_collection"}
_grants: dict[str, dict] = {}
_plans: dict[str, dict] = {}
# ponytail: 本机单用户写入串行；出现多库并行需求时再改为按实例锁。
_lock = threading.RLock()


def authorization_status(server_id) -> dict:
    with _lock:
        grant = _grants.get(server_id)
        return {"state": "cached" if grant else "required", "remembered": bool(grant and grant["remember"]),
                "verified": False, "storage": "process_memory"}


def _require_enabled():
    if not get_settings().zotero_write_enabled:
        raise BusinessError("FORBIDDEN", "Zotero 写入默认关闭；请由用户在 Piece 配置中启用 zotero_write_enabled")


def _write_info(client) -> dict:
    info = client.probe()
    if not info["write_supported"]:
        raise BusinessError("ZOTERO_UNSUPPORTED", "有限写入需要 Zotero 10.0.5+；早期版本的客户端指定 key 写入存在缺陷，仍可只读使用")
    return info


def authorize(server_id: str, *, client=None) -> dict:
    """仅显式调用才弹 Zotero 授权框；key 不离开服务端，不落入配置或工具结果。

    「允许」签发的是单次 key，下一次 apply 即消耗，不论成功与否；「始终允许」才能跨批次复用。
    """
    _require_enabled()
    if not server_id:
        raise BusinessError("INVALID_INPUT", "请先取得 Zotero 状态中的 server_id")
    with _lock, _client_scope(client, server_id=server_id, timeout=AUTHORIZE_TIMEOUT_SECONDS) as local:
        info = _write_info(local)
        _grants.pop(server_id, None)
        response = local._request("POST", "/local/authorize", json={"appName": "Piece"})
        value = local.json(response)
        if (not isinstance(value, dict) or not isinstance(value.get("key"), str) or len(value["key"]) != 32
                or type(value.get("remember")) is not bool):
            raise BusinessError("ZOTERO_ERROR", "Zotero 授权响应格式异常")
        _grants[server_id] = {"key": value["key"], "remember": value["remember"]}
        notice = ("Zotero 已记住授权，本进程内可直接执行后续计划；授权不替代具体变更的确认，Piece 重启后需重新授权。"
                  if value["remember"] else
                  "本次为单次授权，只够执行一次 apply（无论成功、冲突还是超时）；"
                  "多批次变更请在 Zotero 弹窗选择「始终允许」。授权不替代具体变更的确认。")
        return {"zotero": info, "authorized": True, "remembered": value["remember"],
                "storage": "process_memory", "single_use": not value["remember"], "notice": notice}


def _prune_plans():
    now = time.monotonic()
    for key in list(_plans):
        if now - _plans[key]["created"] >= PLAN_TTL_SECONDS:
            del _plans[key]


def _active_item(client, key, *, note=False):
    item = client.item(item_key(key))
    data = item["data"]
    kind = data.get("itemType")
    if data.get("deleted"):
        raise BusinessError("INVALID_INPUT", "不能修改或引用回收站中的条目")
    if note and kind != "note":
        raise BusinessError("INVALID_INPUT", "update_note 只允许修改明确指定的 note 条目")
    if not note and kind in {"attachment", "annotation", "note"}:
        raise BusinessError("INVALID_INPUT", "此操作需要文献条目；首版不修改附件或批注")
    if type(item.get("version")) is not int or item["version"] < 0:
        raise BusinessError("ZOTERO_ERROR", "条目缺少有效的本地版本，不能安全写入")
    return item


def _active_collection(client, key):
    """读回集合并校验版本；删除集合只是置 deleted，条目不删，仅解除该集合归属。"""
    key = item_key(key)
    collection = client.collection(key)
    if collection["data"].get("deleted"):
        raise BusinessError("INVALID_INPUT", "不能修改或引用回收站中的集合")
    if type(collection.get("version")) is not int or collection["version"] < 0:
        raise BusinessError("ZOTERO_ERROR", "集合缺少有效的本地版本，不能安全写入")
    return collection


def _item_type(client, value):
    """校验条目类型是 Zotero 已知类型，避免本地 API 把未知 itemType 当普通文本存入。"""
    if not isinstance(value, str) or not value:
        raise BusinessError("INVALID_INPUT", "item_type 必须是 Zotero 条目类型名，例如 journalArticle、book")
    definitions = client.json(client._get("/itemTypes"))
    if not isinstance(definitions, list):
        raise BusinessError("ZOTERO_ERROR", "Zotero 条目类型定义格式异常")
    types = {entry.get("itemType") for entry in definitions if isinstance(entry, dict)}
    if value not in types:
        raise BusinessError("INVALID_INPUT", f"未知条目类型 {value}；请使用 Zotero 支持的 itemType")
    if value in {"note", "attachment", "annotation"}:
        raise BusinessError("INVALID_INPUT", "新建条目只支持文献类型；笔记请用 create_note")
    return value


def _names(value, name):
    if not isinstance(value, list) or len(value) > 50 or any(
            not isinstance(tag, str) or not tag.strip() or len(tag) > 255 for tag in value):
        raise BusinessError("INVALID_INPUT", f"{name} 必须是最多 50 个非空标签，每个不超过 255 字符")
    return list(dict.fromkeys(value))


def _optional_keys(value, name):
    if value == []:
        return []
    return item_keys(value, name=name)


def _collection_name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise BusinessError("INVALID_INPUT", "name 必须是 1 至 255 字符的非空集合名称")
    return value.strip()


def _creators(client, kind, value):
    if value in (None, []):
        return []
    types = client.json(client._get("/itemTypeCreatorTypes", {"itemType": kind}))
    if not isinstance(types, list):
        raise BusinessError("ZOTERO_ERROR", "Zotero 作者类型定义格式异常")
    creator_types = {entry.get("creatorType") for entry in types if isinstance(entry, dict)}
    if not isinstance(value, list) or len(value) > 100:
        raise BusinessError("INVALID_INPUT", "creators 必须是不超过 100 项的作者列表")
    for creator in value:
        if (not isinstance(creator, dict) or set(creator) - {"creatorType", "name", "firstName", "lastName"}
                or creator.get("creatorType") not in creator_types
                or any(not isinstance(text, str) or len(text) > 1000 for text in creator.values())
                or ("name" in creator and ("firstName" in creator or "lastName" in creator))
                or not (creator.get("name") or creator.get("firstName") or creator.get("lastName"))):
            raise BusinessError("INVALID_INPUT", "作者格式或 creatorType 无效，请使用当前条目类型支持的作者字段")
    return copy.deepcopy(value)


def _fields(client, item_type, fields):
    if not isinstance(fields, dict):
        raise BusinessError("INVALID_INPUT", "fields 必须是元数据字段对象")
    if not fields:
        return {}
    kind = item_type
    definitions = client.json(client._get("/itemTypeFields", {"itemType": kind}))
    if not isinstance(definitions, list):
        raise BusinessError("ZOTERO_ERROR", "Zotero 字段定义格式异常")
    allowed = {field.get("field") for field in definitions if isinstance(field, dict)} - _PROTECTED_FIELDS
    for name, value in fields.items():
        if name == "creators":
            _creators(client, kind, value)
        elif name not in allowed or not isinstance(value, str) or len(value) > 100000:
            raise BusinessError("INVALID_INPUT", f"不允许修改字段 {name}，或字段值不是允许长度的文本")
    return copy.deepcopy(fields)


class _NoteContent(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.links = [], []

    def handle_data(self, data):
        self.text.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.extend(value for key, value in attrs if key == "href" and value is not None)


def _note_identity(value):
    parser = _NoteContent()
    parser.feed(value)
    return " ".join(" ".join(parser.text).split()), sorted(parser.links)


def _render_note(client, content, source_keys):
    if not isinstance(content, str) or not content.strip() or len(content) > 100000:
        raise BusinessError("INVALID_INPUT", "content 必须是 1 至 100000 字符的非空纯文本")
    paragraphs = ["<p>" + html.escape(part).replace("\n", "<br/>") + "</p>" for part in content.split("\n\n")]
    paragraphs.append("<p><em>由 Piece 保存。</em></p>")
    for key in source_keys:
        source = _active_item(client, key)
        title = html.escape(str(source["data"].get("title") or key))
        paragraphs.append(f'<p>来源：<a href="zotero://select/library/items/{key}">{title}</a></p>')
    return "<div>" + "".join(paragraphs) + "</div>"


def _prepare_change(client, change):
    if not isinstance(change, dict):
        raise BusinessError("INVALID_INPUT", "每项变更必须是对象")
    action = change.get("action")
    allowed = {
        "update_item": {"action", "item_key", "fields", "add_tags", "remove_tags", "add_collections", "remove_collections"},
        "create_item": {"action", "item_type", "fields", "creators", "tags", "collection_keys"},
        "create_note": {"action", "content", "parent_item_key", "collection_keys", "source_item_keys"},
        "update_note": {"action", "item_key", "content", "source_item_keys"},
        "delete_item": {"action", "item_key"},
        "create_collection": {"action", "name", "parent_key"},
        "rename_collection": {"action", "collection_key", "name"},
        "delete_collection": {"action", "collection_key"},
    }
    if not isinstance(action, str) or action not in allowed or set(change) - allowed[action]:
        raise BusinessError("INVALID_INPUT", "变更 action 或字段不受支持；只允许 update_item、create_item、create_note、"
                            "update_note、delete_item 以及 create/rename/delete_collection")
    if action == "create_note":
        key = "".join(secrets.choice(_KEY_CHARS) for _ in range(8))
        parent = change.get("parent_item_key")
        collections = _optional_keys(change.get("collection_keys", []), "collection_keys")
        if parent is not None:
            _active_item(client, parent)
            if collections:
                raise BusinessError("INVALID_INPUT", "子笔记继承父条目归属，不能同时指定 collection_keys")
        for collection in collections:
            client.collection(collection)
        sources = _optional_keys(change.get("source_item_keys", []), "source_item_keys")
        if parent and parent not in sources:
            sources.insert(0, parent)
        after: dict = {"itemType": "note", "note": _render_note(client, change.get("content"), sources)}
        if parent:
            after["parentItem"] = parent
        else:
            after["collections"] = collections
        return {"action": action, "key": key, "version": 0, "before": {}, "after": after,
                "status": "pending", "attempted": False}
    if action == "create_item":
        # 新建书目条目：先按 item_type 校验字段白名单，再预分配 key、version: 0，与 create_note 一致。
        kind = _item_type(client, change.get("item_type"))
        fields = _fields(client, kind, change.get("fields", {}))
        creators = _creators(client, kind, change.get("creators"))
        tags = _names(change.get("tags", []), "tags")
        collections = _optional_keys(change.get("collection_keys", []), "collection_keys")
        for collection in collections:
            client.collection(collection)
        after: dict = {"itemType": kind, **fields}
        if creators:
            after["creators"] = creators
        if tags:
            after["tags"] = [{"tag": tag} for tag in tags]
        if collections:
            after["collections"] = collections
        key = "".join(secrets.choice(_KEY_CHARS) for _ in range(8))
        return {"action": action, "key": key, "version": 0, "before": {}, "after": after,
                "status": "pending", "attempted": False}
    if action == "create_collection":
        name = _collection_name(change.get("name"))
        parent = change.get("parent_key")
        if parent is not None:
            _active_collection(client, parent)
        after = {"name": name, "parentCollection": parent or False}
        key = "".join(secrets.choice(_KEY_CHARS) for _ in range(8))
        return {"action": action, "key": key, "version": 0, "before": {}, "after": after,
                "status": "pending", "attempted": False}
    if action in {"rename_collection", "delete_collection"}:
        collection = _active_collection(client, change.get("collection_key"))
        if action == "rename_collection":
            after = {"name": _collection_name(change.get("name"))}
        else:
            after = {"deleted": True}
        before = {name: copy.deepcopy(collection["data"].get(name)) for name in after}
        unchanged = all(_same_value(name, before[name], value) for name, value in after.items())
        return {"action": action, "key": collection["key"], "version": collection["version"],
                "before": before, "after": after, "status": "unchanged" if unchanged else "pending",
                "attempted": False}
    if action == "delete_item":
        # 移入回收站：置 deleted 而非物理删除（DELETE 不可恢复）；已在回收站的条目由 _active_item 拒绝。
        item = _active_item(client, item_key(change.get("item_key")))
        return {"action": action, "key": item["key"], "version": item["version"],
                "before": {"deleted": False}, "after": {"deleted": True}, "status": "pending", "attempted": False}
    key = item_key(change.get("item_key"))
    item = _active_item(client, key, note=action == "update_note")
    data = item["data"]
    if action == "update_note":
        sources = _optional_keys(change.get("source_item_keys", []), "source_item_keys")
        parent = data.get("parentItem")
        if parent and parent not in sources:
            sources.insert(0, parent)
        after = {"note": _render_note(client, change.get("content"), sources)}
    else:
        after = _fields(client, data.get("itemType"), change.get("fields", {}))
        add_tags = _names(change.get("add_tags", []), "add_tags")
        remove_tags = _names(change.get("remove_tags", []), "remove_tags")
        if set(add_tags) & set(remove_tags):
            raise BusinessError("INVALID_INPUT", "同一标签不能同时添加和移除")
        if add_tags or remove_tags:
            tags = [copy.deepcopy(tag) for tag in data.get("tags", []) if tag.get("tag") not in remove_tags]
            names = {tag["tag"] for tag in tags}
            tags.extend({"tag": tag} for tag in add_tags if tag not in names)
            after["tags"] = tags
        add_collections = _optional_keys(change.get("add_collections", []), "add_collections")
        remove_collections = _optional_keys(change.get("remove_collections", []), "remove_collections")
        if set(add_collections) & set(remove_collections):
            raise BusinessError("INVALID_INPUT", "同一集合不能同时加入和移出")
        for collection in set(add_collections + remove_collections):
            client.collection(collection)
        if add_collections or remove_collections:
            after["collections"] = list(dict.fromkeys(
                [key for key in data.get("collections", []) if key not in remove_collections] + add_collections))
    if not after:
        raise BusinessError("INVALID_INPUT", "变更没有指定任何允许修改的内容")
    before = {name: copy.deepcopy(data.get(name)) for name in after}
    unchanged = all(_same_value(name, before[name], value) for name, value in after.items())
    return {"action": action, "key": key, "version": item["version"], "before": before, "after": after,
            "status": "unchanged" if unchanged else "pending", "attempted": False}


def preview_changes(changes: list[dict], server_id: str, *, client=None) -> dict:
    """只读取得本地版本和变更清单；预览 15 分钟有效，不请求写授权。"""
    if not server_id:
        raise BusinessError("INVALID_INPUT", "写入预览必须指定状态接口返回的 server_id")
    if not isinstance(changes, list) or not 1 <= len(changes) <= 50:
        raise BusinessError("INVALID_INPUT", "一次预览需要 1 至 50 项变更")
    try:
        size = len(json.dumps(changes, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise BusinessError("INVALID_INPUT", "变更必须是合法 JSON") from None
    if size > MAX_PLAN_BYTES:
        raise BusinessError("INVALID_INPUT", "变更内容不能超过 512 KiB")
    with _lock, _client_scope(client, server_id=server_id) as local:
        _prune_plans()
        if len(_plans) >= MAX_PLANS:
            raise BusinessError("ZOTERO_RATE_LIMITED", "待确认预览过多，请等待旧预览过期")
        info = _write_info(local)
        operations = [_prepare_change(local, change) for change in changes]
        if len({operation["key"] for operation in operations}) != len(operations):
            raise BusinessError("INVALID_INPUT", "同一条目的变更请合并为一项，不能在一份预览中重复修改")
        if len(json.dumps(operations, ensure_ascii=False).encode("utf-8")) > MAX_PLAN_BYTES:
            raise BusinessError("INVALID_INPUT", "渲染后的变更预览超过 512 KiB，请分批处理")
        plan_id = secrets.token_urlsafe(24)
        expires = (datetime.now(timezone.utc) + timedelta(seconds=PLAN_TTL_SECONDS)).isoformat()
        _plans[plan_id] = {"created": time.monotonic(), "expires_at": expires, "server_id": server_id,
                           "zotero": info, "operations": operations, "tokens": {}}
        return {"plan_id": plan_id, "expires_at": expires, "zotero": info, "requires_confirmation": True,
                "write_enabled": get_settings().zotero_write_enabled,
                "changes": [{key: copy.deepcopy(value) for key, value in operation.items() if key != "attempted"}
                            for operation in operations]}


def _same_value(name, actual, expected):
    if name == "note":
        return isinstance(actual, str) and _note_identity(actual) == _note_identity(expected)
    if name == "collections":
        return sorted(actual or []) == sorted(expected)
    if name == "tags":
        normalize = lambda values: sorted((entry["tag"], entry.get("type", 0)) for entry in values or [])
        return normalize(actual) == normalize(expected)
    if name == "deleted":
        # Zotero 读回可能是 true 或 1，统一按布尔比较，避免把已删除误判为未生效。
        return bool(actual) == bool(expected)
    return actual == expected


def _matches(item, operation):
    data = item["data"]
    # 删除动作读回的对象本就带 deleted；其它动作遇到回收站对象一律不算匹配。
    if data.get("deleted") and operation["after"].get("deleted") is not True:
        return False
    return all(_same_value(name, data.get(name), value) for name, value in operation["after"].items())


def _read_target(client, operation):
    """集合动作读 /collections，其余读 /items；仅新建动作允许对象尚不存在。"""
    read = client.collection if operation["action"] in _COLLECTION_ACTIONS else client.item
    try:
        return read(operation["key"])
    except BusinessError as exc:
        if exc.code == "NOT_FOUND" and operation["action"] in _CREATE_ACTIONS:
            return None
        raise


def _error(operation, code, message, *, status="failed"):
    operation["status"] = status
    operation["error"] = {"code": code, "message": message}


def _verified(operation, item):
    operation["status"] = "succeeded"
    operation["result_version"] = item.get("version")
    operation.pop("error", None)


def _result(plan_id, plan, **extra):
    operations = plan["operations"]
    success = sum(operation["status"] == "succeeded" for operation in operations)
    unchanged = sum(operation["status"] == "unchanged" for operation in operations)
    failed = sum(operation["status"] == "failed" for operation in operations)
    pending = len(operations) - success - unchanged - failed
    return {"plan_id": plan_id, "expires_at": plan["expires_at"], "zotero": plan["zotero"],
            "items": [{key: copy.deepcopy(value) for key, value in operation.items()
                       if key in {"action", "key", "status", "error", "result_version"}} for operation in operations],
            "successful_count": success, "unchanged_count": unchanged, "failed_count": failed,
            "pending_count": pending, "partial": bool(failed or pending), "completed": not failed and not pending,
            **extra}


def apply_changes(plan_id: str, confirmed: bool = False, *, client=None) -> dict:
    """只执行服务端预览；逐对象本地版本 + 固定创建 key，重试不重做成功项。"""
    _require_enabled()
    if confirmed is not True:
        raise BusinessError("CONFIRMATION_REQUIRED", "请先向用户展示变更预览，确认后再执行")
    if not isinstance(plan_id, str):
        raise BusinessError("INVALID_INPUT", "plan_id 必须来自变更预览")
    with _lock:
        _prune_plans()
        plan = _plans.get(plan_id)
        if not plan:
            raise BusinessError("ZOTERO_PLAN_EXPIRED", "预览不存在、已超过 15 分钟或 Piece 已重启；请先核对目标再重新预览")
        with _client_scope(client, server_id=plan["server_id"]) as local:
            _write_info(local)
            pending = []
            for operation in plan["operations"]:
                if operation["status"] in {"succeeded", "unchanged", "failed"}:
                    continue
                try:
                    current = _read_target(local, operation)
                    if operation["attempted"] and current is not None and _matches(current, operation):
                        _verified(operation, current)
                        continue
                    if operation["action"] in _CREATE_ACTIONS:
                        if current is not None:
                            _error(operation, "VERSION_CONFLICT", "预定的新对象 key 已存在且内容不同，请重新预览")
                            continue
                        if operation["after"].get("parentItem"):
                            _active_item(local, operation["after"]["parentItem"])
                        elif operation["after"].get("parentCollection"):
                            _active_collection(local, operation["after"]["parentCollection"])
                    elif current is None or current.get("version") != operation["version"] or current["data"].get("deleted"):
                        _error(operation, "VERSION_CONFLICT", "对象在预览后发生变化，未覆盖，请重新读取和预览")
                        continue
                    pending.append(operation)
                except BusinessError as exc:
                    if exc.code in {"ZOTERO_INSTANCE_CHANGED", "ZOTERO_UNAVAILABLE", "ZOTERO_DISABLED"}:
                        raise BusinessError(exc.code, str(exc), data=_result(plan_id, plan)) from None
                    state = operation["status"] if exc.code in {"ZOTERO_RATE_LIMITED", "ZOTERO_ERROR"} else "failed"
                    _error(operation, exc.code, str(exc), status=state)
            if not pending:
                return _result(plan_id, plan)
            grant = _grants.get(plan["server_id"])
            if not grant:
                consumed = any(operation["attempted"] for operation in plan["operations"])
                raise BusinessError("ZOTERO_AUTH_REQUIRED",
                                    "上一次执行已消耗单次授权；请让用户重新授权（或在 Zotero 中选择「始终允许」），再用同一 plan_id 继续"
                                    if consumed else "请明确调用 Zotero 授权，再使用同一 plan_id 继续",
                                    data=_result(plan_id, plan, needs_authorization=True))
            needs_authorization = False
            # 条目与集合是不同的端点：各自取写 token、各自发请求、各自核对结果。
            # 单次授权 key 只够一次写请求，若两类变更同批出现，第二类会得到 401，需重新授权后用同一计划继续。
            batches = [(f"{USER_PREFIX}/items", [op for op in pending if op["action"] not in _COLLECTION_ACTIONS]),
                       (f"{USER_PREFIX}/collections", [op for op in pending if op["action"] in _COLLECTION_ACTIONS])]
            for path, batch in batches:
                if not batch:
                    continue
                payload = [{"key": operation["key"], "version": operation["version"], **operation["after"]}
                           for operation in batch]
                signature = hashlib.sha256((path + json.dumps(payload, sort_keys=True)).encode("utf-8")).hexdigest()
                token = plan["tokens"].setdefault(signature, secrets.token_hex(16))
                for operation in batch:
                    operation["attempted"] = True
                    operation["status"] = "uncertain"
                try:
                    response = local._request("POST", path, json=payload,
                                              headers={"Zotero-API-Key": grant["key"], "Zotero-Write-Token": token})
                    outcome = local.json(response)
                    if not isinstance(outcome, dict) or any(not isinstance(outcome.get(name, {}), dict)
                                                          for name in ("successful", "success", "unchanged", "failed")):
                        raise BusinessError("ZOTERO_WRITE_UNCERTAIN", "Zotero 批量写入结果异常，需要逐项读回")
                    for index, operation in enumerate(batch):
                        failure = outcome.get("failed", {}).get(str(index))
                        if failure is not None:
                            code = failure.get("code") if isinstance(failure, dict) else None
                            _error(operation, "VERSION_CONFLICT" if code == 412 else "ZOTERO_ITEM_WRITE_FAILED",
                                   f"Zotero 拒绝该项写入（HTTP {code}），请重新核对并预览")
                except BusinessError as exc:
                    if exc.code == "ZOTERO_AUTH_REQUIRED":
                        _grants.pop(plan["server_id"], None)
                        needs_authorization = True
                    for operation in batch:
                        _error(operation, exc.code, str(exc), status="uncertain")
                finally:
                    if not grant["remember"]:
                        _grants.pop(plan["server_id"], None)
            # HTTP 200 不等于逐项成功；即使超时，也先核对固定 key 和变更后的内容。
            for operation in pending:
                try:
                    current = _read_target(local, operation)
                    if current is not None and _matches(current, operation):
                        _verified(operation, current)
                    elif operation["status"] != "failed":
                        if (current is not None and operation["action"] not in _CREATE_ACTIONS
                                and current.get("version") != operation["version"]):
                            _error(operation, "VERSION_CONFLICT", "写入后对象内容或版本不符，停止覆盖；请读回比较")
                        else:
                            _error(operation, "ZOTERO_WRITE_UNCERTAIN", "尚未读回预期结果；仅可用同一 plan_id 核对或继续", status="uncertain")
                except BusinessError as exc:
                    _error(operation, exc.code, str(exc), status="uncertain")
            unfinished = any(operation["status"] in {"pending", "uncertain"} for operation in plan["operations"])
            return _result(plan_id, plan, needs_authorization=needs_authorization or
                           (unfinished and plan["server_id"] not in _grants))
