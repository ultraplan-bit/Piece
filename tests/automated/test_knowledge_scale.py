"""知识图谱大数据量分页不变量：跨页不重不漏、total 稳定、边界语义。

默认跳过（slow 标记）；显式运行：
    uv run pytest tests/automated/test_knowledge_scale.py --run-slow

种子经 scale_fixtures 直插 SQLite，规模与 schema 约束见该模块。全部用例只读，
不触发模型、网络或任务队列。
"""

import pytest

from scale_fixtures import GRAPH_KEYWORD, seed_knowledge_base
from indexing.services import knowledge_service as knowledge
from indexing.services.errors import BusinessError

pytestmark = pytest.mark.slow

# 默认规模：够翻几十页，种子 + 遍历能在 CI 容忍时间内跑完。
SCALE = dict(objects=5000, relations=10000, evidence=5000, hot_object_evidence=250,
             reference_evidence=250, revisions=250, wiki_pages=300, wiki_evidence=250,
             wiki_hot_page_evidence=150, wiki_history=250)


@pytest.fixture(scope="module")
def scale(tmp_path_factory):
    """模块级独立临时库：与 conftest.knowledge_base 同样初始化，但种子只灌一次。"""
    from indexing import settings, database

    tmp_path = tmp_path_factory.mktemp("knowledge-scale")
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
    stats["path"] = tmp_path
    yield stats
    database.close_connection_pool()
    settings._settings = previous


def collect_pages(call, *, limit, key="objects", **kwargs):
    """按 limit 翻到底，返回 (ids, total)。

    同时校验：每页 limit/offset 回显正确、翻页中 total 恒定、offset 单调步进、
    末页不满时必须恰好取尽（不遗漏），满页时不得越过 total。
    """
    ids, offset, total = [], 0, None
    while True:
        page = call(limit=limit, offset=offset, **kwargs)
        assert page["limit"] == limit and page["offset"] == offset, "limit/offset 未正确回显"
        if total is None:
            total = page["total"]
        assert page["total"] == total, "翻页过程中 total 不稳定"
        items = page[key]
        assert len(items) <= limit, "单页超过 limit"
        ids.extend(item["id"] for item in items)
        if len(items) < limit:
            assert offset + len(items) == total, f"末页未取尽：offset={offset} got={len(items)} total={total}"
            break
        offset += limit
    return ids, total


# --------------------------------------------------------------------------- #
# list_objects
# --------------------------------------------------------------------------- #

def test_list_objects_pagination_covers_all_without_duplicates(scale):
    total = knowledge.list_objects(limit=1)["total"]
    assert total == len(scale["object_ids"])

    ids, reported = collect_pages(knowledge.list_objects, limit=100)
    assert reported == total
    assert len(ids) == total, "翻页总数与 total 不一致"
    assert len(set(ids)) == total, "翻页出现重复 ID"
    assert set(ids) == set(scale["object_ids"]), "翻页未覆盖全集"


def test_list_objects_total_stable_across_filters(scale):
    # 过滤条件改变 total，但在该过滤下的所有页保持一致。
    for kwargs in ({}, {"kind": "concept"}, {"status": "active"}):
        ids, total = collect_pages(knowledge.list_objects, limit=100, **kwargs)
        assert len(ids) == total == len(set(ids))


def test_list_objects_divisible_and_not_divisible(scale):
    total = knowledge.list_objects(limit=1)["total"]
    # 恰好整除：limit=100 时最后一页应满页，且 total 为该页 offset+limit。
    ids, _ = collect_pages(knowledge.list_objects, limit=100)
    assert len(ids) == total
    # 不整除：limit=97 时末页不满，遍历仍不重不漏。
    ids, _ = collect_pages(knowledge.list_objects, limit=97)
    assert len(ids) == total == len(set(ids))


def test_list_objects_offset_beyond_total_and_empty_page(scale):
    total = knowledge.list_objects(limit=1)["total"]
    for offset in (total, total + 1, total + 1000):
        page = knowledge.list_objects(limit=100, offset=offset)
        assert page["objects"] == []
        assert page["total"] == total
        assert page["offset"] == offset


def test_list_objects_limit_bounds_and_empty_search(scale):
    # 空结果：与种子正文无任何共享 token 的关键词命中 0 条。
    empty = knowledge.search_objects(query="zzzznonexistentqueryzzzz", limit=100)
    assert empty["objects"] == [] and empty["total"] == 0
    # limit 上限 100 恰好可用；越界与非正数被拒绝。
    assert len(knowledge.list_objects(limit=100)["objects"]) == 100
    for bad in ({"limit": 101}, {"limit": 0}, {"offset": -1}):
        with pytest.raises(BusinessError) as exc:
            knowledge.list_objects(**bad)
        assert exc.value.code == "INVALID_INPUT"


# --------------------------------------------------------------------------- #
# search_objects
# --------------------------------------------------------------------------- #

