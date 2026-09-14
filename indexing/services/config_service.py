"""统一配置更新：原子保存、脱敏、生效分类及显式离线互斥。"""

import json
import sqlite3
from contextlib import ExitStack
from pathlib import Path

from pydantic import ValidationError
from app.platform import database_lock
from .. import settings as settings_module
from ..settings import AppSettings, get_settings, load_settings, save_settings, _get_config_file_path
from .errors import BusinessError
from .task_service import serialized_mutation

_RESTART_GROUPS = {"api", "mcp", "performance", "data_path"}
_MODEL_FIELDS = {"model", "vector_dim", "base_url"}


def redact(value):
    if isinstance(value, dict):
        return {key: ("***" if item else item) if key.lower().endswith(("key", "password", "token", "secret")) else redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _read_config():
    path = _get_config_file_path()
    if not path.is_file():
        raise BusinessError("CONFIG_NOT_FOUND", "配置尚未初始化，请显式执行 piece config init 或 piece serve")
    try:
        return AppSettings.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        raise BusinessError("INVALID_CONFIG", "配置无法读取或校验失败，原文件未修改") from None


def _merge(existing, patch, prefix=""):
    if not isinstance(patch, dict):
        raise BusinessError("INVALID_CONFIG", "配置输入必须为 JSON 对象")
    result = dict(existing)
    for key, value in patch.items():
        if key not in existing:
            raise BusinessError("INVALID_CONFIG", f"未知配置字段：{prefix}{key}")
        if isinstance(existing[key], dict):
            if prefix == "ocr.payload." and key == "extra_payload":
                # extra_payload 是透传给 OCR 服务的开放参数表（键为服务端
                # camelCase 字段名），不参与已知字段校验，整体替换
                result[key] = value if isinstance(value, dict) else {}
            else:
                result[key] = _merge(existing[key], value, f"{prefix}{key}.")
        else:
            if value == "***":
                raise BusinessError("INVALID_CONFIG", "脱敏占位符不能作为配置值写回")
            result[key] = value
    return result


def _validate(data):
    try:
        settings = AppSettings.model_validate(data)
    except ValidationError as exc:
        fields = [".".join(map(str, e["loc"])) for e in exc.errors(include_input=False)]
        raise BusinessError("INVALID_CONFIG", f"配置字段校验失败：{', '.join(fields)}") from None
    path = Path(settings.data_path).expanduser()
    if not path.is_absolute():
        raise BusinessError("INVALID_CONFIG", "data_path 必须为绝对路径")
    settings.data_path = str(path.resolve())
    if any(value <= 0 for value in settings.performance.model_dump().values()):
        raise BusinessError("INVALID_CONFIG", "并发、批量和限流配置必须大于零")
    if settings.ocr.provider not in {"paddle", "vlm"} or settings.office.converter not in {"auto", "com", "libreoffice", "off"}:
        raise BusinessError("INVALID_CONFIG", "解析器或 Office 转换器选项无效")
    for base in (settings.embedding.base_url, settings.ocr.base_url, settings.ocr.vlm_base_url, settings.webdav.hostname):
        if base and not base.startswith(("http://", "https://")):
            raise BusinessError("INVALID_CONFIG", "服务地址必须使用 http:// 或 https://")
    return settings


def show_config(offline=False):
    saved = _read_config()
    result = {"config": redact(saved.model_dump()), "config_path": str(_get_config_file_path())}
    if not offline:
        effective = get_settings()
        result["pending_restart"] = [key for key in _RESTART_GROUPS if getattr(saved, key) != getattr(effective, key)]
    return result


def get_saved_settings():
    """管理界面编辑已保存配置，不把进程中暂未生效的旧值写回磁盘。"""
    return _read_config()


def initialize_config():
    with database_lock(_get_config_file_path()):
        target = _read_config() if _get_config_file_path().exists() else AppSettings()
        with database_lock(target.get_db_path()):
            # 补写管理凭据也必须等两个资源锁都取得后进行。
            settings = load_settings()
            return {"config": redact(settings.model_dump()), "config_path": str(_get_config_file_path())}


def _has_indexed_data(path):
    if not path.exists():
        return False
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return "chunks" in tables and connection.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() is not None
    finally:
        connection.close()


@serialized_mutation
def update_config(patch, offline=False):
    with ExitStack() as locks:
        if offline:
            locks.enter_context(database_lock(_get_config_file_path()))
        saved = _read_config()
        current = saved if offline else get_settings()
        updated = _validate(_merge(saved.model_dump(), patch))
        if offline:
            locks.enter_context(database_lock(saved.get_db_path()))
            if updated.get_db_path().resolve() != saved.get_db_path().resolve():
                locks.enter_context(database_lock(updated.get_db_path()))
        changed = [key for key in updated.model_dump() if getattr(saved, key) != getattr(updated, key)]
        restart = [key for key in _RESTART_GROUPS if getattr(current, key) != getattr(updated, key)]
        if "mcp" in restart and (current.mcp.port == updated.mcp.port
                and current.mcp.auth_enabled == updated.mcp.auth_enabled
                and all(current.mcp.get_api_key(service) == updated.mcp.get_api_key(service) for service in ("retrieval", "index"))):
            restart.remove("mcp")
        reindex = [key for key in changed if key in {"embedding", "ocr", "office"}]
        model_changed = any(getattr(current.embedding, key) != getattr(updated.embedding, key) for key in _MODEL_FIELDS)
        if model_changed and _has_indexed_data(current.get_db_path()):
            raise BusinessError("REINDEX_REQUIRED", "已有向量不能直接切换模型/地址/维度。请保留当前配置，或明确选择空知识库后配置新模型；不会自动适配旧向量。",
                                data={"requires_reindex": ["embedding"]})
        if not offline and model_changed:
            from .task_service import get_active_tasks
            if get_active_tasks():
                raise BusinessError("FILE_BUSY", "存在在途任务，不能切换嵌入模型")
        try:
            if not offline and current.embedding.vector_dim != updated.embedding.vector_dim:
                from ..database import get_db_cursor
                with get_db_cursor(write=True) as cursor:
                    cursor.execute("DROP TABLE vec_chunks")
                    cursor.execute(f"CREATE VIRTUAL TABLE vec_chunks USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{updated.embedding.vector_dim}])")
                    if not save_settings(updated, update_cache=False):
                        raise BusinessError("CONFIG_SAVE_FAILED", "配置写入失败，未应用变更")
            elif not save_settings(updated, update_cache=False):
                raise BusinessError("CONFIG_SAVE_FAILED", "配置写入失败，未应用变更")
        except sqlite3.Error:
            save_settings(saved, update_cache=False)
            raise BusinessError("CONFIG_SAVE_FAILED", "向量维度校验失败，已保留原配置") from None
        if offline:
            settings_module._settings = updated
        else:
            effective = updated.model_copy(deep=True)
            for key in restart:
                setattr(effective, key, getattr(current, key))
            settings_module._settings = effective
            if current.embedding != effective.embedding:
                from .embedding_client import refresh_embeddings_instance
                refresh_embeddings_instance()
        return {"config": redact(updated.model_dump()), "changed": changed,
                "effective_immediately": [key for key in changed if key not in restart],
                "requires_restart": restart, "requires_reindex": reindex}


async def test_config(component):
    """仅用户明确调用测试命令时访问配置的外部服务。"""
    from ..utils import run_sync
    settings = get_settings()
    if component == "embedding":
        from .embedding_client import get_embeddings_model
        model = await run_sync(get_embeddings_model)
        vector = await model.aembed_query("Piece connection test")
        if len(vector) != settings.embedding.vector_dim:
            raise BusinessError("DIMENSION_MISMATCH", f"实际维度 {len(vector)} 与配置 {settings.embedding.vector_dim} 不一致")
        return {"component": component, "vector_dim": len(vector)}
    if component == "office":
        from .office_convert import list_converters
        converters = await run_sync(list_converters)
        return {"component": component, "converters": converters, "available": bool(converters)}
    if component == "ocr":
        if settings.ocr.provider == "vlm":
            from .vlm_client import test_vlm_connection
            ok, message = await run_sync(test_vlm_connection, settings.ocr.vlm_base_url, settings.ocr.vlm_api_key, settings.ocr.vlm_model)
        else:
            from .ocr_client import test_ocr_connection
            ok, message = await run_sync(test_ocr_connection, settings.ocr.base_url, settings.ocr.api_key, settings.ocr.model)
        if not ok:
            raise BusinessError("CONNECTION_FAILED", "解析服务连接测试失败，请检查地址和凭据")
        return {"component": component, "available": True}
    if component == "webdav":
        from webdav4.client import Client
        def check():
            client = Client(settings.webdav.hostname, auth=(settings.webdav.username, settings.webdav.password), timeout=10)
            try:
                client.ls("/")
            finally:
                client.http.close()
        await run_sync(check)
        return {"component": component, "available": True}
    raise BusinessError("INVALID_INPUT", "不支持的连接测试组件")
