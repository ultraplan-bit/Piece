"""
Office 转 PDF 与转换后的解析质量测试

覆盖三块：
- 后端探测：按 converter 配置过滤，auto 下 COM 优先、失败顺位回退
- 缓存：原件改动后自动失效，转换只做一次
- 路由：文本层够用走本地解析，不够且没有 OCR 时回退 markitdown

真实的 COM / LibreOffice 调用不在测试内触发（慢且依赖环境），
用替身验证调度逻辑；转换产物的解析质量则用真实 PDF 验证。
"""

import sys
from pathlib import Path

import pymupdf
import pytest

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from indexing import settings
from indexing.services import office_convert, processor
from indexing.services.chunking import ChunkerFactory
from indexing.services.converter import _format_page_to_markdown
from indexing.services.office_convert import (
    CONVERTER_ONLY_FORMATS,
    PDF_ROUTED_FORMATS,
    convert_to_pdf,
    list_converters,
)
from indexing.services.processor import (
    _converter_only_failure,
    _has_usable_text_layer,
    _office_pdf_route,
)


def _office_config(monkeypatch, converter: str, soffice: str | None = None):
    """把 office.converter 和 LibreOffice 探测结果固定成给定值。"""
    monkeypatch.setattr(
        settings, "get_settings", lambda: settings.AppSettings(
            office=settings.OfficeSettings(converter=converter)
        )
    )
    monkeypatch.setattr(office_convert, "get_settings", settings.get_settings)
    monkeypatch.setattr(office_convert, "find_libreoffice", lambda *_: soffice)


def _cache_dir(monkeypatch, tmp_path: Path) -> Path:
    cache = tmp_path / "office_pdf"
    cache.mkdir()
    monkeypatch.setattr(office_convert, "get_office_pdf_cache_dir", lambda: cache)
    return cache


def _source(tmp_path: Path, name: str = "讲义.pptx") -> Path:
    source = tmp_path / name
    source.write_bytes(b"fake office document")
    return source


def test_xlsx_not_routed_to_pdf():
    """xlsx 留给 markitdown：转 PDF 会把结构化表格压平成版面文字"""
    assert ".xlsx" not in PDF_ROUTED_FORMATS


def test_word_and_slide_formats_routed_to_pdf():
    """Word / PPT 系列（含传统二进制格式和 ODF）都走 PDF 管线"""
    assert PDF_ROUTED_FORMATS == {
        ".docx", ".doc", ".rtf", ".odt",
        ".pptx", ".ppt", ".odp",
    }

    # 走 PDF 管线的格式必须也能被上传受理，否则用户根本传不进来
    supported = set(ChunkerFactory.get_supported_extensions())
    assert PDF_ROUTED_FORMATS <= supported


def test_converter_only_formats():
    """markitdown 读不了的格式单独标记，没有转换器时应当场拒收"""
    # docx/pptx 是 OOXML，markitdown 至少能兜底；其余的必须有转换器
    assert CONVERTER_ONLY_FORMATS == {".doc", ".rtf", ".odt", ".ppt", ".odp"}
    assert CONVERTER_ONLY_FORMATS <= PDF_ROUTED_FORMATS


def test_converter_selection(monkeypatch):
    """后端列表按配置过滤，auto 下 COM 排在 LibreOffice 前面"""
    monkeypatch.setattr(office_convert, "_com_available", lambda: True)

    _office_config(monkeypatch, "auto", "soffice")
    assert list_converters() == [("com", ""), ("libreoffice", "soffice")]

    _office_config(monkeypatch, "com", "soffice")
    assert list_converters() == [("com", "")]

    _office_config(monkeypatch, "libreoffice", "soffice")
    assert list_converters() == [("libreoffice", "soffice")]

    _office_config(monkeypatch, "off", "soffice")
    assert list_converters() == []


def test_no_converter_falls_back(monkeypatch, tmp_path):
    """一个后端都没有时返回 None，交给 markitdown"""
    _cache_dir(monkeypatch, tmp_path)
    _office_config(monkeypatch, "auto", None)
    monkeypatch.setattr(office_convert, "_com_available", lambda: False)

    assert convert_to_pdf(_source(tmp_path)) is None


def test_falls_through_to_next_converter(monkeypatch, tmp_path):
    """COM 失败时顺位交给 LibreOffice，而不是直接放弃"""
    _cache_dir(monkeypatch, tmp_path)
    _office_config(monkeypatch, "auto", "soffice")
    monkeypatch.setattr(office_convert, "_com_available", lambda: True)

    def _com_boom(source, target):
        raise OSError("COM 不可用")

    def _lo_ok(soffice, source, target):
        target.write_bytes(b"%PDF-1.7")
        return True

    monkeypatch.setattr(office_convert, "_convert_with_com", _com_boom)
    monkeypatch.setattr(office_convert, "_convert_with_libreoffice", _lo_ok)

    result = convert_to_pdf(_source(tmp_path))
    assert result is not None and result.is_file()


