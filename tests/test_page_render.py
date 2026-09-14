"""
PDF 原页渲染与插图回退测试

验证 VLM / 本地文本层解析的 PDF（正文不含插图引用）能回退到渲染原页：
- 正文有可用插图时不渲染原页
- 正文无插图且能取到页码时渲染原页
- 非 PDF 原件、无页码、页码越界都不回退
- 渲染结果按原件与页码缓存复用
"""

import sys
from pathlib import Path

import pymupdf
import pytest
from PIL import Image as PILImage

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from indexing.services import page_render
from indexing.services.page_render import (
    VIEW_DPI,
    page_number_from_heading,
    render_pdf_page,
)
from retrieval.tools import chunk_images
from retrieval.tools.chunk_images import collect_chunk_images


def _make_pdf(path: Path, pages: int = 3) -> Path:
    """生成指定页数的 PDF，每页写一行可辨识的文字。"""
    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page()
        page.insert_text((72, 72), f"page {index + 1}")
    document.save(path)
    document.close()
    return path


def _workspace(tmp_path: Path, monkeypatch) -> Path:
    """构造 working/<文档名>.md 布局，并把渲染缓存重定向到临时目录。"""
    working = tmp_path / "working"
    working.mkdir()
    md = working / "报告.md"
    md.write_text("# 报告", encoding="utf-8")

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(page_render, "get_page_cache_dir", lambda: cache)
    monkeypatch.setattr(chunk_images, "render_pdf_page", render_pdf_page)
    return md


def _doc(md: Path, chunk_text: str, original: Path | None, heading: str) -> dict:
    return {
        "chunk_text": chunk_text,
        "file_path": str(md),
        "original_file_path": str(original) if original else None,
        "heading_path": heading,
    }


def test_renders_page_when_no_inline_images(tmp_path, monkeypatch):
    """正文没有插图引用时，PDF 回退到渲染原页"""
    md = _workspace(tmp_path, monkeypatch)
    pdf = _make_pdf(tmp_path / "报告.pdf")

    documents = {"报告_第2页": _doc(md, "正文没有插图", pdf, "报告 / 第2页")}
    images, skipped = collect_chunk_images(documents)

    assert skipped == 0
    assert len(images) == 1
    assert images[0]["ref"] == "原文第2页"
    assert Path(images[0]["path"]).is_file()


def test_inline_images_take_precedence(tmp_path, monkeypatch):
    """正文已有可用插图时不渲染原页"""
    md = _workspace(tmp_path, monkeypatch)
    image_dir = md.parent / "报告"
    image_dir.mkdir()
    PILImage.new("RGB", (640, 386)).save(image_dir / "fig-1.jpg")
    pdf = _make_pdf(tmp_path / "报告.pdf")

    documents = {
        "报告_第2页": _doc(md, "![图1](报告/fig-1.jpg)", pdf, "报告 / 第2页")
    }
    images, _ = collect_chunk_images(documents)

    assert [img["ref"] for img in images] == ["报告/fig-1.jpg"]


def test_image_pagination_preserves_inline_and_source_page_order(tmp_path, monkeypatch):
    """分页跳过的插图不能触发原页回退，真正的原页图片也要能续取。"""
    md = _workspace(tmp_path, monkeypatch)
    image_dir = md.parent / "报告"
    image_dir.mkdir()
    for index in (1, 2):
        PILImage.new("RGB", (640, 386)).save(image_dir / f"fig-{index}.jpg")
    pdf = _make_pdf(tmp_path / "报告.pdf")
    documents = {
        f"报告_第{index}页": _doc(
            md,
            f"![图{index}](报告/fig-{index}.jpg)" if index < 3 else "正文没有插图",
            pdf,
            f"报告 / 第{index}页",
        )
        for index in (1, 2, 3)
    }
    rendered_headings = []
    render_source_page = chunk_images._render_source_page

    def track_render(doc):
        rendered_headings.append(doc["heading_path"])
        return render_source_page(doc)

    monkeypatch.setattr(chunk_images, "_render_source_page", track_render)
    expected = ["报告/fig-1.jpg", "报告/fig-2.jpg", "原文第3页"]
    for offset in range(4):
        images, remaining = collect_chunk_images(
            documents, max_images=1, image_offset=offset
        )
        assert [img["ref"] for img in images] == expected[offset:offset + 1]
        assert remaining == max(0, 2 - offset)
        if images:
            assert images[0]["doc_title"] == f"报告_第{offset + 1}页"
            assert Path(images[0]["path"]).is_file()

    assert rendered_headings == ["报告 / 第3页"] * 4


