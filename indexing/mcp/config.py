"""
索引 MCP 配置模块
"""


def get_mcp_port(retrieval_port: int | None = None) -> int:
    """
    获取索引 MCP 服务端口

    优先级：环境变量 PIECE_INDEX_MCP_PORT > 检索 MCP 端口 + 1。
    可传入尚未保存的 retrieval_port，供设置页预览。
    """
    from indexing.settings import get_index_mcp_port
    return get_index_mcp_port(retrieval_port)
