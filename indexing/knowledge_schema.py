"""新库图谱与独立 Wiki DDL；仅初始化创建，不迁移旧 schema。"""

from uuid import UUID, uuid4


REQUIRED_COLUMNS = {
    "library_metadata": {"singleton", "library_id"},
    "knowledge_objects": {"id", "kind", "title", "title_norm", "summary", "aliases_json", "aliases_norm_json", "status", "revision", "created_at", "updated_at"},
    "knowledge_relations": {"id", "source_id", "predicate", "target_id", "description", "qualifier", "basis", "status", "revision", "created_at", "updated_at"},
    "knowledge_evidence": {"id", "object_id", "relation_id", "source_kind", "stance", "source_library_id", "source_file_id", "source_chunk_id", "source_title", "source_url", "heading_path", "page_number", "quote", "content_hash", "dedup_key", "created_at"},
    "knowledge_revisions": {"id", "object_id", "relation_id", "before_revision", "after_revision", "before_json", "after_json", "reason", "batch_id", "actor", "created_at"},
    "knowledge_requests": {"request_key", "request_hash", "result_json", "created_at"},
    "knowledge_fts": {"object_id", "title", "summary"},
    # Wiki 表只是导航/全文缓存；正文、身份及证据始终从 MD 读取。
    "wiki_pages": {"id", "path", "kind", "title", "title_norm", "summary", "aliases_json", "aliases_norm_json", "status", "revision", "content_hash", "created_at", "updated_at"},
    "wiki_links": {"source_id", "target_id"},
    "wiki_evidence": {"id", "page_id", "source_library_id", "source_file_id"},
    "wiki_fts": {"page_id", "title", "summary", "body"},
    # 请求与操作是独立审计，不是可被重建丢弃的缓存，也不用于自动恢复正文。
    "wiki_requests": {"request_key", "request_hash", "result_json", "created_at"},
    "wiki_operations": {"id", "request_key", "page_id", "path", "before_hash", "after_hash", "before_json", "after_json", "result_json", "state", "reason", "actor", "created_at"},
}
REQUIRED_OBJECTS = set(REQUIRED_COLUMNS) | {
    "idx_knowledge_title", "idx_knowledge_relations_target",
    "idx_knowledge_evidence_object", "idx_knowledge_evidence_relation", "idx_knowledge_evidence_source",
    "idx_knowledge_history_object", "idx_knowledge_history_relation", "knowledge_objects_ad",
    "idx_wiki_links_target", "idx_wiki_evidence_source", "idx_wiki_operations_page", "wiki_pages_ad",
}


