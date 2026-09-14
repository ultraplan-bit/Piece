"""有版本的本地 HTTP 适配：身份、认证和参数校验，业务规则留在服务层。"""

import base64
import hashlib
import inspect
import json
import logging
import os
import secrets
import shutil
import urllib.parse
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import ConfigDict, Field, ValidationError, create_model
from starlette.datastructures import Headers

from app.platform import get_default_data_dir
from app.skills import get_version
from indexing.settings import get_settings
from indexing.utils import run_sync
from indexing.services.errors import BusinessError

logger = logging.getLogger(__name__)
API_VERSION = 1
APP_VERSION = get_version()
MAX_JSON_BYTES = 2 * 1024 * 1024


def envelope(data=None, message="成功", *, code=None):
    return {"success": code is None, "message": message, "data": data,
            "error": {"code": code, "message": message} if code else None}


def identity(runtime):
    config_dir = get_default_data_dir().resolve()
    db_path = runtime.database_path
    target = os.path.normcase(str(config_dir)) + "\n" + os.path.normcase(str(db_path))
    return {"application": "Piece", "api_version": API_VERSION, "version": APP_VERSION,
            "target_id": hashlib.sha256(target.encode("utf-8")).hexdigest(), "ready": runtime.ready}


def _equal(left, right):
    return bool(left and right and secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8")))


class LocalSecurity:
    """API Bearer 与 GUI 浏览器会话分离；包括 WebSocket 的同源检查。"""
    def __init__(self, app, runtime):
        self.app = app
        self.runtime = runtime
        self.session = secrets.token_urlsafe(32)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        hosts = {f"127.0.0.1:{self.runtime.port}", f"localhost:{self.runtime.port}"}
        origin = headers.get("origin")
        path = scope["path"]

        async def deny(code, message, status=403, extra=None):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse(envelope(message=message, code=code), status_code=status, headers=extra)(scope, receive, send)

        if headers.get("host") not in hosts:
            await deny("INVALID_HOST", "仅允许访问本机 Piece 地址")
            return
        if origin and origin not in {f"http://{host}" for host in hosts}:
            await deny("INVALID_ORIGIN", "拒绝跨来源请求")
            return
        if headers.get("sec-fetch-site") == "cross-site":
            await deny("INVALID_ORIGIN", "请直接打开本机管理地址，不从其他网页发起请求")
            return
        api = path.startswith("/api/")
        settings = get_settings()
        auth = headers.get("authorization", "")
        authenticated_gui = False
        if api:
            if origin:
                await deny("INVALID_ORIGIN", "业务 API 仅接受显式 Bearer 客户端调用")
                return
            if path == "/api/v1/handshake" and scope.get("method") == "GET":
                await self.app(scope, receive, send)
                return
            token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
            read_key = settings.mcp.get_api_key("retrieval")
            write_key = settings.mcp.get_api_key("index")
            role = None
            if _equal(token, settings.api.admin_key):
                role = "admin"
            elif write_key != read_key and _equal(token, write_key):
                role = "write"
            elif _equal(token, read_key):
                role = "read"
            if role is None:
                await deny("UNAUTHORIZED", "需要有效的 Piece API 凭据", 401)
                return
            if headers.get("x-piece-target") != identity(self.runtime)["target_id"]:
                await deny("TARGET_MISMATCH", "目标知识库不一致，请核对 --data-dir 和 data_path", 409)
                return
            if not self.runtime.ready and path != "/api/v1/status":
                await deny("NOT_READY", "核心服务尚未就绪或正在停止", 503)
                return
            scope.setdefault("state", {})["piece_role"] = role
        elif path == "/bootstrap" and scope.get("method") == "GET":
            # 服务发起的开窗携带一次性令牌：验证即发放会话 cookie 并跳转，
            # 令牌随之作废；失败仍回落到 Basic 登录，不泄露令牌是否过期。
            if not self.runtime.with_gui:
                await deny("GUI_DISABLED", "GUI 未启用；请在任务结束后用启用 GUI 的参数重启服务", 404)
                return
            query = urllib.parse.parse_qs(scope.get("query_string", b"").decode("latin-1"))
            token = query.get("token", [""])[0]
            if self.runtime.bootstrap_tokens.consume(token):
                response = RedirectResponse("/", status_code=303)
                await response(scope, receive, self._secure_send(send, True))
                return
            await deny("UNAUTHORIZED", "引导链接无效或已过期；请用用户名 piece 登录，密码为 config.json 中的 api.admin_key",
                       401, {"WWW-Authenticate": 'Basic realm="Piece local GUI", charset="UTF-8"'})
            return
        else:
            if not self.runtime.with_gui:
                await deny("GUI_DISABLED", "GUI 未启用；请在任务结束后用启用 GUI 的参数重启服务", 404)
                return
            cookie = SimpleCookie()
            try:
                cookie.load(headers.get("cookie", ""))
            except Exception:
                pass
            session = cookie.get("piece_session")
            authenticated_gui = bool(session and _equal(session.value, self.session))
            if not authenticated_gui and auth.startswith("Basic "):
                try:
                    user, password = base64.b64decode(auth[6:], validate=True).decode("utf-8").split(":", 1)
                    authenticated_gui = user == "piece" and _equal(password, settings.api.admin_key)
                except (ValueError, UnicodeError):
                    pass
            if not authenticated_gui:
                await deny("UNAUTHORIZED", "管理界面登录：用户名 piece，密码为 config.json 中的 api.admin_key", 401,
                           {"WWW-Authenticate": 'Basic realm="Piece local GUI", charset="UTF-8"'})
                return
            if (scope["type"] == "websocket" or scope.get("method") not in {"GET", "HEAD"}) and not origin:
                await deny("INVALID_ORIGIN", "浏览器写入和 WebSocket 必须携带同源 Origin")
                return
            if not self.runtime.ready:
                await deny("NOT_READY", "核心服务正在停止", 503)
                return

        await self.app(scope, receive, self._secure_send(send, authenticated_gui))

    def _secure_send(self, send, authenticated_gui: bool):
        """GUI 响应统一补安全头；已认证会话（含引导令牌首次进入）追加会话 cookie。"""
        async def wrapper(message):
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.extend([(b"x-frame-options", b"DENY"), (b"x-content-type-options", b"nosniff"),
                                         (b"referrer-policy", b"no-referrer")])
                if authenticated_gui:
                    response_headers.append((b"set-cookie", f"piece_session={self.session}; HttpOnly; SameSite=Strict; Path=/".encode()))
                message["headers"] = response_headers
            await send(message)
        return wrapper


class ExportResponse(FileResponse):
    def __init__(self, snapshot):
        super().__init__(snapshot["path"], filename=snapshot["filename"], media_type="application/octet-stream")
        self.temporary_dir = snapshot["temporary_dir"]

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await run_sync(shutil.rmtree, self.temporary_dir)


def _require_role(request, role):
    ranks = {"read": 0, "write": 1, "admin": 2}
    if ranks[request.state.piece_role] < ranks[role]:
        raise BusinessError("FORBIDDEN", "当前凭据没有此操作的权限")


async def _json_body(request):
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > MAX_JSON_BYTES:
            raise BusinessError("INPUT_TOO_LARGE", "JSON 输入不能超过 2 MiB")
    try:
        return json.loads(body or b"{}")
    except (ValueError, UnicodeError):
        raise BusinessError("INVALID_INPUT", "需要有效的 UTF-8 JSON 输入") from None


def _model(model_name, **fields):
    return create_model(model_name, __config__=ConfigDict(extra="forbid"), **fields)


Id = Annotated[int, Field(strict=True, ge=1)]
Ids = Annotated[list[Id], Field(min_length=1, max_length=100)]
Limit = Annotated[int, Field(strict=True, ge=1, le=100)]
Offset = Annotated[int, Field(strict=True, ge=0)]
Names = Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(max_length=100)]
Text = Annotated[str, Field(max_length=200000)]
Key = Annotated[str, Field(min_length=1, max_length=180)] | None


