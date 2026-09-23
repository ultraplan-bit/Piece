"""Zotero Local API 导入：用 httpx MockTransport 模拟本机 Zotero，不依赖真实进程。"""

from pathlib import Path

import httpx
import pytest

from indexing.services import file_service, zotero_service as zotero
from indexing.services.errors import BusinessError


def _item(key, item_type="journalArticle", title="标题", creators=(), date="2024-05-01", collections=(), **extra):
    return {"key": key, "version": 1, "links": {}, "meta": {"parsedDate": date, "numChildren": 1},
            "data": {"key": key, "itemType": item_type, "title": title, "creators": list(creators), "date": date,
                     "collections": list(collections), "tags": [], **extra}}


def _attachment(key, parent, path: Path | None, content_type="application/pdf", link_mode="imported_url", title="Full Text PDF"):
    links = {"enclosure": {"href": path.as_uri(), "type": content_type}} if path else {}
    return {"key": key, "version": 1, "links": links, "meta": {},
            "data": {"key": key, "itemType": "attachment", "parentItem": parent, "contentType": content_type,
                     "linkMode": link_mode, "title": title, "filename": path.name if path else ""}}


def _collection(key, name, parent=False):
    return {"key": key, "version": 1, "meta": {"numItems": 1}, "data": {"key": key, "name": name, "parentCollection": parent}}


@pytest.fixture
def library(tmp_path):
    pdf_a = tmp_path / "storage" / "AAAA1111" / "乱码 名.pdf"
    pdf_a.parent.mkdir(parents=True)
    pdf_a.write_bytes(b"%PDF-1.4 a")
    pdf_b = tmp_path / "storage" / "BBBB2222" / "b.pdf"
    pdf_b.parent.mkdir(parents=True)
    pdf_b.write_bytes(b"%PDF-1.4 b")
    pdf_c = tmp_path / "storage" / "CCCC3333" / "c.pdf"
    pdf_c.parent.mkdir(parents=True)
    pdf_c.write_bytes(b"%PDF-1.4 c")
    missing = tmp_path / "storage" / "DDDD4444" / "gone.pdf"
    state = {"enabled": True, "requests": []}
    top = [
        _item("ITEM0001", title="乳腺超声报告: 结构化/方法?", date="2024-02-23", collections=["COLLCHLD"],
              creators=[{"creatorType": "author", "lastName": "高", "firstName": "言"},
                        {"creatorType": "author", "name": "GAO Yan"}],
              publicationTitle="计算机应用文摘", DOI="10.1/x", abstractNote="摘要" * 300,
              tags=[{"tag": "超声", "type": 1}, {"tag": "LLM"}]),
        _item("ITEM0002", title="Two PDFs", collections=["COLLROOT"], creators=[{"creatorType": "author", "lastName": "Lin"}]),
        _item("ITEM0003", title="No file", collections=[]),
        _item("ITEM0004", item_type="webpage", title="Web only", collections=["COLLROOT"]),
        _item("ITEM0005", title="Missing on disk", collections=[]),
    ]
    attachments = [
        _attachment("AAAA1111", "ITEM0001", pdf_a),
        _attachment("BBBB2222", "ITEM0002", pdf_b, title="Full Text PDF"),
        _attachment("CCCC3333", "ITEM0002", pdf_c, title="Submitted Version"),
        _attachment("HTML0001", "ITEM0004", None, content_type="text/html", link_mode="linked_url"),
        _attachment("DDDD4444", "ITEM0005", missing),
    ]
    collections = [_collection("COLLROOT", "顶层"), _collection("COLLCHLD", "子集", "COLLROOT"), _collection("COLLOTHR", "其他")]

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(str(request.url))
        if not state["enabled"]:
            return httpx.Response(403, text="Forbidden")
        path = request.url.path
        params = request.url.params
        headers = {"X-Zotero-Version": "10.0.2", "Zotero-API-Version": "3", "Zotero-Schema-Version": "44"}
        if path == "/api/":
            return httpx.Response(200, headers=headers, text="")

        def page(items):
            start, limit = int(params.get("start", 0)), int(params.get("limit", 100))
            return httpx.Response(200, headers={**headers, "Total-Results": str(len(items))},
                                  json=items[start:start + limit])
        if path == "/api/users/0/items/top":
            return page(top)
        if path == "/api/users/0/items" and params.get("itemType") == "attachment":
            return page(attachments)
        if path == "/api/users/0/collections":
            return page(collections)
        return httpx.Response(404, text="Not found")

    client = zotero.ZoteroClient(transport=httpx.MockTransport(handler))
    yield {"client": client, "state": state, "pdf_a": pdf_a, "pdf_b": pdf_b}
    client.close()


def test_file_url_to_path_round_trip(tmp_path):
    source = tmp_path / "storage" / "K" / "中文 100%.pdf"
    assert zotero.file_url_to_path(source.as_uri()) == source
    assert zotero.file_url_to_path("https://example.com/x.pdf") is None


