"""知识服务契约回归：原子写入、版本、幂等、证据定位、检索与图的有界行为。

只使用 knowledge_base 临时新库；本地卡片通过 SQL 直接装夹，不触发模型、网络或任务队列。
"""

import copy
from uuid import uuid4

import pytest

from indexing.database import get_db_cursor
from indexing.services import knowledge_service as knowledge
from indexing.services.errors import BusinessError


# --------------------------------------------------------------------------- #
# 装夹与工具
# --------------------------------------------------------------------------- #

def _batch(**parts):
    return {"request_key": f"req-{uuid4().hex}", "reason": "测试批次", **parts}


def _create_objects(*specs):
    """按顺序创建对象并返回 id 列表；spec 可含 ref/title 及可选 summary/body/aliases/status。"""
    objects = []
    for spec in specs:
        item = {"ref": spec["ref"], "kind": spec.get("kind", "concept"), "title": spec["title"]}
        for key in ("summary", "body", "aliases", "status"):
            if key in spec:
                item[key] = spec[key]
        objects.append(item)
    result = knowledge.apply(_batch(objects=objects))
    return [result["refs"][spec["ref"]]["id"] for spec in specs]


def _seed_chunk(kb, text, *, doc_title="资料", heading_path=None, filename=None):
    """SQL 插入文件与卡片，返回 (file_id, chunk_id)。"""
    name = filename or f"资料-{uuid4().hex}.md"
    with get_db_cursor(write=True) as cursor:
        cursor.execute(
            "INSERT INTO files (file_hash, filename, file_path) VALUES (?,?,?)",
            (uuid4().hex, name, str(kb.path / name)),
        )
        file_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO chunks (file_id, doc_title, chunk_text, chunk_index, heading_path) VALUES (?,?,?,?,?)",
            (file_id, doc_title, text, 0, heading_path),
        )
        return file_id, cursor.lastrowid


def _library_id():
    with get_db_cursor() as cursor:
        return cursor.execute("SELECT library_id FROM library_metadata WHERE singleton=1").fetchone()[0]


def _execute(sql, args=()):
    with get_db_cursor(write=True) as cursor:
        cursor.execute(sql, args)


def _scalar(sql, args=()):
    with get_db_cursor() as cursor:
        return cursor.execute(sql, args).fetchone()[0]


def _rows(sql, args=()):
    with get_db_cursor() as cursor:
        return cursor.execute(sql, args).fetchall()


# --------------------------------------------------------------------------- #
# 批次 ref、原子性与幂等
# --------------------------------------------------------------------------- #

def test_library_identity_persists_and_new_database_is_distinct(knowledge_base, tmp_path):
    import sqlite3
    from indexing import database

    original = _library_id()
    database.init_database(knowledge_base.settings.get_db_path())
    assert _library_id() == original
    copied = tmp_path / "copy.db"
    with sqlite3.connect(knowledge_base.settings.get_db_path()) as source, sqlite3.connect(copied) as target:
        source.backup(target)
    database.init_database(copied)
    fresh = tmp_path / "fresh.db"
    database.init_database(fresh)
    with sqlite3.connect(copied) as conn:
        assert conn.execute("SELECT library_id FROM library_metadata").fetchone()[0] == original
        conn.execute("UPDATE library_metadata SET library_id='invalid'")
    with pytest.raises(RuntimeError, match="身份无效"):
        database.init_database(copied)
    with sqlite3.connect(fresh) as conn:
        assert conn.execute("SELECT library_id FROM library_metadata").fetchone()[0] != original


def test_batch_refs_and_evidence_on_new_relation(knowledge_base):
    data = {
        **_batch(
            reason="建关系与证据",
            objects=[{"ref": "a", "kind": "concept", "title": "概念甲"},
                     {"ref": "b", "kind": "concept", "title": "概念乙"}],
            relations=[{"ref": "rel", "source": {"ref": "a"}, "predicate": "supports",
                        "target": {"ref": "b"}, "description": "甲支持乙", "basis": "explicit"}],
            evidence=[{"relation": {"ref": "rel"}, "source_kind": "user", "quote": "用户陈述支持关系"}],
        ),
    }
    result = knowledge.apply(data)
    aid = result["refs"]["a"]["id"]
    bid = result["refs"]["b"]["id"]
    rid = result["refs"]["rel"]["id"]
    assert result["counts"] == {"created": 4, "updated": 0, "reused": 0}
    relation = knowledge.get_record(kind="relation", id=rid)["record"]
    assert relation["source_id"] == aid and relation["target_id"] == bid
    owner = knowledge.get_record(kind="relation", id=rid)
    assert owner["has_evidence"] is True
    assert owner["evidence"]["items"][0]["location_status"] == "unverified"
    assert knowledge.get_record(kind="object", id=aid)["has_evidence"] is False


def test_batch_ref_must_be_globally_unique(knowledge_base):
    with pytest.raises(BusinessError) as exc:
        knowledge.apply(_batch(objects=[{"ref": "dup", "kind": "concept", "title": "甲"},
                                        {"ref": "dup", "kind": "concept", "title": "乙"}]))
    assert exc.value.code == "INVALID_REFERENCE"
    assert knowledge.list_objects()["total"] == 0


