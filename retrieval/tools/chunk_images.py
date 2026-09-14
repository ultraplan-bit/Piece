"""
切片插图提取：把切片正文中的相对图片引用解析为工作目录下的真实文件路径

OCR 解析产出的工作文件把插图落在 working/<文档名>/ 下，正文以相对路径引用
（HTML 的 src="文档名/xxx.jpg" 或 Markdown 的 ![](文档名/xxx.jpg)），
本模块负责把这些引用还原成绝对路径，供 MCP 工具编码为 base64 返回给 AI。

VLM 和本地文本层解析都不落图片文件，正文里的引用为空，这类 PDF 回退到
从原件按需渲染出该切片对应的原页。
"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List

from indexing.services.page_render import (
    page_number_from_heading,
    render_pdf_page,
    resolve_source_pdf,
)

# 排除协议和根前缀的规则与 app/ui/components.py 一致；这里只取图片引用，
# 故 Markdown 侧要求 ![...] 前缀，不匹配普通链接
_RELATIVE_IMG_SRC = re.compile(r'src="(?!https?://|/|data:)([^"]+)"')
_RELATIVE_IMG_MD = re.compile(r'!\[[^\]]*\]\((?!https?://|/|data:)([^)\s]+)\)')

# 页眉 logo、分隔线等装饰性小图对回答无价值，按尺寸滤掉
MIN_IMAGE_EDGE = 120

# 单次 get-docs 调用返回的图片总数上限（3 篇图文文档约 4-6 张）
MAX_IMAGES_PER_CALL = 6

logger = logging.getLogger(__name__)


def _iter_refs(chunk_text: str) -> List[str]:
    """取出去重后的相对图片引用（HTML 形式在前，Markdown 形式在后）。

    单个切片通常只用其中一种形式，故不额外按正文位置归并排序。
    """
    refs = _RELATIVE_IMG_SRC.findall(chunk_text) + _RELATIVE_IMG_MD.findall(chunk_text)
    seen = set()
    ordered = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            ordered.append(ref)
    return ordered


def _resolve(base_dir: Path, ref: str) -> Path | None:
    """把引用解析为绝对路径，越出工作目录或文件不存在时返回 None。

    引用来自文档解析结果，不应能指向工作目录之外，故显式做一次包含性校验，
    避免构造出 "../../" 之类的引用读到任意文件。
    """
    try:
        path = (base_dir / ref).resolve()
        path.relative_to(base_dir.resolve())
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def _is_large_enough(path: Path) -> bool:
    """宽或高低于阈值的装饰性小图返回 False；无法读取尺寸时保守放行。"""
    try:
        from PIL import Image as PILImage

        with PILImage.open(path) as img:
            width, height = img.size
    except Exception:
        logger.warning("[Images] 无法读取图片尺寸，按保留处理: %s", path)
        return True
    return width >= MIN_IMAGE_EDGE and height >= MIN_IMAGE_EDGE


def _render_source_page(doc: Dict[str, Any]) -> tuple[Path, str] | None:
    """把切片对应的原页渲染出来，返回 (图片路径, 引用说明)。

    仅对 PDF 和转得出 PDF 的 Word/PPT 有效：其它格式的插图在索引时已落盘，
    正文没有引用即表示确实没有图。
    """
    page_number = page_number_from_heading(doc.get("heading_path"))
    if page_number is None:
        return None

    pdf_path = resolve_source_pdf(doc.get("original_file_path"), None)
    if pdf_path is None:
        return None

    path = render_pdf_page(pdf_path, page_number)
    if path is None:
        return None
    return path, f"原文第{page_number}页"


def collect_chunk_images(
    documents: Dict[str, Dict[str, Any]],
    max_images: int = MAX_IMAGES_PER_CALL,
    image_offset: int = 0,
) -> tuple[List[Dict[str, str]], int]:
    """按固定顺序分页收集若干文档正文中的插图。

    正文没有可用插图时，PDF 会回退到渲染原页——VLM 和本地文本层解析都不落
    图片文件，这类文档只能从原件取图。

    Args:
        documents: doc_title 到文档详情的映射，需包含 chunk_text 和 file_path
        max_images: 本批返回图片数量上限，必须大于 0
        image_offset: 跳过的有效图片数量，跨文档累计，从 0 开始

    Returns:
        (本批图片列表, 本批之后剩余的图片数量)。列表元素为
        {"doc_title": ..., "ref": 正文中的相对引用, "path": 绝对路径字符串}
    """
    if max_images < 1:
        raise ValueError("max_images must be at least 1")
    if image_offset < 0:
        raise ValueError("image_offset must be at least 0")

    images: List[Dict[str, str]] = []

    for doc_title, doc in documents.items():
        file_path = doc.get("file_path")
        if not file_path:
            continue
        # 图片相对工作文件所在目录存放（working/<文档名>/xxx.jpg）
        base_dir = Path(file_path).parent

        collected = 0
        for ref in _iter_refs(doc.get("chunk_text") or ""):
            path = _resolve(base_dir, ref)
            if path is None or not _is_large_enough(path):
                continue
            images.append(
                {"doc_title": doc_title, "ref": ref, "path": str(path)}
            )
            collected += 1

        if collected:
            continue

        rendered = _render_source_page(doc)
        if rendered is None:
            continue
        page_path, ref = rendered
        images.append(
            {"doc_title": doc_title, "ref": ref, "path": str(page_path)}
        )

    # 先确定有效图片与原页回退，再分页，避免被跳过的插图误触发原页回退。
    end = image_offset + max_images
    return images[image_offset:end], max(0, len(images) - end)
