"""Wiki 大数据量分页不变量：list/search/references/lint/history 与证据页。

默认跳过（slow 标记）；显式运行：
    uv run pytest tests/automated/test_wiki_scale.py --run-slow

Wiki 页面真源是磁盘上的 MD 文件，规模由 scale_fixtures 直接落盘并同步派生索引。
"""

import pytest

from scale_fixtures import WIKI_KEYWORD, seed_knowledge_base
from indexing.services import wiki_service as wiki
from indexing.services.errors import BusinessError

pytestmark = pytest.mark.slow

SCALE = dict(objects=500, relations=1000, evidence=500, hot_object_evidence=250,
             reference_evidence=250, revisions=250, wiki_pages=300, wiki_evidence=250,
             wiki_hot_page_evidence=150, wiki_history=250)


@pytest.fixture(scope="module")
def scale(tmp_path_factory):
    from indexing import settings, database

    tmp_path = tmp_path_factory.mktemp("wiki-scale")
    previous = settings._settings
    config = settings.AppSettings(data_path=str(tmp_path))
    config.embedding.vector_dim = 2
    config.mcp.api_key = "read-test-key"
    config.mcp.index_api_key = "write-test-key"
    settings._settings = config
    database.close_connection_pool()
    database.init_database()
    database.init_connection_pool()
    stats = seed_knowledge_base(tmp_path, **SCALE)
    yield stats
    database.close_connection_pool()
    settings._settings = previous


def collect_pages(call, *, limit, key, **kwargs):
    """按 limit 翻到底，返回 (ids, total)，并校验 total 稳定与末页取尽。"""
    ids, offset, total = [], 0, None
    while True:
        page = call(limit=limit, offset=offset, **kwargs)
        assert page["limit"] == limit and page["offset"] == offset
        if total is None:
            total = page["total"]
        assert page["total"] == total, "翻页过程中 total 不稳定"
        items = page[key]
        assert len(items) <= limit
        ids.extend(item["id"] for item in items)
        if len(items) < limit:
            assert offset + len(items) == total
            break
        offset += limit
    return ids, total


def test_list_pages_covers_all_and_is_consistent(scale):
    base = wiki.list_pages(limit=1)
    assert base["total"] == len(scale["wiki_pages"])
    assert base["index_status"] == "current" and base["errors"] == [], "种子索引应与磁盘一致"

    ids, reported = collect_pages(wiki.list_pages, limit=100, key="pages")
    assert reported == len(scale["wiki_pages"])
    assert len(ids) == reported == len(set(ids))
    assert set(ids) == {page["id"] for page in scale["wiki_pages"]}


def test_list_pages_offset_beyond_total_and_empty(scale):
    total = wiki.list_pages(limit=1)["total"]
    for offset in (total, total + 5, total + 500):
        page = wiki.list_pages(limit=100, offset=offset)
        assert page["pages"] == [] and page["total"] == total and page["offset"] == offset
    empty = wiki.list_pages(kind="entity", limit=100)
    assert empty["pages"] == [] and empty["total"] == 0


def test_list_pages_limit_bounds(scale):
    assert len(wiki.list_pages(limit=100)["pages"]) == 100
    for bad in ({"limit": 101}, {"limit": 0}, {"offset": -1}):
        with pytest.raises(BusinessError) as exc:
            wiki.list_pages(**bad)
        assert exc.value.code == "INVALID_INPUT"


def test_search_pages_covers_all(scale):
    total = wiki.search_pages(query=WIKI_KEYWORD, limit=1)["total"]
    assert total == len(scale["wiki_pages"]), "共享关键词应命中全部页面"

    ids, reported = collect_pages(wiki.search_pages, limit=100, key="pages", query=WIKI_KEYWORD)
    assert reported == total
    assert len(ids) == total == len(set(ids))
    assert set(ids) == {page["id"] for page in scale["wiki_pages"]}

    empty = wiki.search_pages(query="zzz不存在的关键词zzz", limit=100)
    assert empty["total"] == 0 and empty["pages"] == []


def test_references_pagination(scale):
    total = wiki.references(source_library_id=scale["library_id"],
                            source_file_id=scale["source_file_id"], limit=1)["total"]
    assert total == scale["wiki_reference_evidence"]

    ids, reported = collect_pages(
        wiki.references, limit=100, key="evidence",
        source_library_id=scale["library_id"], source_file_id=scale["source_file_id"])
    assert reported == total
    assert len(ids) == total == len(set(ids))

    missing = wiki.references(source_library_id=scale["library_id"], source_file_id=999999)
    assert missing["total"] == 0 and missing["evidence"] == []


def test_lint_pagination_checks_each_page_once(scale):
    total = wiki.lint(limit=1)["total_pages"]
    assert total == len(scale["wiki_pages"])

    offset, checked = 0, 0
    while True:
        report = wiki.lint(limit=100, offset=offset)
        assert report["total_pages"] == total
        assert report["offset"] == offset and report["limit"] == 100
        assert report["checked_pages"] <= 100
        checked += report["checked_pages"]
        if report["checked_pages"] < 100:
            break
        offset += 100
    assert checked == total


def test_get_record_page_evidence_pagination(scale):
    page = wiki.get_record(kind="page", id=scale["wiki_hot_page"], limit=1)
    total = page["evidence"]["total"]
    assert total == scale["wiki_hot_page_evidence"] and total > 100

    ids, reported = collect_pages(
        lambda **kw: wiki.get_record(kind="page", id=scale["wiki_hot_page"], **kw)["evidence"],
        limit=100, key="items")
    assert reported == total
    assert len(ids) == total == len(set(ids))


def test_history_pagination(scale):
    total = wiki.history(id=scale["wiki_history_page"], limit=1)["total"]
    assert total == SCALE["wiki_history"]

    ids, reported = collect_pages(
        lambda **kw: wiki.history(id=scale["wiki_history_page"], **kw), limit=100, key="history")
    assert reported == total
    assert len(ids) == total == len(set(ids))


def test_page_links_navigation_is_bounded(scale):
    # 每 10 页一条出链；逐页检查 links/backlinks 的切片与 total 自洽。
    # 每个 get_record 都会全量扫描 MD，样本取小以免用例过慢。
    checked = 0
    for page in scale["wiki_pages"][:12]:
        record = wiki.get_record(kind="page", id=page["id"], limit=100)
        links, backlinks = record["links"], record["backlinks"]
        assert links["total"] == (1 if page["index"] % 10 == 0 else 0)
        assert len(links["items"]) == links["total"] <= links["limit"]
        assert len(backlinks["items"]) <= backlinks["total"]
        for item in links["items"]:
            assert item["source_id"] == page["id"] and item["exists"] is True
        checked += 1
    assert checked == 12