def test_failed_batch_rolls_back_everything(knowledge_base):
    data = _batch(
        request_key="rollback-key",
        objects=[{"ref": "a", "kind": "concept", "title": "不应存在的对象"}],
        links=[{"source": {"ref": "a"}, "target": {"ref": "a"}}],
    )
    with pytest.raises(BusinessError) as exc:
        knowledge.apply(data)
    assert exc.value.code == "INVALID_REFERENCE"
    assert knowledge.list_objects()["total"] == 0
    assert knowledge.search_objects(query="不应存在")["total"] == 0
    with pytest.raises(BusinessError) as exc2:
        knowledge.request_result("rollback-key")
    assert exc2.value.code == "NOT_FOUND"


def test_dry_run_does_not_consume_request_key(knowledge_base):
    payload = {"request_key": "dry-key", "reason": "预检",
               "objects": [{"ref": "a", "kind": "concept", "title": "预检对象"}]}
    preview = knowledge.apply({**payload, "dry_run": True})
    assert preview["dry_run"] is True and preview["committed"] is False
    assert preview["refs"]["a"]["id"]
    assert knowledge.list_objects()["total"] == 0
    with pytest.raises(BusinessError):
        knowledge.request_result("dry-key")

    committed = knowledge.apply({**payload, "dry_run": False})
    assert committed["committed"] is True
    assert knowledge.list_objects()["total"] == 1
    assert knowledge.request_result("dry-key")["counts"]["created"] == 1


def test_idempotent_retry_after_revision_advanced(knowledge_base):
    [oid] = _create_objects({"ref": "a", "title": "原题"})
    update = {"request_key": "idem-key", "reason": "修订",
              "objects": [{"id": oid, "expected_revision": 1, "title": "新题"}]}
    first = knowledge.apply(copy.deepcopy(update))
    assert first["objects"][0] == {"id": oid, "action": "updated", "revision": 2, "submitted_fields": ["title"]}

    # 同一 request_key + 同一内容重试：应命中幂等记录，而不是按旧 revision 再次校验。
    retry = knowledge.apply(copy.deepcopy(update))
    assert retry == first
    record = knowledge.get_record(kind="object", id=oid)["record"]
    assert record["title"] == "新题" and record["revision"] == 2
    assert knowledge.history(kind="object", id=oid)["total"] == 2


def test_same_request_key_rejects_different_payload(knowledge_base):
    knowledge.apply(_batch(request_key="same-key",
                           objects=[{"ref": "a", "kind": "concept", "title": "甲"}]))
    with pytest.raises(BusinessError) as exc:
        knowledge.apply(_batch(request_key="same-key",
                               objects=[{"ref": "a", "kind": "concept", "title": "乙"}]))
    assert exc.value.code == "REQUEST_CONFLICT"


# --------------------------------------------------------------------------- #
# 更新语义与乐观并发
# --------------------------------------------------------------------------- #

def test_update_clears_with_empty_rejects_null_and_unknown(knowledge_base):
    [oid] = _create_objects({"ref": "a", "title": "标题", "summary": "摘要",
                             "body": "正文", "aliases": ["别名"]})
    result = knowledge.apply(_batch(objects=[{"id": oid, "expected_revision": 1,
                                              "summary": "", "aliases": [], "body": ""}]))
    record = knowledge.get_record(kind="object", id=oid)["record"]
    assert result["objects"][0]["revision"] == 2
    assert result["objects"][0]["submitted_fields"] == ["aliases", "body", "summary"]
    assert record["summary"] == "" and record["aliases"] == [] and record["body"] == ""
    assert record["title"] == "标题" and record["kind"] == "concept"
    assert knowledge.search_objects(query="别名")["total"] == 0

    with pytest.raises(BusinessError) as exc:
        knowledge.apply(_batch(objects=[{"id": oid, "expected_revision": 2, "title": None}]))
    assert exc.value.code == "INVALID_INPUT"

    with pytest.raises(BusinessError) as exc2:
        knowledge.apply(_batch(objects=[{"id": oid, "expected_revision": 2, "bogus": 1}]))
    assert exc2.value.code == "INVALID_INPUT"

    with pytest.raises(BusinessError) as exc3:
        knowledge.apply(_batch(objects=[{"id": oid, "expected_revision": 2, "unknown_top": 1}]))
    assert exc3.value.code == "INVALID_INPUT"


def test_optimistic_version_conflict(knowledge_base):
    [oid] = _create_objects({"ref": "a", "title": "原题"})
    knowledge.apply(_batch(objects=[{"id": oid, "expected_revision": 1, "title": "v2"}]))
    with pytest.raises(BusinessError) as exc:
        knowledge.apply(_batch(objects=[{"id": oid, "expected_revision": 1, "title": "v3"}]))
    assert exc.value.code == "VERSION_CONFLICT"
    assert exc.value.data["current_revision"] == 2
    assert exc.value.data["expected_revision"] == 1


# --------------------------------------------------------------------------- #
# 语义关系的对称、方向、条件与唯一性
# --------------------------------------------------------------------------- #

