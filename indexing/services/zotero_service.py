"""Zotero 本机查阅和按需导入；书目、附件来源与 Piece 文件内容分别管理。"""

from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse
from urllib.request import url2pathname

from ..database import get_db_cursor
from . import collection_service, file_service
from .errors import BusinessError
from .zotero_client import (
    DEFAULT_BASE_URL, USER_PREFIX, PERSONAL_LIBRARY, ZoteroClient,
    item_key as _key, item_keys as _keys, pagination,
)

logger = logging.getLogger(__name__)

PDF_TYPE = "application/pdf"
MAX_ABSTRACT = 500
COLLECTION_MODES = ("path", "top", "none")
_INVALID_NAME_CHARS = re.compile(r'[/\\:<>"|?*\x00-\x1f]+')
_WHITESPACE = re.compile(r"\s+")


@contextmanager
def _client_scope(client=None, *, base_url=None, server_id=None, timeout=30.0):
    owned = client is None
    client = client or ZoteroClient(base_url, server_id=server_id, timeout=timeout)
    try:
        client.bind(server_id)
        yield client
    finally:
        if owned:
            client.close()


def status(server_id=None, *, client=None) -> dict:
    """只探测，不弹授权框；缓存的授权仍可能已在 Zotero 中撤销。"""
    from ..settings import get_settings
    from .zotero_write_service import authorization_status
    enabled = get_settings().zotero_write_enabled
    try:
        with _client_scope(client, server_id=server_id) as local:
            info = local.probe()
    except BusinessError as exc:
        return {"connected": False, "write_enabled": enabled,
                "error": {"code": exc.code, "message": str(exc)}}
    return {**info, "connected": True, "write_enabled": enabled,
            "authorization": authorization_status(info["server_id"]),
            "capabilities": {"read": True, "write": info["write_supported"] and enabled,
                             "group_libraries": False, "search_scope": "title_creator_year",
                             "search_scopes": sorted(SEARCH_SCOPES)}}


def _summary(item: dict) -> dict:
    data = item.get("data") or {}
    names = ("itemType", "title", "creators", "date", "DOI", "url", "tags", "collections",
             "parentItem", "contentType", "linkMode", "annotationType", "annotationPageLabel")
    result = {"key": item.get("key"), "version": item.get("version"),
              "data": {name: data[name] for name in names if name in data}, "meta": item.get("meta", {})}
    truncated = []
    for name in ("abstractNote", "note", "annotationText", "annotationComment"):
        if name in data:
            value = str(data[name])
            result["data"][name] = value[:1000]
            if len(value) > 1000:
                truncated.append(name)
    if truncated:
        result["truncated_fields"] = truncated
    result["source_kind"] = {"note": "user_note", "annotation": "user_annotation",
                             "attachment": "attachment"}.get(data.get("itemType"), "bibliographic_metadata")
    return result


SEARCH_SCOPES = {"title_creator_year": "titleCreatorYear", "everything": "everything"}


def search(query="", collection_key=None, tag=None, item_type=None, limit=20, offset=0,
           server_id=None, scope="title_creator_year", *, client=None) -> dict:
    """scope=everything 走 Zotero 本地 quicksearch-everything：覆盖全部字段、笔记与已索引全文；
    是否真的搜到附件正文取决于 Zotero 自己的全文索引是否完整，结果不等于“全文无此内容”。"""
    pagination(limit, offset)
    if not isinstance(query, str) or len(query) > 1000:
        raise BusinessError("INVALID_INPUT", "query 必须是不超过 1000 字符的文本")
    if scope not in SEARCH_SCOPES:
        raise BusinessError("INVALID_INPUT", f"scope 只能是 {', '.join(SEARCH_SCOPES)}")
    params = {"q": query, "qmode": SEARCH_SCOPES[scope]}
    if tag is not None:
        if not isinstance(tag, str) or not tag.strip() or len(tag) > 1000:
            raise BusinessError("INVALID_INPUT", "tag 必须是不超过 1000 字符的非空标签筛选条件")
        params["tag"] = tag
    if item_type is not None:
        if not isinstance(item_type, str) or not re.fullmatch(r"[A-Za-z]{1,50}", item_type):
            raise BusinessError("INVALID_INPUT", "item_type 必须是单个 Zotero 条目类型")
        params["itemType"] = item_type
    with _client_scope(client, server_id=server_id) as local:
        info = local.probe()
        path = f"{USER_PREFIX}/items/top"
        if collection_key is not None:
            local.collection(collection_key)
            path = f"{USER_PREFIX}/collections/{collection_key}/items/top"
        page = local.page(path, params, limit=limit, offset=offset)
    everything = scope == "everything"
    return {**page, "items": [_summary(item) for item in page["items"]], "zotero": info,
            "search_scope": scope, "fulltext_searched": everything,
            "notes_searched": everything, "annotations_searched": everything,
            "collection_includes_descendants": False}


