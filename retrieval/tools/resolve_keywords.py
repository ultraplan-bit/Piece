"""旧 Python 导入路径的薄兼容入口；调用方迁到 retrieval.service 后可移除。"""

from retrieval.service import build_graph, resolve_database_keywords

__all__ = ["build_graph", "resolve_database_keywords"]
