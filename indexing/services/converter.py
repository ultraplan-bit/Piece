"""
文件格式转换模块

职责:
- 将各种格式文件转换为 Markdown
- PDF 使用 PyMuPDF Native 流式处理（内存安全）
- 支持多种文档格式（.md, .pdf, .docx, .pptx, .xlsx）
"""

import base64
import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Iterator, Optional, TypedDict

from .parser_helper import run_parser

from .chunking import ChunkerFactory


# 支持的文件格式（与分块策略保持单一真值）
SUPPORTED_FORMATS = set(ChunkerFactory.get_supported_extensions())

# PDF 处理限制；每批文本有界，helper 完成一批即退出，不把原生状态留在服务内。
MAX_PDF_PAGES = 3000
PDF_BATCH_PAGES = 40

# 内嵌图片的 mime 子类型到落盘扩展名的映射，未列出的按 png 存
_IMAGE_EXTENSIONS = {
    "jpeg": "jpg",
    "jpg": "jpg",
    "png": "png",
    "gif": "gif",
    "bmp": "bmp",
    "webp": "webp",
    "tiff": "tiff",
}

# keep_data_uris 打开后 MarkItDown 产出的内嵌图片引用；base64 正文不含 ")"，
# 故用非 ")" 匹配到结尾括号即可
_DATA_URI_IMG = re.compile(
    r"!\[([^\]]*)\]\(\s*data:image/([A-Za-z0-9.+-]+);base64,([^)]*?)\s*\)"
)

# 兜底清理：任何未能落盘的 data URI 都必须从正文剥离，否则 base64 会随切片
# 进入全文索引和嵌入请求
_ANY_DATA_URI_IMG = re.compile(r"!\[([^\]]*)\]\(\s*data:[^)]*\)")
_ANY_DATA_URI_SRC = re.compile(r'<img[^>]*src="data:[^"]*"[^>]*>')

# Unicode 数学字母数字符号（U+1D400–U+1D7FF）是 ASCII 字母数字的样式变体，
# Office 公式编辑器大量使用。原样保留会让「AHA」检索不到「𝑨𝑯𝑨」，因此只归一
# 化这一段，不做全量 NFKC——那会顺带把中文全角标点转成半角，改变原文排版
_MATH_ALNUM = re.compile(r"[\U0001D400-\U0001D7FF]+")

# 标题判定用的相对字号倍率。原先是 >16pt 就算标题的绝对阈值，对论文类 PDF
# 尚可，但 PPT 正文普遍在 18pt 以上，整页会被标成 ## 标题
_H2_RATIO = 1.4
_H3_RATIO = 1.15

# span 之间横向间隙超过字号的这个比例才补空格
_SPACE_GAP_RATIO = 0.2


class PdfPageData(TypedDict):
    """单页 PDF 转换结果。"""

    page_number: int
    total_pages: int
    page_text: str


def _normalize_math_alnum(text: str) -> str:
    """把数学字母数字符号还原成 ASCII，让公式能被检索到。"""
    if not _MATH_ALNUM.search(text):
        return text
    return _MATH_ALNUM.sub(
        lambda match: unicodedata.normalize("NFKC", match.group()), text
    )


def _line_to_text(line: dict) -> tuple[str, float]:
    """把一行的 span 拼成文本，并返回该行最大字号。

    span 的切分只反映字体/字号变化，不代表词间距：公式里每个字符往往自成
    一个 span，逐个补空格会把 𝑨𝑯𝑨 拆成「𝑨 𝑯 𝑨」。因此保留 span 原文，
    只在两个 span 有明显横向间隙时才补空格。
    """
    parts: list[str] = []
    max_font_size = 0.0
    prev_right: Optional[float] = None

    for span in line.get("spans", []):
        text = span.get("text", "")
        if not text:
            continue

        font_size = span.get("size", 12)
        max_font_size = max(max_font_size, font_size)
        bbox = span.get("bbox")

        if (
            parts
            and bbox is not None
            and prev_right is not None
            and not text[:1].isspace()
            and not parts[-1][-1:].isspace()
            and bbox[0] - prev_right > font_size * _SPACE_GAP_RATIO
        ):
            parts.append(" ")

        parts.append(text)
        if bbox is not None:
            prev_right = bbox[2]

    return _normalize_math_alnum("".join(parts)).strip(), max_font_size


