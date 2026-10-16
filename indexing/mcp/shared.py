"""piece-graph 与 piece-wiki 两个 MCP 服务共用的读写包装与意图式写入辅助。

两套能力各自独立成服务，但读工具的 params 包装、写回执的 success/message/data 语义、
以及"扁平参数 + 自动 request_key"的意图组装完全一致，集中在这里维护，
避免两个服务各写一份再各自漂移。
"""

import json
from typing import Annotated, List, Literal
from uuid import uuid4

from fastmcp.exceptions import ToolError
from pydantic import Field

from indexing.services import knowledge_intent as intent
from indexing.services.errors import BusinessError
from indexing.utils import run_sync

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}
WRITE_ADD = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
INTENT = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
INTENT_UPDATE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}

Evidence = Annotated[List[Annotated[str, Field(min_length=3, max_length=4200)]], Field(max_length=50)]
Stances = Annotated[List[Literal["supports", "contradicts", "context"]], Field(max_length=50)]
Reason = Annotated[str, Field(min_length=1, max_length=2000)]
Revision = Annotated[int, Field(strict=True, ge=1)]
ContentHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


async def read(function, params) -> dict:
    """params 模型直通业务层；阻塞 I/O 卸载到线程，业务错误转成可读的 ToolError。"""
    try:
        return await run_sync(function, **params.model_dump())
    except BusinessError as exc:
        raise ToolError(json.dumps({"code": exc.code, "message": str(exc), "data": exc.data}, ensure_ascii=False)) from exc


def write_envelope(result, partial_message):
    """把服务层回执包成 success/message/data/error。

    PARTIAL_FAILURE 只留给"至少写入一条但没全成"的情形。零写入（committed=false 且 errors 非空）
    直接透出首条错误的真实失败码（VERSION_CONFLICT/CONTENT_CONFLICT 等），否则调用方会被
    "不要重发"的提示误导：此时什么都没写，按冲突码重读再决定才是正确动作。
    """
    partial = bool(result.get("partial")) or result.get("index_status") not in (None, "current")
    if not partial:
        return {"success": True, "message": "已提交" if result.get("committed") else "预检通过，未写入", "data": result}
    errors = result.get("errors") or []
    if not result.get("committed") and errors and isinstance(errors[0], dict) and errors[0].get("code"):
        first = errors[0]
        message = first.get("message") or "未写入任何记录，请按 data.errors 处理后重试"
        return {"success": False, "message": message, "data": result,
                "error": {"code": first["code"], "message": message}}
    return {"success": False, "message": partial_message, "data": result,
            "error": {"code": "PARTIAL_FAILURE", "message": partial_message}}


async def write(function, params, actor: str) -> dict:
    try:
        result = await run_sync(function, params, actor=actor)
    except BusinessError as exc:
        return {"success": False, "message": str(exc), "data": exc.data,
                "error": {"code": exc.code, "message": str(exc)}}
    # 部分提交不能返回 success=true 冒充全成功；committed 只表示至少一条已写入，不预设失败原因。
    return write_envelope(result, "部分提交：committed 只表示至少一条已写入；请按 data.errors 与逐页回执核对，不要整批复发")


def intent_coerce(value):
    """接受 JSON 字符串形式的数组，弱模型常把 ["a","b"] 作为字符串传入。"""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        return parsed if isinstance(parsed, list) else [value]
    return value


def intent_error(exc: BusinessError) -> dict:
    """把业务错误转成回执，并为常见冲突补上下一步工具指引。"""
    data = dict(exc.data or {})
    if exc.code == "EVIDENCE_SELECTION_REQUIRED" and data.get("chunk_id"):
        data["next_tool"] = {"name": "query_chunk_info", "arguments": {"chunk_id": data["chunk_id"]},
                             "hint": "读回正文后改用 CHUNK_ID:N-M 行号定位唯一引文，不自动取首条"}
    elif exc.code == "KNOWLEDGE_CONFLICT" and data.get("section") == "relations" and isinstance(data.get("existing"), dict):
        existing = data["existing"]
        data["next_tool"] = {"name": "knowledge-relation-update",
                             "arguments": {"id": existing["id"], "expected_revision": existing["revision"]},
                             "hint": "data.existing 是已有关系的当前内容；要改它就带上这两个参数和待改字段，"
                                     "要保留它就不必再写，不要换 qualifier 重复新建"}
    return {"success": False, "message": str(exc), "data": data, "error": {"code": exc.code, "message": str(exc)}}


def intent_submit(service, kind, action, values, evidence, stances, *, reason, dry_run,
                  record_id=None, expected_revision=None, expected_content_hash=None, actor: str):
    """同步线程内完成：解析证据 → 组装批次 → 自动补 request_key → 提交。"""
    resolved = intent.resolve_evidence(intent_coerce(evidence) or [])
    payload = intent.build_intent(kind, action, values, resolved, intent_coerce(stances),
                                  reason=reason, record_id=record_id, expected_revision=expected_revision,
                                  expected_content_hash=expected_content_hash, actor="MCP")
    if dry_run:
        payload["dry_run"] = True
    else:
        payload["request_key"] = str(uuid4())
    result = service.apply(payload, actor=actor)
    if "request_key" in payload:
        result = {"request_key": payload["request_key"], **result}
    return result


async def intent_write(service, *args, actor: str, **kwargs):
    try:
        result = await run_sync(intent_submit, service, *args, actor=actor, **kwargs)
    except BusinessError as exc:
        return intent_error(exc)
    return write_envelope(result, "部分提交：committed 只表示至少一条已写入；请按 data.errors 核对，不要重发")


async def rebuild_index(function) -> dict:
    """无参数重建派生索引；图谱与 Wiki 的成功/部分失败回执一致。"""
    try:
        result = await run_sync(function)
    except BusinessError as exc:
        return {"success": False, "message": str(exc), "data": exc.data,
                "error": {"code": exc.code, "message": str(exc)}}
    partial = bool(result.get("partial")) or result.get("index_status") not in (None, "current")
    if partial:
        message = "索引重建未全部完成，请按 data.errors 处理后可原样重试"
        return {"success": False, "message": message, "data": result,
                "error": {"code": "PARTIAL_FAILURE", "message": message}}
    return {"success": True, "message": "索引已重建", "data": result}