def test_cache_reused_and_invalidated(monkeypatch, tmp_path):
    """同一原件只转一次，原件改动后重新转"""
    _cache_dir(monkeypatch, tmp_path)
    _office_config(monkeypatch, "libreoffice", "soffice")
    monkeypatch.setattr(office_convert, "_com_available", lambda: False)

    calls = []

    def _lo_ok(soffice, source, target):
        calls.append(source)
        target.write_bytes(b"%PDF-1.7")
        return True

    monkeypatch.setattr(office_convert, "_convert_with_libreoffice", _lo_ok)

    source = _source(tmp_path)
    first = convert_to_pdf(source)
    assert convert_to_pdf(source) == first
    assert len(calls) == 1, "第二次应直接命中缓存"

    source.write_bytes(b"fake office document v2")
    second = convert_to_pdf(source)
    assert second != first and len(calls) == 2


def test_fallback_does_not_poison_preferred_cache(monkeypatch, tmp_path):
    """COM 失败回退时，产物存进 LibreOffice 自己的缓存位

    缓存位曾经按「首选后端」命名，于是一次瞬时的 COM 失败会把保真度更差的
    LibreOffice 产物钉在 COM 的位置上，之后 COM 恢复也不再重转——实测让一份
    2 页的 Word 文档长期被解析成 4 页。
    """
    _cache_dir(monkeypatch, tmp_path)
    _office_config(monkeypatch, "auto", "soffice")
    monkeypatch.setattr(office_convert, "_com_available", lambda: True)

    com_works = {"value": False}

    def _com(source, target):
        if not com_works["value"]:
            raise OSError("COM 实例被并发调用掐掉")
        target.write_bytes(b"%PDF-com")
        return True

    def _lo(soffice, source, target):
        target.write_bytes(b"%PDF-lo")
        return True

    monkeypatch.setattr(office_convert, "_convert_with_com", _com)
    monkeypatch.setattr(office_convert, "_convert_with_libreoffice", _lo)

    source = _source(tmp_path)
    first = convert_to_pdf(source)
    assert first is not None and first.read_bytes() == b"%PDF-lo"
    assert not office_convert._cache_path(source, "com").is_file(), (
        "回退产物不能占用 COM 的缓存位"
    )

    # COM 恢复后会被重新尝试，其产物取代回退产物
    com_works["value"] = True
    second = convert_to_pdf(source)
    assert second != first and second.read_bytes() == b"%PDF-com"

    # 之后直接命中 COM 缓存，不再触发任何转换
    monkeypatch.setattr(
        office_convert,
        "_convert_with_com",
        lambda *_: pytest.fail("应命中缓存，不该再转一次"),
    )
    assert convert_to_pdf(source) == second


def test_libreoffice_env_strips_python_vars(monkeypatch):
    """soffice 自带内嵌 Python，继承宿主的 PYTHONHOME/PYTHONPATH 会让它找不到标准库"""
    monkeypatch.setenv("PYTHONHOME", "/opt/py")
    monkeypatch.setenv("PYTHONPATH", "/opt/py/lib")
    monkeypatch.setenv("PIECE_KEEP_ME", "1")

    env = office_convert._libreoffice_env()

    assert "PYTHONHOME" not in env
    assert "PYTHONPATH" not in env
    assert env["PIECE_KEEP_ME"] == "1", "其余环境变量要原样传下去"


def test_com_writes_through_temp_file(tmp_path):
    """COM 先写中间文件再改名：同一份文件被并发转换时不会互相写坏产物"""
    target = tmp_path / "office-abc.pdf"
    temp_target = office_convert._temp_target(target)

    assert temp_target != target
    assert temp_target.parent == target.parent, "中间文件要同盘，改名才是原子的"
    assert temp_target.suffix == ".pdf"


def _make_pdf(path: Path, text: str, pages: int = 2) -> Path:
    document = pymupdf.open()
    for _ in range(pages):
        document.new_page().insert_text((72, 72), text)
    document.save(path)
    document.close()
    return path


def test_text_layer_detection(tmp_path):
    """文本层是否够用按每页平均字符数判定"""
    rich = _make_pdf(tmp_path / "rich.pdf", "x" * 200)
    poor = _make_pdf(tmp_path / "poor.pdf", "x")

    assert _has_usable_text_layer(rich) is True
    assert _has_usable_text_layer(poor) is False
    assert _has_usable_text_layer(tmp_path / "missing.pdf") is False


def test_route_prefers_ocr_over_text_layer(monkeypatch, tmp_path):
    """配了 OCR 就走 OCR，哪怕文本层字符数很足。

    本地文本层按行读取，矩阵、表格这类二维版面会被拆成一行一个元素，
    字符数够多并不代表结构正确。
    """
    pdf = _make_pdf(tmp_path / "rich.pdf", "x" * 200)
    monkeypatch.setattr(processor, "convert_to_pdf", lambda _: pdf)
    monkeypatch.setattr(processor, "get_pdf_parser", lambda: "paddle")

    assert _office_pdf_route(_source(tmp_path)) == pdf


