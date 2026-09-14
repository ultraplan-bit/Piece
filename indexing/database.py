"""
SQLite 数据库初始化和管理模块

职责:
- 数据库创建和初始化（表结构、FTS5、vec0）
- 数据写入操作
- 数据库维护（清理、重建索引等）

表结构设计（5 张普通表 + 2 张虚拟表）:
- files 表: 文件物理元数据（母表，去重、状态追踪、自定义属性）
- chunks 表: 检索单元（子表，外键关联 files，记录标题层级路径）
- tasks 表: 异步任务队列
- collections 表: 集合（跨领域归类，一个文件可属于多个集合）
- file_collections 表: 文件与集合的多对多关联
- chunks_fts 虚拟表: FTS5 全文检索索引（通过触发器自动同步）
- vec_chunks 虚拟表: sqlite-vec 向量检索索引（需手动同步）
"""

import sqlite3
import logging
import threading
from pathlib import Path
from contextlib import contextmanager, nullcontext
import re

from app.platform import load_sqlite_vec as _load_sqlite_vec
from .settings import get_settings, get_vector_dim

logger = logging.getLogger(__name__)

# 全局连接池（线程安全）
_connection_pool = None
_pool_lock = threading.Lock()
_pool_size = 10  # 连接池大小
_write_lock = threading.Lock()


class ConnectionPool:
    """
    SQLite 连接池实现

    特性:
    - 支持多个连接并发使用
    - 自动回收和复用连接
    - 线程安全
    """

    def __init__(self, db_path: Path, pool_size: int = 5):
        """
        初始化连接池

        Args:
            db_path: 数据库文件路径
            pool_size: 连接池大小
        """
        self.db_path = db_path
        self.pool_size = pool_size
        self._available = []
        self._all_connections = []
        self._borrowed = set()
        self._condition = threading.Condition()
        self._closing = False

        try:
            for _ in range(pool_size):
                conn = self._create_connection()
                self._available.append(conn)
                self._all_connections.append(conn)
        except BaseException:
            for conn in self._all_connections:
                conn.close()
            raise

        logger.info(f"[DB Pool] 连接池初始化成功，大小: {pool_size}")

    def _create_connection(self) -> sqlite3.Connection:
        """创建新的数据库连接"""
        conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=5.0
        )
        conn.row_factory = sqlite3.Row

        # 启用 WAL 模式
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

        # 启用外键约束
        conn.execute("PRAGMA foreign_keys=ON")

        # 性能优化
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")  # 64MB 缓存
        conn.execute("PRAGMA temp_store=MEMORY")

        # 加载 sqlite-vec 扩展
        _load_sqlite_vec(conn)

        return conn

    def get_connection(self, timeout: float = 5.0) -> sqlite3.Connection:
        """借出连接；关闭开始后拒绝新借用，不把已借出的连接提前关掉。"""
        with self._condition:
            available = self._condition.wait_for(lambda: self._available or self._closing, timeout)
            if self._closing:
                raise RuntimeError("核心数据库正在关闭")
            if not available:
                raise RuntimeError("连接池已耗尽，无法获取连接")
            conn = self._available.pop()
            self._borrowed.add(conn)
            return conn

    def return_connection(self, conn: sqlite3.Connection) -> None:
        with self._condition:
            self._borrowed.remove(conn)
            self._available.append(conn)
            self._condition.notify_all()

    def close_all(self) -> None:
        """等待所有已借出的连接归还；关闭异常向运行时传播。"""
        with self._condition:
            self._closing = True
            self._condition.notify_all()
            self._condition.wait_for(lambda: not self._borrowed)
            for conn in self._all_connections:
                conn.close()
            self._all_connections.clear()
            self._available.clear()
        logger.info("[DB Pool] 所有连接已关闭")


def get_db_path() -> Path:
    """获取数据库路径"""
    settings = get_settings()
    return settings.get_db_path()


