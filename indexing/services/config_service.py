"""统一配置更新：原子保存、脱敏、生效分类及显式离线互斥。"""

import hashlib
import json
from contextlib import ExitStack
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError
from app.platform import database_lock
from .. import settings as settings_module
from ..settings import (AppSettings, McpSettings, MCP_LISTENER_FIELDS, get_settings, load_settings,
                        save_settings, _get_config_file_path)
from .embedding_config import check_embedding_change, prepare_embedding_change
from .errors import BusinessError
from .task_service import serialized_mutation

_RESTART_GROUPS = ("api", "performance", "data_path")
_mcp_reconfigure: Callable[[McpSettings], bool] | None = None
_mcp_status: Callable[[], dict] | None = None
_mcp_apply_error: str | None = None


def bind_mcp_reconfigure(callback, status=None):
    """运行中的 MCP 管理器注册应用入口；配置层不启动服务器或等待事件循环。"""
    global _mcp_reconfigure, _mcp_status, _mcp_apply_error
    _mcp_reconfigure = callback
    _mcp_status = status
    _mcp_apply_error = None


def unbind_mcp_reconfigure(callback):
    global _mcp_reconfigure, _mcp_status, _mcp_apply_error
    if _mcp_reconfigure == callback:
        _mcp_reconfigure = None
        _mcp_status = None
        _mcp_apply_error = None


def mcp_service_status(service):
    return _mcp_status().get(service) if _mcp_status is not None else None


@serialized_mutation
def mark_mcp_applied(config, *, error=None):
    """只有资源拥有者确认监听就绪后，才发布对应字段的有效值。"""
    global _mcp_apply_error
    _mcp_apply_error = error
    if error is None:
        effective = get_settings().model_copy(deep=True)
        for key in MCP_LISTENER_FIELDS:
            setattr(effective.mcp, key, getattr(config, key))
        settings_module._settings = effective


def config_effects(saved, effective=None):
    """GUI 和 API 共用生效判断；同组的即时字段不受待重启字段牵连。"""
    effective = effective or get_settings()
    restart = [key for key in _RESTART_GROUPS if getattr(saved, key) != getattr(effective, key)]
    listeners_pending = any(getattr(saved.mcp, key) != getattr(effective.mcp, key) for key in MCP_LISTENER_FIELDS)
    failed = _mcp_status is not None and any(status["status"] == "failed" for status in _mcp_status().values())
    pending_apply = []
    if listeners_pending or failed:
        (pending_apply if _mcp_reconfigure is not None else restart).append("mcp")
    error = _mcp_apply_error or ("MCP 监听未能就绪" if failed else None)
    return {"pending_restart": restart, "pending_apply": pending_apply,
            "apply_error": error if listeners_pending or failed else None}


def _reindex_groups(saved, updated):
    credentials = {"embedding": {"api_key"}, "ocr": {"api_key", "vlm_api_key", "mineru_token"}, "office": set()}
    groups = [group for group, excluded in credentials.items()
              if getattr(saved, group).model_dump(exclude=excluded) != getattr(updated, group).model_dump(exclude=excluded)]
    if "ocr" not in groups and settings_module.get_pdf_parser(saved.ocr) != settings_module.get_pdf_parser(updated.ocr):
        groups.append("ocr")
    return groups


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
    if settings.ocr.provider not in {"paddle", "vlm", "mineru"} or settings.office.converter not in {"auto", "com", "libreoffice", "off"}:
        raise BusinessError("INVALID_CONFIG", "解析器或 Office 转换器选项无效")
    for base in (settings.embedding.base_url, settings.ocr.base_url, settings.ocr.vlm_base_url, settings.webdav.hostname):
        if base and not base.startswith(("http://", "https://")):
            raise BusinessError("INVALID_CONFIG", "服务地址必须使用 http:// 或 https://")
    return settings


def show_config(offline=False):
    saved = _read_config()
    result = {"config": redact(saved.model_dump()), "config_path": str(_get_config_file_path())}
    if not offline:
        result.update(config_effects(saved))
    return result


def get_saved_settings():
    """管理界面编辑已保存配置，不把进程中暂未生效的旧值写回磁盘。"""
    return _read_config()


def config_revision():
    """交互确认期间不持有文件锁；保存时用原文件摘要检测并发改动。"""
    try:
        return hashlib.sha256(_get_config_file_path().read_bytes()).hexdigest()
    except FileNotFoundError:
        return "missing"
    except OSError:
        raise BusinessError("INVALID_CONFIG", "配置无法读取，原文件未修改") from None


