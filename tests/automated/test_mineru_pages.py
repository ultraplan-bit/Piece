"""MinerU 解析链路：逐页对齐、版面块还原、图片落盘与分批页码换算。

页对齐是这条链路唯一会静默出错的地方：页码错了不会报错，只会让知识卡片
的原页对比指向别的页面，所以这里重点覆盖空白页与分批偏移。
"""

import json
import zipfile
from pathlib import Path

import pytest

from indexing import settings as settings_module
from indexing.services import mineru_client
from indexing.settings import AppSettings, OcrSettings

ONE_MB = 1024 * 1024

# 工作文件同名目录会带上文档名，真实文档名常含空格和括号：
# Markdown 的 ![](...) 遇到空格即截断，图片引用必须走 HTML 形态
DOC_NAME = "报告 (1)"


def _write_zip(dest: Path, blocks, images=None, inner_name="doc_content_list.json"):
    with zipfile.ZipFile(dest, "w") as archive:
        archive.writestr(inner_name, json.dumps(blocks, ensure_ascii=False))
        for name, content in (images or {}).items():
            archive.writestr(f"images/{name}", content)


class _FakeClient:
    """替身：按批次顺序吐出预置的结果包，不触碰网络与原生 helper。"""

    # 每批的 content_list；需要插图时写成 {"blocks": [...], "images": {...}}
    batches: list = []
    # 每次申请上传链接时收到的入参，用于验证配置有没有真的传下去
    calls: list = []

    def __init__(self, token, timeout=60.0):
        self.token = token
        self.served = 0
        self.batch_names: list = []

    def close(self):
        pass

    def request_upload(self, name, **options):
        type(self).calls.append({"name": name, **options})
        self.batch_names.append(name)
        return f"batch-{len(self.batch_names)}", f"https://upload.invalid/{len(self.batch_names)}"

    def upload(self, url, file_path):
        pass

    def wait_batch(self, batch_id, file_name, poll_timeout=7200.0, stop_check=None, progress_callback=None):
        if progress_callback is not None:
            progress_callback(1, 1)
        return f"zip-{self.served}"

    def download_zip(self, url, dest):
        batch = type(self).batches[self.served]
        self.served += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(batch, dict):
            _write_zip(dest, batch["blocks"], batch.get("images"))
        else:
            _write_zip(dest, batch)


@pytest.fixture
def mineru(monkeypatch):
    """把客户端、页数探测与切页换成替身，只留下被验证的编排逻辑。"""
    monkeypatch.setattr(mineru_client, "get_ocr_config",
                        lambda: OcrSettings(provider="mineru", mineru_token="token"))
    monkeypatch.setattr(mineru_client, "MineruClient", _FakeClient)
    monkeypatch.setattr(mineru_client, "_extract_page_range",
                        lambda path, start, end, dest: dest.write_bytes(b"%PDF-1.4"))
    return monkeypatch


def _run(mineru, tmp_path, total_pages, batches):
    _FakeClient.batches = batches
    _FakeClient.calls = []
    mineru.setattr(mineru_client, "_get_total_pages", lambda path: total_pages)
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"%PDF-1.4" + b"0" * 64)
    progress = []
    pages = list(mineru_client.iter_mineru_pdf_pages(
        source,
        tmp_path / "working" / DOC_NAME,
        on_parse_progress=lambda parsed, total: progress.append((parsed, total)),
    ))
    return pages, progress


def test_blank_page_keeps_page_numbers_aligned(mineru, tmp_path):
    """内容列表里没有的页是空白页，必须补位，否则后续页整体前移。"""
    pages, _ = _run(mineru, tmp_path, 3, [[
        {"type": "text", "text": "第一页", "page_idx": 0},
        {"type": "text", "text": "第三页", "page_idx": 2},
    ]])

    assert [page.page_number for page in pages] == [1, 2, 3]
    assert [page.page_text for page in pages] == ["第一页", "", "第三页"]
    assert all(page.total_pages == 3 for page in pages)