def test_symmetric_relation_reuses_reversed_duplicate(knowledge_base):
    [a, b] = _create_objects({"ref": "a", "title": "甲"}, {"ref": "b", "title": "乙"})
    first = knowledge.apply(_batch(relations=[
        {"ref": "r1", "source": {"id": a}, "predicate": "related_to", "target": {"id": b},
         "description": "互相关联", "basis": "explicit", "qualifier": "同一条"}]))
    r1 = first["refs"]["r1"]["id"]

    second = knowledge.apply(_batch(relations=[
        {"ref": "r2", "source": {"id": b}, "predicate": "related_to", "target": {"id": a},
         "description": "互相关联", "basis": "explicit", "qualifier": "同一条"}]))
    assert second["relations"][0]["action"] == "reused"
    assert second["relations"][0]["id"] == r1
    assert second["refs"]["r2"]["id"] == r1
    record = knowledge.get_record(kind="relation", id=r1)["record"]
    assert record["symmetric"] is True
    titles = {a: "甲", b: "乙"}
    assert record["display"] == f"{titles[record['source_id']]} —related_to— {titles[record['target_id']]}"
    assert "→" not in record["display"]
    graph = knowledge.graph(root_id=a, edge_types=["relation"])
    assert graph["edges"][0]["display"] == record["display"]
    assert second["relations"][0]["submitted_fields"] == ["basis", "description", "predicate", "qualifier", "source", "target"]


def test_directed_predicate_keeps_direction(knowledge_base):
    [a, b] = _create_objects({"ref": "a", "title": "甲"}, {"ref": "b", "title": "乙"})
    result = knowledge.apply(_batch(relations=[
        {"ref": "r1", "source": {"id": a}, "predicate": "is_a", "target": {"id": b},
         "description": "甲是乙", "basis": "explicit"},
        {"ref": "r2", "source": {"id": b}, "predicate": "is_a", "target": {"id": a},
         "description": "乙是甲", "basis": "explicit"}]))
    r1 = result["refs"]["r1"]["id"]
    r2 = result["refs"]["r2"]["id"]
    assert r1 != r2
    assert knowledge.get_record(kind="relation", id=r1)["record"]["source_id"] == a
    assert knowledge.get_record(kind="relation", id=r2)["record"]["source_id"] == b


def test_relation_display_and_submitted_fields_do_not_infer_structure(knowledge_base):
    a, b = _create_objects({"ref": "a", "title": "增量维护"}, {"ref": "b", "title": "稳定身份"})
    created = knowledge.apply(_batch(relations=[{
        "ref": "r", "source": {"id": a}, "predicate": "part_of", "target": {"id": b},
        "description": "原描述", "basis": "explicit"}]))
    rid = created["refs"]["r"]["id"]
    update = _batch(relations=[{"id": rid, "expected_revision": 1, "description": "增量维护依赖稳定身份"}])
    result = knowledge.apply(update)
    assert result["relations"][0]["submitted_fields"] == ["description"]
    assert knowledge.request_result(update["request_key"]) == result
    record = knowledge.get_record(kind="relation", id=rid)["record"]
    assert (record["source_id"], record["predicate"], record["target_id"], record["revision"]) == (a, "part_of", b, 2)
    assert record["display"] == "增量维护 —part_of→ 稳定身份"
    assert knowledge.graph(root_id=a, edge_types=["relation"])["edges"][0]["display"] == record["display"]

    # 相同值仍是显式提交字段；名称展示只在读时生成，不进入请求缓存或关系历史。
    same = knowledge.apply(_batch(relations=[{"id": rid, "expected_revision": 2, "description": record["description"]}]))
    assert same["relations"][0]["submitted_fields"] == ["description"]
    assert "display" not in knowledge.history(kind="relation", id=rid)["history"][0]["after"]
    cached = _scalar("SELECT result_json FROM knowledge_requests WHERE request_key=?", (update["request_key"],))
    assert "增量维护" not in cached and "稳定身份" not in cached and "display" not in cached
    knowledge.apply(_batch(objects=[{"id": a, "expected_revision": 1, "title": "增量修订"}]))
    assert knowledge.get_record(kind="relation", id=rid)["record"]["display"] == "增量修订 —part_of→ 稳定身份"


def test_relation_evidence_count_is_not_inherited_or_limited_to_supports(knowledge_base):
    a, b = _create_objects({"ref": "a", "title": "甲"}, {"ref": "b", "title": "乙"})
    result = knowledge.apply(_batch(
        relations=[{"ref": "r", "source": {"id": a}, "predicate": "depends_on", "target": {"id": b},
                    "description": "甲需要乙", "basis": "user_statement"}],
        evidence=[{"object": {"id": a}, "source_kind": "user", "quote": "甲的说明"}]))
    rid = result["refs"]["r"]["id"]
    assert knowledge.graph(root_id=a, edge_types=["relation"])["edges"][0]["evidence_count"] == 0
    knowledge.apply(_batch(evidence=[
        {"relation": {"id": rid}, "source_kind": "user", "stance": stance, "quote": f"{stance} 的说明"}
        for stance in ("supports", "contradicts", "context")]))
    edge = knowledge.graph(root_id=a, edge_types=["relation"])["edges"][0]
    assert edge["evidence_count"] == 3 and edge["revision"] == 1
    assert knowledge.get_record(kind="object", id=a)["record"]["revision"] == 1


def test_relation_qualifier_variants_and_update_uniqueness(knowledge_base):
    [a, b] = _create_objects({"ref": "a", "title": "甲"}, {"ref": "b", "title": "乙"})
    result = knowledge.apply(_batch(relations=[
        {"ref": "r1", "source": {"id": a}, "predicate": "related_to", "target": {"id": b},
         "description": "一", "basis": "explicit", "qualifier": "条件一"},
        {"ref": "r2", "source": {"id": a}, "predicate": "related_to", "target": {"id": b},
         "description": "二", "basis": "explicit", "qualifier": "条件二"}]))
    r1 = result["refs"]["r1"]["id"]
    r2 = result["refs"]["r2"]["id"]
    assert r1 != r2

    # 自更新不应与自身冲突。
    knowledge.apply(_batch(relations=[{"id": r1, "expected_revision": 1, "description": "一改"}]))
    assert knowledge.get_record(kind="relation", id=r1)["record"]["description"] == "一改"

    # 更新到与另一条相同的三元组与适用条件 → 冲突。
    with pytest.raises(BusinessError) as exc:
        knowledge.apply(_batch(relations=[{"id": r1, "expected_revision": 2, "qualifier": "条件二"}]))
    assert exc.value.code == "KNOWLEDGE_CONFLICT"
    assert exc.value.data["id"] == r2


