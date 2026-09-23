"""Zotero 文献库一次性导入：通过 Zotero 7+ 的本机 Local API 读取元数据与附件路径。

只读、零新增依赖（httpx 已有）。一个带 PDF 附件的 Zotero 条目对应一个 Piece 文件：
附件按 API 给出的本机绝对路径交给 file_service.import_file 复制入库，条目元数据随
登记写入 files.metadata，Zotero 集合树按路径拼接映射到 Piece 的扁平集合。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse
from urllib.request import url2pathname

import httpx

from . import collection_service, file_service
from .errors import BusinessError

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:23119/api"
USER_PREFIX = "/users/0"
PAGE_SIZE = 100
PDF_TYPE = "application/pdf"
MAX_ABSTRACT = 500
COLLECTION_MODES = ("path", "top", "none")
_ENABLE_HINT = "请在 Zotero 的「设置 → 高级 → 允许本机其他应用程序与 Zotero 通信」中开启"
_INVALID_NAME_CHARS = re.compile(r'[/\\:<>"|?*\x00-\x1f]+')
# Zotero 在 creatorSummary 里加了双向隔离控制符，自行拼作者串时不引入
_WHITESPACE = re.compile(r"\s+")


class ZoteroClient:
    """Zotero Local API 的最小只读客户端（阻塞调用，由上层放到线程池执行）。"""

    def __init__(self, base_url: str | None = None, *, timeout: float = 30.0, transport=None):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
        url = f"{self.base_url}{path}"
        try:
            response = self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise BusinessError("ZOTERO_UNAVAILABLE",
                                f"无法连接 Zotero（{self.base_url}）：{exc}；请确认 Zotero 7 及以上版本正在运行") from exc
        if response.status_code == 403:
            raise BusinessError("ZOTERO_DISABLED", f"Zotero 拒绝了本机 API 访问；{_ENABLE_HINT}")
        if response.status_code == 404:
            raise BusinessError("NOT_FOUND", f"Zotero 中不存在该对象：{path}")
        if not (200 <= response.status_code < 300):
            raise BusinessError("ZOTERO_ERROR", f"Zotero 返回 {response.status_code}：{response.text[:200]}")
        return response

    def probe(self) -> Dict[str, Any]:
        """确认 Local API 可用，并取回版本信息。"""
        response = self._get("/")
        return {
            "base_url": self.base_url,
            "zotero_version": response.headers.get("X-Zotero-Version"),
            "api_version": response.headers.get("Zotero-API-Version"),
            "schema_version": response.headers.get("Zotero-Schema-Version"),
        }

    def _paged(self, path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """按 Total-Results 分页取全；Local API 默认不分页，但大库仍分批更稳。"""
        items: List[Dict[str, Any]] = []
        start = 0
        while True:
            response = self._get(path, {**(params or {}), "limit": PAGE_SIZE, "start": start})
            page = response.json()
            if not isinstance(page, list):
                raise BusinessError("ZOTERO_ERROR", f"Zotero 响应格式异常：{path}")
            items.extend(page)
            total = response.headers.get("Total-Results")
            start += len(page)
            if not page or total is None or start >= int(total):
                break
        return items

    def top_items(self) -> List[Dict[str, Any]]:
        return self._paged(f"{USER_PREFIX}/items/top")

    def attachments(self) -> List[Dict[str, Any]]:
        return self._paged(f"{USER_PREFIX}/items", {"itemType": "attachment"})

    def collections(self) -> List[Dict[str, Any]]:
        return self._paged(f"{USER_PREFIX}/collections")

    def attachment_file_url(self, key: str) -> str:
        """附件在本机的 file:// 地址（纯文本）。"""
        return self._get(f"{USER_PREFIX}/items/{key}/file/view/url").text.strip()


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
    for attachment in attachments:
        data = attachment["data"]
        if data.get("contentType") != PDF_TYPE or data.get("linkMode") == "linked_url":
            continue
        grouped.setdefault(data.get("parentItem") or attachment["key"], []).append(attachment)
    return grouped


def _attachment_path(client: ZoteroClient, attachment: Dict[str, Any]) -> Optional[Path]:
    href = (attachment.get("links") or {}).get("enclosure", {}).get("href")
    if not href:
        try:
            href = client.attachment_file_url(attachment["key"])
        except BusinessError:
            return None
    return file_url_to_path(href)