def init_connection_pool(db_path: Path = None, pool_size: int = None) -> None:
    """
    初始化数据库连接池（应用启动时调用一次）

    使用真正的连接池（多个连接），支持并发访问。

    Args:
        db_path: 数据库文件路径，默认使用配置路径
        pool_size: 连接池大小，默认使用应用连接池配置
    """
    global _connection_pool

    with _pool_lock:
        if _connection_pool is not None:
            logger.warning("[DB Pool] 连接池已存在，跳过初始化")
            return

        if db_path is None:
            db_path = get_db_path()

        # 确保 data 目录存在
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # 创建连接池
        _connection_pool = ConnectionPool(
            db_path,
            pool_size=pool_size if pool_size is not None else _pool_size,
        )

        logger.info(f"[DB Pool] 连接池初始化成功: {db_path}")


def close_connection_pool() -> None:
    """关闭数据库连接池（应用关闭时调用）"""
    global _connection_pool

    with _pool_lock:
        if _connection_pool is not None:
            _connection_pool.close_all()
            _connection_pool = None
            logger.info("[DB Pool] 连接池已关闭")


@contextmanager
def get_db_cursor(write: bool = False):
    """
    获取数据库游标（上下文管理器，推荐使用）

    Args:
        write: 是否执行写操作。写事务在进程内串行化，避免多个线程同时争抢 SQLite 写锁。

    使用方式:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT * FROM files")
            rows = cursor.fetchall()

        with get_db_cursor(write=True) as cursor:
            cursor.execute("UPDATE files SET status = ? WHERE id = ?", (status, file_id))

    特性:
    - 自动提交/回滚事务
    - 写事务在进程内串行化，缩短 SQLite 锁等待
    - 自动归还连接

    Yields:
        sqlite3.Cursor: 数据库游标
    """
    global _connection_pool

    pool = _connection_pool
    if pool is None:
        raise RuntimeError("核心数据库尚未初始化；业务操作必须连接已经运行的 Piece 服务")

    operation_lock = _write_lock if write else nullcontext()
    with operation_lock:
        # 从连接池获取连接
        conn = pool.get_connection()
        cursor = conn.cursor()

        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            # 归还连接到连接池
            pool.return_connection(conn)


