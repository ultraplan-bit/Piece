"""
索引 MCP 配置模块

各服务端口统一由 indexing.settings.get_mcp_service_port 计算：
检索用基端口，索引/图谱/Wiki 由基端口加固定偏移派生（可被各自环境变量覆盖），
Zotero 用独立字段。这里只保留按服务名取端口与 MCP 服务名的便捷入口。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from indexing.settings import McpService

# 服务内部名 -> MCP 客户端里显示的服务名。
SERVICE_NAMES: dict["McpService", str] = {
    "retrieval": "piece-kb",
    "index": "piece-index",
    "graph": "piece-graph",
    "wiki": "piece-wiki",
    "zotero": "piece-zotero",
}

# 配置模板里的端口占位符，按 MCP 服务名索引。
PORT_PLACEHOLDERS: dict[str, str] = {
    "piece-kb": "{port}",
    "piece-index": "{index_port}",
    "piece-graph": "{graph_port}",
    "piece-wiki": "{wiki_port}",
    "piece-zotero": "{zotero_port}",
}


def get_mcp_port(retrieval_port: int | None = None) -> int:
    """索引 MCP 端口；保留旧名，等价于 get_mcp_service_port("index")。"""
    from indexing.settings import get_mcp_service_port
    return get_mcp_service_port("index", retrieval_port)


def get_service_ports(base_port: int | None = None) -> dict[str, int]:
    """全部 MCP 服务的监听端口，键是客户端显示名；base_port 用于设置页预览未保存的基端口。"""
    from indexing.settings import MCP_SERVICES, get_mcp_service_port
    return {SERVICE_NAMES[service]: get_mcp_service_port(service, base_port) for service in MCP_SERVICES}
