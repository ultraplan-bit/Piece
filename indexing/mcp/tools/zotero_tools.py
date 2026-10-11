"""Zotero MCP 参数适配；查询复用服务，导入必须有显式范围且每批最多 50 个附件。"""

import json

from indexing.services import zotero_service as zotero
from indexing.services.errors import BusinessError
from indexing.services.zotero_service import (
    status, search, list_collections, list_tags, get_item, get_children, get_fulltext,
)
from indexing.services.zotero_write_service import authorize, preview_changes, apply_changes

MCP_MAX_ITEMS = 50


def _as_str_list(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        value = parsed if isinstance(parsed, list) else [parsed if isinstance(parsed, str) else value]
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise BusinessError("INVALID_INPUT", "请提供非空字符串列表或其 JSON 表示")
    return list(dict.fromkeys(item.strip() for item in value))


def explicit_scope(collection_keys=None, item_keys=None, attachment_keys=None):
    scope = {"collection_keys": _as_str_list(collection_keys), "item_keys": _as_str_list(item_keys),
             "attachment_keys": _as_str_list(attachment_keys)}
    chosen = [value for value in scope.values() if value is not None]
    if len(chosen) != 1 or not chosen[0]:
        raise BusinessError("INVALID_INPUT", "必须且只能提供 collection_keys、item_keys、attachment_keys 之一，且不能为空；不会退回全库")
    return scope


def export_citations(item_keys, format="bib", style="apa", locale="en-US", server_id=None):
    return zotero.export_citations(_as_str_list(item_keys), format=format, style=style, locale=locale, server_id=server_id)


def get_links(item_keys=None, attachment_keys=None, limit=100, offset=0, server_id=None):
    return zotero.get_links(item_keys=_as_str_list(item_keys), attachment_keys=_as_str_list(attachment_keys),
                            limit=limit, offset=offset, server_id=server_id)


def preview_import(collection_keys=None, collection_mode="path", *, item_keys=None, attachment_keys=None, server_id=None):
    return zotero.preview_import(**explicit_scope(collection_keys, item_keys, attachment_keys),
                                 collection_mode=collection_mode, server_id=server_id, max_items=MCP_MAX_ITEMS)


def import_library(collection_keys=None, collection_mode="path", collections=None, *, item_keys=None,
                   attachment_keys=None, server_id=None):
    return zotero.import_library(**explicit_scope(collection_keys, item_keys, attachment_keys),
                                 collection_mode=collection_mode, collections=_as_str_list(collections),
                                 server_id=server_id, max_items=MCP_MAX_ITEMS)