def list_collections(parent_key=None, limit=50, offset=0, server_id=None, *, client=None) -> dict:
    pagination(limit, offset)
    with _client_scope(client, server_id=server_id) as local:
        info = local.probe()
        path = f"{USER_PREFIX}/collections"
        if parent_key is not None:
            local.collection(parent_key)
            path += f"/{parent_key}/collections"
        return {**local.page(path, limit=limit, offset=offset), "zotero": info}


def list_tags(collection_key=None, limit=50, offset=0, server_id=None, *, client=None) -> dict:
    pagination(limit, offset)
    with _client_scope(client, server_id=server_id) as local:
        info = local.probe()
        path = f"{USER_PREFIX}/tags"
        if collection_key is not None:
            local.collection(collection_key)
            # Local API 没有 /collections/{key}/tags 路由（Web API 才有），只有 /items/tags 形式。
            path = f"{USER_PREFIX}/collections/{collection_key}/items/tags"
        return {**local.page(path, limit=limit, offset=offset), "zotero": info}


def get_item(item_key, server_id=None, *, client=None) -> dict:
    _key(item_key)
    with _client_scope(client, server_id=server_id) as local:
        info = local.probe()
        item = local.item(item_key)
    return {"zotero": info, "item": item, "source_kind": _summary(item)["source_kind"]}


def get_children(item_key, item_type=None, limit=20, offset=0, server_id=None, *, client=None) -> dict:
    _key(item_key)
    pagination(limit, offset)
    if item_type not in {None, "note", "attachment", "annotation"}:
        raise BusinessError("INVALID_INPUT", "子项类型仅支持 note、attachment、annotation")
    with _client_scope(client, server_id=server_id) as local:
        info = local.probe()
        parent = local.item(item_key)
        if item_type == "annotation" and parent["data"].get("itemType") != "attachment":
            raise BusinessError("INVALID_INPUT", "读取批注请先取得 PDF 附件 key，不能用文献条目 key 代替")
        page = local.page(f"{USER_PREFIX}/items/{item_key}/children",
                          {"itemType": item_type} if item_type else None, limit=limit, offset=offset)
    return {**page, "items": [_summary(item) for item in page["items"]],
            "zotero": info, "parent": _summary(parent)}


def get_fulltext(attachment_key, offset=0, limit=12000, server_id=None, *, client=None) -> dict:
    _key(attachment_key)
    pagination(limit, offset, maximum=50000)
    with _client_scope(client, server_id=server_id) as local:
        info = local.probe()
        attachment = local.item(attachment_key)
        if attachment["data"].get("itemType") != "attachment":
            raise BusinessError("INVALID_INPUT", "全文接口需要附件 key")
        try:
            response = local._get(f"{USER_PREFIX}/items/{attachment_key}/fulltext")
        except BusinessError as exc:
            if exc.code == "NOT_FOUND":
                # 直接给出可执行的替代路径：先按同一附件预览导入，确认后再 import，由 Piece 解析实际 PDF。
                raise BusinessError(
                    "ZOTERO_FULLTEXT_UNAVAILABLE",
                    "附件没有可用的 Zotero 全文索引；可改为把实际 PDF 导入 Piece 后再读正文：先 zotero_preview_import，"
                    "确认后用相同范围 zotero_import，索引完成后用检索服务读取",
                    data={"attachment_key": attachment_key, "parent_item_key": attachment["data"].get("parentItem"),
                          "next_tool": {"name": "zotero_preview_import",
                                        "arguments": {"attachment_keys": [attachment_key],
                                                      **({"server_id": server_id} if server_id else {})},
                                        "hint": "预览只列出将导入的附件，不写入；确认后用相同 attachment_keys 调 zotero_import"},
                          "next_command": f"zotero preview --attachment-key {attachment_key}"
                                          + (f" --server-id {server_id}" if server_id else "") + " --json"},
                ) from None
            raise
        value = local.json(response)
        if not isinstance(value, dict) or not isinstance(value.get("content"), str):
            raise BusinessError("ZOTERO_ERROR", "Zotero 全文响应格式异常")
    content = value["content"]
    coverage = {name: value[name] for name in ("indexedChars", "totalChars", "indexedPages", "totalPages") if name in value}
    complete = None
    for unit in ("Pages", "Chars"):
        indexed, total = coverage.get(f"indexed{unit}"), coverage.get(f"total{unit}")
        if type(total) is int and total > 0 and type(indexed) is int:
            complete = indexed >= total
            break
    return {"zotero": info, "attachment_key": attachment_key, "parent_item_key": attachment["data"].get("parentItem"),
            "source_kind": "zotero_indexed_fulltext", "content": content[offset:offset + limit],
            "offset": offset, "limit": limit, "text_length": len(content),
            "next_offset": offset + limit if offset + limit < len(content) else None,
            "coverage": coverage, "complete": complete, "version": response.headers.get("Last-Modified-Version"),
            "notice": "这是 Zotero 已索引文本，不保证覆盖完整 PDF；公式、表格和原页需用实际附件核对。"}


