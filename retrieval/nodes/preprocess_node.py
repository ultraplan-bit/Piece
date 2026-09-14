"""
查询预处理节点：文本清洗、格式验证、分词、文件名解析、状态初始化
"""
import jieba
import re
from typing import List, Optional
from .state import State
from ..config import config
from indexing.database import get_db_cursor


def tokenize_query(query: str) -> list[str]:
    """
    使用jieba分词提取关键词

    Args:
        query: 用户输入的查询问题

    Returns:
        分词后的token列表
    """
    # 使用jieba搜索引擎模式分词（会额外切分长词）
    tokens = list(jieba.cut_for_search(query))

    # 过滤停用词和单字符
    tokens = [
        t.strip()
        for t in tokens
        if len(t.strip()) > 1 and t.strip() not in config.stopwords
    ]

    # 清理FTS5特殊字符（移除会导致语法错误的字符）
    fts5_special_chars = r'[."*(){}\[\]:^$|&!+\-=<>~/]'
    tokens = [re.sub(fts5_special_chars, '', t) for t in tokens]

    # 过滤清理后为空或单字符的token
    tokens = [t for t in tokens if len(t) > 1]

    # 去重并按长度降序排序（优先匹配长词）
    tokens = sorted(list(set(tokens)), key=len, reverse=True)

    return tokens


def resolve_filenames_to_ids(filenames: Optional[List[str]]) -> Optional[List[int]]:
    """
    将文件名列表解析为文件ID列表（模糊匹配）

    Args:
        filenames: 文件名列表（可选）

    Returns:
        匹配到的文件ID列表，如果没有匹配或参数为空则返回 None
    """
    if not filenames:
        return None

    try:
        with get_db_cursor() as cursor:
            file_ids = []
            for filename in filenames:
                # 模糊匹配：filename 包含用户输入的关键词
                cursor.execute(
                    "SELECT id FROM files WHERE filename LIKE ? AND status = 'indexed'",
                    (f"%{filename}%",)
                )
                rows = cursor.fetchall()
                file_ids.extend([row[0] for row in rows])

            # 去重
            file_ids = list(set(file_ids))
            return file_ids

    except Exception:
        raise


def resolve_collections_to_ids(collections: Optional[List[str]]) -> Optional[List[int]]:
    """
    将集合名列表解析为文件ID列表（模糊匹配）

    Args:
        collections: 集合名列表（可选）

    Returns:
        匹配到的文件ID列表，如果没有匹配或参数为空则返回 None
    """
    if not collections:
        return None

    from indexing.services.collection_service import resolve_names_to_file_ids
    return resolve_names_to_file_ids(collections)


def preprocess_node(state: State) -> State:
    """
    查询预处理节点

    功能：
    1. 文本清洗（去除多余空白）
    2. 格式验证（检查是否为空）
    3. jieba分词
    4. 文件名/集合解析（可选，模糊匹配转换为 file_ids）
    5. 状态初始化

    Args:
        state: 当前状态

    Returns:
        更新后的状态
    """
    query = state["query"]

    # 文本清洗：去除多余空白
    cleaned_query = " ".join(query.strip().split())

    # 格式验证
    if not cleaned_query:
        return {
            "error": "查询文本不能为空",
        }

    # jieba分词
    tokens = tokenize_query(cleaned_query)

    if not tokens:
        return {
            "error": "未提取到有效关键词",
            "cleaned_query": cleaned_query,
        }

    # 显式 ID 范围优先；未匹配和空交集不能扩大为全库检索。
    file_ids = state.get("file_ids")
    if file_ids is None:
        file_ids = resolve_filenames_to_ids(state.get("filenames"))
    collection_file_ids = resolve_collections_to_ids(state.get("collections"))
    if file_ids is not None and collection_file_ids is not None:
        file_ids = sorted(set(file_ids) & set(collection_file_ids))
    elif collection_file_ids is not None:
        file_ids = collection_file_ids

    # 更新状态
    return {
        "cleaned_query": cleaned_query,
        "tokens": tokens,
        "file_ids": file_ids,
    }
