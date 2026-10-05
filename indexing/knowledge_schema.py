"""新库知识层 DDL；仅由 database.init_database 创建，不提供迁移。"""

from uuid import UUID, uuid4


REQUIRED_COLUMNS = {
    "library_metadata": {"singleton", "library_id"},
    "knowledge_objects": {"id", "kind", "title", "title_norm", "summary", "body", "aliases_json", "aliases_norm_json", "status", "revision", "created_at", "updated_at"},
    "knowledge_links": {"id", "source_id", "target_id", "created_at"},
    "knowledge_relations": {"id", "source_id", "predicate", "target_id", "description", "qualifier", "basis", "status", "revision", "created_at", "updated_at"},
    "knowledge_evidence": {"id", "object_id", "relation_id", "source_kind", "stance", "source_library_id", "source_file_id", "source_chunk_id", "source_title", "source_url", "heading_path", "page_number", "quote", "content_hash", "dedup_key", "created_at"},
    "knowledge_revisions": {"id", "object_id", "relation_id", "before_revision", "after_revision", "before_json", "after_json", "reason", "batch_id", "actor", "created_at"},
    "knowledge_requests": {"request_key", "request_hash", "result_json", "created_at"},
    "knowledge_fts": {"object_id", "title", "summary", "body"},
}
REQUIRED_OBJECTS = set(REQUIRED_COLUMNS) | {
    "idx_knowledge_title", "idx_knowledge_links_target", "idx_knowledge_relations_target",
    "idx_knowledge_evidence_object", "idx_knowledge_evidence_relation", "idx_knowledge_evidence_source",
    "idx_knowledge_history_object", "idx_knowledge_history_relation", "knowledge_objects_ad",
}


STATEMENTS = [
    """CREATE TABLE library_metadata (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1), library_id TEXT NOT NULL UNIQUE
    )""",
    """CREATE TABLE knowledge_objects (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('concept','entity','topic','synthesis','source_summary')),
        title TEXT NOT NULL, title_norm TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
        aliases_json TEXT NOT NULL DEFAULT '[]', aliases_norm_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL CHECK(status IN ('active','disputed','outdated')),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX idx_knowledge_title ON knowledge_objects(title_norm)",
    """CREATE TABLE knowledge_links (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES knowledge_objects(id) ON DELETE CASCADE,
        target_id TEXT NOT NULL REFERENCES knowledge_objects(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(source_id,target_id), CHECK(source_id != target_id)
    )""",
    "CREATE INDEX idx_knowledge_links_target ON knowledge_links(target_id,source_id)",
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
    "CREATE VIRTUAL TABLE knowledge_fts USING fts5(object_id UNINDEXED,title,summary,body,tokenize='unicode61')",
    """CREATE TRIGGER knowledge_objects_ad AFTER DELETE ON knowledge_objects BEGIN
        DELETE FROM knowledge_fts WHERE object_id = old.id;
    END""",
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


def create_schema(conn):
    for statement in STATEMENTS:
        conn.execute(statement)
    conn.execute("INSERT INTO library_metadata VALUES (1, ?)", (str(uuid4()),))