def test_batched_pages_map_to_global_numbers(mineru, tmp_path):
    """分批提交时，批内 page_idx 必须加上批次起点才能得到全局页码。"""
    mineru.setattr(mineru_client, "MAX_PAGES_PER_BATCH", 2)
    batches = [
        [{"type": "text", "text": f"第{n}页", "page_idx": n - 1} for n in (1, 2)],
        [{"type": "text", "text": f"第{n}页", "page_idx": n - 3} for n in (3, 4)],
        [{"type": "text", "text": "第5页", "page_idx": 0}],
    ]
    pages, progress = _run(mineru, tmp_path, 5, batches)

    assert [page.page_number for page in pages] == [1, 2, 3, 4, 5]
    assert [page.page_text for page in pages] == [f"第{n}页" for n in range(1, 6)]
    # 进度回调上报全局页数，总页数始终是文档总页数
    assert progress[-1] == (5, 5)
    assert all(total == 5 for _, total in progress)


def test_out_of_range_page_index_is_skipped(mineru, tmp_path):
    """服务端多返回的块不能把内容挤到别的页上。"""
    pages, _ = _run(mineru, tmp_path, 2, [[
        {"type": "text", "text": "第一页", "page_idx": 0},
        {"type": "text", "text": "越界", "page_idx": 7},
    ]])

    assert [page.page_text for page in pages] == ["第一页", ""]


def test_images_are_extracted_and_referenced_by_work_dir(mineru, tmp_path):
    """插图落到工作文件同名目录，正文引用改写成可解析的相对路径。

    文档名含空格与括号时，![](...) 会被 Markdown 在空格处截断，
    界面与 MCP 的重写正则也接不住，因此必须产出 HTML 形态的引用。
    """
    pages, _ = _run(mineru, tmp_path, 1, [{
        "blocks": [{"type": "image", "img_path": "images/ab_cd.jpg", "image_caption": ["图 1"], "page_idx": 0}],
        "images": {"ab_cd.jpg": b"jpeg-bytes", "unused.jpg": b"x"},
    }])

    image_dir = tmp_path / "working" / DOC_NAME
    assert pages[0].page_text == f'<img src="{DOC_NAME}/ab-cd.jpg">\n\n图 1'
    assert (image_dir / "ab-cd.jpg").read_bytes() == b"jpeg-bytes"
    # 正文没引用的图不落盘
    assert not (image_dir / "unused.jpg").exists()


def test_image_reference_survives_the_ui_rewrite(mineru, tmp_path):
    """界面把相对引用补成 /working/ 前缀，带空格括号的路径也必须命中。"""
    from app.ui.components import _absolutize_image_srcs

    pages, _ = _run(mineru, tmp_path, 1, [{
        "blocks": [{"type": "image", "img_path": "images/ab_cd.jpg", "page_idx": 0}],
        "images": {"ab_cd.jpg": b"jpeg-bytes"},
    }])

    rewritten = _absolutize_image_srcs(pages[0].page_text)
    assert rewritten == f'<img src="/working/{DOC_NAME}/ab-cd.jpg">'


def test_missing_image_reference_is_left_alone(tmp_path):
    archive_path = tmp_path / "result.zip"
    _write_zip(archive_path, [])

    with zipfile.ZipFile(archive_path) as archive:
        text = mineru_client._store_images('<img src="images/gone.jpg">', archive, tmp_path / "doc")

    assert text == '<img src="images/gone.jpg">'


def test_chunk_images_can_resolve_the_reference(mineru, tmp_path):
    """MCP 取图用的也是相对路径正则，产出的引用要能被它解析到真实文件。"""
    from retrieval.tools.chunk_images import _iter_refs, _resolve

    pages, _ = _run(mineru, tmp_path, 1, [{
        "blocks": [{"type": "image", "img_path": "images/ab_cd.jpg", "page_idx": 0}],
        "images": {"ab_cd.jpg": b"jpeg-bytes"},
    }])

    base = tmp_path / "working"
    refs = _iter_refs(pages[0].page_text)
    assert refs == [f"{DOC_NAME}/ab-cd.jpg"]
    assert _resolve(base, refs[0]) == (base / DOC_NAME / "ab-cd.jpg")