def test_build_filename_and_metadata():
    item = _item("K1", title="A" * 300, creators=[{"creatorType": "author", "lastName": "Smith"}, {"creatorType": "author", "lastName": "Wu"}])
    name = zotero.build_filename(item)
    assert name.startswith("Smith 等 - 2024 - AAAA") and name.endswith(".pdf")
    assert len(name) - len(".pdf") <= file_service.MAX_STORED_STEM
    tagged = zotero.build_filename(item, disambiguator="K1")
    assert tagged.endswith(" - K1.pdf") and len(tagged) == len(name)
    bare = zotero.build_filename(_item("K2", title="", date=""))
    assert bare == "K2.pdf"
    metadata = zotero.build_metadata(_item("K3", title="T", creators=[{"creatorType": "author", "lastName": "高", "firstName": "言"}],
                                           DOI="10.1/x", tags=[{"tag": "a"}]), ["顶层/子集"])
    assert metadata["authors"] == ["言 高"] and metadata["year"] == "2024" and metadata["doi"] == "10.1/x"
    assert metadata["tags"] == ["a"] and metadata["zotero_collections"] == ["顶层/子集"] and metadata["zotero_key"] == "K3"


def test_preview_lists_importable_and_skipped(library):
    preview = zotero.preview_import(client=library["client"])
    assert preview["zotero"]["zotero_version"] == "10.0.2"
    assert [c["path"] for c in preview["collections"]] == ["顶层", "顶层/子集", "其他"]
    assert preview["items_total"] == 5 and preview["skipped_count"] == 2
    assert {s["key"]: s["reason"] for s in preview["skipped"]} == {"ITEM0003": "no_pdf", "ITEM0004": "no_pdf"}
    by_key = {}
    for item in preview["items"]:
        by_key.setdefault(item["key"], []).append(item)
    assert by_key["ITEM0001"][0]["filename"] == "高 等 - 2024 - 乳腺超声报告 结构化 方法.pdf"
    assert by_key["ITEM0001"][0]["collections"] == ["顶层/子集"]
    assert [i["filename"] for i in by_key["ITEM0002"]] == ["Lin - 2024 - Two PDFs - Full Text PDF.pdf", "Lin - 2024 - Two PDFs - Submitted Version.pdf"]
    assert "metadata" not in preview["items"][0]
    # 范围限定到父集合时包含子集合里的条目；top 模式只保留顶层名
    scoped = zotero.preview_import(collection_keys=["COLLROOT"], collection_mode="top", client=library["client"])
    assert {i["key"] for i in scoped["items"]} == {"ITEM0001", "ITEM0002"}
    assert scoped["items"][0]["collections"] == ["顶层"]
    with pytest.raises(BusinessError, match="集合不存在"):
        zotero.preview_import(collection_keys=["NOPE"], client=library["client"])


def test_disabled_and_unreachable_are_distinguished(library):
    library["state"]["enabled"] = False
    with pytest.raises(BusinessError) as disabled:
        zotero.preview_import(client=library["client"])
    assert disabled.value.code == "ZOTERO_DISABLED" and "允许" in str(disabled.value)

    def refuse(request):
        raise httpx.ConnectError("refused")
    with zotero.ZoteroClient(transport=httpx.MockTransport(refuse)) as offline:
        with pytest.raises(BusinessError) as unreachable:
            zotero.preview_import(client=offline)
    assert unreachable.value.code == "ZOTERO_UNAVAILABLE"


def test_import_copies_pdfs_writes_metadata_and_is_idempotent(knowledge_base, library):
    from indexing.services import file_service, metadata_service, collection_service, task_service
    result = zotero.import_library(collection_mode="path", client=library["client"])
    assert result["accepted_count"] == 3 and result["duplicate_count"] == 0
    assert result["failed_count"] == 1 and result["failures"][0]["reason"] == "file_missing"
    assert result["skipped_count"] == 2
    first = next(i for i in result["items"] if i["key"] == "ITEM0001")
    info = file_service.get_file_by_id(first["file_id"])
    assert info["filename"] == "高 等 - 2024 - 乳腺超声报告 结构化 方法.md"
    assert Path(info["original_file_path"]).read_bytes() == library["pdf_a"].read_bytes()
    assert library["pdf_a"].is_file()
    metadata = metadata_service.get_file_metadata(first["file_id"])
    assert metadata["publication"] == "计算机应用文摘" and len(metadata["abstract"]) == 500
    assert metadata["zotero_collections"] == ["顶层/子集"]
    names = {c["name"] for c in collection_service.list_collections()}
    assert names == {"顶层/子集", "顶层"}
    # 重跑：内容哈希命中，全部记为重复，不产生新任务
    again = zotero.import_library(client=library["client"])
    assert again["duplicate_count"] == 3 and again["accepted_count"] == 0
    assert len(file_service.get_files_list()) == 3
    with pytest.raises(BusinessError, match="集合不存在"):
        zotero.import_library(collections=["未创建"], client=library["client"])
    assert all(task_service.get_task(t)["status"] == "pending" for t in result["task_ids"])
