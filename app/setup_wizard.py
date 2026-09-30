"""离线初始化向导：提示写 stderr，密钥隐藏输入，确认前不写任何配置。"""

import getpass
import sys
import warnings
from urllib.parse import urlsplit

from app.client import ClientError


def _cancelled():
    return ClientError("初始化已取消，配置未保存", code="CONFIG_CANCELLED")


def _readline(prompt):
    sys.stderr.write(prompt)
    sys.stderr.flush()
    line = sys.stdin.readline()
    if not line:
        raise _cancelled()
    return line.strip()


def _url(value):
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.port == 0
            or any(char.isspace() for char in value)):
        raise ValueError("服务地址必须是有效的 http:// 或 https:// URL")
    return value


def _dimension(value):
    try:
        number = int(value)
    except ValueError:
        raise ValueError("向量维度必须是 1–65536 之间的整数") from None
    if not 1 <= number <= 65536:
        raise ValueError("向量维度必须是 1–65536 之间的整数")
    return number


def _prompt(label, default, validate=None):
    while True:
        value = _readline(f"{label} [{default}]：") or str(default)
        try:
            if not value:
                raise ValueError("该字段不能为空")
            return validate(value) if validate is not None else value
        except ValueError as exc:
            print(f"输入无效：{exc}", file=sys.stderr)


def initialize_interactively():
    if not sys.stdin.isatty():
        raise ClientError("交互初始化需要终端；脚本请使用 config init 和 config update --offline --input",
                          code="INTERACTIVE_REQUIRED")
    from indexing.services.config_service import config_revision, get_saved_settings, initialize_config
    from indexing.settings import AppSettings

    revision = config_revision()
    current = AppSettings() if revision == "missing" else get_saved_settings()
    print("配置嵌入模型：回车保留当前值，密钥不会显示；确认前不保存，也不访问外部服务。", file=sys.stderr)
    print("已有知识库不能直接切换嵌入模型、服务地址或维度。", file=sys.stderr)
    try:
        embedding = {
            "base_url": _prompt("OpenAI 兼容服务地址", current.embedding.base_url, _url),
            "model": _prompt("嵌入模型名称", current.embedding.model),
            "vector_dim": _prompt("向量维度", current.embedding.vector_dim, _dimension),
        }
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("API 密钥（隐藏输入，回车保留）：", stream=sys.stderr).strip()
        if key:
            embedding["api_key"] = key
        if not key and not current.embedding.api_key:
            print("尚未填写嵌入 API 密钥；若服务需要认证，请使用前补全。", file=sys.stderr)
        answer = _readline("保存配置？输入 yes 确认，其他输入取消：")
        if answer.lower() not in {"y", "yes"}:
            raise _cancelled()
    except (EOFError, KeyboardInterrupt):
        raise _cancelled() from None
    except getpass.GetPassWarning:
        raise ClientError("当前终端无法隐藏密钥输入，配置未保存", code="INTERACTIVE_REQUIRED") from None
    result = initialize_config({"embedding": embedding}, expected_revision=revision)
    print("已保存。可用 piece start 启动核心服务，再用 piece config test embedding 显式测试连接。", file=sys.stderr)
    print("OCR、Office、WebDAV 等可用 config update 或管理界面继续配置。", file=sys.stderr)
    return result