def export_citations(item_keys, format="bib", style="apa", locale="en-US", server_id=None, *, client=None) -> dict:
    keys = _keys(item_keys, name="item_keys")
    if format not in {"bib", "citation", "bibtex", "ris", "csljson"}:
        raise BusinessError("INVALID_INPUT", "format 仅支持 bib、citation、bibtex、ris、csljson")
    if not isinstance(style, str) or not re.fullmatch(r"[a-z0-9-]{1,200}", style):
        raise BusinessError("INVALID_INPUT", "style 需要 Zotero 引用样式名，不接受 URL")
    if not isinstance(locale, str) or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", locale):
        raise BusinessError("INVALID_INPUT", "locale 格式无效，例如 zh-CN 或 en-US")
    with _client_scope(client, server_id=server_id) as local:
        info = local.probe()
        for key in keys:
            item = local.item(key)
            if item["data"].get("itemType") in {"note", "annotation", "attachment"}:
                raise BusinessError("INVALID_INPUT", "引用导出需要文献条目 key，而不是笔记或附件 key")
        params = {"itemKey": ",".join(keys), "format": "json" if format == "citation" else format,
                  "style": style, "locale": locale, "limit": len(keys)}
        if format == "citation":
            params["include"] = "citation"
        # /items?itemKey= 会连带返回附件、笔记子项，再按 limit 截断就会漏掉排序靠后的文献；/items/top 只返回顶层条目。
        response = local._get(f"{USER_PREFIX}/items/top", params)
        content = local.json(response) if format in {"citation", "csljson"} else response.text
        returned = len(content) if isinstance(content, list) else None
    result = {"zotero": info, "item_keys": keys, "format": format, "content": content}
    if returned is not None and returned != len(keys):
        result["warning"] = f"Zotero 返回 {returned} 条，与请求的 {len(keys)} 条不一致，请核对条目"
    return result