# --------------------------------------------------------------------------- #
# 删除：影响范围、确认与在线历史/请求不留正文或引用
# --------------------------------------------------------------------------- #

def test_delete_impact_conflict_and_history_cleanup(knowledge_base):
    body = "历史正文SECRETBODY"
    [a, b] = _create_objects({"ref": "a", "title": "对象甲", "body": body},
                             {"ref": "b", "title": "对象乙"})
    preview = knowledge.delete({"kind": "object", "id": a, "expected_revision": 1, "dry_run": True})
    assert preview["dry_run"] is True and preview["committed"] is False
    assert preview["counts"]["history"] == 1
    token = preview["impact_token"]

    # 预览后新增依赖 → token 过期。
    knowledge.apply(_batch(relations=[{"ref": "r", "source": {"id": a}, "predicate": "supports",
                                      "target": {"id": b}, "description": "d", "basis": "explicit"}]))
    with pytest.raises(BusinessError) as exc:
        knowledge.delete({"kind": "object", "id": a, "expected_revision": 1, "request_key": "del-old",
                          "impact_token": token, "dry_run": False, "confirmed": True})
    assert exc.value.code == "IMPACT_CONFLICT"

    fresh = knowledge.delete({"kind": "object", "id": a, "expected_revision": 1, "dry_run": True})
    with pytest.raises(BusinessError) as exc2:
        knowledge.delete({"kind": "object", "id": a, "expected_revision": 1, "request_key": "del-no-confirm",
                          "impact_token": fresh["impact_token"], "dry_run": False})
    assert exc2.value.code == "CONFIRMATION_REQUIRED"

    done = knowledge.delete({"kind": "object", "id": a, "expected_revision": 1, "request_key": "del-final",
                             "impact_token": fresh["impact_token"], "dry_run": False, "confirmed": True})
    assert done["committed"] is True and done["deletes_files"] is False

    with pytest.raises(BusinessError) as exc3:
        knowledge.get_record(kind="object", id=a)
    assert exc3.value.code == "NOT_FOUND"
    with pytest.raises(BusinessError) as exc4:
        knowledge.history(kind="object", id=a)
    assert exc4.value.code == "NOT_FOUND"

    # 在线历史已随对象级联清理；请求结果从不保存正文。
    assert _scalar("SELECT COUNT(*) FROM knowledge_revisions WHERE object_id=?", (a,)) == 0
    assert not any(body in row[0] for row in _rows("SELECT result_json FROM knowledge_requests"))


def test_delete_evidence_leaves_no_quote_in_requests(knowledge_base):
    quote = "证据引文SECRETQUOTE"
    fid, cid = _seed_chunk(knowledge_base, quote + " 的上下文")
    [oid] = _create_objects({"ref": "a", "title": "对象"})
    created = knowledge.apply(_batch(evidence=[
        {"object": {"id": oid}, "source_kind": "piece", "source_library_id": _library_id(),
         "source_file_id": fid, "source_chunk_id": cid, "quote": quote}]))
    eid = created["evidence"][0]["id"]

    preview = knowledge.delete({"kind": "evidence", "id": eid, "dry_run": True})
    done = knowledge.delete({"kind": "evidence", "id": eid, "request_key": "del-ev",
                             "impact_token": preview["impact_token"], "dry_run": False, "confirmed": True})
    assert done["committed"] is True
    with pytest.raises(BusinessError) as exc:
        knowledge.get_record(kind="evidence", id=eid)
    assert exc.value.code == "NOT_FOUND"

    assert _scalar("SELECT COUNT(*) FROM knowledge_evidence WHERE id=?", (eid,)) == 0
    assert not any(quote in row[0] for row in _rows("SELECT result_json FROM knowledge_requests"))


# --------------------------------------------------------------------------- #
# 知识检索：中文 FTS、特殊字符、更新/删除无幽灵
# --------------------------------------------------------------------------- #

def test_search_chinese_refresh_and_special_characters(knowledge_base):
    [oid] = _create_objects({"ref": "a", "title": "机器学习入门", "body": "监督学习与神经网络"})
    assert knowledge.search_objects(query="机器")["total"] == 1

    knowledge.apply(_batch(objects=[{"id": oid, "expected_revision": 1,
                                     "title": "深度学习", "body": "卷积网络"}]))
    # 旧标题独有的分词（机器）必须随更新消失，新内容可被检索。
    assert knowledge.search_objects(query="机器")["total"] == 0
    assert knowledge.search_objects(query="深度")["total"] == 1
    assert knowledge.search_objects(query="卷积")["total"] == 1

    [b] = _create_objects({"ref": "b", "title": "测试C++ 100%_完成"})
    # 特殊字符不得抛错，且仍能命中普通词。
    assert knowledge.search_objects(query='C++ (测试) %_ "引号" *')["total"] >= 1
    # % 按普通字符处理，不走通配。
    found = knowledge.search_objects(query="%")
    assert found["total"] == 1 and found["objects"][0]["id"] == b


