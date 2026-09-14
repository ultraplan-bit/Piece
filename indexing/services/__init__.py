"""Indexing 服务层模块。"""

import importlib

__all__ = [
    "file_service",
    "task_service",
    "chunk_service",
    "collection_service",
    "metadata_service",
    "converter",
    "chunking",
    "processor",
    "TaskProcessor",
    "get_embeddings_model",
    "get_embeddings_model_with_config",
    "refresh_embeddings_instance",
]


_MODULES = {
    "file_service",
    "task_service",
    "chunk_service",
    "collection_service",
    "metadata_service",
    "converter",
    "chunking",
}
_EMBEDDING_EXPORTS = {
    "get_embeddings_model",
    "get_embeddings_model_with_config",
    "refresh_embeddings_instance",
}


def __getattr__(name):
    """按需加载较重的索引依赖，解析 helper 不加载业务服务。"""
    if name in _MODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    if name in _EMBEDDING_EXPORTS:
        module = importlib.import_module(f"{__name__}.embedding_client")
        value = getattr(module, name)
        globals()[name] = value
        return value
    if name == "processor":
        from .processor import processor
        return processor
    if name == "TaskProcessor":
        from .processor import TaskProcessor
        return TaskProcessor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
