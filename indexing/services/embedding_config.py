"""嵌入配置的向量兼容性与空库表结构更新。"""

import sqlite3
from contextlib import contextmanager

from .errors import BusinessError

_MODEL_FIELDS = ("model", "vector_dim", "base_url")


def check_embedding_change(current, updated):
    changed = any(getattr(current.embedding, key) != getattr(updated.embedding, key) for key in _MODEL_FIELDS)
    path = current.get_db_path()
    if changed and path.exists():
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            indexed = "chunks" in tables and connection.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() is not None
        finally:
            connection.close()
        if indexed:
            raise BusinessError("REINDEX_REQUIRED", "已有向量不能直接切换模型/地址/维度。请保留当前配置，或明确选择空知识库后配置新模型；不会自动适配旧向量。",
                                data={"requires_reindex": ["embedding"]})
    return changed


@contextmanager
def prepare_embedding_change(current, updated, *, offline=False):
    """保存失败回滚向量 DDL；在途任务及已有向量不允许被配置切换绕过。"""
    changed = check_embedding_change(current, updated)
    if changed and not offline:
        from .task_service import get_active_tasks
        if get_active_tasks():
            raise BusinessError("FILE_BUSY", "存在在途任务，不能切换嵌入模型")
    if offline or current.embedding.vector_dim == updated.embedding.vector_dim:
        yield
        return
    from ..database import get_db_cursor
    try:
        with get_db_cursor(write=True) as cursor:
            cursor.execute("DROP TABLE vec_chunks")
            cursor.execute(f"CREATE VIRTUAL TABLE vec_chunks USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{updated.embedding.vector_dim}])")
            yield
    except sqlite3.Error:
        raise BusinessError("VECTOR_CONFIG_FAILED", "向量维度更新失败，未应用配置") from None
