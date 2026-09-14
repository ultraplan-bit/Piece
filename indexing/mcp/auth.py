"""
MCP Bearer Token 认证

piece-kb（检索）和 piece-index（索引）共用中间件实现，密钥按服务选择。
"""

import secrets

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext

from indexing.settings import McpService, get_mcp_api_key, is_mcp_auth_enabled


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
    """按服务选择密钥，认证配置在重启应用后生效。"""
    if is_mcp_auth_enabled(service):
        mcp.add_middleware(BearerAuthMiddleware(valid_token=get_mcp_api_key(service)))
