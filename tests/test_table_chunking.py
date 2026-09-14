"""
表格分块测试

OCR 服务对复杂表格只输出 HTML 表格，整张表压在同一行里且每个单元格都带 style，
此前的切分器只认 Markdown 管道表格，于是从标签中间硬切，产出
"rd-wrap: break-word;'>PE5TRACED2-E3D2---5" 这类碎片。验证：
- HTML 表格被识别为受保护区域，不会被从标签中间切开
- 装不下一个切片的表格按行切开，每片补回表头且标签闭合
- 表格标签上的 style 噪声被去掉
- 图注 <div>、<img> 等行内 HTML 同样不会被切在标签内部
"""

import re
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from indexing.services.chunking.utils import (
    find_table_boundaries,
    normalize_tables,
    recursive_split,
    strip_table_styles,
)


# 片首停在标签内部：以 > 开头，或 > 之前出现属性赋值/引号（中间不能有 <）
_HEAD_FRAGMENT = re.compile(
    r'^\s*/?>|^[^<>]{0,400}?(?:[a-zA-Z-]+\s*=\s*["\'][^<>]*|["\'])\s*/?>'
)

CHUNK_SIZE = 800


def _tail_fragment(chunk: str) -> bool:
    """片尾停在标签中间：最后一个 < 之后再没有 >"""
    index = chunk.rfind("<")
    if index == -1 or ">" in chunk[index:]:
        return False
    return bool(re.match(r"</?[a-zA-Z]", chunk[index:]))


def _assert_no_broken_tags(chunks):
    for i, chunk in enumerate(chunks):
        assert not _HEAD_FRAGMENT.match(chunk), f"切片 {i} 以标签碎片开头: {chunk[:60]!r}"
        assert not _tail_fragment(chunk), f"切片 {i} 在标签中间截断: {chunk[-60:]!r}"
        assert chunk.count("<table") == chunk.count(
            "</table>"
        ), f"切片 {i} 表格标签不闭合"


def _html_table(rows: int, cell_style: bool = True) -> str:
    """构造 OCR 风格的 HTML 表格：首行为列名，单元格带 style"""
    style = " style='text-align: center; word-wrap: break-word;'" if cell_style else ""
    header = f"<tr><td{style}>引脚</td><td{style}>功能</td></tr>"
    body = "".join(
        f"<tr><td{style}>PE{i}</td><td{style}>TRACED{i} 复用功能说明</td></tr>"
        for i in range(rows)
    )
    return f"<table border=1 style='margin: auto;'>{header}{body}</table>"


def test_html_table_is_protected():
    """HTML 表格进入受保护区域，不再只认 Markdown 管道表格"""
    text = f"前言。\n\n{_html_table(3)}\n\n后记。"
    boundaries = find_table_boundaries(text)

    assert len(boundaries) == 1
    start, end = boundaries[0]
    assert text[start:end].startswith("<table")
    assert text[start:end].endswith("</table>")


def test_table_styles_stripped():
    """表格标签上的 style 属性被去掉，正文里的 div 样式保留"""
    text = '<div style="text-align: center;">图注</div>' + _html_table(2)
    cleaned = strip_table_styles(text)

    assert "word-wrap" not in cleaned
    assert "<td>" in cleaned and "<table border=1>" in cleaned
    # 非表格标签不受影响
    assert '<div style="text-align: center;">图注</div>' in cleaned


def test_small_table_not_split():
    """能装下的表格整块保留，不会被从标签中间切开"""
    table = _html_table(4)
    text = "背景说明。" * 120 + "\n\n" + table + "\n\n" + "后续说明。" * 120

    chunks = recursive_split(text, chunk_size=CHUNK_SIZE)

    _assert_no_broken_tags(chunks)
    stripped = strip_table_styles(table)
    assert any(stripped in chunk for chunk in chunks), "表格未被完整保留在某个切片中"


def test_oversized_html_table_split_by_row_with_header():
    """超长 HTML 表格按 <tr> 切开，每片都带表头且标签闭合"""
    text = _html_table(60)
    assert len(strip_table_styles(text)) > CHUNK_SIZE

    chunks = recursive_split(text, chunk_size=CHUNK_SIZE)

    assert len(chunks) > 1, "超长表格应被拆成多片"
    _assert_no_broken_tags(chunks)
    for i, chunk in enumerate(chunks):
        assert "<td>引脚</td><td>功能</td>" in chunk, f"切片 {i} 缺少表头行"
        assert chunk.startswith("<table"), f"切片 {i} 不是完整表格"


def test_oversized_markdown_table_repeats_header():
    """超长 Markdown 表格按行切开，每片都带表头和分隔行"""
    header = "| 项目 | 数值 | 说明 |\n|------|------|------|"
    rows = "\n".join(f"| 项目{i} | {i}.00 | 这是一段足够长的说明文字 |" for i in range(80))
    text = f"{header}\n{rows}"

    chunks = recursive_split(text, chunk_size=CHUNK_SIZE)

    assert len(chunks) > 1
    for i, chunk in enumerate(chunks):
        assert chunk.startswith("| 项目 | 数值 | 说明 |"), f"切片 {i} 缺少表头"
        assert "|------|------|------|" in chunk, f"切片 {i} 缺少分隔行"


def test_normalize_tables_is_idempotent():
    """归一化会在分块器和切分器里各跑一次，必须幂等"""
    header = "| 项目 | 数值 |\n|------|------|"
    rows = "\n".join(f"| 项目{i} | {i}.00 |" for i in range(80))
    for text in (_html_table(60), f"说明。\n\n{header}\n{rows}\n\n结束。"):
        once = normalize_tables(text, CHUNK_SIZE)
        assert normalize_tables(once, CHUNK_SIZE) == once


def test_caption_div_not_split_mid_tag():
    """图注 div / img 标签同样不会被切在标签内部"""
    caption = (
        '<div style="text-align: center;">'
        '<img src="文档/img-in-image-box-12-265-1154-1312.jpg" alt="Image" width="74%" />'
        "</div>"
    )
    # 图注紧跟在一段没有标点的正文之后，切分点附近唯一的"分隔符"是标签内的
    # 空格和分号——这正是老实现从 style 属性中间切开的场景
    text = "正文段落" * 190 + caption + "后续段落。" * 100

    chunks = recursive_split(text, chunk_size=CHUNK_SIZE)

    _assert_no_broken_tags(chunks)
    assert any(caption in chunk for chunk in chunks), "图注整体未被保留"


def test_long_text_still_splits_and_terminates():
    """普通长文本仍按语义边界切分，且不会因保护逻辑陷入死循环"""
    text = "。".join(f"这是第{i}句话，用来凑够足够的长度" for i in range(300))

    chunks = recursive_split(text, chunk_size=CHUNK_SIZE)

    assert len(chunks) > 1
    assert all(chunk.strip() for chunk in chunks)
    # 覆盖完整：去掉重叠后每个句子都应出现在某个切片里
    joined = "".join(chunks)
    assert "这是第0句话" in joined and "这是第299句话" in joined


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"[OK] {name}")
    print("表格分块测试全部通过")
