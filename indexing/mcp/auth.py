"""
MCP Bearer Token 认证

各 MCP 服务复用中间件实现，密钥按服务独立选择。
"""

import asyncio
import logging
import secrets
import time

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext

from indexing.settings import McpService, get_mcp_api_key, is_mcp_auth_enabled

logger = logging.getLogger(__name__)


class CallTimingMiddleware(Middleware):
    """Record MCP tool names and server-side duration without tool data."""

    def __init__(self, service: str):
        self.service = service

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        started = time.perf_counter()
        status, error_type = "ok", None
        try:
            result = await call_next(context)
            content = getattr(result, "structured_content", None)
            if isinstance(content, dict) and content.get("success") is False:
                status = "business_error"
            return result
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            status = "error"
            error_type = "unauthorized" if isinstance(exc, ToolError) and str(exc).startswith("Unauthorized:") else type(exc).__name__
            raise
        finally:
            try:
                logger.info("[MCP] service=%r tool=%r status=%s error_type=%s duration_ms=%.2f",
                            self.service, context.message.name, status, error_type or "-",
                            (time.perf_counter() - started) * 1000)
            except Exception:
                pass


class BearerAuthMiddleware(Middleware):
    """Bearer Token 认证中间件"""

    def __init__(self, valid_token: str):
        self.valid_token = valid_token

    def _verify_token(self) -> bool:
        """验证 Bearer Token"""
        headers = get_http_headers() or {}
        auth_header = headers.get("authorization", "")

        if not auth_header.startswith("Bearer "):
            return False

        token = auth_header[7:]  # 去掉 "Bearer " 前缀
        return secrets.compare_digest(token.encode("utf-8"), self.valid_token.encode("utf-8"))

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        if not self._verify_token():
            raise ToolError("Unauthorized: Invalid or missing API key")
        return await call_next(context)

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        if not self._verify_token():
            raise ToolError("Unauthorized: Invalid or missing API key")
        return await call_next(context)


def apply_bearer_auth(mcp, service: McpService) -> None:
    """按服务选择密钥，认证配置在重启应用后生效；调用计时始终启用。"""
    # FastMCP 逆序构建中间件链；先注册计时层，使它包住认证拒绝路径。
    mcp.add_middleware(CallTimingMiddleware(str(service)))
    if is_mcp_auth_enabled(service):
        mcp.add_middleware(BearerAuthMiddleware(valid_token=get_mcp_api_key(service)))