def test_delete_removes_fts_ghost(knowledge_base):
    [oid] = _create_objects({"ref": "a", "title": "幽灵对象词"})
    assert knowledge.search_objects(query="幽灵")["total"] == 1
    preview = knowledge.delete({"kind": "object", "id": oid, "expected_revision": 1, "dry_run": True})
    knowledge.delete({"kind": "object", "id": oid, "expected_revision": 1, "request_key": "ghost-del",
                      "impact_token": preview["impact_token"], "dry_run": False, "confirmed": True})
    assert knowledge.search_objects(query="幽灵")["total"] == 0
    assert _scalar("SELECT COUNT(*) FROM knowledge_fts WHERE object_id=?", (oid,)) == 0


# --------------------------------------------------------------------------- #
# 局部图：深度、循环、预算、端点完整与稳定排序
# --------------------------------------------------------------------------- #

def test_graph_depth_cycle_endpoints_and_stability(knowledge_base):
    [a, b, c, d] = _create_objects({"ref": "a", "title": "甲"}, {"ref": "b", "title": "乙"},
                                   {"ref": "c", "title": "丙"}, {"ref": "d", "title": "丁"})
    knowledge.apply(_batch(links=[
        {"source": {"id": a}, "target": {"id": b}},
        {"source": {"id": b}, "target": {"id": a}},  # 双向环
        {"source": {"id": b}, "target": {"id": c}},
        {"source": {"id": c}, "target": {"id": d}}]))

    one = knowledge.graph(root_id=a, depth=1)
    ids1 = {node["id"] for node in one["nodes"]}
    assert a in ids1 and b in ids1 and c not in ids1 and d not in ids1

    two = knowledge.graph(root_id=a, depth=2)
    assert two["nodes"][0]["id"] == a  # 根始终在前
    node_ids = {node["id"] for node in two["nodes"]}
    assert c in node_ids and d not in node_ids  # depth=2 是两跳，丁需三跳
    for edge in two["edges"]:
        assert edge["source_id"] in node_ids and edge["target_id"] in node_ids

    again = knowledge.graph(root_id=a, depth=2)
    assert [n["id"] for n in again["nodes"]] == [n["id"] for n in two["nodes"]]
    assert [e["id"] for e in again["edges"]] == [e["id"] for e in two["edges"]]


def test_graph_budget_truncates_without_breaking_endpoints(knowledge_base):
    specs = [{"ref": f"n{i}", "title": f"节点{i}"} for i in range(6)]
    ids = _create_objects(*specs)
    root = ids[0]
    knowledge.apply(_batch(links=[{"source": {"id": root}, "target": {"id": node}} for node in ids[1:]]))

    result = knowledge.graph(root_id=root, depth=1, max_nodes=2, max_edges=1)
    assert result["truncated"] is True
    assert len(result["nodes"]) <= 2 and len(result["edges"]) <= 1
    node_ids = {node["id"] for node in result["nodes"]}
    assert root in node_ids
    for edge in result["edges"]:
        assert edge["source_id"] in node_ids and edge["target_id"] in node_ids


# --------------------------------------------------------------------------- #
# 证据来源：本库校验、跨库、外部/用户与定位状态
# --------------------------------------------------------------------------- #

def test_piece_evidence_quote_hash_and_file_ownership(knowledge_base):
    text = "支持关系成立的原始段落。"
    fid, cid = _seed_chunk(knowledge_base, text)
    [oid] = _create_objects({"ref": "a", "title": "对象甲"})
    lib = _library_id()

    created = knowledge.apply(_batch(evidence=[
        {"object": {"id": oid}, "source_kind": "piece", "source_library_id": lib,
         "source_file_id": fid, "source_chunk_id": cid, "quote": "支持关系",
         "expected_content_hash": knowledge.content_hash(text)}]))
    eid = created["evidence"][0]["id"]
    record = knowledge.get_record(kind="evidence", id=eid)["record"]
    assert record["location_status"] == "current"
    assert record["content_hash"] == knowledge.content_hash(text)
    assert record["source_title"] == "资料" and record["source_file_id"] == fid

    # 引文不在当前正文中。
    with pytest.raises(BusinessError) as exc:
        knowledge.apply(_batch(evidence=[
            {"object": {"id": oid}, "source_kind": "piece", "source_library_id": lib,
             "source_file_id": fid, "source_chunk_id": cid, "quote": "不存在的引文"}]))
    assert exc.value.code == "QUOTE_MISMATCH"

    # 过期正文哈希。
    with pytest.raises(BusinessError) as exc2:
        knowledge.apply(_batch(evidence=[
            {"object": {"id": oid}, "source_kind": "piece", "source_library_id": lib,
             "source_file_id": fid, "source_chunk_id": cid, "quote": "支持关系",
             "expected_content_hash": "0" * 64}]))
    assert exc2.value.code == "SOURCE_CHANGED"

    # 卡片不属于指定文件。
    other_fid, _ = _seed_chunk(knowledge_base, "另一份资料")
    with pytest.raises(BusinessError) as exc3:
        knowledge.apply(_batch(evidence=[
            {"object": {"id": oid}, "source_kind": "piece", "source_library_id": lib,
             "source_file_id": other_fid, "source_chunk_id": cid, "quote": "支持关系"}]))
    assert exc3.value.code == "INVALID_SOURCE"


