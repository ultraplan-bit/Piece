"""知识层大数据量种子：经 SQLite 批量直插，满足真实 schema 约束。

供 test_knowledge_scale / test_wiki_scale / scale_benchmark 复用。
逐条走服务层在几千条规模下过慢，这里直接用 ``get_db_cursor(write=True)``
的批量 ``executemany`` 插入，但每一行都满足 knowledge_schema.py 的
CHECK / UNIQUE / FK 约束，否则分页结论无意义：

- knowledge_objects：kind ∈ {concept,entity}，status 合法，revision>=1，title_norm 归一化
- knowledge_relations：predicate/basis/status 合法，source≠target，(source,predicate,target,qualifier) 唯一
- knowledge_evidence：object_id/relation_id 恰有其一，source_kind/stance 合法，dedup_key 唯一
- knowledge_revisions：after_json/reason/batch_id/actor 非空，object_id/relation_id 恰有其一
- wiki_pages：path 唯一，content_hash 与真实 MD 字节一致
- wiki_operations：state 合法，(request_key,page_id) 唯一

FTS 派生行按服务端一致的分词（tokenize_query）写入，保证 search_* 的
命中与真实写入路径一致，而不是绕过分词制造假命中。
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from indexing.database import get_db_cursor
from indexing.knowledge_models import normalize_name
from indexing.services.knowledge_common import content_hash

NAMESPACE = NAMESPACE_URL

# 关系/证据的取值池，全部落在 schema CHECK 白名单内。
PREDICATES = ("is_a", "part_of", "depends_on", "applies_to", "supports", "contradicts", "related_to")
BASES = ("explicit", "synthesis", "inference", "user_statement")
STATUSES = ("active", "disputed", "outdated")
KINDS = ("concept", "entity")
STANCES = ("supports", "contradicts", "context")

# 图谱对象标题统一携带共享关键词，便于 search_objects 构造“命中全集”的查询。
GRAPH_KEYWORD = "分页压测"
WIKI_KEYWORD = "分页压测"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _oid(prefix, index):
    """确定性 UUID，便于失败时按索引复现。"""
    return str(uuid5(NAMESPACE, f"piece-scale-{prefix}-{index}"))


def _dedup(*parts):
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def library_id(cursor):
    return cursor.execute("SELECT library_id FROM library_metadata WHERE singleton=1").fetchone()[0]


# --------------------------------------------------------------------------- #
# 图谱：对象 / 关系 / 证据 / 修订
# --------------------------------------------------------------------------- #

def insert_objects(cursor, count, *, prefix="obj", start=0, title_prefix=GRAPH_KEYWORD + "对象"):
    """插入 count 个对象并同步 knowledge_fts，返回 id 列表。"""
    from retrieval.nodes.preprocess_node import tokenize_query
    rows, fts_rows, ids = [], [], []
    summary = GRAPH_KEYWORD + "共享关键词"
    for index in range(start, start + count):
        oid = _oid(prefix, index)
        title = f"{title_prefix}{index:06d}"
        rows.append((oid, KINDS[index % 2], title, normalize_name(title), summary,
                     "[]", "[]", STATUSES[index % 3], 1))
        fts_rows.append((oid, " ".join(tokenize_query(title)), "", " ".join(tokenize_query(summary))))
        ids.append(oid)
    cursor.executemany(
        "INSERT INTO knowledge_objects(id,kind,title,title_norm,summary,aliases_json,aliases_norm_json,status,revision)"
        " VALUES (?,?,?,?,?,?,?,?,?)", rows)
    cursor.executemany("INSERT INTO knowledge_fts(object_id,title,aliases,summary) VALUES (?,?,?,?)", fts_rows)
    return ids


def insert_relations(cursor, object_ids, count, *, prefix="rel", start=0):
    """在对象之间插入 count 条关系；qualifier 带序号保证 UNIQUE 约束不冲突。"""
    from retrieval.nodes.preprocess_node import tokenize_query
    rows, fts_rows, ids = [], [], []
    total = len(object_ids)
    for index in range(start, start + count):
        source = object_ids[index % total]
        target = object_ids[(index * 7 + 1) % total]
        if target == source:  # CHECK(source_id != target_id)
            target = object_ids[(index * 7 + 2) % total]
        rid = _oid(prefix, index)
        description, qualifier = f"关系说明{index}", f"q{index}"
        rows.append((rid, source, PREDICATES[index % len(PREDICATES)], target,
                     description, qualifier, BASES[index % len(BASES)], STATUSES[index % 3], 1))
        fts_rows.append((rid, " ".join(tokenize_query(description)), " ".join(tokenize_query(qualifier))))
        ids.append(rid)
    cursor.executemany(
        "INSERT INTO knowledge_relations(id,source_id,predicate,target_id,description,qualifier,basis,status,revision)"
        " VALUES (?,?,?,?,?,?,?,?,?)", rows)
    cursor.executemany("INSERT INTO knowledge_relation_fts(relation_id,description,qualifier) VALUES (?,?,?)", fts_rows)
    return ids


def insert_owner_evidence(cursor, owner_id, owner_kind, count, *, prefix="ev", start=0):
    """把 count 条 user 证据挂在同一个 object/relation 上，用于证据分页。"""
    owner_column = "object_id" if owner_kind == "object" else "relation_id"
    null_column = "relation_id" if owner_kind == "object" else "object_id"
    sql = (f"INSERT INTO knowledge_evidence(id,{owner_column},{null_column},source_kind,stance,quote,dedup_key)"
           " VALUES (?,?,NULL,'user',?,?,?)")
    rows = []
    for index in range(start, start + count):
        rows.append((_oid(prefix, index), owner_id, STANCES[index % 3], f"用户陈述引文{index}",
                     _dedup("user", owner_kind, owner_id, index)))
    cursor.executemany(sql, rows)
    return [row[0] for row in rows]


def insert_source_file(cursor, base_path: Path, text, *, name=None):
    """插入一条 files + chunks 记录，返回 (file_id, chunk_id)。触发器会同步 chunks_fts。"""
    filename = name or f"规模资料-{uuid4().hex}.md"
    cursor.execute("INSERT INTO files (file_hash, filename, file_path) VALUES (?,?,?)",
                   (uuid4().hex, filename, str(Path(base_path) / filename)))
    file_id = cursor.lastrowid
    cursor.execute("INSERT INTO chunks (file_id, doc_title, chunk_text, chunk_index, heading_path) VALUES (?,?,?,?,?)",
                   (file_id, "规模资料", text, 0, "文档 / 第1页 / 正文"))
    return file_id, cursor.lastrowid


def insert_piece_evidence(cursor, lib, file_id, chunk_id, chunk_text, owner_ids, count,
                          *, prefix="refev", start=0):
    """插入指向同一 (库 UUID, 文件 ID) 的 piece 证据，用于 references 反查分页。

    content_hash 取真实卡片正文哈希、quote 为正文精确子串，故 location_status 为 current。
    """
    digest = content_hash(chunk_text)
    quote = chunk_text.splitlines()[0]
    rows = []
    total = len(owner_ids)
    for index in range(start, start + count):
        eid = _oid(prefix, index)
        owner = owner_ids[index % total]
        rows.append((eid, owner, lib, file_id, chunk_id, "规模资料", "文档 / 第1页 / 正文", 1,
                     quote, digest, _dedup("piece", lib, file_id, index)))
    cursor.executemany(
        "INSERT INTO knowledge_evidence(id,object_id,source_kind,stance,source_library_id,source_file_id,"
        "source_chunk_id,source_title,heading_path,page_number,quote,content_hash,dedup_key)"
        " VALUES (?,?,'piece','supports',?,?,?,?,?,?,?,?,?)", rows)
    return [row[0] for row in rows]


def insert_revisions(cursor, owner_id, owner_kind, count, *, prefix="rev", start=0):
    """为同一 object/relation 插入 count 条历史，用于 history 分页。"""
    column = "object_id" if owner_kind == "object" else "relation_id"
    other = "relation_id" if owner_kind == "object" else "object_id"
    rows = []
    for index in range(start, start + count):
        rows.append((owner_id, index + 1, index + 2,
                     f'{{"revision":{index + 1}}}', f'{{"revision":{index + 2}}}',
                     f"历史原因{index}", _oid(prefix + "-batch", index), "tester"))
    cursor.executemany(
        f"INSERT INTO knowledge_revisions({column},{other},before_revision,after_revision,before_json,after_json,"
        "reason,batch_id,actor) VALUES (?,NULL,?,?,?,?,?,?,?)", rows)
    return count


# --------------------------------------------------------------------------- #
# Wiki：真实 MD 文件 + 派生索引 + 证据 + 链接 + 操作历史
# --------------------------------------------------------------------------- #

def _wiki_file_bytes(page_id, title, body, *, summary="", kind="topic", status="active",
                     revision=1, created_at=None, updated_at=None, evidence=None):
    """按 wiki_service._encode 的规范生成 frontmatter + 正文的字节，返回 (bytes, content_hash)。

    evidence 是 StoredEvidence 字典列表，直接写进 frontmatter，作为 references/lint 的真源。
    """
    import json
    stamp = created_at or _now()
    document = {"format": 1, "id": page_id, "kind": kind, "title": title, "summary": summary,
                "aliases": [], "status": status, "revision": revision,
                "created_at": stamp, "updated_at": updated_at or stamp,
                "evidence": evidence or []}
    text = "---\n" + json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n---\n" + body
    data = text.encode("utf-8")
    return data, hashlib.sha256(data).hexdigest()


def _stored_evidence(index, lib, file_id, chunk_id, chunk_text):
    """构造一条 location_status=current 的 piece 证据快照（正文哈希与引文逐字匹配）。"""
    return {"id": _oid("wikiev", index), "source_kind": "piece", "stance": "supports",
            "source_library_id": lib, "source_file_id": file_id, "source_chunk_id": chunk_id,
            "source_title": "规模资料", "heading_path": "文档 / 第1页 / 正文", "page_number": 1,
            "quote": chunk_text.splitlines()[0], "content_hash": content_hash(chunk_text),
            "created_at": _now()}


def seed_wiki_pages(base_path: Path, count, *, title_prefix=WIKI_KEYWORD + "页面",
                    evidence_by_page=None):
    """在 <data>/wiki 下写 count 个真实 MD 文件，并写 wiki_pages / wiki_fts 索引行。

    返回包含 id / path / content_hash 的页字典列表。正文共享关键词，便于构造全集查询；
    每 10 页含一条指向下一页的 piece://wiki 链接，供 lint / 导航使用。
    evidence_by_page 是 {页序号: [StoredEvidence 字典]}，写进对应页 frontmatter，作为
    references / lint / get_record 证据页的真源。
    """
    from retrieval.nodes.preprocess_node import tokenize_query

    evidence_by_page = evidence_by_page or {}
    root = Path(base_path) / "wiki"
    root.mkdir(parents=True, exist_ok=True)
    pages = []
    for index in range(count):
        page_id = _oid("wiki", index)
        pages.append({"id": page_id, "index": index,
                      "path": f"scale-{index:06d}.md",
                      "title": f"{title_prefix}{index:06d}"})
    for page in pages:
        link_target = pages[(page["index"] + 1) % count]["id"]
        body = f"# {page['title']}\n\n{WIKI_KEYWORD}正文内容，用于分页与性能验证。\n"
        if page["index"] % 10 == 0:
            body += f"\n参见 [相邻页](piece://wiki/{link_target})。\n"
        data, digest = _wiki_file_bytes(page["id"], page["title"], body, summary=WIKI_KEYWORD + "摘要",
                                        evidence=evidence_by_page.get(page["index"], []))
        (root / page["path"]).write_bytes(data)
        page["content_hash"] = digest
        page["body"] = body

    with get_db_cursor(write=True) as cursor:
        page_rows, fts_rows = [], []
        for page in pages:
            title = page["title"]
            page_rows.append((page["id"], page["path"], "topic", title, normalize_name(title),
                              WIKI_KEYWORD + "摘要", "[]", "[]", "active", 1, page["content_hash"]))
            fts_rows.append((page["id"], " ".join(tokenize_query(title)), " ".join(tokenize_query(WIKI_KEYWORD + "摘要")),
                             " ".join(tokenize_query(page["body"]))))
        cursor.executemany(
            "INSERT INTO wiki_pages(id,path,kind,title,title_norm,summary,aliases_json,aliases_norm_json,"
            "status,revision,content_hash,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)", page_rows)
        cursor.executemany("INSERT INTO wiki_fts(page_id,title,summary,body) VALUES (?,?,?,?)", fts_rows)
        # 链接表：仅登记真实存在的目标，避免与 lint 的 BROKEN_BODY_LINK 语义混淆。
        links = [(page["id"], pages[(page["index"] + 1) % count]["id"]) for page in pages if page["index"] % 10 == 0]
        cursor.executemany("INSERT INTO wiki_links(source_id,target_id) VALUES (?,?)", links)
    return pages


def insert_wiki_operations(cursor, page_id, path, content_hash_value, count, *, prefix="wikiop"):
    """为同一页面插入 count 条已应用的操作历史；state='applied' 时 _is_applied 直接成立。"""
    request_rows, operation_rows = [], []
    for index in range(count):
        key = _oid(prefix + "-req", index)
        request_rows.append((key, _dedup(key), "{}"))
        operation_rows.append((_oid(prefix, index), key, page_id, path, None, content_hash_value,
                               None, "{}", "{}", "applied", f"历史原因{index}", "tester"))
    cursor.executemany("INSERT INTO wiki_requests(request_key,request_hash,result_json) VALUES (?,?,?)", request_rows)
    cursor.executemany(
        "INSERT INTO wiki_operations(id,request_key,page_id,path,before_hash,after_hash,before_json,after_json,"
        "result_json,state,reason,actor) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", operation_rows)
    return count


# --------------------------------------------------------------------------- #
# 编排：一次性灌入图谱 + Wiki 规模数据
# --------------------------------------------------------------------------- #

def seed_knowledge_base(base_path, *, objects=3000, relations=6000, evidence=3000,
                        hot_object_evidence=250, reference_evidence=250, revisions=250,
                        wiki_pages=800, wiki_evidence=250, wiki_hot_page_evidence=150, wiki_history=250):
    """在已初始化的临时库里灌入规模数据，返回供测试引用的句柄字典。

    规模参数全部可调；默认值兼顾 CI 时间与“翻几十页”的分页覆盖。
    """
    chunk_text = "规模引用正文第一行\n第二行"
    stats = {}
    with get_db_cursor(write=True) as cursor:
        lib = library_id(cursor)
        object_ids = insert_objects(cursor, objects)
        relation_ids = insert_relations(cursor, object_ids, relations)

        # 普通对象证据：分散到各对象，验证 get_record 证据页与全库分页。
        spread = []
        for index in range(evidence):
            spread.append((_oid("ev", index), object_ids[index % len(object_ids)], STANCES[index % 3],
                           f"用户陈述引文{index}", _dedup("user", "object", index)))
        cursor.executemany(
            "INSERT INTO knowledge_evidence(id,object_id,relation_id,source_kind,stance,quote,dedup_key)"
            " VALUES (?,?,NULL,'user',?,?,?)", spread)

        # 热点对象：单个对象挂大量证据，用于 _evidence_page 分页。
        hot_object = object_ids[0]
        insert_owner_evidence(cursor, hot_object, "object", hot_object_evidence, prefix="hotev")

        # references：同一 (库 UUID, 文件 ID) 的 piece 证据。
        file_id, chunk_id = insert_source_file(cursor, base_path, chunk_text)
        reference_ids = insert_piece_evidence(cursor, lib, file_id, chunk_id, chunk_text, object_ids, reference_evidence)

        # history：同一对象的大量修订。
        history_object = object_ids[1]
        insert_revisions(cursor, history_object, "object", revisions)

        stats.update(library_id=lib, object_ids=object_ids, relation_ids=relation_ids,
                     hot_object=hot_object, history_object=history_object,
                     source_file_id=file_id, source_chunk_id=chunk_id,
                     reference_evidence=len(reference_ids))

    # Wiki 证据真源在 MD frontmatter，故建文件时一并写入，复用图谱的同一份资料文件。
    # 分散证据轮转到各页；热点页（最后一页）额外挂大量证据，用于 get_record 证据分页。
    evidence_by_page = {}
    for index in range(wiki_evidence):
        evidence_by_page.setdefault(index % wiki_pages, []).append(
            _stored_evidence(index, lib, file_id, chunk_id, chunk_text))
    hot_page_index = wiki_pages - 1
    for offset in range(wiki_hot_page_evidence):
        evidence_by_page.setdefault(hot_page_index, []).append(
            _stored_evidence(wiki_evidence + offset, lib, file_id, chunk_id, chunk_text))
    wiki = seed_wiki_pages(base_path, wiki_pages, evidence_by_page=evidence_by_page)
    with get_db_cursor(write=True) as cursor:
        first = wiki[0]
        insert_wiki_operations(cursor, first["id"], first["path"], first["content_hash"], wiki_history)
    stats.update(wiki_pages=wiki, wiki_history_page=wiki[0]["id"], wiki_hot_page=wiki[hot_page_index]["id"],
                 wiki_hot_page_evidence=len(evidence_by_page.get(hot_page_index, [])),
                 wiki_reference_evidence=wiki_evidence + wiki_hot_page_evidence)
    return stats