def create_api(runtime):
    from indexing.services import file_service as files, chunk_service as chunks, task_service as tasks
    from indexing.services import collection_service as collections, metadata_service
    from indexing.services import config_service, maintenance_service as maintenance
    from retrieval.service import search
    from app.logging_config import get_log_buffer

    app = FastAPI(title="Piece local API", version="1", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalSecurity, runtime=runtime)

    @app.exception_handler(BusinessError)
    async def business_error(request, exc):
        status = {"NOT_FOUND": 404, "FORBIDDEN": 403, "FILE_BUSY": 409, "REQUEST_CONFLICT": 409}.get(exc.code, 400)
        return JSONResponse(jsonable_encoder(envelope(exc.data, str(exc), code=exc.code)), status_code=status)

    @app.exception_handler(RequestValidationError)
    async def request_error(request, exc):
        return JSONResponse(envelope(message="请求参数无效", code="INVALID_INPUT"), status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        # 外部 SDK 异常可能包含请求体或凭据，不把原始异常返回客户端或日志。
        logger.error("本地 API 操作失败：%s (%s)", request.url.path, type(exc).__name__)
        return JSONResponse(envelope(message="操作失败，请检查配置、依赖和服务状态", code="OPERATION_FAILED"), status_code=500)

    @app.get("/api/v1/handshake")
    async def handshake():
        return envelope(identity(runtime))

    @app.get("/api/v1/status")
    async def status(request: Request):
        return envelope({**identity(runtime), **runtime.status(), "config_dir": str(get_default_data_dir()),
                         "data_path": str(get_settings().get_data_path())})

    def register(operation, role, schema, function):
        async def endpoint(request: Request):
            _require_role(request, role)
            try:
                payload = schema.model_validate(await _json_body(request)).model_dump()
            except ValidationError as exc:
                fields = [".".join(map(str, e["loc"])) for e in exc.errors(include_input=False)]
                raise BusinessError("INVALID_INPUT", f"参数校验失败：{', '.join(fields)}") from None
            if inspect.iscoroutinefunction(function):
                data = await function(**payload)
            else:
                data = await run_sync(function, **payload)
            if data is None:
                raise BusinessError("NOT_FOUND", "请求的对象不存在")
            if isinstance(data, dict):
                if data.get("success") is False or data.get("rejected_count", 0) or data.get("failed_count", 0):
                    return envelope(data, data.get("message", "部分操作失败，请保留已受理的 ID，不要整批重发"), code="PARTIAL_FAILURE")
                if data.get("not_found"):
                    return envelope(data, "部分任务不存在，不要继续轮询缺失的 ID", code="NOT_FOUND")
                if data.get("task_ids"):
                    return envelope(data, "已受理，尚未完成；请查询任务状态")
            return envelope(data)
        app.add_api_route(f"/api/v1/{operation}", endpoint, methods=["POST"], name=operation)

    def list_files(limit=20, offset=0, status=None, collections=None):
        scope = None if collections is None else collection_ids(collections)
        return files.get_files_list_paginated(limit, offset, status, scope)

    def collection_ids(names):
        return collections.resolve_collection_ids(names)

    def import_file(path, collections=None):
        return files.import_file(path, collection_ids=collection_ids(collections))

    def create_file(filename, collections=None):
        return files.create_empty_file(filename, collection_ids(collections))

    def reindex(file_id, source=None, confirmed=False, request_key=None, dry_run=False):
        if dry_run or not confirmed:
            preview = files.preview_reindex(file_id, source)
            if dry_run:
                return {**preview, "dry_run": True}
            raise BusinessError("CONFIRMATION_REQUIRED", "重新索引会覆盖手工修改，成功后卡片 ID 可能改变", data=preview)
        return files.reindex_file(file_id, source, request_key)

    def chunk_add(file_id, doc_title, chunk_text, request_key=None):
        task_id = chunks.create_chunk_add_task(file_id, doc_title, chunk_text, request_key)
        return {"file_id": file_id, "task_id": task_id, "task_ids": [task_id], "status": "accepted", "request_key": request_key}

    def chunk_update(chunk_id, doc_title=None, chunk_text=None, request_key=None):
        if doc_title is None and chunk_text is None:
            raise BusinessError("INVALID_INPUT", "至少提供标题或正文")
        if chunk_text is None:
            result = chunks.update_chunk_title(chunk_id, doc_title)
            if result is None:
                raise BusinessError("NOT_FOUND", "卡片不存在")
            return result
        task_id = chunks.create_chunk_update_task(chunk_id, chunk_text, doc_title, request_key)
        return {"chunk_id": chunk_id, "task_id": task_id, "task_ids": [task_id], "status": "accepted", "request_key": request_key}

    def public_task(task):
        if task is None:
            raise BusinessError("NOT_FOUND", "任务不存在")
        return {key: value for key, value in task.items() if key != "input"}

    def list_tasks(**kwargs):
        data = tasks.list_tasks(**kwargs)
        data["tasks"] = [public_task(task) for task in data["tasks"]]
        return data

    def query_tasks(task_ids):
        data = tasks.task_summary(task_ids)
        data["tasks"] = [public_task(task) for task in data["tasks"]]
        return data

    def retry(task_id):
        accepted = tasks.retry_task(task_id)
        return {"task_id": accepted, "task_ids": [accepted], "status": "accepted"}

    def list_collections(limit=20, offset=0):
        items = collections.list_collections()
        return {"collections": items[offset:offset + limit], "total": len(items), "limit": limit, "offset": offset}

    def images(chunk_id):
        items, skipped = maintenance.chunk_images(chunk_id)
        return {"chunk_id": chunk_id, "images": [{"index": i, "ref": item["ref"], "filename": Path(item["path"]).name} for i, item in enumerate(items)], "skipped": skipped}

    pagination = {"limit": (Limit, 20), "offset": (Offset, 0)}
    empty = _model("Empty")
    file_id_model = _model("FileId", file_id=(Id, ...))
    chunk_id_model = _model("ChunkId", chunk_id=(Id, ...))
    task_id_model = _model("TaskId", task_id=(Id, ...))
    confirm = {"dry_run": (bool, False), "confirmed": (bool, False)}
    register("file/list", "read", _model("FileList", **pagination, status=(Literal["pending", "indexed", "error", "empty"] | None, None), collections=(Names | None, None)), list_files)
    register("file/get", "read", file_id_model, maintenance.file_info)
    register("file/create", "write", _model("FileCreate", filename=(str, ...), collections=(Names | None, None)), create_file)
    # 本机路径读取是管理权限，索引凭据不能借导入读取任意本机文件。
    register("file/import", "admin", _model("FileImport", path=(str, ...), collections=(Names | None, None)), import_file)
    register("file/reindex", "write", _model("Reindex", file_id=(Id, ...), source=(Literal["original", "working"] | None, None), confirmed=(bool, False), request_key=(Key, None), dry_run=(bool, False)), reindex)
    register("file/delete", "write", _model("DeleteFiles", file_ids=(Ids, ...), **confirm), maintenance.delete_files)
    register("file/properties", "write", _model("Properties", file_id=(Id, ...), properties=(dict[str, Any], ...)), lambda file_id, properties: {"file_id": file_id, "updated": metadata_service.save_file_metadata(file_id, properties)})
    register("chunk/list", "read", _model("ChunkList", file_id=(Id, ...), page=(Id, 1), page_size=(Limit, 50)), files.get_chunks_paginated)
    register("chunk/get", "read", chunk_id_model, maintenance.chunk_info)
    register("chunk/add", "write", _model("ChunkAdd", file_id=(Id, ...), doc_title=(Text, ...), chunk_text=(Text, ...), request_key=(Key, None)), chunk_add)
    register("chunk/batch-add", "write", _model("ChunkBatch", file_id=(Id, ...), chunks=(Annotated[list[dict[str, str]], Field(min_length=1, max_length=50)], ...), request_key=(Key, None)), chunks.create_chunks)
    register("chunk/update", "write", _model("ChunkUpdate", chunk_id=(Id, ...), doc_title=(Text | None, None), chunk_text=(Text | None, None), request_key=(Key, None)), chunk_update)
    register("chunk/delete", "write", _model("DeleteChunks", chunk_ids=(Ids, ...), **confirm), maintenance.delete_chunks)
    register("chunk/images", "read", chunk_id_model, images)
    register("collection/list", "read", _model("CollectionList", **pagination), list_collections)
    register("collection/create", "write", _model("CollectionCreate", name=(str, ...), description=(str | None, None)), collections.create_collection)
    register("collection/rename", "write", _model("CollectionRename", collection_id=(Id, ...), name=(str, ...)), collections.rename_collection)
    register("collection/delete", "write", _model("CollectionDelete", collection_id=(Id, ...), **confirm), maintenance.delete_collection)
    register("collection/set", "write", _model("CollectionSet", file_ids=(Ids, ...), collection_names=(Names, ...)), lambda file_ids, collection_names: collections.assign_collections(file_ids, collection_names))
    register("task/list", "read", _model("TaskList", **pagination, status=(Literal["pending", "processing", "completed", "failed", "cancelled"] | None, None), request_key=(Key, None)), list_tasks)
    register("task/get", "read", task_id_model, lambda task_id: public_task(tasks.get_task(task_id)))
    register("task/query", "read", _model("TaskQuery", task_ids=(Ids, ...)), query_tasks)
    register("task/cancel", "write", task_id_model, lambda task_id: public_task(tasks.cancel_task(task_id)))
    register("task/retry", "write", task_id_model, retry)
    register("search", "read", _model("Search", query=(str, ...), file_ids=(Ids | None, None), collections=(Names | None, None), limit=(Annotated[int, Field(ge=1, le=50)], 20), diagnostics=(bool, False)), search)
    register("config/show", "admin", empty, config_service.show_config)
    register("config/update", "admin", _model("ConfigPatch", patch=(dict[str, Any], ...)), config_service.update_config)
    register("config/test", "admin", _model("ConfigTest", component=(Literal["embedding", "ocr", "office", "webdav"], ...)), config_service.test_config)
    register("sync/status", "admin", empty, maintenance.sync_status)
    register("sync/run", "admin", _model("SyncRun", confirmed=(bool, False)), maintenance.run_sync_job)
    register("logs", "admin", _model("Logs", level=(str | None, None), limit=(Limit, 100)), get_log_buffer)
    register("mcp-config", "admin", _model("McpConfig", service=(Literal["retrieval", "index"], "retrieval"), include_secrets=(bool, False)), maintenance.mcp_config)

    @app.post("/api/v1/shutdown")
    async def shutdown(request: Request):
        _require_role(request, "admin")
        active = await run_sync(tasks.get_active_tasks)
        runtime.request_shutdown()
        return envelope({"active_tasks": len(active), "status": "stopping"},
                        "服务正在安全停止：未领取任务保留，处理中任务将标记为中断")

    @app.post("/api/v1/window/open")
    @app.post("/api/window/open")
    async def open_window(request: Request):
        _require_role(request, "admin")
        if not runtime.with_gui:
            raise BusinessError("GUI_DISABLED", "当前服务未启用 GUI；请在任务结束后启用 GUI 重启，不会自动重启服务")
        token = runtime.bootstrap_tokens.issue()
        await run_sync(runtime.open_window, f"http://127.0.0.1:{runtime.port}/bootstrap?token={token}")
        return envelope(message="已请求打开管理界面；本次免登录，手动访问时用户名 piece，密码见 config.json 的 api.admin_key")

    @app.get("/api/v1/file/{file_id}/content")
    @app.get("/gui/file/{file_id}/content")
    async def content(file_id: int, format: Literal["markdown", "original"] = "markdown", include_resources: bool = False):
        snapshot = await run_sync(files.export_snapshot, file_id, format, include_resources)
        return ExportResponse(snapshot)

    @app.get("/api/v1/file/{file_id}/page/{page_number}")
    async def page(request: Request, file_id: int, page_number: int):
        path = await run_sync(maintenance.source_page, file_id, page_number)
        return FileResponse(path, filename=path.name, media_type="image/png")

    @app.get("/api/v1/chunk/{chunk_id}/image/{index}")
    async def image(request: Request, chunk_id: int, index: int):
        items, _ = await run_sync(maintenance.chunk_images, chunk_id)
        if not 0 <= index < len(items):
            raise BusinessError("NOT_FOUND", "图片不存在")
        path = Path(items[index]["path"])
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    return app