def _source_links(server_id, *, item_keys=None, attachment_keys=None, limit=100, offset=0) -> dict:
    if not server_id:
        return {"links": [], "total": 0, "identity_verified": False,
                "notice": "旧版 Zotero 无稳定实例标识；不能仅凭 key 判断已有关联，请按附件内容查重。"}
    field, values = ("item_key", item_keys) if item_keys is not None else ("attachment_key", attachment_keys)
    placeholders = ",".join("?" for _ in values)
    sql = f"""WITH sources AS (
        SELECT f.id AS file_id, f.filename, f.status, f.file_hash,
               CASE WHEN s.type='object' THEN s.value ELSE '{{}}' END AS source
        FROM files f, json_each(CASE WHEN json_valid(f.metadata) THEN f.metadata ELSE '{{}}' END, '$.zotero_sources') s
    ) SELECT * FROM sources WHERE json_extract(source, '$.server_id')=?
        AND json_extract(source, '$.library_type')='user' AND json_extract(source, '$.library_id')=0
        AND json_extract(source, '$.{field}') IN ({placeholders})"""
    params = (server_id, *values)
    with get_db_cursor() as cursor:
        cursor.execute("BEGIN")
        total = cursor.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
        rows = cursor.execute(sql + " ORDER BY file_id LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
    links = [{**dict(row), "source": json.loads(row["source"])} for row in rows]
    return {"links": links, "total": total, "limit": limit, "offset": offset,
            "has_more": offset + len(links) < total,
            "next_offset": offset + len(links) if offset + len(links) < total else None,
            "identity_verified": True}


def get_links(item_keys=None, attachment_keys=None, limit=100, offset=0, server_id=None, *, client=None) -> dict:
    if (item_keys is None) == (attachment_keys is None):
        raise BusinessError("INVALID_INPUT", "请明确指定 item_keys 或 attachment_keys，不能同时指定")
    if item_keys is not None:
        item_keys = _keys(item_keys, name="item_keys")
    if attachment_keys is not None:
        attachment_keys = _keys(attachment_keys, name="attachment_keys")
    pagination(limit, offset)
    with _client_scope(client, server_id=server_id) as local:
        info = local.probe()
        return {"zotero": info, **_source_links(info["server_id"], item_keys=item_keys,
                                               attachment_keys=attachment_keys, limit=limit, offset=offset)}


def file_url_to_path(url: str) -> Optional[Path]:
    """把 API 返回的 file:// 地址还原为本机路径；不是 file 协议时返回 None。

    url2pathname 自行做百分号解码，这里不能再 unquote 一次，否则文件名里的 %25 会被吃掉。
    """
    parsed = urlparse(url.strip())
    if parsed.scheme != "file":
        return None
    path = url2pathname(parsed.path)
    if parsed.netloc and parsed.netloc != "localhost":
        # UNC：file://server/share/x
        path = f"\\\\{parsed.netloc}{path}"
    return Path(path)


def _collection_paths(collections: Iterable[Dict[str, Any]]) -> Dict[str, List[str]]:
    """把 Zotero 的集合树展开成 key → [顶层名, ..., 自身名] 的路径段。"""
    nodes = {c["key"]: c["data"] for c in collections}
    cache: Dict[str, List[str]] = {}

    def resolve(key: str, seen: tuple = ()) -> List[str]:
        if key in cache:
            return cache[key]
        data = nodes.get(key)
        if data is None or key in seen:
            return []
        parent = data.get("parentCollection")
        chain = resolve(parent, seen + (key,)) if isinstance(parent, str) and parent else []
        cache[key] = chain + [str(data.get("name") or key).strip()]
        return cache[key]

    return {key: resolve(key) for key in nodes}


def _descendants(collections: Iterable[Dict[str, Any]], roots: Iterable[str]) -> set:
    """选中的集合连同其全部子集合的 key。"""
    children: Dict[str, List[str]] = {}
    for c in collections:
        parent = c["data"].get("parentCollection")
        if isinstance(parent, str) and parent:
            children.setdefault(parent, []).append(c["key"])
    selected = set()
    stack = list(roots)
    while stack:
        key = stack.pop()
        if key in selected:
            continue
        selected.add(key)
        stack.extend(children.get(key, []))
    return selected


def _creator_name(creator: Dict[str, Any]) -> str:
    if creator.get("name"):
        return str(creator["name"]).strip()
    last = str(creator.get("lastName") or "").strip()
    first = str(creator.get("firstName") or "").strip()
    return f"{first} {last}".strip() if first else last


def _author_label(creators: List[Dict[str, Any]]) -> str:
    """文件名里的作者部分：第一作者姓，多人时加「等」。"""
    authors = [c for c in creators if c.get("creatorType") in (None, "author", "editor")] or creators
    if not authors:
        return ""
    first = authors[0]
    label = str(first.get("name") or first.get("lastName") or first.get("firstName") or "").strip()
    label = _WHITESPACE.sub(" ", label)
    if len(authors) > 1 and label:
        label += " 等"
    return label


def _year(item: Dict[str, Any]) -> str:
    parsed = str(item.get("meta", {}).get("parsedDate") or "")
    if len(parsed) >= 4 and parsed[:4].isdigit():
        return parsed[:4]
    match = re.search(r"\b(1[6-9]\d{2}|20\d{2})\b", str(item["data"].get("date") or ""))
    return match.group(1) if match else ""


def _clean_name_part(text: str) -> str:
    text = _INVALID_NAME_CHARS.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip(" .")


def build_filename(item: Dict[str, Any], suffix: str = ".pdf", *, disambiguator: str = "") -> str:
    """`作者 - 年份 - 标题.pdf`：可读的文件名决定卡片标题，比元数据更影响检索质量。"""
    data = item["data"]
    title = _clean_name_part(str(data.get("title") or "")) or item["key"]
    parts = [p for p in (_clean_name_part(_author_label(data.get("creators") or [])), _year(item)) if p]
    prefix = " - ".join(parts)
    tail = f" - {disambiguator}" if disambiguator else ""
    # 落盘时主干超过 MAX_STORED_STEM 会被截断，这里先截标题，保住作者、年份和区分后缀
    budget = file_service.MAX_STORED_STEM - len(tail) - (len(prefix) + 3 if prefix else 0)
    if budget < 8:
        prefix, budget = "", file_service.MAX_STORED_STEM - len(tail)
    title = title[:budget].rstrip(" .")
    name = f"{prefix} - {title}" if prefix else title
    return name + tail + suffix


def build_metadata(item: Dict[str, Any], collection_paths: List[str]) -> Dict[str, Any]:
    """写入 files.metadata 的属性；键名与 Obsidian frontmatter 常用字段保持一致。"""
    data = item["data"]
    metadata: Dict[str, Any] = {"title": data.get("title"), "item_type": data.get("itemType")}
    authors = [name for name in (_creator_name(c) for c in data.get("creators") or []) if name]
    if authors:
        metadata["authors"] = authors
    year = _year(item)
    if year:
        metadata["year"] = year
    for source, target in (("date", "date"), ("publicationTitle", "publication"), ("bookTitle", "publication"),
                           ("conferenceName", "publication"), ("university", "publication"),
                           ("DOI", "doi"), ("url", "url"), ("language", "language")):
        value = data.get(source)
        if value and target not in metadata:
            metadata[target] = str(value)
    tags = [str(t.get("tag")) for t in data.get("tags") or [] if isinstance(t, dict) and t.get("tag")]
    if tags:
        metadata["tags"] = tags
    abstract = str(data.get("abstractNote") or "").strip()
    if abstract:
        metadata["abstract"] = abstract[:MAX_ABSTRACT]
    metadata["zotero_key"] = item["key"]
    if collection_paths:
        metadata["zotero_collections"] = collection_paths
    return {k: v for k, v in metadata.items() if v not in (None, "", [])}


def _pdf_attachments(attachments: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """按父条目分组的本机 PDF 附件；独立附件条目以自身 key 为组。

    linked_url 只是网址没有文件；imported_* 与 linked_file 在磁盘上都有文件。
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for attachment in {entry["key"]: entry for entry in attachments}.values():
        data = attachment["data"]
        if data.get("contentType") != PDF_TYPE or data.get("linkMode") == "linked_url":
            continue
        grouped.setdefault(data.get("parentItem") or attachment["key"], []).append(attachment)
    return grouped


def _attachment_path(client: ZoteroClient, attachment: Dict[str, Any]) -> Optional[Path]:
    href = (attachment.get("links") or {}).get("enclosure", {}).get("href")
    if href and urlparse(href).scheme == "file":
        return file_url_to_path(href)
    try:
        return file_url_to_path(client.attachment_file_url(attachment["key"]))
    except BusinessError as exc:
        if exc.code == "NOT_FOUND":
            return None
        raise


def _selection(collection_keys, item_keys, attachment_keys):
    if sum(value is not None for value in (collection_keys, item_keys, attachment_keys)) > 1:
        raise BusinessError("INVALID_INPUT", "collection_keys、item_keys、attachment_keys 只能指定一种范围")
    for name, values in (("collection_keys", collection_keys), ("item_keys", item_keys), ("attachment_keys", attachment_keys)):
        if values is not None and (not isinstance(values, list) or not 1 <= len(values) <= 50):
            raise BusinessError("INVALID_INPUT", f"{name} 需要明确指定 1 至 50 个 key，空范围不会退回全库")
    return (_keys(item_keys, name="item_keys") if item_keys is not None else None,
            _keys(attachment_keys, name="attachment_keys") if attachment_keys is not None else None)


def _selected_items(client, item_keys, attachment_keys):
    if item_keys is None and attachment_keys is None:
        return client.top_items(), _pdf_attachments(client.attachments())
    items, attachments = {}, []
    for key in item_keys or attachment_keys:
        item = client.item(key)
        data = item["data"]
        if attachment_keys is not None and data.get("itemType") != "attachment":
            raise BusinessError("INVALID_INPUT", f"{key} 不是附件，请使用 item_keys 选择文献")
        if data.get("itemType") == "attachment":
            attachments.append(item)
            parent = client.item(data["parentItem"]) if data.get("parentItem") else item
            items[parent["key"]] = parent
        else:
            items[key] = item
            if data.get("itemType") not in {"note", "annotation"}:
                attachments.extend(client.children(key, "attachment"))
    return list(items.values()), _pdf_attachments(attachments)


def _plan(client: ZoteroClient, collection_keys: Optional[List[str]], collection_mode: str,
          item_keys=None, attachment_keys=None, max_items=None) -> Dict[str, Any]:
    """只计算显式范围的计划；旧导入入口未指定范围时仍支持整库。"""
    if collection_mode not in COLLECTION_MODES:
        raise BusinessError("INVALID_INPUT", f"collection_mode 只能是 {', '.join(COLLECTION_MODES)}")
    item_keys, attachment_keys = _selection(collection_keys, item_keys, attachment_keys)
    if max_items is not None and (type(max_items) is not int or max_items < 1):
        raise BusinessError("INVALID_INPUT", "max_items 必须是正整数")
    info = client.probe()
    collections = client.collections()
    paths = _collection_paths(collections)
    selected = _descendants(collections, collection_keys) if collection_keys is not None else None
    if collection_keys is not None:
        unknown = [key for key in collection_keys if key not in paths]
        if unknown:
            raise BusinessError("NOT_FOUND", f"Zotero 集合不存在：{', '.join(unknown)}")
    top_items, attachments = _selected_items(client, item_keys, attachment_keys)

    entries: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    relevant_collections = set()
    for item in top_items:
        data = item["data"]
        item_collections = [key for key in data.get("collections") or [] if key in paths]
        if selected is not None and not (set(item_collections) & selected):
            continue
        relevant_collections.update(item_collections)
        pdfs = attachments.get(item["key"], [])
        summary = {"key": item["key"], "title": data.get("title") or "", "item_type": data.get("itemType")}
        if not pdfs:
            skipped.append({**summary, "reason": "no_pdf"})
            continue
        if collection_mode == "none":
            names: List[str] = []
        elif collection_mode == "top":
            names = [paths[key][0] for key in item_collections if paths.get(key)]
        else:
            names = ["/".join(paths[key]) for key in item_collections if paths.get(key)]
        names = [name[:200] for name in dict.fromkeys(names) if name]
        for index, attachment in enumerate(pdfs):
            if max_items is not None and len(entries) >= max_items:
                scope = "集合" if collection_keys is not None else ("条目" if item_keys is not None else "附件")
                raise BusinessError(
                    "IMPORT_LIMIT_EXCEEDED",
                    f"所选{scope}展开后的 PDF 附件超过单批上限 {max_items} 个；范围本身有效，请缩小范围或分批："
                    "改选子集合、用 zotero_search 分页后按 item_keys/attachment_keys 逐批导入",
                    data={"max_items": max_items, "scope": scope, "counted_at_least": len(entries) + 1,
                          "collection_keys": collection_keys, "collection_mode": collection_mode})
            disambiguator = ""
            if len(pdfs) > 1:
                disambiguator = _clean_name_part(str(attachment["data"].get("title") or ""))[:40] or str(index + 1)
            metadata = build_metadata(item, ["/".join(paths[key]) for key in item_collections if paths.get(key)])
            metadata["zotero_sources"] = [{
                "server_id": info["server_id"], "library_type": "user", "library_id": 0,
                "item_key": item["key"], "attachment_key": attachment["key"],
                "item_version": item.get("version", 0), "attachment_version": attachment.get("version", 0),
            }]
            entries.append({
                **summary, "attachment_key": attachment["key"],
                "item_version": item.get("version"), "attachment_version": attachment.get("version"),
                "filename": build_filename(item, disambiguator=disambiguator), "collections": names,
                "metadata": metadata, "_attachment": attachment,
            })
    visible_collections = [c for c in collections if item_keys is None and attachment_keys is None
                           or c["key"] in relevant_collections]
    return {
        "zotero": info,
        "collections": [{"key": c["key"], "name": c["data"].get("name"), "path": "/".join(paths[c["key"]]),
                         "item_count": (c.get("meta") or {}).get("numItems")} for c in visible_collections],
        "entries": entries, "skipped": skipped, "items_total": len(top_items),
    }


def _inspect_attachment(client, entry):
    path = _attachment_path(client, entry["_attachment"])
    try:
        if path is None or not path.is_file():
            return {"available": False, "reason": "file_missing"}
        if path.stat().st_size > file_service.get_max_file_size():
            return {"available": False, "reason": "FILE_TOO_LARGE"}
        fingerprint = file_service.calculate_file_hash_from_path(path)
    except OSError:
        return {"available": False, "reason": "file_unreadable"}
    file_id = file_service.check_file_hash_exists(fingerprint)
    existing = file_service.get_file_by_id(file_id) if file_id else None
    return {"available": True, "file_hash": fingerprint, "file_id": file_id,
            "duplicate": file_id is not None, "file_status": existing["status"] if existing else None}


def preview_import(collection_keys: Optional[List[str]] = None, collection_mode: str = "path",
                   *, base_url: Optional[str] = None, client: Optional[ZoteroClient] = None,
                   item_keys=None, attachment_keys=None, server_id=None, max_items=None) -> Dict[str, Any]:
    """只读预览选定附件的可用性和内容去重结果，不创建 Piece 文件。"""
    with _client_scope(client, base_url=base_url, server_id=server_id) as local:
        plan = _plan(local, collection_keys, collection_mode, item_keys, attachment_keys, max_items)
        items = [{**{k: v for k, v in entry.items() if k not in ("_attachment", "metadata")},
                  **_inspect_attachment(local, entry)} for entry in plan["entries"]]
    return {
        "zotero": plan["zotero"], "collections": plan["collections"], "items_total": plan["items_total"],
        "importable_count": sum(item["available"] for item in items),
        "unavailable_count": sum(not item["available"] for item in items),
        "duplicate_count": sum(bool(item.get("duplicate")) for item in items),
        "skipped_count": len(plan["skipped"]), "items": items, "skipped": plan["skipped"],
    }


def import_library(collection_keys: Optional[List[str]] = None, collection_mode: str = "path",
                   collections: Optional[List[str]] = None, *, base_url: Optional[str] = None,
                   client: Optional[ZoteroClient] = None, item_keys=None, attachment_keys=None,
                   server_id=None, max_items=None) -> Dict[str, Any]:
    """复制选定 PDF 入库并返回任务；不把受理当作索引完成，也不写回 Zotero。"""
    with _client_scope(client, base_url=base_url, server_id=server_id) as local:
        plan = _plan(local, collection_keys, collection_mode, item_keys, attachment_keys, max_items)
        fixed_ids = collection_service.resolve_collection_ids(collections) if collections else []
        items, failures, task_ids = [], [], []
        duplicates = 0
        for index, entry in enumerate(plan["entries"]):
            summary = {k: v for k, v in entry.items() if k not in ("_attachment", "metadata")}
            try:
                path = _attachment_path(local, entry["_attachment"])
                if path is None or not path.is_file():
                    failures.append({**summary, "reason": "file_missing"})
                    continue
                ids = collection_service.resolve_collection_ids(entry["collections"], create=True) + fixed_ids
                result = file_service.import_file(path, filename=entry["filename"], collection_ids=ids,
                                                  metadata=entry["metadata"])
            except BusinessError as exc:
                failures.append({**summary, "reason": exc.code, "message": str(exc)})
                if exc.code in {"ZOTERO_INSTANCE_CHANGED", "ZOTERO_UNAVAILABLE", "ZOTERO_DISABLED"}:
                    failures.extend({"key": rest["key"], "attachment_key": rest["attachment_key"],
                                     "reason": "not_attempted"} for rest in plan["entries"][index + 1:])
                    break
                continue
            except OSError:
                failures.append({**summary, "reason": "file_unreadable"})
                continue
            duplicates += bool(result.get("duplicate"))
            task_ids.extend(t for t in result.get("task_ids", []) if t not in task_ids)
            file_info = file_service.get_file_by_id(result["file_id"]) or {}
            items.append({**summary, "file_id": result["file_id"], "duplicate": result.get("duplicate", False),
                          "task_id": result.get("task_id"), "file_status": file_info.get("status", "missing"),
                          "file_hash": file_info.get("file_hash")})
    return {
        "zotero": plan["zotero"], "items": items, "failures": failures, "skipped": plan["skipped"],
        "task_ids": task_ids, "accepted_count": len(items) - duplicates, "duplicate_count": duplicates,
        "failed_count": len(failures), "skipped_count": len(plan["skipped"]),
    }