def test_piece_evidence_computes_page_number(knowledge_base):
    fid, cid = _seed_chunk(knowledge_base, "第3页的正文内容", heading_path="文档 / 第3页 / 方法")
    [oid] = _create_objects({"ref": "a", "title": "对象"})
    created = knowledge.apply(_batch(evidence=[
        {"object": {"id": oid}, "source_kind": "piece", "source_library_id": _library_id(),
         "source_file_id": fid, "source_chunk_id": cid, "quote": "正文内容"}]))
    record = knowledge.get_record(kind="evidence", id=created["evidence"][0]["id"])["record"]
    assert record["page_number"] == 3
    assert record["heading_path"] == "文档 / 第3页 / 方法"


def test_cross_library_external_and_user_sources(knowledge_base):
    fid, cid = _seed_chunk(knowledge_base, "本地资料")
    [oid] = _create_objects({"ref": "a", "title": "对象"})
    other = str(uuid4())

    cross = knowledge.apply(_batch(evidence=[
        {"object": {"id": oid}, "source_kind": "piece", "source_library_id": other,
         "source_file_id": fid, "source_chunk_id": cid, "source_title": "未接入库资料", "quote": "别库引用"}]))
    assert knowledge.get_record(kind="evidence", id=cross["evidence"][0]["id"])["record"]["location_status"] == "unresolved"

    with pytest.raises(BusinessError) as exc:
        knowledge.apply(_batch(evidence=[
            {"object": {"id": oid}, "source_kind": "piece", "source_library_id": other,
             "source_file_id": fid, "source_chunk_id": cid, "quote": "x"}]))
    assert exc.value.code == "INVALID_SOURCE"

    with pytest.raises(BusinessError) as exc2:
        knowledge.apply(_batch(evidence=[
            {"object": {"id": oid}, "source_kind": "piece", "source_library_id": other,
             "source_file_id": fid, "source_chunk_id": cid, "source_title": "t", "quote": "x",
             "expected_content_hash": "0" * 64}]))
    assert exc2.value.code == "INVALID_SOURCE"

    external = knowledge.apply(_batch(evidence=[
        {"object": {"id": oid}, "source_kind": "external", "source_title": "网页标题",
         "source_url": "https://example.com/a?b=1", "quote": "网页引文"}]))
    assert knowledge.get_record(kind="evidence", id=external["evidence"][0]["id"])["record"]["location_status"] == "unverified"

    for bad in ("ftp://example.com/a", "http://user:pw@example.com/a",
                "javascript:alert(1)", "https://example.com/a\\b"):
        with pytest.raises(BusinessError) as exc3:
            knowledge.apply(_batch(evidence=[
                {"object": {"id": oid}, "source_kind": "external", "source_title": "网页",
                 "source_url": bad, "quote": "q"}]))
        assert exc3.value.code == "INVALID_SOURCE"

    with pytest.raises(BusinessError) as exc4:
        knowledge.apply(_batch(evidence=[
            {"object": {"id": oid}, "source_kind": "external", "source_title": "网页", "quote": "q"}]))
    assert exc4.value.code == "INVALID_INPUT"

    user = knowledge.apply(_batch(evidence=[{"object": {"id": oid}, "source_kind": "user", "quote": "用户陈述"}]))
    user_record = knowledge.get_record(kind="evidence", id=user["evidence"][0]["id"])["record"]
    assert user_record["location_status"] == "unverified" and user_record["source_title"] == "用户陈述"

    with pytest.raises(BusinessError) as exc5:
        knowledge.apply(_batch(evidence=[
            {"object": {"id": oid}, "source_kind": "user", "source_url": "https://example.com", "quote": "q"}]))
    assert exc5.value.code == "INVALID_INPUT"


def test_evidence_location_changes_on_edit_delete_and_reindex(knowledge_base):
    fid, cid = _seed_chunk(knowledge_base, "原始正文内容")
    [oid] = _create_objects({"ref": "a", "title": "知识对象", "body": "保留正文"})
    created = knowledge.apply(_batch(evidence=[
        {"object": {"id": oid}, "source_kind": "piece", "source_library_id": _library_id(),
         "source_file_id": fid, "source_chunk_id": cid, "quote": "原始正文"}]))
    eid = created["evidence"][0]["id"]
    assert knowledge.get_record(kind="evidence", id=eid)["record"]["location_status"] == "current"

    _execute("UPDATE chunks SET chunk_text=? WHERE id=?", ("修改后的正文内容", cid))
    changed = knowledge.get_record(kind="evidence", id=eid)["record"]
    assert changed["location_status"] == "changed"
    assert changed["quote"] == "原始正文"  # 引用快照保留

    # 重索引：旧卡片消失，同文件出现新卡片；旧证据不得被偷偷绑定。
    _execute("DELETE FROM chunks WHERE id=?", (cid,))
    _execute("INSERT INTO chunks (file_id, doc_title, chunk_text, chunk_index) VALUES (?,?,?,?)",
             (fid, "资料", "重新索引的正文", 0))
    assert knowledge.get_record(kind="evidence", id=eid)["record"]["location_status"] == "missing"

    # 资料层变化不影响知识对象与其证据快照。
    assert knowledge.get_record(kind="object", id=oid)["record"]["body"] == "保留正文"
    _execute("DELETE FROM files WHERE id=?", (fid,))
    assert knowledge.get_record(kind="evidence", id=eid)["record"]["location_status"] == "missing"


