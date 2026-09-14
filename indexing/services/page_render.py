"""
PDF 原页渲染模块

职责:
- 把原件对应的 PDF 指定页按需渲染为图片
- 渲染结果缓存到 data/cache/pages/，避免重复渲染

背景:
PaddleOCR 解析会把插图落到 working/<文档名>/ 下，正文以相对路径引用；
而 VLM 和本地文本层解析都不落图片，这类文档的切片正文里没有任何插图，
UI 和 MCP 都拿不到图。原始 PDF 一直保留在 originals/ 下，可以按需渲染出
该页原貌补上这个缺口。docx/pptx 则渲染 Office 转换产出的 PDF。

缓存放在 data/cache/ 而非 data/files/ 下，因为它可由原件重新生成，
不需要占用云同步的带宽。
"""

import hashlib
import logging
import re
import tempfile
import threading
from pathlib import Path
from typing import Optional

from .parser_helper import run_preview_renderer

from ..settings import get_settings
from .chunking.utils import HEADING_SEPARATOR
from .office_convert import PDF_ROUTED_FORMATS, convert_to_pdf

logger = logging.getLogger(__name__)
_render_lock = threading.Lock()

# 渲染精度：MCP 把原页编码后交给模型，分辨率越高 token 成本越高，
# A4 在 100 DPI 下约 827x1170，正文可读即可
RENDER_DPI = 100

# UI 对比栏占半个窗口，且需要经得起放大，用更高精度单独渲染一份
VIEW_DPI = 150

# PageChunker 写入的页码层级（heading_path 形如 "文件名 / 第12页 / 第2部分"）
_PAGE_SEGMENT = re.compile(r"第(\d+)页")

# 能按页回看原件的格式：PDF 用原件，Word/PPT 用 Office 转换产出的 PDF
PAGE_VIEWABLE_FORMATS = {".pdf"} | PDF_ROUTED_FORMATS


def get_page_cache_dir() -> Path:
    """获取原页渲染缓存目录"""
    cache_dir = get_settings().get_data_path() / "cache" / "pages"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def page_number_from_heading(heading_path: Optional[str]) -> Optional[int]:
    """从切片的标题路径中取出 PDF 页码，取不到时返回 None。

    heading_path 首段是文件基础名，可能自带"第N页"字样，因此从第二段起找。
    """
    if not heading_path:
        return None

    for segment in heading_path.split(HEADING_SEPARATOR)[1:]:
        match = _PAGE_SEGMENT.fullmatch(segment.strip())
        if match:
            return int(match.group(1))
    return None


def resolve_source_pdf(
    original_file_path: Optional[str], original_file_type: Optional[str]
) -> Optional[Path]:
    """把原件解析成可按页渲染的 PDF，无法按页回看时返回 None。

    PDF 直接用原件；docx/pptx 用 Office 转换缓存里的产物——绝不能拿原件去渲染，
    PyMuPDF 虽然打得开 pptx，解析出的页数却和实际幻灯片对不上（实测 16 页的
    PPT 只认出 7 页），渲染出来必然张冠李戴。

    缓存缺失时会触发一次真实转换（数秒到数十秒），因此调用方应放在线程里。
    """
    if not original_file_path:
        return None

    source = Path(original_file_path)
    suffix = f".{original_file_type}".lower() if original_file_type else source.suffix.lower()

    if suffix == ".pdf":
        return source if source.is_file() else None

    if suffix in PDF_ROUTED_FORMATS:
        return convert_to_pdf(source)

    return None


def render_pdf_page(
    pdf_path: Path, page_number: int, dpi: int = RENDER_DPI
) -> Optional[Path]:
    """渲染 PDF 的指定页为图片，返回缓存文件路径；失败时返回 None。

    Args:
        pdf_path: 原始 PDF 路径
        page_number: 页码（从 1 开始）
        dpi: 渲染精度，UI 展示传 VIEW_DPI，MCP 返回用默认值

    Returns:
        渲染结果的绝对路径，页码越界或渲染失败时为 None
    """
    if page_number < 1 or not pdf_path.is_file():
        return None

    try:
        # 原件被替换后缓存应失效，故把修改时间纳入缓存键
        stat = pdf_path.stat()
        key = f"{pdf_path}|{stat.st_mtime_ns}|{page_number}|{dpi}"
    except OSError:
        return None

    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    cache_path = get_page_cache_dir() / f"page-{digest}.png"
    if cache_path.is_file():
        return cache_path

    # 预览进程本就串行；持锁后复查缓存，合并同时到达的同页请求。
    with _render_lock:
        if cache_path.is_file():
            return cache_path
        try:
            image = run_preview_renderer(pdf_path, page_number, dpi)
            if image is None:
                return None
            # UI 与 MCP 可能同时读取一页，不能发布半张 PNG。
            with tempfile.NamedTemporaryFile(dir=cache_path.parent, suffix=".png", delete=False) as temp:
                temp_path = Path(temp.name)
            try:
                temp_path.write_bytes(image)
                temp_path.replace(cache_path)
            finally:
                temp_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning(
                "[原页渲染] 失败: %s 第%s页 - %s", pdf_path.name, page_number, exc
            )
            return None

    logger.info("[原页渲染] %s 第%s页 -> %s", pdf_path.name, page_number, cache_path.name)
    return cache_path