def _body_font_size(page_dict: dict) -> float:
    """按字符数加权取本页最常见的字号，作为正文基准。"""
    weights: dict[float, int] = {}

    for block in page_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "").strip()
                if not text:
                    continue
                size = round(span.get("size", 12), 1)
                weights[size] = weights.get(size, 0) + len(text)

    if not weights:
        return 12.0
    return max(weights.items(), key=lambda item: item[1])[0]


def _format_page_to_markdown(page_dict: dict, page_num: int) -> str:
    """
    将 PyMuPDF 页面字典转换为 Markdown 格式

    标题按相对于本页正文字号的倍率判定，而非固定磅值：PPT 转出的 PDF
    正文普遍在 18pt 以上，用绝对阈值会把整页都标成标题。

    Args:
        page_dict: PyMuPDF page.get_text("dict") 返回的字典
        page_num: 页码（从 0 开始）

    Returns:
        Markdown 格式的页面内容
    """
    lines = [f"<!-- Page {page_num + 1} -->\n"]
    body_size = _body_font_size(page_dict)

    for block in page_dict.get("blocks", []):
        if block["type"] == 0:  # 文本块
            block_lines = []

            for line in block.get("lines", []):
                line_text, max_font_size = _line_to_text(line)
                if not line_text:
                    continue

                if max_font_size >= body_size * _H2_RATIO:
                    block_lines.append(f"## {line_text}")
                elif max_font_size >= body_size * _H3_RATIO:
                    block_lines.append(f"### {line_text}")
                else:
                    block_lines.append(line_text)

            if block_lines:
                lines.append("\n".join(block_lines) + "\n")

        elif block["type"] == 1:  # 图片块
            lines.append("[图片]\n")

    return "\n".join(lines)


def iter_pdf_pages(file_path: Path, stop_check=None) -> Iterator[PdfPageData]:
    """分批在短命 helper 中提取 PDF 文本，仍按页返回原有格式。

    ponytail: 每批重开 PDF 以换取简单的崩溃隔离；仅当超大文件实测受此限制时
    再改成单文件流式 helper，不能用常驻原生进程把崩溃边界重新扩大。
    """
    start = 0
    while True:
        result = run_parser(
            "text_pages", file_path, start, start + PDF_BATCH_PAGES,
            stop_check=stop_check,
        )
        yield from result["pages"]
        start += PDF_BATCH_PAGES
        if start >= result["total_pages"]:
            break


def convert_pdf_to_markdown(
    file_path: Path, progress_callback=None, output_path: Path | None = None
) -> str:
    """
    将 PDF 转换为 Markdown 格式。

    未提供 output_path 时返回完整 Markdown，保持通用转换接口兼容；提供
    output_path 时逐页写入文件，供大型 PDF 处理流程降低内存峰值。

    Args:
        file_path: PDF 文件路径
        progress_callback: 进度回调函数 callback(current_page, total_pages)
        output_path: 可选的 Markdown 输出路径

    Returns:
        未提供 output_path 时返回 Markdown 文本，否则返回空字符串

    Raises:
        ValueError: PDF 页数超过限制
    """
    import logging

    logger = logging.getLogger(__name__)
    markdown_parts = [] if output_path is None else None
    output_file = None

    try:
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_file = open(output_path, "w", encoding="utf-8")

        for page_data in iter_pdf_pages(file_path):
            page_number = page_data["page_number"]
            total_pages = page_data["total_pages"]
            page_text = page_data["page_text"]

            if page_number == 1:
                logger.info(
                    f"[PDF转换] 开始转换: {file_path.name}, 总页数: {total_pages}"
                )

            if output_file is not None:
                output_file.write(page_text)
                output_file.write("\n\n")
            else:
                markdown_parts.append(page_text)

            if progress_callback:
                progress_callback(page_number, total_pages)

        logger.info(f"[PDF转换] 完成: {file_path.name}")
        if markdown_parts is None:
            return ""
        return "\n\n".join(markdown_parts)
    finally:
        if output_file is not None:
            output_file.close()