def test_references_isolates_source_library(knowledge_base):
    fid, cid = _seed_chunk(knowledge_base, "本地引用资料")
    [oid] = _create_objects({"ref": "a", "title": "对象"})
    lib = _library_id()
    other = str(uuid4())
    knowledge.apply(_batch(evidence=[
        {"object": {"id": oid}, "source_kind": "piece", "source_library_id": lib,
         "source_file_id": fid, "source_chunk_id": cid, "quote": "本地引用"},
        {"object": {"id": oid}, "source_kind": "piece", "source_library_id": other,
         "source_file_id": fid, "source_chunk_id": cid, "source_title": "别库同 ID", "quote": "别库引用"}]))

    local = knowledge.references(source_library_id=lib, source_file_id=fid)
    assert local["total"] == 1
    assert local["evidence"][0]["owner"] == {"id": oid, "kind": "concept", "title": "对象",
                                             "summary": "", "status": "active", "revision": 1}
    assert local["evidence"][0]["owner_kind"] == "object"

    foreign = knowledge.references(source_library_id=other, source_file_id=fid)
    assert foreign["total"] == 1
    assert foreign["evidence"][0]["quote"] == "别库引用"


# --------------------------------------------------------------------------- #
# 结构检查
# --------------------------------------------------------------------------- #

def test_lint_reports_structural_issues(knowledge_base):
    [iso, amb1, amb2, owner, target, rel_target] = _create_objects(
        {"ref": "iso", "title": "孤立对象"},
        {"ref": "amb1", "title": "同名对象"},
        {"ref": "amb2", "title": "同名对象"},
        {"ref": "o", "title": "有关系的对象"},
        {"ref": "t", "title": "目标对象"},
        {"ref": "rt", "title": "关系目标"})

    knowledge.apply(_batch(relations=[
        {"ref": "rel", "source": {"id": owner}, "predicate": "supports", "target": {"id": rel_target},
         "description": "断言", "basis": "inference"}]))

    missing_uuid = str(uuid4())
    knowledge.apply(_batch(objects=[{"id": target, "expected_revision": 1,
                                     "body": f"参见 piece://knowledge/{owner} 与 piece://knowledge/{missing_uuid}"}]))
    knowledge.apply(_batch(objects=[{"id": amb1, "expected_revision": 1, "body": "没有内部链接"}],
                           links=[{"source": {"id": amb1}, "target": {"id": amb2}}]))

    report = knowledge.lint()
    codes = {issue["code"] for issue in report["issues"]}
    assert {"ISOLATED_OBJECT", "RELATION_WITHOUT_EVIDENCE", "BROKEN_BODY_LINK",
            "UNREGISTERED_BODY_LINK", "LINK_NOT_IN_BODY", "AMBIGUOUS_NAME"} <= codes
    assert report["read_only"] is True and report["semantic_review"] is False
    assert report["truncated"] is False


def test_lint_normalizes_uppercase_internal_uuid(knowledge_base):
    [source, target] = _create_objects({"ref": "s", "title": "来源"}, {"ref": "t", "title": "目标"})
    knowledge.apply(_batch(
        objects=[{"id": source, "expected_revision": 1, "body": f"[目标](piece://knowledge/{target.upper()})"}],
        links=[{"source": {"id": source}, "target": {"id": target}}]))
    assert knowledge.lint(object_ids=[source])["issues"] == []


def test_lint_truncated_links_do_not_report_registered_target_missing(knowledge_base):
    [source, target] = _create_objects({"ref": "s", "title": "来源"}, {"ref": "t", "title": "目标"})
    knowledge.apply(_batch(objects=[{
        "id": source, "expected_revision": 1, "body": f"[目标](piece://knowledge/{target})"}]))
    # 300 条入链先于正文出链，保证读取到的附属列表恰好截掉它。
    from uuid import UUID
    with get_db_cursor(write=True) as cursor:
        for index in range(1, 301):
            incoming = str(UUID(int=index))
            cursor.execute("""INSERT INTO knowledge_objects
                (id,kind,title,title_norm,status,revision) VALUES (?,'entity',?,?,'active',1)""",
                (incoming, f"入链{index}", f"入链{index}"))
            cursor.execute("INSERT INTO knowledge_links(id,source_id,target_id) VALUES (?,?,?)",
                           (incoming, incoming, source))
        cursor.execute("INSERT INTO knowledge_links(id,source_id,target_id) VALUES (?,?,?)",
                       (str(UUID(int=2**128 - 1)), source, target))
    report = knowledge.lint(object_ids=[source])
    assert report["truncated"]
    assert report["issues"] == []
    graph = knowledge.graph(root_id=source)
    assert graph["truncated"] and len(graph["nodes"]) <= 100 and len(graph["edges"]) <= 300
    assert graph == knowledge.graph(root_id=source)
    ids = {node["id"] for node in graph["nodes"]}
    assert all(edge["source_id"] in ids and edge["target_id"] in ids for edge in graph["edges"])


def test_lint_object_filter_is_scoped(knowledge_base):
    [iso, other] = _create_objects({"ref": "iso", "title": "孤立"}, {"ref": "o", "title": "另一个孤立"})
    filtered = knowledge.lint(object_ids=[iso])
    assert filtered["checked_objects"] == 1
    assert {issue["object_id"] for issue in filtered["issues"] if "object_id" in issue} == {iso}
    assert filtered["truncated"] is False
    assert other not in {issue.get("object_id") for issue in filtered["issues"]}


# --------------------------------------------------------------------------- #
# 引文提取：精确子串、定位宽松、直接可提交
# --------------------------------------------------------------------------- #

