"""检索写入与查询共用的 jieba 分词和 FTS5 字面量。"""

import jieba


def tokenize_fts_text(text: str) -> str:
    """保留词频、顺序和单字；查询端再按检索策略过滤停用词。"""
    return " ".join(jieba.cut_for_search(text))


def quote_fts_term(term: str) -> str:
    """FTS 参数绑定不转义 MATCH 语法；双引号中的词项才是字面量。"""
    return '"' + term.replace('"', '""') + '"'