def initialize_config(patch=None, *, expected_revision=None):
    with database_lock(_get_config_file_path()):
        if expected_revision is not None and config_revision() != expected_revision:
            raise BusinessError("CONFIG_CHANGED", "配置在交互期间已被修改，请重新运行初始化，未覆盖现有配置")
        exists = _get_config_file_path().exists()
        target = _read_config() if exists else AppSettings()
        with ExitStack() as locks:
            locks.enter_context(database_lock(target.get_db_path()))
            if patch is None:
                # 补写管理凭据也必须等两个资源锁都取得后进行。
                settings = load_settings()
            else:
                settings = _validate(_merge(target.model_dump(), patch))
                if settings.get_db_path().resolve() != target.get_db_path().resolve():
                    locks.enter_context(database_lock(settings.get_db_path()))
                check_embedding_change(target, settings)
                if not exists:
                    settings.mcp.api_key = settings_module.generate_api_key()
                    settings.mcp.index_api_key = settings_module.generate_api_key()
                if not save_settings(settings):
                    raise BusinessError("CONFIG_SAVE_FAILED", "配置保存失败，原配置未修改")
            return {"config": redact(settings.model_dump()), "config_path": str(_get_config_file_path())}


@serialized_mutation
def update_config(patch, offline=False):
    with ExitStack() as locks:
        if offline:
            locks.enter_context(database_lock(_get_config_file_path()))
        saved = _read_config()
        current = saved if offline else get_settings()
        updated = _validate(_merge(saved.model_dump(), patch))
        effective = updated.model_copy(deep=True)
        if not offline:
            for key in _RESTART_GROUPS:
                setattr(effective, key, getattr(current, key))
            for key in MCP_LISTENER_FIELDS:
                setattr(effective.mcp, key, getattr(current.mcp, key))
            try:
                effective = AppSettings.model_validate(effective.model_dump())
            except ValidationError:
                raise BusinessError("INVALID_CONFIG", "新 MCP 凭据与当前管理凭据冲突，请先重启以应用管理密钥；配置未保存") from None
        if offline:
            locks.enter_context(database_lock(saved.get_db_path()))
            if updated.get_db_path().resolve() != saved.get_db_path().resolve():
                locks.enter_context(database_lock(updated.get_db_path()))
        try:
            with prepare_embedding_change(current, updated, offline=offline):
                if not save_settings(updated, update_cache=False):
                    raise BusinessError("CONFIG_SAVE_FAILED", "配置写入失败，未应用变更")
        except BusinessError as exc:
            if exc.code == "VECTOR_CONFIG_FAILED":
                save_settings(saved, update_cache=False)
                raise BusinessError("CONFIG_SAVE_FAILED", "向量维度校验失败，已保留原配置") from None
            raise
        changed = [key for key in updated.model_dump() if getattr(saved, key) != getattr(updated, key)]
        # 保存层可能因同步目标变化清空历史，运行时发布同一结果。
        effective.webdav.last_sync_time = updated.webdav.last_sync_time
        settings_module._settings = effective
        effects = config_effects(updated, effective)
        callback = _mcp_reconfigure
        listener_changes = any(getattr(saved.mcp, key) != getattr(updated.mcp, key) for key in MCP_LISTENER_FIELDS)
        if not offline and callback is not None and "mcp" in patch and (listener_changes or effects["pending_apply"]):
            global _mcp_apply_error
            _mcp_apply_error = None
            if not callback(updated.mcp):
                effects["pending_apply"] = []
                effects["pending_restart"].append("mcp")
        waiting = set(effects["pending_restart"] + effects["pending_apply"])
        return {"config": redact(updated.model_dump()), "changed": changed,
                "effective_immediately": [key for key in changed if key not in waiting],
                "requires_restart": effects["pending_restart"], "pending_apply": effects["pending_apply"],
                "requires_reindex": _reindex_groups(saved, updated)}


@serialized_mutation
def record_sync_success(snapshot, timestamp):
    """校验和写回共用配置变更锁，旧任务不能为替代目标建立同步历史。"""
    target = snapshot.get_sync_target()
    if _read_config().get_sync_target() != target or get_settings().get_sync_target() != target:
        return False
    update_config({"webdav": {"last_sync_time": timestamp}})
    return True


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
        elif settings.ocr.provider == "mineru":
            from .mineru_client import test_mineru_connection
            ok, message = await run_sync(test_mineru_connection, settings.ocr.mineru_token, settings.ocr.mineru_model_version)
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
