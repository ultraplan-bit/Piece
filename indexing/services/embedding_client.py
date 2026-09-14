"""
Embedding 客户端模块

职责:
- 统一管理 OpenAIEmbeddings 实例（单例模式）
- 自动复用 HTTP 连接，避免重复初始化
- 支持配置变更时刷新实例
"""

import threading
from typing import Optional
import logging

from langchain_openai import OpenAIEmbeddings

from ..settings import get_embedding_config

logger = logging.getLogger(__name__)

# 全局单例
_embeddings_instance: Optional[OpenAIEmbeddings] = None
_embeddings_lock = threading.Lock()
_current_config_hash: Optional[str] = None


def _get_config_hash(config: dict) -> str:
    """计算配置的哈希值，用于检测配置变更"""
    return f"{config.get('base_url')}|{config.get('api_key')}|{config.get('model')}"


def get_embeddings_model(force_refresh: bool = False) -> OpenAIEmbeddings:
    """
    获取 OpenAIEmbeddings 实例（单例模式）

    特点:
    - 首次调用时创建实例
    - 后续调用复用同一实例（复用 HTTP 连接）
    - 配置变更时自动刷新实例

    Args:
        force_refresh: 是否强制刷新实例（用于测试连接等场景）

    Returns:
        OpenAIEmbeddings 实例
    """
    global _embeddings_instance, _current_config_hash

    config = get_embedding_config()
    config_hash = _get_config_hash(config)

    with _embeddings_lock:
        # 检查是否需要刷新实例
        need_refresh = (
            force_refresh
            or _embeddings_instance is None
            or _current_config_hash != config_hash
        )

        if need_refresh:
            if _embeddings_instance is not None:
                logger.info("[EmbeddingClient] 配置已变更，刷新实例")

            _embeddings_instance = OpenAIEmbeddings(**config)
            _current_config_hash = config_hash
            logger.info(
                f"[EmbeddingClient] 实例已创建: model={config.get('model')}, "
                f"base_url={config.get('base_url')}"
            )

        return _embeddings_instance


def get_embeddings_model_with_config(
    base_url: str,
    api_key: str,
    model: str,
) -> OpenAIEmbeddings:
    """
    使用指定配置创建 OpenAIEmbeddings 实例（不缓存）

    用于测试连接等需要临时配置的场景

    Args:
        base_url: API 基础 URL
        api_key: API 密钥
        model: 模型名称

    Returns:
        新创建的 OpenAIEmbeddings 实例
    """
    return OpenAIEmbeddings(
        base_url=base_url,
        api_key=api_key,
        model=model,
        check_embedding_ctx_length=False,
    )


def refresh_embeddings_instance() -> None:
    """
    强制刷新 embedding 实例

    用于配置保存后立即刷新
    """
    global _embeddings_instance, _current_config_hash

    with _embeddings_lock:
        _embeddings_instance = None
        _current_config_hash = None
        logger.info("[EmbeddingClient] 实例已清除，下次调用时将重新创建")
