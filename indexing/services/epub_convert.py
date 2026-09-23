"""EPUB 转 Markdown：按 spine 逐章转换，章名取自目录，书籍元数据写成 frontmatter。

不直接用 MarkItDown 的 EpubConverter：它把元数据以粗体行混进正文首块、整本书拼成
一段、插图不落盘。这里每个 spine 条目对应一个 ``## 章名`` 段落，章内标题整体降两级，
正好交给 HeadingChunker 按章切分；插图解压到工作文件同名目录，引用形态与 Office /
OCR 解析产出一致。
"""

import posixpath
import re
import zipfile
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import unquote, urldefrag

from bs4 import BeautifulSoup
from defusedxml import ElementTree as ET

from .converter import store_image
from .html_convert import html_to_markdown
from .metadata_service import render_frontmatter

_NS = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
}

# 能被 UI 渲染和 MCP 返回的位图；SVG 既读不出尺寸也不便回传，直接剥离
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HEADING = re.compile(r"^(#{1,6})(\s+)(.*?)\s*$")
_FENCE = re.compile(r"^(`{3,}|~{3,})")

# 章内标题降级幅度：章名占 ##，章内 h1 变 ###，超过六级的全部压到 ######
_DEMOTE_LEVELS = 2
_MAX_DESCRIPTION = 500


def _find(element, tag: str):
    """OPF 元素优先按命名空间查找，兼容个别漏写命名空间的电子书。"""
    found = element.find(f"opf:{tag}", _NS)
    return found if found is not None else element.find(tag)


def _iter(element, tag: str):
    found = element.findall(f"opf:{tag}", _NS)
    return found if found else element.findall(tag)


def _join(base: str, href: str) -> str:
    """把相对 href 解析为 zip 内的规范路径。"""
    return posixpath.normpath(posixpath.join(base, unquote(urldefrag(href)[0])))


def _rootfile(archive: zipfile.ZipFile, names: set) -> str:
    if "META-INF/container.xml" in names:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = container.find(".//container:rootfile", _NS)
        if rootfile is None:
            rootfile = container.find(".//rootfile")
        full_path = str(rootfile.get("full-path") or "") if rootfile is not None else ""
        if full_path in names:
            return full_path
    for name in sorted(names):
        if name.lower().endswith(".opf"):
            return name
    raise ValueError("EPUB 缺少 OPF 包文件，无法识别章节")


def _metadata(opf) -> Dict[str, object]:
    meta = _find(opf, "metadata")
    if meta is None:
        return {}

    def texts(tag: str) -> List[str]:
        values = []
        for node in meta.iterfind(f"dc:{tag}", _NS):
            text = " ".join((node.text or "").split())
            if text:
                values.append(text)
        return values

    description = " ".join(texts("description"))
    if description:
        description = BeautifulSoup(description, "html.parser").get_text(" ", strip=True)[:_MAX_DESCRIPTION]
    candidates = {
        "title": " ".join(texts("title")[:1]),
        "authors": texts("creator"),
        "publisher": " ".join(texts("publisher")[:1]),
        "date": " ".join(texts("date")[:1]),
        "language": " ".join(texts("language")[:1]),
        "identifier": " ".join(texts("identifier")[:1]),
        "description": description,
    }
    return {key: value for key, value in candidates.items() if value}


def _toc_labels(archive: zipfile.ZipFile, names: set, manifest: Dict[str, tuple], spine) -> Dict[str, str]:
    """目录条目 → {章节文件路径: 章名}；同一文件多个锚点只取第一个。"""
    labels: Dict[str, str] = {}

    def record(base: str, href: Optional[str], label: str) -> None:
        label = " ".join(label.split())
        if href and label:
            labels.setdefault(_join(base, href), label)

    nav = next((entry for entry in manifest.values() if "nav" in entry[2].split()), None)
    if nav and nav[0] in names:
        soup = BeautifulSoup(archive.read(nav[0]), "html.parser")
        toc = soup.find("nav", attrs={"epub:type": "toc"}) or soup.find("nav")
        for anchor in (toc or soup).find_all("a", href=True):
            record(posixpath.dirname(nav[0]), str(anchor.get("href") or ""), anchor.get_text(" ", strip=True))
        if labels:
            return labels

    ncx_id = spine.get("toc") if spine is not None else None
    ncx = manifest.get(ncx_id) if ncx_id else None
    if ncx is None:
        ncx = next((entry for entry in manifest.values() if entry[1] == "application/x-dtbncx+xml"), None)
    if ncx and ncx[0] in names:
        root = ET.fromstring(archive.read(ncx[0]))
        for point in root.iter(f"{{{_NS['ncx']}}}navPoint"):
            text = point.find("ncx:navLabel/ncx:text", _NS)
            content = point.find("ncx:content", _NS)
            if text is not None and content is not None:
                record(posixpath.dirname(ncx[0]), content.get("src"), text.text or "")
    return labels