def test_block_rendering_covers_layout_types():
    render = mineru_client._render_block

    assert render({"type": "text", "text": "正文"}) == "正文"
    assert render({"type": "text", "text": "标题", "text_level": 2}) == "## 标题"
    assert render({"type": "equation", "text": "$$\nx=1\n$$"}) == "$$\nx=1\n$$"
    assert render({"type": "code", "code_body": "print(1)"}) == "```\nprint(1)\n```"
    assert render({"type": "list", "list_items": ["甲", "- 乙"]}) == "- 甲\n- 乙"
    # 页眉页脚等页面装饰不进正文，否则每页都会重复一遍
    assert render({"type": "header", "text": "页眉"}) == ""
    assert render({"type": "footer", "text": "页脚"}) == ""
    assert render({"type": "page_number", "text": "3"}) == ""
    # 未知类型不能拖垮整页
    assert render({"type": "brand_new_block", "text": "?"}) == ""


def test_table_body_drops_html_wrapper():
    """切分侧的表格保护只认 <table>，外层 html/body 必须剥掉。"""
    rendered = mineru_client._render_block({
        "type": "table",
        "table_caption": ["表 1"],
        "table_body": "<html><body><table><tr><td>1</td></tr></table></body></html>",
    })

    assert rendered == "表 1\n\n<table><tr><td>1</td></tr></table>"
    assert "html" not in rendered and "body" not in rendered


def test_parse_options_come_from_config(mineru, tmp_path):
    """模型版本、OCR 开关与语言必须真的随每次分批请求发出去。"""
    mineru.setattr(mineru_client, "get_ocr_config", lambda: OcrSettings(
        provider="mineru", mineru_token="token",
        mineru_model_version="pipeline", mineru_is_ocr=True, mineru_language="korean",
    ))
    _run(mineru, tmp_path, 1, [[{"type": "text", "text": "正文", "page_idx": 0}]])

    assert _FakeClient.calls == [{
        "name": "doc.pdf", "model_version": "pipeline", "is_ocr": True, "language": "korean",
    }]


def test_empty_model_version_falls_back_to_vlm(mineru, tmp_path):
    """配置被 CLI 清空时不能把一个空模型版本发过去。"""
    mineru.setattr(mineru_client, "get_ocr_config", lambda: OcrSettings(
        provider="mineru", mineru_token="token", mineru_model_version="", mineru_language="",
    ))
    _run(mineru, tmp_path, 1, [[{"type": "text", "text": "正文", "page_idx": 0}]])

    assert _FakeClient.calls[0]["model_version"] == "vlm"
    assert _FakeClient.calls[0]["language"] == "ch"


def test_oversized_file_fails_before_upload(mineru, tmp_path):
    """换后端后重新索引大文件时，要给可读原因而不是服务端的 -60005。"""
    mineru.setattr(mineru_client, "MINERU_MAX_FILE_SIZE", ONE_MB)
    mineru.setattr(mineru_client, "_get_total_pages", lambda path: 3)
    source = tmp_path / "big.pdf"
    source.write_bytes(b"%PDF-1.4" + b"0" * (2 * ONE_MB))

    with pytest.raises(mineru_client.MineruError, match="上限"):
        list(mineru_client.iter_mineru_pdf_pages(source, tmp_path / "doc"))


def test_parser_selection_and_size_limit_follow_provider(monkeypatch):
    """限制绑定当前生效的后端，不能拖累没配 MinerU 的用户。"""
    for provider, token, expected_parser, expected_limit in (
        ("mineru", "token", "mineru", settings_module.MINERU_MAX_FILE_SIZE),
        ("mineru", "", "local", None),
        ("paddle", "", "local", None),
    ):
        monkeypatch.setattr(settings_module, "_settings", AppSettings(
            ocr=OcrSettings(provider=provider, mineru_token=token)
        ))
        assert settings_module.get_pdf_parser() == expected_parser
        assert settings_module.get_parser_max_size() == expected_limit


def test_import_limit_follows_provider(monkeypatch):
    from indexing.services import file_service

    oversize = settings_module.MINERU_MAX_FILE_SIZE + 1
    monkeypatch.setattr(settings_module, "_settings", AppSettings(
        ocr=OcrSettings(provider="mineru", mineru_token="token")
    ))
    with pytest.raises(Exception, match="200 MiB"):
        file_service.validate_import("big.pdf", oversize)

    # 换回没有额外限制的后端后，同一份文件应当放行
    monkeypatch.setattr(settings_module, "_settings", AppSettings())
    file_service.validate_import("big.pdf", oversize)
