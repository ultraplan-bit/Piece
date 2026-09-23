"""HTML 转 Markdown：浏览器另存的网页与 EPUB 章节共用。

MarkItDown 的 HtmlConverter 只做"去 script/style、转 body"。另存的网页会把导航、
侧栏、页脚一起带进正文，污染首尾切片，因此这里先选出主内容容器再交给它转换。
网页的标题、作者、来源地址等元数据写成 YAML frontmatter，与 .md 导入走同一条
属性通道：索引时由 parse_frontmatter 剥离并写入 files.metadata。
"""

import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from bs4 import BeautifulSoup

from .metadata_service import render_frontmatter

# 主内容容器候选，按优先级：微信公众号正文、语义化 article/main、ARIA 主区域。
# 同一选择器命中多个时取文字最多的那个（评论区常常也是 article）
_MAIN_SELECTORS = ("#js_content", "article", "main", "[role=main]")

# 任何来源都不是正文的标签；网页再去掉导航、侧栏和表单，EPUB 章节里的 aside 常是脚注，要保留
_CHAPTER_NOISE_TAGS = ["script", "style", "noscript", "iframe", "svg"]
_PAGE_NOISE_TAGS = _CHAPTER_NOISE_TAGS + ["nav", "aside", "form", "button"]
# 回退到整个 body 时再去掉页眉页脚；主容器内的 header 可能就是文章标题区
_BODY_NOISE_TAGS = ["header", "footer"]

# 懒加载图片把真实地址放在这些属性里，src 只是占位符
_LAZY_SRC_ATTRIBUTES = ("data-src", "data-original", "data-lazy-src")

_H2_LINE = re.compile(r"^##\s+\S", re.M)


def _attr(tag, name: str) -> str:
    """取属性并压成单行字符串；bs4 对少数属性返回列表。"""
    value = tag.get(name) if tag is not None else None
    if isinstance(value, list):
        value = " ".join(value)
    return " ".join(str(value).split()) if value else ""


def _meta_content(soup: BeautifulSoup, *names: str) -> Optional[str]:
    """按顺序取第一个非空的 <meta property|name> 内容。"""
    for name in names:
        for attribute in ("property", "name"):
            content = _attr(soup.find("meta", attrs={attribute: name}), "content")
            if content:
                return content
    return None


def extract_metadata(soup: BeautifulSoup) -> Dict[str, Any]:
    """从 <head> 取出可作为出处的元数据，只保留有值的键。"""
    title = _meta_content(soup, "og:title")
    if not title and soup.title is not None:
        title = " ".join(soup.title.get_text().split())
    source_url = _attr(soup.find("link", rel="canonical"), "href")
    candidates = {
        "title": title,
        "author": _meta_content(soup, "author", "og:article:author", "article:author"),
        "source_url": source_url or _meta_content(soup, "og:url"),
        "site": _meta_content(soup, "og:site_name"),
        "published_at": _meta_content(soup, "article:published_time"),
        "description": _meta_content(soup, "description", "og:description"),
    }
    return {key: value for key, value in candidates.items() if value}


def _select_main(soup: BeautifulSoup):
    """返回 (容器, 是否回退到了 body)。"""
    for selector in _MAIN_SELECTORS:
        matches = [node for node in soup.select(selector) if node.get_text(strip=True)]
        if matches:
            return max(matches, key=lambda node: len(node.get_text())), False
    return soup.body or soup, True


def _promote_lazy_images(container) -> None:
    for img in container.find_all("img"):
        src = _attr(img, "src")
        lazy = next((_attr(img, name) for name in _LAZY_SRC_ATTRIBUTES if _attr(img, name)), "")
        if lazy and (not src or src.startswith("data:")):
            img["src"] = lazy


def html_to_markdown(
    html: bytes | str, *, main_only: bool = True, keep_data_uris: bool = False
) -> Tuple[str, Dict[str, Any]]:
    """把 HTML 转成 Markdown，返回 (正文, 元数据)。

    Args:
        html: 原始 HTML；bytes 时按 <meta charset> 自动识别编码
        main_only: 网页取主内容容器并把页面标题补成 ## 首行；EPUB 章节传 False，整个 body 都是正文
        keep_data_uris: 保留内嵌 data URI 图片，交由 extract_embedded_images 落盘
    """
    soup = BeautifulSoup(html, "html.parser")
    metadata = extract_metadata(soup)

    container, fell_back = _select_main(soup) if main_only else (soup.body or soup, True)
    noise = _PAGE_NOISE_TAGS + (_BODY_NOISE_TAGS if fell_back else []) if main_only else _CHAPTER_NOISE_TAGS
    for tag in container.find_all(noise):
        tag.decompose()
    _promote_lazy_images(container)

    from markitdown.converters import HtmlConverter

    markdown = HtmlConverter().convert_string(str(container), keep_data_uris=keep_data_uris).markdown.strip()

    if main_only and markdown and not _H2_LINE.search(markdown):
        # 没有 ## 小节的页面会整页按长度硬切、卡片以正文前几个字命名。
        # 页面自带 h1 就把它升成 ##，否则用页面标题补一行，让卡片以标题命名；
        # 已有 ## 小节的页面不动，避免多出一张只含标题的空卡
        if markdown.startswith("# "):
            markdown = "#" + markdown
        elif metadata.get("title"):
            markdown = f"## {metadata['title']}\n\n{markdown}"
    return markdown, metadata


def convert_html_file(file_path: Path, *, keep_data_uris: bool = False) -> str:
    """网页文件 → 带 frontmatter 的 Markdown；内嵌图片是否保留由调用方决定。"""
    markdown, metadata = html_to_markdown(file_path.read_bytes(), keep_data_uris=keep_data_uris)
    return render_frontmatter(metadata, markdown)
