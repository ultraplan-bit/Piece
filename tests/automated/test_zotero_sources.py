"""import_file 对 metadata['zotero_sources'] 的受控合并：不接真实 Zotero，直接构造来源数组。"""

import hashlib
from pathlib import Path

import pytest

from indexing.services import file_service, metadata_service
from indexing.services.errors import BusinessError


def source(item_key="ITEM1", attachment_key="ATT1", *, server_id="srv-1",
           library_type="user", library_id=0, item_version=1, attachment_version=1):
    return {"server_id": server_id, "library_type": library_type, "library_id": library_id,
            "item_key": item_key, "attachment_key": attachment_key,
            "item_version": item_version, "attachment_version": attachment_version}


def _pdf(base, name, payload=b"%PDF-1.4 x"):
    path = base / name
    path.write_bytes(payload)
    return path


def _sources(file_id):
    return metadata_service.get_file_metadata(file_id)["zotero_sources"]


def test_new_file_saves_metadata_and_real_hash(knowledge_base):
    payload = b"%PDF-1.4 AAA"
    result = file_service.import_file(_pdf(knowledge_base.path, "a.pdf", payload),
                                      metadata={"title": "标题", "tags": ["x"], "zotero_key": "ITEM1",
                                                "zotero_sources": [source()]})
    assert not result["duplicate"]
    stored = metadata_service.get_file_metadata(result["file_id"])
    assert stored["title"] == "标题" and stored["tags"] == ["x"] and stored["zotero_key"] == "ITEM1"
    assert stored["zotero_sources"] == [{**source(), "file_hash": hashlib.sha256(payload).hexdigest()}]


def test_duplicate_merges_sources_without_touching_user_attributes(knowledge_base):
    payload = b"%PDF-1.4 same"
    accepted = file_service.import_file(_pdf(knowledge_base.path, "first.pdf", payload),
                                        metadata={"title": "原名", "tags": ["keep"], "zotero_key": "K0",
                                                  "zotero_sources": [source(item_version=2, attachment_version=2)]})
    again = file_service.import_file(_pdf(knowledge_base.path, "second.pdf", payload),
                                     metadata={"title": "不该覆盖", "zotero_key": "K2",
                                               "zotero_sources": [source(item_version=5, attachment_version=6)]})
    assert again["duplicate"] and again["file_id"] == accepted["file_id"]
    stored = metadata_service.get_file_metadata(accepted["file_id"])
    assert stored["title"] == "原名" and stored["tags"] == ["keep"] and stored["zotero_key"] == "K0"
    assert stored["zotero_sources"] == [
        {**source(item_version=5, attachment_version=6), "file_hash": hashlib.sha256(payload).hexdigest()}]


def test_multiple_sources_kept_and_older_version_ignored(knowledge_base):
    payload = b"%PDF-1.4 multi"
    accepted = file_service.import_file(_pdf(knowledge_base.path, "f.pdf", payload),
                                        metadata={"zotero_sources": [
                                            source(item_key="A", attachment_key="ATT-A", item_version=4, attachment_version=4)]})
    file_service.import_file(_pdf(knowledge_base.path, "g.pdf", payload),
                             metadata={"zotero_sources": [
                                 source(item_key="B", attachment_key="ATT-B"),
                                 source(item_key="A", attachment_key="ATT-A", item_version=1, attachment_version=1)]})
    by_key = {s["item_key"]: s for s in _sources(accepted["file_id"])}
    assert len(by_key) == 2 and by_key["B"]["item_key"] == "B"
    assert by_key["A"]["item_version"] == 4  # 旧版本不覆盖
    # 不同 server_id 是不同来源，全部保留
    file_service.import_file(_pdf(knowledge_base.path, "h.pdf", payload),
                             metadata={"zotero_sources": [
                                 source(item_key="A", attachment_key="ATT-A", server_id="srv-2")]})
    assert len(_sources(accepted["file_id"])) == 3


def test_legacy_sources_dedupe_by_full_record(knowledge_base):
    payload = b"%PDF-1.4 legacy"
    legacy = source(server_id=None, item_key="L", attachment_key="ATT-L")
    accepted = file_service.import_file(_pdf(knowledge_base.path, "l1.pdf", payload),
                                        metadata={"zotero_sources": [legacy]})
    # 整条记录相同不重复
    file_service.import_file(_pdf(knowledge_base.path, "l2.pdf", payload),
                             metadata={"zotero_sources": [dict(legacy)]})
    assert len(_sources(accepted["file_id"])) == 1
    # null ID 不是稳定身份：同 key 不同版本的旧版记录作为独立记录保留，不覆盖原记录
    file_service.import_file(_pdf(knowledge_base.path, "l3.pdf", payload),
                             metadata={"zotero_sources": [
                                 source(server_id=None, item_key="L", attachment_key="ATT-L",
                                        item_version=9, attachment_version=9)]})
    assert sorted(s["item_version"] for s in _sources(accepted["file_id"])) == [1, 9]