def test_office_original_uses_converted_pdf(tmp_path, monkeypatch):
    """Word/PPT 渲染的是 Office 转换产出的 PDF，不是原件本身"""
    md = _workspace(tmp_path, monkeypatch)
    pptx = tmp_path / "报告.pptx"
    pptx.write_bytes(b"not a real pptx")
    converted = _make_pdf(tmp_path / "converted.pdf")
    monkeypatch.setattr(page_render, "convert_to_pdf", lambda _: converted)

    documents = {"报告_第2页": _doc(md, "正文没有插图", pptx, "报告 / 第2页")}
    images, skipped = collect_chunk_images(documents)

    assert skipped == 0
    assert [img["ref"] for img in images] == ["原文第2页"]
    assert Path(images[0]["path"]).is_file()


def test_office_original_without_converter(tmp_path, monkeypatch):
    """转换器不可用时不渲染，别拿 pptx 原件顶上"""
    md = _workspace(tmp_path, monkeypatch)
    pptx = tmp_path / "报告.pptx"
    pptx.write_bytes(b"not a real pptx")
    monkeypatch.setattr(page_render, "convert_to_pdf", lambda _: None)

    documents = {"报告_第2页": _doc(md, "正文没有插图", pptx, "报告 / 第2页")}

    assert collect_chunk_images(documents) == ([], 0)


def test_unsupported_original_is_not_rendered(tmp_path, monkeypatch):
    """txt 之类没有页的原件不回退渲染，正文无引用即表示没有图"""
    md = _workspace(tmp_path, monkeypatch)
    txt = tmp_path / "报告.txt"
    txt.write_text("纯文本", encoding="utf-8")

    documents = {"报告_第2页": _doc(md, "正文没有插图", txt, "报告 / 第2页")}
    images, skipped = collect_chunk_images(documents)

    assert images == []
    assert skipped == 0


def test_missing_or_out_of_range_page(tmp_path, monkeypatch):
    """取不到页码或页码越界时不回退"""
    md = _workspace(tmp_path, monkeypatch)
    pdf = _make_pdf(tmp_path / "报告.pdf", pages=2)

    no_page = {"报告_概述": _doc(md, "无插图", pdf, "报告 / 概述")}
    assert collect_chunk_images(no_page) == ([], 0)

    beyond = {"报告_第9页": _doc(md, "无插图", pdf, "报告 / 第9页")}
    assert collect_chunk_images(beyond) == ([], 0)


def test_render_cache_is_reused(tmp_path, monkeypatch):
    """同一原件同一页只渲染一次"""
    _workspace(tmp_path, monkeypatch)
    pdf = _make_pdf(tmp_path / "报告.pdf")

    first = render_pdf_page(pdf, 1)
    second = render_pdf_page(pdf, 1)

    assert first is not None and first == second
    assert len(list(first.parent.iterdir())) == 1


def test_view_dpi_cached_separately(tmp_path, monkeypatch):
    """UI 用的高精度与 MCP 用的低精度各自缓存，互不覆盖"""
    _workspace(tmp_path, monkeypatch)
    pdf = _make_pdf(tmp_path / "报告.pdf")

    default = render_pdf_page(pdf, 1)
    detailed = render_pdf_page(pdf, 1, VIEW_DPI)

    assert default is not None and detailed is not None
    assert default != detailed
    assert detailed.stat().st_size > default.stat().st_size


def test_page_number_parsing():
    """页码取自 PageChunker 写入的层级，文件基础名不参与匹配"""
    assert page_number_from_heading("报告 / 第12页") == 12
    assert page_number_from_heading("报告 / 第12页 / 第2部分") == 12
    assert page_number_from_heading("第3页汇总 / 概述") is None
    assert page_number_from_heading("报告 / 概述") is None
    assert page_number_from_heading(None) is None