def test_search_objects_pagination_covers_all(scale):
    total = knowledge.search_objects(query=GRAPH_KEYWORD, limit=1)["total"]
    assert total == len(scale["object_ids"]), "共享关键词应命中全部对象"

    ids, reported = collect_pages(knowledge.search_objects, limit=100, query=GRAPH_KEYWORD)
    assert reported == total
    assert len(ids) == total == len(set(ids))
    assert set(ids) == set(scale["object_ids"])


def test_search_objects_ranking_stable_across_pages(scale):
    # 排序键 (rank, id) 唯一，翻页不应出现重排导致的重复/遗漏。
    ids, _ = collect_pages(knowledge.search_objects, limit=37, query=GRAPH_KEYWORD)
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------- #
# 证据分页：get_record 附属证据、references 反查
# --------------------------------------------------------------------------- #

def test_get_record_evidence_pagination(scale):
    total = knowledge.get_record(kind="object", id=scale["hot_object"], limit=1)["evidence"]["total"]
    assert total > 100, "热点对象应有跨页证据"

    ids, reported = collect_pages(
        lambda **kw: knowledge.get_record(kind="object", id=scale["hot_object"], **kw)["evidence"],
        limit=100, key="items")
    assert reported == total
    assert len(ids) == total == len(set(ids))


def test_references_pagination(scale):
    total = knowledge.references(source_library_id=scale["library_id"],
                                 source_file_id=scale["source_file_id"], limit=1)["total"]
    assert total == scale["reference_evidence"]

    ids, reported = collect_pages(
        knowledge.references, limit=100, key="evidence",
        source_library_id=scale["library_id"], source_file_id=scale["source_file_id"])
    assert reported == total
    assert len(ids) == total == len(set(ids))

    # 不存在的文件 ID 返回空页但 total 为 0。
    missing = knowledge.references(source_library_id=scale["library_id"], source_file_id=999999)
    assert missing["total"] == 0 and missing["evidence"] == []


# --------------------------------------------------------------------------- #
# lint：逐对象检查，翻页以 checked_objects 计数
# --------------------------------------------------------------------------- #

def test_lint_pagination_checks_each_object_once(scale):
    total = knowledge.lint(limit=1)["total_objects"]
    assert total == len(scale["object_ids"])

    offset, checked = 0, 0
    while True:
        report = knowledge.lint(limit=100, offset=offset)
        assert report["total_objects"] == total
        assert report["offset"] == offset and report["limit"] == 100
        assert report["checked_objects"] <= 100
        checked += report["checked_objects"]
        if report["checked_objects"] < 100:
            break
        offset += 100
    assert checked == total, "lint 翻页未覆盖全部对象"


def test_lint_object_filter_is_scoped(scale):
    subset = scale["object_ids"][:7]
    report = knowledge.lint(object_ids=subset, limit=100)
    assert report["total_objects"] == 7 and report["checked_objects"] == 7


# --------------------------------------------------------------------------- #
# graph：有界邻域、确定性、截断
# --------------------------------------------------------------------------- #

def test_graph_neighborhood_is_bounded_and_deterministic(scale):
    root = scale["object_ids"][0]
    first = knowledge.graph(root_id=root, depth=2)
    second = knowledge.graph(root_id=root, depth=2)
    assert first["nodes"] == second["nodes"], "同一邻域查询必须确定"
    assert first["edges"] == second["edges"]
    assert first["root_id"] == root
    assert first["nodes"][0]["id"] == root, "根节点应排在最前"
    assert len(first["nodes"]) <= first["max_nodes"]
    assert len(first["edges"]) <= first["max_edges"]
    # 邻域内节点 id 唯一、边 id 唯一。
    assert len({node["id"] for node in first["nodes"]}) == len(first["nodes"])
    assert len({edge["id"] for edge in first["edges"]}) == len(first["edges"])


def test_graph_truncates_at_max_nodes_and_max_edges(scale):
    root = scale["object_ids"][0]
    limited = knowledge.graph(root_id=root, depth=2, max_nodes=1, max_edges=1)
    assert limited["truncated"] is True
    assert len(limited["nodes"]) == 1 and limited["nodes"][0]["id"] == root
    assert limited["edges"] == []

    capped = knowledge.graph(root_id=root, depth=2, max_nodes=100, max_edges=1)
    assert len(capped["edges"]) <= 1


# --------------------------------------------------------------------------- #
# history
# --------------------------------------------------------------------------- #

def test_history_pagination(scale):
    total = knowledge.history(kind="object", id=scale["history_object"], limit=1)["total"]
    assert total == SCALE["revisions"]

    ids, reported = collect_pages(
        lambda **kw: knowledge.history(kind="object", id=scale["history_object"], **kw),
        limit=100, key="history")
    assert reported == total
    assert len(ids) == total == len(set(ids))
    # ORDER BY id DESC，翻页应按 id 严格递减。
    assert ids == sorted(ids, reverse=True)