def test_duplicate_without_sources_keeps_old_behaviour(knowledge_base):
    payload = b"%PDF-1.4 old"
    accepted = file_service.import_file(_pdf(knowledge_base.path, "o1.pdf", payload),
                                        metadata={"title": "保持"})
    again = file_service.import_file(_pdf(knowledge_base.path, "o2.pdf", payload),
                                     metadata={"title": "改", "zotero_key": "K9"})
    assert again["duplicate"]
    assert metadata_service.get_file_metadata(accepted["file_id"]) == {"title": "保持"}


def test_parse_and_reindex_preserve_sources(knowledge_base):
    path = knowledge_base.path / "note.md"
    path.write_text("# 正文\n\n内容", encoding="utf-8")
    accepted = file_service.import_file(path, filename="note.md",
                                        metadata={"title": "笔记", "zotero_sources": [source()]})
    expected = {**source(), "file_hash": hashlib.sha256(path.read_bytes()).hexdigest()}
    knowledge_base.drain()
    stored = metadata_service.get_file_metadata(accepted["file_id"])
    assert stored["title"] == "笔记" and stored["zotero_sources"] == [expected]
    file_service.reindex_file(accepted["file_id"])
    knowledge_base.drain()
    assert metadata_service.get_file_metadata(accepted["file_id"])["zotero_sources"] == [expected]


def test_working_frontmatter_reindex_keeps_sources(knowledge_base):
    """工作文件带上非空 frontmatter 后从 working 重索引，来源不能被解析属性覆盖丢掉。"""
    path = knowledge_base.path / "note.md"
    path.write_text("# 正文\n\n内容", encoding="utf-8")
    accepted = file_service.import_file(path, filename="note.md",
                                        metadata={"title": "笔记", "zotero_sources": [source()]})
    expected = {**source(), "file_hash": hashlib.sha256(path.read_bytes()).hexdigest()}
    knowledge_base.drain()
    working = Path(file_service.get_file_by_id(accepted["file_id"])["file_path"])
    working.write_text("---\ntitle: 新标题\n---\n\n# 正文\n\n调整后的内容", encoding="utf-8")
    file_service.reindex_file(accepted["file_id"], source="working")
    knowledge_base.drain()
    stored = metadata_service.get_file_metadata(accepted["file_id"])
    assert stored["title"] == "新标题"  # frontmatter 覆盖普通属性的既有语义不变
    assert stored["zotero_sources"] == [expected]


def test_source_versions_advance_independently(knowledge_base):
    path = _pdf(knowledge_base.path, "versions.pdf")
    result = file_service.import_file(path, metadata={"zotero_sources": [source(item_version=5, attachment_version=10)]})
    file_service.import_file(path, metadata={"zotero_sources": [source(item_version=6, attachment_version=8)]})
    stored = _sources(result["file_id"])[0]
    assert stored["item_version"] == 6 and stored["attachment_version"] == 10
    assert stored["file_hash"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_stale_frontmatter_cannot_drop_new_source(knowledge_base):
    path = knowledge_base.path / "sources.md"
    path.write_text("# 正文\n\n同一份内容", encoding="utf-8")
    result = file_service.import_file(path, metadata={"zotero_sources": [source()]})
    knowledge_base.drain()
    file_service.import_file(path, metadata={"zotero_sources": [source(item_key="OTHER", attachment_key="OTHERATT")]})
    expected = _sources(result["file_id"])
    working = Path(file_service.get_file_by_id(result["file_id"])["file_path"])
    working.write_text("---\ntitle: 新标题\nzotero_sources: []\n---\n\n# 正文\n\n编辑过的正文", encoding="utf-8")
    file_service.reindex_file(result["file_id"], source="working")
    knowledge_base.drain()
    assert metadata_service.get_file_metadata(result["file_id"])["title"] == "新标题"
    assert _sources(result["file_id"]) == expected and len(expected) == 2


@pytest.mark.parametrize("bad", [
    "not-a-list",
    [123],
    [{"server_id": 5, "library_type": "user", "library_id": 0, "item_key": "I",
      "attachment_key": "A", "item_version": 1, "attachment_version": 1}],
    [{"server_id": None, "library_type": "user", "library_id": 0, "item_key": "I",
      "item_version": 1, "attachment_version": 1}],
    [{"server_id": None, "library_type": "user", "library_id": "0", "item_key": "I",
      "attachment_key": "A", "item_version": 1, "attachment_version": 1}],
])
def test_invalid_source_structure_rejected(knowledge_base, bad):
    path = _pdf(knowledge_base.path, "bad.pdf", b"%PDF-1.4 bad")
    with pytest.raises(BusinessError, match="zotero_sources"):
        file_service.import_file(path, metadata={"zotero_sources": bad})
    assert file_service.get_files_list() == []