def init_database(db_path: Path = None) -> None:
    """
    初始化数据库：创建表结构、FTS5 虚拟表、vec0 虚拟表

    表结构：
    - files: 文件物理元数据（母表）
    - chunks: 检索单元（子表，外键关联 files）

    Args:
        db_path: 数据库文件路径，默认使用配置路径
    """
    if db_path is None:
        db_path = get_db_path()

    # 确保 data 目录存在
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 创建临时连接（仅用于初始化）
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if (tables and version != 1) or version not in (0, 1):
            raise RuntimeError(
                f"知识库结构不受支持（schema={version}）：{db_path}。"
                "本版本仅支持新库；请停止服务后检查并显式选择空数据目录，程序不会删除或迁移此库。"
            )
        if version == 1:
            required = {"task_type", "input_json", "result_json", "request_key", "error_code"}
            actual = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            if not required <= actual:
                raise RuntimeError("知识库任务结构不完整，拒绝自动修复或覆盖")
        # 启用 WAL 模式
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

        # 启用外键约束
        conn.execute("PRAGMA foreign_keys=ON")

        # 加载 sqlite-vec 扩展
        _load_sqlite_vec(conn)
        # 1. 创建 files 表（母表：文件物理元数据）
        # 主表记录工作文件（working/），新增字段记录原始文件（originals/）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_hash TEXT UNIQUE NOT NULL,
                filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_size INTEGER,
                original_file_type TEXT,
                original_file_path TEXT,
                metadata TEXT,
                working_dirty INTEGER NOT NULL DEFAULT 0,
                status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'indexed', 'error', 'empty')),
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        # 2. 创建 files 表索引
        conn.execute("CREATE INDEX IF NOT EXISTS idx_file_hash ON files(file_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_file_status ON files(status)")

        # 3. 创建 chunks 表（子表：检索单元）
        # heading_path 记录切片在文档中的标题层级（"文件名 / 二级标题 / 三级标题"），
        # heading_level 为该路径的层级数，供工作文件重建时还原 Markdown 标题级别
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                doc_title TEXT NOT NULL,
                chunk_text TEXT NOT NULL,
                chunk_index INTEGER DEFAULT 0,
                heading_path TEXT,
                heading_level INTEGER DEFAULT 2,
                embedding BLOB,
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            )
        """
        )

        # 4. 创建 chunks 表索引
        conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_title ON chunks(doc_title)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_file_id ON chunks(file_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunk_order ON chunks(file_id, chunk_index)"
        )

        # 5. 创建 FTS5 虚拟表（全文检索 chunk_text 和 doc_title）
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
            USING fts5(
                chunk_text,
                doc_title,
                content='chunks',
                content_rowid='id',
                tokenize='unicode61'
            )
        """
        )

        # 6. 创建触发器：chunks 表插入时同步到 FTS5
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS chunks_ai
            AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, chunk_text, doc_title)
                VALUES (new.id, new.chunk_text, new.doc_title);
            END
        """
        )

        # 7. 创建触发器：chunks 表删除时同步到 FTS5
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS chunks_ad
            AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, chunk_text, doc_title)
                VALUES ('delete', old.id, old.chunk_text, old.doc_title);
            END
        """
        )

        # 8. 创建触发器：chunks 表更新时同步到 FTS5
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS chunks_au
            AFTER UPDATE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, chunk_text, doc_title)
                VALUES ('delete', old.id, old.chunk_text, old.doc_title);
                INSERT INTO chunks_fts(rowid, chunk_text, doc_title)
                VALUES (new.id, new.chunk_text, new.doc_title);
            END
        """
        )

        # 9. 创建 vec0 虚拟表（向量检索）
        vector_dim = get_vector_dim()
        existing_vec = conn.execute("SELECT sql FROM sqlite_master WHERE name='vec_chunks'").fetchone()
        if existing_vec:
            dimension = re.search(r"embedding\s+float\[(\d+)\]", existing_vec[0], re.IGNORECASE)
            if not dimension:
                raise RuntimeError("向量表结构不受支持，拒绝覆盖")
            if int(dimension[1]) != vector_dim:
                if conn.execute("SELECT 1 FROM chunks LIMIT 1").fetchone():
                    raise RuntimeError("已有向量维度与配置不一致，请恢复原嵌入配置；不会覆盖已有索引")
                # 显式离线改空库配置后，在持有数据库锁的初始化阶段同步空向量表。
                conn.execute("DROP TABLE vec_chunks")
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                chunk_id INTEGER PRIMARY KEY,
                embedding float[{vector_dim}]
            )
        """
        )

        # 10. 创建 tasks 表（异步任务队列）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER,
                original_filename TEXT NOT NULL,
                task_type TEXT NOT NULL CHECK(task_type IN ('file_index', 'chunk_add', 'chunk_update')),
                input_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                request_key TEXT UNIQUE,
                error_code TEXT,
                status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'processing', 'completed', 'failed', 'cancelled')),
                progress INTEGER DEFAULT 0,
                current_page INTEGER DEFAULT 0,
                total_pages INTEGER DEFAULT 0,
                processed_chunks INTEGER DEFAULT 0,
                stage TEXT DEFAULT 'queued',
                error_message TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE SET NULL
            )
        """
        )

        # 11. 创建 tasks 表索引
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_status ON tasks(status)")
        # 界面每秒按 updated_at 拉一次任务增量，靠这个索引做范围扫描
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_updated ON tasks(updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_file_status ON tasks(file_id, status, id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS staged_chunks (
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                doc_title TEXT NOT NULL,
                chunk_text TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                heading_path TEXT,
                heading_level INTEGER NOT NULL DEFAULT 2,
                embedding BLOB NOT NULL,
                PRIMARY KEY(task_id, chunk_index)
            )
        """)

        # 12. 创建 collections 表（集合：跨领域归类）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                description TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        # 13. 创建 file_collections 关联表（多对多：一个文件可属于多个集合）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_collections (
                file_id INTEGER NOT NULL,
                collection_id INTEGER NOT NULL,
                PRIMARY KEY (file_id, collection_id),
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
                FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE
            )
        """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_collections_collection "
            "ON file_collections(collection_id)"
        )

        conn.execute("PRAGMA user_version=1")
        conn.commit()
        logger.info(f"[DB] 数据库初始化完成: {db_path}")

    finally:
        conn.close()