STATEMENTS = [
    """CREATE TABLE library_metadata (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1), library_id TEXT NOT NULL UNIQUE
    )""",
    """CREATE TABLE knowledge_objects (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('concept','entity')),
        title TEXT NOT NULL, title_norm TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
        aliases_json TEXT NOT NULL DEFAULT '[]', aliases_norm_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL CHECK(status IN ('active','disputed','outdated')),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX idx_knowledge_title ON knowledge_objects(title_norm)",
    """CREATE TABLE knowledge_relations (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES knowledge_objects(id) ON DELETE CASCADE,
        predicate TEXT NOT NULL CHECK(predicate IN ('is_a','part_of','depends_on','applies_to','supports','contradicts','related_to')),
        target_id TEXT NOT NULL REFERENCES knowledge_objects(id) ON DELETE CASCADE,
        description TEXT NOT NULL, qualifier TEXT NOT NULL DEFAULT '',
        basis TEXT NOT NULL CHECK(basis IN ('explicit','synthesis','inference','user_statement')),
        status TEXT NOT NULL CHECK(status IN ('active','disputed','outdated')),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(source_id,predicate,target_id,qualifier), CHECK(source_id != target_id)
    )""",
    "CREATE INDEX idx_knowledge_relations_target ON knowledge_relations(target_id,source_id)",
    """CREATE TABLE knowledge_evidence (
        id TEXT PRIMARY KEY,
        object_id TEXT REFERENCES knowledge_objects(id) ON DELETE CASCADE,
        relation_id TEXT REFERENCES knowledge_relations(id) ON DELETE CASCADE,
        source_kind TEXT NOT NULL CHECK(source_kind IN ('piece','external','user')),
        stance TEXT NOT NULL CHECK(stance IN ('supports','contradicts','context')),
        source_library_id TEXT, source_file_id INTEGER, source_chunk_id INTEGER,
        source_title TEXT, source_url TEXT, heading_path TEXT, page_number INTEGER,
        quote TEXT NOT NULL, content_hash TEXT, dedup_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK((object_id IS NOT NULL) != (relation_id IS NOT NULL))
    )""",
    "CREATE INDEX idx_knowledge_evidence_object ON knowledge_evidence(object_id,id)",
    "CREATE INDEX idx_knowledge_evidence_relation ON knowledge_evidence(relation_id,id)",
    "CREATE INDEX idx_knowledge_evidence_source ON knowledge_evidence(source_library_id,source_file_id,id)",
    """CREATE TABLE knowledge_revisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        object_id TEXT REFERENCES knowledge_objects(id) ON DELETE CASCADE,
        relation_id TEXT REFERENCES knowledge_relations(id) ON DELETE CASCADE,
        before_revision INTEGER, after_revision INTEGER NOT NULL,
        before_json TEXT, after_json TEXT NOT NULL,
        reason TEXT NOT NULL, batch_id TEXT NOT NULL, actor TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK((object_id IS NOT NULL) != (relation_id IS NOT NULL))
    )""",
    "CREATE INDEX idx_knowledge_history_object ON knowledge_revisions(object_id,id)",
    "CREATE INDEX idx_knowledge_history_relation ON knowledge_revisions(relation_id,id)",
    """CREATE TABLE knowledge_requests (
        request_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, result_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE VIRTUAL TABLE knowledge_fts USING fts5(object_id UNINDEXED,title,summary,tokenize='unicode61')",
    """CREATE TRIGGER knowledge_objects_ad AFTER DELETE ON knowledge_objects BEGIN
        DELETE FROM knowledge_fts WHERE object_id = old.id;
    END""",
    """CREATE TABLE wiki_pages (
        id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
        title TEXT NOT NULL, title_norm TEXT NOT NULL, summary TEXT NOT NULL,
        aliases_json TEXT NOT NULL, aliases_norm_json TEXT NOT NULL, status TEXT NOT NULL,
        revision INTEGER NOT NULL, content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE wiki_links (
        source_id TEXT NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
        target_id TEXT NOT NULL, PRIMARY KEY(source_id,target_id)
    )""",
    "CREATE INDEX idx_wiki_links_target ON wiki_links(target_id,source_id)",
    """CREATE TABLE wiki_evidence (
        id TEXT PRIMARY KEY, page_id TEXT NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
        source_library_id TEXT, source_file_id INTEGER
    )""",
    "CREATE INDEX idx_wiki_evidence_source ON wiki_evidence(source_library_id,source_file_id,page_id)",
    "CREATE VIRTUAL TABLE wiki_fts USING fts5(page_id UNINDEXED,title,summary,body,tokenize='unicode61')",
    """CREATE TRIGGER wiki_pages_ad AFTER DELETE ON wiki_pages BEGIN
        DELETE FROM wiki_fts WHERE page_id = old.id;
    END""",
    """CREATE TABLE wiki_requests (
        request_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, result_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE wiki_operations (
        id TEXT PRIMARY KEY, request_key TEXT NOT NULL REFERENCES wiki_requests(request_key),
        page_id TEXT NOT NULL, path TEXT NOT NULL, before_hash TEXT, after_hash TEXT,
        before_json TEXT, after_json TEXT, result_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','applied','abandoned')),
        reason TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(request_key,page_id)
    )""",
    "CREATE INDEX idx_wiki_operations_page ON wiki_operations(page_id,created_at,id)",
]


def validate_identity(conn):
    rows = conn.execute("SELECT singleton, library_id FROM library_metadata").fetchall()
    if len(rows) != 1 or rows[0][0] != 1:
        raise RuntimeError("知识库身份缺失，拒绝自动修复")
    try:
        if str(UUID(rows[0][1])) != rows[0][1]:
            raise ValueError("UUID 未规范化")
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("知识库身份无效，拒绝自动修复") from exc
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_links'").fetchone():
        raise RuntimeError("检测到旧耦合结构，拒绝自动迁移")
    if "body" in {row[1] for row in conn.execute("PRAGMA table_info(knowledge_objects)")}:
        raise RuntimeError("图谱实体不能包含旧页面正文，拒绝自动迁移")


def create_schema(conn):
    for statement in STATEMENTS:
        conn.execute(statement)
    conn.execute("INSERT INTO library_metadata VALUES (1, ?)", (str(uuid4()),))