def extract_embedded_images(markdown_text: str, image_dir: Path) -> str:
    """把 Markdown 中的 base64 内嵌图片落盘，引用改写为 <目录名>/<文件名>。

    MarkItDown 转换 Office 文档时，图片默认被替换成指向不存在文件的占位名
    （如 Picture2.jpg），渲染时必然断链；打开 keep_data_uris 后图片改为 data URI
    内嵌，但 base64 会随正文进入切片，污染全文索引和嵌入请求。因此统一在此落盘，
    布局与 OCR 解析产出保持一致（working/<文档名>/），使切片渲染和 MCP 插图返回
    无需区分图片来源。

    同一张图（如每页复用的 logo）按内容哈希命名，天然去重。

    Args:
        markdown_text: keep_data_uris=True 转换得到的 Markdown
        image_dir: 图片落盘目录，约定为工作文件的同名目录

    Returns:
        图片引用改写后的 Markdown
    """
    import logging

    logger = logging.getLogger(__name__)

    saved: set[str] = set()

    def _replace(match: re.Match) -> str:
        alt, subtype, payload = match.group(1), match.group(2).lower(), match.group(3)

        try:
            blob = base64.b64decode(re.sub(r"\s+", "", payload), validate=True)
        except Exception:
            logger.warning("[图片提取] base64 解码失败，剥离该引用")
            return f"![{alt}]"

        filename = (
            f"image-{hashlib.sha1(blob).hexdigest()[:12]}."
            f"{_IMAGE_EXTENSIONS.get(subtype, 'png')}"
        )
        if filename not in saved:
            try:
                image_dir.mkdir(parents=True, exist_ok=True)
                (image_dir / filename).write_bytes(blob)
            except OSError as exc:
                logger.warning("[图片提取] 写入失败，剥离该引用: %s", exc)
                return f"![{alt}]"
            saved.add(filename)

        return f"![{alt}]({image_dir.name}/{filename})"

    content = _DATA_URI_IMG.sub(_replace, markdown_text)

    # 兜底：正文里不允许残留任何 data URI，否则 base64 会进入切片
    content = _ANY_DATA_URI_IMG.sub(r"![\1]", content)
    content = _ANY_DATA_URI_SRC.sub("", content)

    if saved:
        logger.info("[图片提取] 落盘 %s 张内嵌图片: %s", len(saved), image_dir.name)
    return content


def convert_to_markdown(file_path: Path, image_dir: Optional[Path] = None) -> str:
    """
    将文件转换为 Markdown 格式

    Args:
        file_path: 文件路径
        image_dir: Office 文档内嵌图片的落盘目录（约定为工作文件同名目录）。
            为 None 时不提取图片，正文中的图片引用会被剥离

    Returns:
        Markdown 格式的文本内容

    Raises:
        ValueError: 不支持的文件格式或 PDF 页数超限
        Exception: 转换失败
    """
    suffix = file_path.suffix.lower()

    if suffix not in SUPPORTED_FORMATS:
        raise ValueError(f"不支持的文件格式: {suffix}")

    if suffix in {".md", ".txt"}:
        # 文本类文件直接读取
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    elif suffix == ".pdf":
        # PDF 使用 PyMuPDF Native 流式处理
        return convert_pdf_to_markdown(file_path)
    else:
        # 其他格式使用 MarkItDown 转换。图片只能以 data URI 形式取出，
        # 转换后立即落盘，避免 base64 留在正文里
        from markitdown import MarkItDown

        md = MarkItDown()
        result = md.convert(str(file_path), keep_data_uris=image_dir is not None)
        markdown_text = result.markdown or ""
        if image_dir is None:
            return markdown_text
        return extract_embedded_images(markdown_text, image_dir)