def _pptx_state(tmp_path) -> dict:
    return {
        "selected_file_id": 1,
        "source_pane_open": True,
        "source_page": None,
        "chunks_data": [{"heading_path": "数学练习 / 第7页"}],
        "files_data": [
            {
                "id": 1,
                "filename": "数学练习.md",
                "original_file_type": "pptx",
                "original_file_path": str(tmp_path / "数学练习.pptx"),
            }
        ],
    }


def test_pptx_original_is_never_rendered_directly(tmp_path, monkeypatch):
    """对比栏渲染 PPT 时用的必须是转换产物，不能是 pptx 原件。

    PyMuPDF 打得开 pptx，解析出的页数却和实际幻灯片对不上（实测 16 页的
    PPT 只认出 7 页），直接渲染必然张冠李戴。
    """
    import asyncio

    from app.ui.handlers.chunk_handlers import ChunkHandlers

    converted = _make_pdf(tmp_path / "converted.pdf")
    monkeypatch.setattr(page_render, "convert_to_pdf", lambda _: converted)

    calls = []
    monkeypatch.setattr(
        "app.ui.handlers.chunk_handlers.render_pdf_page",
        lambda pdf, page, *args: calls.append((pdf, page)) or None,
    )

    state = _pptx_state(tmp_path)
    handlers = ChunkHandlers(state, {}, lambda *_: None)

    asyncio.run(handlers.sync_source_page())

    assert calls == [(converted, 7)], "应渲染转换后的 PDF 第 7 页"


def test_stale_render_does_not_leak_into_other_file(tmp_path, monkeypatch):
    """慢转换返回时用户已切走，结果不能写进新文件的对比栏。

    Office 转换冷启动实测近 20 秒，这段时间足够用户换好几个文件。
    """
    import asyncio

    from app.ui.handlers.chunk_handlers import ChunkHandlers

    converted = _make_pdf(tmp_path / "converted.pdf")
    monkeypatch.setattr(page_render, "convert_to_pdf", lambda _: converted)

    state = _pptx_state(tmp_path)
    handlers = ChunkHandlers(state, {}, lambda *_: None)

    def _slow_render(*args, **kwargs):
        # 模拟转换期间用户切到了别的文件
        state["selected_file_id"] = 2
        return tmp_path / "page.png"

    monkeypatch.setattr(
        "app.ui.handlers.chunk_handlers.render_pdf_page", _slow_render
    )

    asyncio.run(handlers.sync_source_page())

    assert state["source_page"] is None, "过期结果不应写入对比栏"


def test_refresh_survives_deleted_element(tmp_path, monkeypatch):
    """对比栏连同父元素被销毁后刷新会抛错，静默跳过而不是崩掉整个 handler"""
    import asyncio

    from app.ui.handlers.chunk_handlers import ChunkHandlers

    converted = _make_pdf(tmp_path / "converted.pdf")
    monkeypatch.setattr(page_render, "convert_to_pdf", lambda _: converted)
    monkeypatch.setattr(
        "app.ui.handlers.chunk_handlers.render_pdf_page",
        lambda *args, **kwargs: tmp_path / "page.png",
    )

    class _DeadColumn:
        def refresh(self):
            raise RuntimeError("The parent element this slot belongs to has been deleted.")

    handlers = ChunkHandlers(
        _pptx_state(tmp_path),
        {"source_column": _DeadColumn()},
        lambda *_: None,
    )

    asyncio.run(handlers.sync_source_page())


def test_source_pane_cleared_without_converter(tmp_path, monkeypatch):
    """转不出 PDF 时对比栏收起，而不是挂着上一个文件的页面"""
    import asyncio

    from app.ui.handlers.chunk_handlers import ChunkHandlers

    monkeypatch.setattr(page_render, "convert_to_pdf", lambda _: None)

    calls = []
    monkeypatch.setattr(
        "app.ui.handlers.chunk_handlers.render_pdf_page",
        lambda *args, **kwargs: calls.append(args) or None,
    )

    state = _pptx_state(tmp_path)
    state["source_page"] = {"url": "/pages/old.png", "page": 3, "caption": ""}
    handlers = ChunkHandlers(state, {}, lambda *_: None)

    asyncio.run(handlers.sync_source_page())

    assert calls == [], "转换不可用时不应触发渲染"
    assert state["source_page"] is None
