"""
国际化（i18n）模块

职责:
- 加载翻译文件
- 提供 t() 翻译函数
- 管理当前语言设置
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 模块目录
I18N_DIR = Path(__file__).parent
LOCALES_DIR = I18N_DIR / "locales"

# 支持的语言
SUPPORTED_LANGUAGES = {
    "zh": "中文",
    "en": "English",
}

# 默认语言
DEFAULT_LANGUAGE = "zh"

# 当前语言
_current_language: str = DEFAULT_LANGUAGE

# 翻译缓存
_translations: dict = {}


def _load_translations(lang: str) -> dict:
    """加载指定语言的翻译文件"""
    file_path = LOCALES_DIR / f"{lang}.json"
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"[i18n] 加载翻译文件失败 {lang}: {e}", exc_info=True)
            return {}
    return {}


def init(lang: Optional[str] = None):
    """
    初始化 i18n 模块

    Args:
        lang: 语言代码，如 'zh', 'en'。不指定则使用默认语言
    """
    global _current_language, _translations

    if lang and lang in SUPPORTED_LANGUAGES:
        _current_language = lang
    else:
        _current_language = DEFAULT_LANGUAGE

    _translations = _load_translations(_current_language)

    # 如果不是中文，加载中文作为 fallback
    if _current_language != "zh":
        _translations["_fallback"] = _load_translations("zh")


def set_language(lang: str):
    """切换语言"""
    init(lang)


def t(key: str, **kwargs) -> str:
    """
    获取翻译文本

    Args:
        key: 翻译键，支持点号分隔的嵌套键，如 'sidebar.files'
        **kwargs: 用于格式化的参数

    Returns:
        翻译后的文本，找不到则返回 key
    """
    # 支持嵌套键
    keys = key.split(".")
    value = _translations

    for k in keys:
        if isinstance(value, dict) and k in value:
            value = value[k]
        else:
            # 尝试 fallback
            fallback = _translations.get("_fallback", {})
            for k2 in keys:
                if isinstance(fallback, dict) and k2 in fallback:
                    fallback = fallback[k2]
                else:
                    return key  # 找不到翻译，返回 key
            value = fallback
            break

    if isinstance(value, str):
        # 支持参数替换
        if kwargs:
            try:
                return value.format(**kwargs)
            except KeyError:
                return value
        return value

    return key


# 模块加载时自动初始化
def _auto_init():
    """自动从配置加载语言设置"""
    try:
        from indexing.settings import get_settings
        settings = get_settings()
        lang = getattr(settings.appearance, 'language', DEFAULT_LANGUAGE)
        init(lang)
    except Exception:
        init(DEFAULT_LANGUAGE)


_auto_init()