def _plan(client: ZoteroClient, collection_keys: Optional[List[str]], collection_mode: str) -> Dict[str, Any]:
    """读取文库并算出导入计划；preview 与 import 共用，保证预览即所得。"""
    if collection_mode not in COLLECTION_MODES:
        raise BusinessError("INVALID_INPUT", f"collection_mode 只能是 {', '.join(COLLECTION_MODES)}")
    info = client.probe()
    collections = client.collections()
    paths = _collection_paths(collections)
    selected = _descendants(collections, collection_keys) if collection_keys else None
    if collection_keys:
        unknown = [key for key in collection_keys if key not in paths]
        if unknown:
            raise BusinessError("NOT_FOUND", f"Zotero 集合不存在：{', '.join(unknown)}")
    attachments = _pdf_attachments(client.attachments())
    top_items = client.top_items()

    entries: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for item in top_items:
        data = item["data"]
        item_collections = [key for key in data.get("collections") or [] if key in paths]
        if selected is not None and not (set(item_collections) & selected):
            continue
        # 独立附件条目没有父条目，在附件列表里以自身 key 成组
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
            disambiguator = ""
            if len(pdfs) > 1:
                # 同一条目多个 PDF：附件自身标题（如 Full Text PDF / Submitted Version）通常能区分，否则编号
                disambiguator = _clean_name_part(str(attachment["data"].get("title") or ""))[:40] or str(index + 1)
            entries.append({
                **summary,
                "attachment_key": attachment["key"],
                "filename": build_filename(item, disambiguator=disambiguator),
                "collections": names,
                "metadata": build_metadata(item, ["/".join(paths[key]) for key in item_collections if paths.get(key)]),
                "_attachment": attachment,
            })
    return {
        "zotero": info,
        "collections": [{"key": c["key"], "name": c["data"].get("name"), "path": "/".join(paths[c["key"]]),
                         "item_count": (c.get("meta") or {}).get("numItems")} for c in collections],
        "entries": entries,
        "skipped": skipped,
        "items_total": len(top_items),
    }


def preview_import(collection_keys: Optional[List[str]] = None, collection_mode: str = "path",
                   *, base_url: Optional[str] = None, client: Optional[ZoteroClient] = None) -> Dict[str, Any]:
    """只读预览：Zotero 是否可达、有哪些集合、哪些条目会导入、哪些会跳过。"""
    owned = client is None
    client = client or ZoteroClient(base_url)
    try:
        plan = _plan(client, collection_keys, collection_mode)
    finally:
        if owned:
            client.close()
    return {
        "zotero": plan["zotero"],
        "collections": plan["collections"],
        "items_total": plan["items_total"],
        "importable_count": len(plan["entries"]),
        "skipped_count": len(plan["skipped"]),
        "items": [{k: v for k, v in e.items() if k not in ("_attachment", "metadata")} for e in plan["entries"]],
        "skipped": plan["skipped"],
    }


def import_library(collection_keys: Optional[List[str]] = None, collection_mode: str = "path",
                   collections: Optional[List[str]] = None, *, base_url: Optional[str] = None,
                   client: Optional[ZoteroClient] = None) -> Dict[str, Any]:
    """按预览计划逐条导入；重复文件由内容哈希去重，重跑安全。

    ``collections`` 是额外附加到每个文件的 Piece 集合名（须已存在）；
    由 Zotero 集合映射出的集合名不存在时自动创建。
    """
    owned = client is None
    client = client or ZoteroClient(base_url)
    try:
        plan = _plan(client, collection_keys, collection_mode)
        fixed_ids = collection_service.resolve_collection_ids(collections) if collections else []
        items: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []
        task_ids: List[int] = []
        duplicates = 0
        for entry in plan["entries"]:
            summary = {k: v for k, v in entry.items() if k not in ("_attachment", "metadata")}
            path = _attachment_path(client, entry["_attachment"])
            if path is None or not path.is_file():
                failures.append({**summary, "reason": "file_missing", "path": str(path) if path else None})
                continue
            try:
                ids = collection_service.resolve_collection_ids(entry["collections"], create=True) + fixed_ids
                result = file_service.import_file(path, filename=entry["filename"], collection_ids=ids,
                                                  metadata=entry["metadata"])
            except BusinessError as exc:
                failures.append({**summary, "reason": exc.code, "message": str(exc)})
                continue
            duplicates += bool(result.get("duplicate"))
            # 重复文件若仍在途也会带回任务 ID，供调用方等待；但不算本次新受理
            task_ids.extend(t for t in result.get("task_ids", []) if t not in task_ids)
            items.append({**summary, "file_id": result["file_id"], "duplicate": result.get("duplicate", False),
                          "task_id": result.get("task_id")})
    finally:
        if owned:
            client.close()
    return {
        "zotero": plan["zotero"],
        "items": items,
        "failures": failures,
        "skipped": plan["skipped"],
        "task_ids": task_ids,
        "accepted_count": len(items) - duplicates,
        "duplicate_count": duplicates,
        "failed_count": len(failures),
        "skipped_count": len(plan["skipped"]),
    }