def _first_heading(markdown: str) -> Optional[str]:
    for line in markdown.splitlines():
        if line.strip():
            match = _HEADING.match(line)
            return match.group(3).strip("# ").strip() if match else None
    return None


def _demote_headings(markdown: str, title: str) -> str:
    """章内标题整体降级；与章名重复的首行标题去掉。代码块内的 # 行保持原样。"""
    lines = markdown.splitlines()
    result: List[str] = []
    fence: Optional[str] = None
    seen_text = False
    for line in lines:
        fence_match = _FENCE.match(line.strip())
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker
            elif line.strip().startswith(fence):
                fence = None
            result.append(line)
            seen_text = True
            continue
        heading = _HEADING.match(line) if fence is None else None
        if heading:
            text = heading.group(3).strip("# ").strip()
            if not seen_text and " ".join(text.split()).casefold() == " ".join(title.split()).casefold():
                seen_text = True
                continue
            level = min(len(heading.group(1)) + _DEMOTE_LEVELS, 6)
            line = f"{'#' * level}{heading.group(2)}{heading.group(3)}"
        if line.strip():
            seen_text = True
        result.append(line)
    return "\n".join(result)


def _rewrite_images(markdown: str, item_path: str, archive: zipfile.ZipFile, names: set,
                    image_dir: Optional[Path]) -> str:
    """把章节内的相对图片引用解压到 image_dir，改写为 <目录名>/<文件名>；取不到的引用剥离。"""

    def replace(match: re.Match) -> str:
        alt, src = match.group(1), match.group(2)
        if src.startswith(("http://", "https://")):
            return match.group(0)
        target = _join(posixpath.dirname(item_path), src) if not src.startswith("data:") else ""
        suffix = posixpath.splitext(target)[1].lower()
        if image_dir is None or target not in names or suffix not in _IMAGE_SUFFIXES:
            return f"![{alt}]"
        filename = store_image(image_dir, archive.read(target), suffix.lstrip("."))
        return f"![{alt}]({image_dir.name}/{filename})"

    return _MD_IMAGE.sub(replace, markdown)


def convert_epub(file_path: Path, image_dir: Optional[Path] = None) -> str:
    """把 EPUB 转成带 frontmatter 的 Markdown，每个 spine 章节一个 ``##`` 段。

    Args:
        file_path: EPUB 路径
        image_dir: 插图落盘目录（约定为工作文件同名目录）；None 时剥离图片引用

    Raises:
        ValueError: 加密（DRM）、缺少包文件或没有可读章节
    """
    with zipfile.ZipFile(file_path) as archive:
        names = set(archive.namelist())
        if "META-INF/encryption.xml" in names:
            raise ValueError("EPUB 已加密或受 DRM 保护，无法导入")
        opf_path = _rootfile(archive, names)
        opf = ET.fromstring(archive.read(opf_path))
        base = posixpath.dirname(opf_path)

        manifest: Dict[str, tuple] = {}
        manifest_element = _find(opf, "manifest")
        for item in _iter(manifest_element, "item") if manifest_element is not None else []:
            item_id, href = item.get("id") or "", item.get("href") or ""
            if item_id and href:
                manifest[item_id] = (_join(base, href), item.get("media-type") or "", item.get("properties") or "")
        spine = _find(opf, "spine")
        chapters_paths: List[str] = []
        for ref in _iter(spine, "itemref") if spine is not None else []:
            idref = ref.get("idref") or ""
            if idref in manifest and ref.get("linear", "yes") != "no":
                chapters_paths.append(manifest[idref][0])
        labels = _toc_labels(archive, names, manifest, spine)
        saved_metadata = _metadata(opf)

        chapters: List[str] = []
        for index, item_path in enumerate(chapters_paths, start=1):
            if item_path not in names:
                continue
            markdown, _ = html_to_markdown(archive.read(item_path), main_only=False)
            markdown = _rewrite_images(markdown, item_path, archive, names, image_dir)
            title = labels.get(item_path) or _first_heading(markdown) or f"第{index}节"
            markdown = _demote_headings(markdown, title).strip()
            if markdown:
                chapters.append(f"## {title}\n\n{markdown}")

    if not chapters:
        raise ValueError("EPUB 没有可读的章节内容")
    return render_frontmatter(saved_metadata, "\n\n".join(chapters) + "\n")
