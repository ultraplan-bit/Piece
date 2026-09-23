"""
文件属性服务模块

职责:
- 解析 Markdown YAML frontmatter，转换为文件自定义属性
- 读写 files.metadata（JSON 字符串）

属性用于给检索结果补充出处信息（作者、来源、日期等），
并作为 UI 上的文件属性面板数据源。
"""

import json
import logging
import re
from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple

from ..repositories import FileRepository
from .task_service import serialized_mutation, ensure_file_idle
from .errors import BusinessError

logger = logging.getLogger(__name__)

_file_repo = FileRepository()

# YAML frontmatter：文件开头由 --- 包裹的块
_FRONTMATTER_PATTERN = re.compile(
    r"^\s*---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL
)

# 属性值过长时会挤占界面，也不适合作为出处信息返回给模型
MAX_VALUE_LENGTH = 500


def _to_plain(value: Any) -> Any:
    """把 YAML 解析结果转成可 JSON 序列化的简单值。"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple, set)):
        return [_to_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_plain(item) for key, item in value.items()}
    return str(value)


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """
    分离 Markdown 开头的 YAML frontmatter

    Args:
        content: Markdown 全文

    Returns:
        (属性字典, 去掉 frontmatter 后的正文)。没有 frontmatter 或解析失败时
        返回 ({}, 原文)——属性是可选增强，不能因为格式问题阻断索引。
    """
    if not content:
        return {}, content

    match = _FRONTMATTER_PATTERN.match(content)
    if not match:
        return {}, content

    try:
        import yaml

        parsed = yaml.safe_load(match.group(1))
    except Exception as exc:
        logger.warning("[Metadata] frontmatter 解析失败，按普通正文处理: %s", exc)
        return {}, content

    if not isinstance(parsed, dict):
        return {}, content

    metadata = {}
    for key, value in parsed.items():
        plain = _to_plain(value)
        if plain is None or plain == "" or plain == []:
            continue
        text = plain if isinstance(plain, (str, list)) else str(plain)
        if isinstance(text, str) and len(text) > MAX_VALUE_LENGTH:
            text = text[:MAX_VALUE_LENGTH]
        metadata[str(key)] = text

    return metadata, content[match.end():]


def render_frontmatter(metadata: Optional[Dict[str, Any]], body: str) -> str:
    """把属性写成正文开头的 YAML frontmatter，与 parse_frontmatter 互为逆操作。

    网页、EPUB 转换结果和只传正文的导入入口都靠它把出处属性随原件保存，
    重索引时属性能从原件重新解析出来，不依赖数据库里的那一份。
    字符串值压成单行，避免多行文本里出现 --- 破坏分隔符。
    """
    if not metadata:
        return body
    import yaml

    plain = {}
    for key, value in metadata.items():
        value = _to_plain(value)
        if isinstance(value, str):
            value = " ".join(value.split())
        if value is None or value == "" or value == []:
            continue
        plain[str(key)] = value
    if not plain:
        return body
    dumped = yaml.safe_dump(plain, allow_unicode=True, sort_keys=False, width=10000)
    return f"---\n{dumped}---\n\n{body.lstrip()}"


def get_file_metadata(file_id: int) -> Dict[str, Any]:
    """读取文件属性，无属性或解析失败时返回空字典。"""
    file_info = _file_repo.find_by_id(file_id)
    return decode_metadata(file_info.get("metadata") if file_info else None)


def decode_metadata(metadata_json: Optional[str]) -> Dict[str, Any]:
    """把库中的 JSON 字符串还原为属性字典。"""
    if not metadata_json:
        return {}
    try:
        data = json.loads(metadata_json)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# 属性总量上限：超过会挤占 UI 面板，也不适合作为出处信息整段回给模型
MAX_METADATA_LENGTH = 20000


def encode_metadata(metadata: Optional[Dict[str, Any]]) -> Optional[str]:
    """校验并序列化属性；None 或空字典返回 None，表示不写属性。"""
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise BusinessError("INVALID_INPUT", f"文件属性必须为不超过 {MAX_METADATA_LENGTH} 字符的 JSON 对象")
    if not metadata:
        return None
    try:
        encoded = json.dumps(metadata, ensure_ascii=False)
    except (TypeError, ValueError):
        raise BusinessError("INVALID_INPUT", "文件属性必须可序列化为 JSON") from None
    if len(encoded) > MAX_METADATA_LENGTH:
        raise BusinessError("INVALID_INPUT", f"文件属性必须为不超过 {MAX_METADATA_LENGTH} 字符的 JSON 对象")
    return encoded


@serialized_mutation
def save_file_metadata(file_id: int, metadata: Dict[str, Any]) -> bool:
    """
    覆盖式保存文件属性

    Args:
        file_id: 文件 ID
        metadata: 属性字典，空字典表示清空

    Returns:
        是否写入成功
    """
    if not _file_repo.exists(file_id):
        raise BusinessError("NOT_FOUND", "文件不存在")
    ensure_file_idle(file_id)
    if not isinstance(metadata, dict):
        raise BusinessError("INVALID_INPUT", f"文件属性必须为不超过 {MAX_METADATA_LENGTH} 字符的 JSON 对象")
    return _file_repo.update_metadata(file_id, encode_metadata(metadata))