def test_route_falls_back_to_local_without_ocr(monkeypatch, tmp_path):
    """没有 OCR 时靠本地文本层兜底，仍好过 markitdown"""
    pdf = _make_pdf(tmp_path / "rich.pdf", "x" * 200)
    monkeypatch.setattr(processor, "convert_to_pdf", lambda _: pdf)
    monkeypatch.setattr(processor, "get_pdf_parser", lambda: "local")

    assert _office_pdf_route(_source(tmp_path)) == pdf


def test_route_uses_ocr_when_text_layer_is_poor(monkeypatch, tmp_path):
    """LibreOffice 把公式画成图片，文本层几乎为空，只能靠 OCR"""
    pdf = _make_pdf(tmp_path / "poor.pdf", "x")
    monkeypatch.setattr(processor, "convert_to_pdf", lambda _: pdf)
    monkeypatch.setattr(processor, "get_pdf_parser", lambda: "vlm")

    assert _office_pdf_route(_source(tmp_path)) == pdf


def test_route_falls_back_without_ocr(monkeypatch, tmp_path):
    """文本层不足又没有 OCR 时回退 markitdown，别产出一堆空页"""
    pdf = _make_pdf(tmp_path / "poor.pdf", "x")
    monkeypatch.setattr(processor, "convert_to_pdf", lambda _: pdf)
    monkeypatch.setattr(processor, "get_pdf_parser", lambda: "local")

    assert _office_pdf_route(_source(tmp_path)) is None


def test_route_skipped_without_converter(monkeypatch, tmp_path):
    """转换器不可用时同样回退 markitdown"""
    monkeypatch.setattr(processor, "convert_to_pdf", lambda _: None)

    assert _office_pdf_route(_source(tmp_path)) is None


def test_converter_only_failure_reason(monkeypatch):
    """.doc 这类格式失败时要说清缺的是转换器还是 OCR，别甩一句 markitdown 的报错"""
    monkeypatch.setattr(processor, "list_converters", lambda: [])
    assert "LibreOffice" in _converter_only_failure(".doc")

    monkeypatch.setattr(processor, "list_converters", lambda: [("com", "")])
    assert "OCR" in _converter_only_failure(".doc")


def _span(text: str, size: float, x0: float, x1: float) -> dict:
    return {"text": text, "size": size, "bbox": (x0, 0.0, x1, size)}


def _page_dict(*lines: list) -> dict:
    return {
        "blocks": [
            {"type": 0, "lines": [{"spans": spans} for spans in lines]}
        ]
    }


def test_heading_uses_relative_font_size():
    """标题按本页正文字号的倍率判定，而非固定磅值。

    PPT 正文普遍在 18pt 以上，用旧的 >16pt 绝对阈值整页都会变成 ## 标题。
    """
    page = _page_dict(
        [_span("这是幻灯片标题", 32, 0, 200)],
        [_span("这是一段足够长的正文内容用来撑出众数字号", 18, 0, 400)],
        [_span("这是另一段同样字号的正文内容继续撑住众数", 18, 0, 400)],
    )

    markdown = _format_page_to_markdown(page, 0)

    assert "## 这是幻灯片标题" in markdown
    assert "\n这是一段足够长的正文内容用来撑出众数字号" in markdown
    assert "## 这是一段" not in markdown


def test_adjacent_spans_are_not_split():
    """公式里每个字符自成一个 span，紧挨着就不能补空格"""
    page = _page_dict(
        [_span("A", 12, 0, 8), _span("H", 12, 8, 16), _span("A", 12, 16, 24)]
    )

    assert "AHA" in _format_page_to_markdown(page, 0)


def test_gap_between_spans_keeps_space():
    """真有横向间隙时仍要补空格，别把两个词粘在一起"""
    page = _page_dict([_span("求", 12, 0, 12), _span("特征值", 12, 40, 76)])

    assert "求 特征值" in _format_page_to_markdown(page, 0)


def test_math_alphanumerics_are_normalized():
    """数学字母数字符号归一化成 ASCII，否则「AHA」检索不到「𝑨𝑯𝑨」"""
    page = _page_dict([_span("求 \U0001d468\U0001d46f\U0001d468 的特征值", 12, 0, 120)])

    markdown = _format_page_to_markdown(page, 0)

    assert "AHA" in markdown
    assert "\U0001d468" not in markdown


def test_fullwidth_punctuation_is_preserved():
    """只归一化数学符号：全量 NFKC 会把中文全角标点转成半角，改变原文"""
    page = _page_dict([_span("步骤（一）：求特征值。", 12, 0, 120)])

    assert "（一）：" in _format_page_to_markdown(page, 0)