def test_extract_chunk_locates_exact_quote_and_roundtrips(knowledge_base):
    from indexing.services import maintenance_service as maintenance

    text = "第一行普通文本\n第二行 $ V_{DD} $ 公式\n第三行 <td>表格</td>\n第四行结论"
    fid, cid = _seed_chunk(knowledge_base, text, heading_path="文档 / 第3页 / 方法")

    by_lines = maintenance.extract_chunk(cid, lines="2-3")
    assert by_lines["match_count"] == 1 and by_lines["total_lines"] == 4
    assert by_lines["matches"][0]["quote"] == "第二行 $ V_{DD} $ 公式\n第三行 <td>表格</td>"
    assert by_lines["page_number"] == 3 and by_lines["file_id"] == fid

    by_grep = maintenance.extract_chunk(cid, grep="V_{DD}")
    assert by_grep["matches"][0]["quote"] == "第二行 $ V_{DD} $ 公式"

    # 相邻匹配保持独立；只有 context 重叠才合并。
    assert maintenance.extract_chunk(cid, grep="行")["match_count"] == 4
    merged = maintenance.extract_chunk(cid, grep="第三行", context=1)
    assert merged["match_count"] == 1
    assert (merged["matches"][0]["line_start"], merged["matches"][0]["line_end"]) == (2, 4)
    assert maintenance.extract_chunk(cid, grep=r"^第[一二]行", regex=True)["match_count"] == 2
    truncated = maintenance.extract_chunk(cid, grep="行", max_matches=2)
    assert truncated["match_count"] == 2 and truncated["truncated"] is True
    assert maintenance.extract_chunk(cid, grep="不存在")["match_count"] == 0

    with pytest.raises(BusinessError) as exc:
        maintenance.extract_chunk(cid, lines="99")
    assert exc.value.code == "NOT_FOUND" and "4 行" in str(exc.value)
    with pytest.raises(BusinessError) as exc2:
        maintenance.extract_chunk(cid, lines="abc")
    assert exc2.value.code == "INVALID_INPUT"

    # 切出的 evidence 直接通过 apply 的逐字校验。
    [oid] = _create_objects({"ref": "a", "title": "对象"})
    applied = knowledge.apply(_batch(evidence=[{**by_grep["matches"][0]["evidence"], "object": {"id": oid}}]))
    record = knowledge.get_record(kind="evidence", id=applied["evidence"][0]["id"])["record"]
    assert record["location_status"] == "current"


def test_extract_chunk_requires_exactly_one_locator(knowledge_base):
    from indexing import knowledge_models as km
    from pydantic import ValidationError

    _, cid = _seed_chunk(knowledge_base, "正文")
    for bad in ({}, {"lines": "1", "grep": "x"}, {"lines": "1", "context": 2}):
        with pytest.raises(ValidationError):
            km.ChunkExtractInput(chunk_id=cid, **bad)


# --------------------------------------------------------------------------- #
# 报错定位：批次内第几条记录、哪个 ref/id
# --------------------------------------------------------------------------- #

def test_apply_errors_carry_record_locator(knowledge_base):
    fid, cid = _seed_chunk(knowledge_base, "正文内容")
    [oid] = _create_objects({"ref": "a", "title": "对象"})

    with pytest.raises(BusinessError) as exc:
        knowledge.apply(_batch(evidence=[
            {"object": {"id": oid}, "source_kind": "piece", "source_library_id": _library_id(),
             "source_file_id": fid, "source_chunk_id": cid, "quote": "不存在的引文"}]))
    assert exc.value.code == "QUOTE_MISMATCH"
    assert exc.value.data["section"] == "evidence" and exc.value.data["index"] == 0
    assert exc.value.data["object"] == oid

    with pytest.raises(BusinessError) as exc2:
        knowledge.apply(_batch(relations=[
            {"ref": "bad", "source": {"ref": "missing"}, "predicate": "supports",
             "target": {"id": oid}, "description": "d", "basis": "explicit"}]))
    assert exc2.value.code == "INVALID_REFERENCE"
    assert exc2.value.data["section"] == "relations" and exc2.value.data["index"] == 0
    assert exc2.value.data["ref"] == "bad"


def test_validation_issues_carry_ref_and_index(knowledge_base):
    with pytest.raises(BusinessError) as exc:
        knowledge.apply({"reason": "r", "objects": [
            {"ref": "ok", "kind": "concept", "title": "好"},
            {"ref": "bad", "kind": "concept", "title": None}]})
    assert exc.value.code == "INVALID_INPUT"
    issue = next(item for item in exc.value.data["issues"] if item.get("index") == 1)
    assert issue["section"] == "objects" and issue["ref"] == "bad"


def test_lint_relation_without_evidence_includes_endpoints(knowledge_base):
    [a, b] = _create_objects({"ref": "a", "title": "甲"}, {"ref": "b", "title": "乙"})
    result = knowledge.apply(_batch(relations=[
        {"ref": "r", "source": {"id": a}, "predicate": "depends_on", "target": {"id": b},
         "description": "依赖说明", "basis": "inference"}]))
    issue = next(item for item in knowledge.lint(object_ids=[a])["issues"]
                 if item["code"] == "RELATION_WITHOUT_EVIDENCE")
    assert issue["relation_id"] == result["refs"]["r"]["id"] and issue["predicate"] == "depends_on"
    assert issue["source_title"] == "甲" and issue["target_title"] == "乙"
    assert issue["description"] == "依赖说明"
