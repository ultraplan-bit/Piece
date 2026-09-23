"""MinerU 解析链路：逐页对齐、版面块还原、图片落盘与分批页码换算。

页对齐是这条链路唯一会静默出错的地方：页码错了不会报错，只会让知识卡片
的原页对比指向别的页面，所以这里重点覆盖空白页与分批偏移。
"""

import json
import logging
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
    # 图与图注之间是单换行：空行是切分器的首选切点，会把图和说明拆到两张卡片
    assert pages[0].page_text == f'<img src="{DOC_NAME}/ab-cd.jpg">\n图 1'
    assert (image_dir / "ab-cd.jpg").read_bytes() == b"jpeg-bytes"
    # 正文没引用的图不落盘
    assert not (image_dir / "unused.jpg").exists()


def _jpeg(width: int, height: int) -> bytes:
    import io
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height)).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_caption_and_subfigure_labels_stay_with_their_images(mineru, tmp_path):
    """vlm 后端把图注和子图标签放在紧随其后的 text 块里，必须与插图合成一段。

    段落之间是空行，而空行是切分器的最高优先级切点：图和图注隔着空行就会
    被拆到不同切片，留下一张只有若干 <img> 的卡片。
    """
    pages, _ = _run(mineru, tmp_path, 1, [{
        "blocks": [
            {"type": "text", "text": "正文一段。", "page_idx": 0},
            {"type": "image", "img_path": "images/a.jpg", "page_idx": 0},
            {"type": "text", "text": "(a) 甲", "page_idx": 0},
            {"type": "image", "img_path": "images/b.jpg", "page_idx": 0},
            {"type": "text", "text": "图 3 组合图说明\nFig.3 Composite", "page_idx": 0},
            {"type": "text", "text": "后续正文。", "page_idx": 0},
            # 没挨着插图的"表 N"说明保持独立段落，不能被吸进上一张图
            {"type": "text", "text": "表 1 数据表", "page_idx": 0},
            {"type": "table", "table_body": "<table><tr><td>1</td></tr></table>", "page_idx": 0},
        ],
        "images": {"a.jpg": _jpeg(300, 200), "b.jpg": _jpeg(300, 200)},
    }])

    assert pages[0].page_text == (
        "正文一段。\n\n"
        f'<img src="{DOC_NAME}/a.jpg">\n(a) 甲\n<img src="{DOC_NAME}/b.jpg">\n图 3 组合图说明\nFig.3 Composite\n\n'
        "后续正文。\n\n"
        "表 1 数据表\n\n"
        "<table><tr><td>1</td></tr></table>"
    )


def test_long_text_after_image_is_not_treated_as_caption(mineru, tmp_path):
    """以"图 N"开头的整段正文（如"图 2 展示了……"的长段落）不是图注。"""
    long_text = "图 2 展示了" + "模型在各个数据集上的表现，" * 8
    pages, _ = _run(mineru, tmp_path, 1, [{
        "blocks": [
            {"type": "image", "img_path": "images/a.jpg", "page_idx": 0},
            {"type": "text", "text": long_text, "page_idx": 0},
        ],
        "images": {"a.jpg": _jpeg(300, 200)},
    }])

    assert pages[0].page_text == f'<img src="{DOC_NAME}/a.jpg">\n\n{long_text}'


def test_decorative_small_images_are_dropped(mineru, tmp_path):
    """刊头、logo、作者头像之类的小图不落盘，正文里的引用一并去掉。"""
    pages, _ = _run(mineru, tmp_path, 1, [{
        "blocks": [
            {"type": "image", "img_path": "images/logo.jpg", "page_idx": 0},
            {"type": "text", "text": "正文", "page_idx": 0},
            {"type": "image", "img_path": "images/fig.jpg", "page_idx": 0},
            {"type": "text", "text": "图 1 说明", "page_idx": 0},
        ],
        "images": {"logo.jpg": _jpeg(228, 65), "fig.jpg": _jpeg(640, 386)},
    }])

    image_dir = tmp_path / "working" / DOC_NAME
    assert pages[0].page_text == f'正文\n\n<img src="{DOC_NAME}/fig.jpg">\n图 1 说明'
    assert not (image_dir / "logo.jpg").exists()
    assert (image_dir / "fig.jpg").exists()


def test_unreadable_image_is_kept(tmp_path):
    """读不出尺寸的图按保留处理，不能因为一张坏图丢掉整段引用。"""
    archive_path = tmp_path / "result.zip"
    _write_zip(archive_path, [], images={"odd.jpg": b"not-really-a-jpeg"})

    with zipfile.ZipFile(archive_path) as archive:
        text = mineru_client._store_images('<img src="images/odd.jpg">', archive, tmp_path / "doc")

    assert text == '<img src="doc/odd.jpg">'
    assert (tmp_path / "doc" / "odd.jpg").exists()


def test_transient_batch_failure_is_retried(mineru, tmp_path, monkeypatch):
    """服务端偶发在最后几页翻成 failed，重新提交同一批次即可，不能直接判死整份文档。"""
    monkeypatch.setattr(mineru_client, "BATCH_RETRY_DELAY", 0.0)
    attempts = {"n": 0}
    original_wait = _FakeClient.wait_batch

    def flaky_wait(self, batch_id, file_name, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise mineru_client.MineruTransientError("MinerU 任务失败: parsing failed, please try again later")
        return original_wait(self, batch_id, file_name, **kwargs)

    monkeypatch.setattr(_FakeClient, "wait_batch", flaky_wait)
    pages, _ = _run(mineru, tmp_path, 1, [[{"type": "text", "text": "正文", "page_idx": 0}]])

    assert [page.page_text for page in pages] == ["正文"]
    assert attempts["n"] == 3
    # 每次重试都重新申请上传链接，而不是复用已判失败的 batch
    assert len(_FakeClient.calls) == 3


def test_transient_failure_gives_up_after_max_attempts(mineru, tmp_path, monkeypatch):
    monkeypatch.setattr(mineru_client, "BATCH_RETRY_DELAY", 0.0)

    def always_fail(self, batch_id, file_name, **kwargs):
        raise mineru_client.MineruTransientError("parsing failed")

    monkeypatch.setattr(_FakeClient, "wait_batch", always_fail)
    with pytest.raises(mineru_client.MineruTransientError, match="parsing failed"):
        _run(mineru, tmp_path, 1, [[{"type": "text", "text": "正文", "page_idx": 0}]])
    assert len(_FakeClient.calls) == mineru_client.MAX_BATCH_ATTEMPTS


def test_deterministic_failure_is_not_retried(mineru, tmp_path, monkeypatch):
    """Token 无效、额度用尽这类确定性错误重试只会白白等待。"""
    monkeypatch.setattr(mineru_client, "BATCH_RETRY_DELAY", 0.0)

    def auth_fail(self, name, **options):
        type(self).calls.append({"name": name, **options})
        raise mineru_client.MineruError("MinerU 认证失败 (401)，请检查 Token")

    monkeypatch.setattr(_FakeClient, "request_upload", auth_fail)
    with pytest.raises(mineru_client.MineruError, match="认证失败"):
        _run(mineru, tmp_path, 1, [[{"type": "text", "text": "正文", "page_idx": 0}]])
    assert len(_FakeClient.calls) == 1


def test_image_reference_survives_the_ui_rewrite(mineru, tmp_path):
    """界面把相对引用补成 /working/ 前缀，带空格括号的路径也必须命中。"""
    from app.ui.components import _absolutize_image_srcs

    pages, _ = _run(mineru, tmp_path, 1, [{
        "blocks": [{"type": "image", "img_path": "images/ab_cd.jpg", "page_idx": 0}],
        "images": {"ab_cd.jpg": b"jpeg-bytes"},
    }])

    rewritten = _absolutize_image_srcs(pages[0].page_text)
    # 路径按 URL 编码发给浏览器，解码后必须还是同一个相对引用
    from urllib.parse import unquote
    assert rewritten.startswith('<img src="/working/')
    assert unquote(rewritten) == f'<img src="/working/{DOC_NAME}/ab-cd.jpg">'


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
    # 在线服务把参考文献每条输出为顶层 ref_text 块，不认它整份文档的参考文献都会丢
    assert render({"type": "ref_text", "text": "[1] 甲. 乙[J]. 丙, 2020."}) == "[1] 甲. 乙[J]. 丙, 2020."
    # 未知类型不能拖垮整页：带文本的按正文保留，没有文本才跳过
    assert render({"type": "brand_new_block", "text": "?"}) == "?"
    assert render({"type": "brand_new_block"}) == ""


def test_unknown_block_type_is_warned_once(monkeypatch, caplog):
    """新块类型只在首次出现时告警：一种类型一条，不按块数刷屏。"""
    monkeypatch.setattr(mineru_client, "_UNKNOWN_TYPES_SEEN", set())
    caplog.set_level(logging.WARNING, logger=mineru_client.__name__)
    for _ in range(3):
        mineru_client._render_block({"type": "brand_new_block", "text": "?"})
    warnings = [
        record for record in caplog.records
        if record.name == mineru_client.__name__ and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "brand_new_block" in warnings[0].getMessage()


def test_reference_entries_are_kept_on_their_pages(mineru, tmp_path):
    """参考文献页：标题是 text 块，条目是 ref_text 块，二者都得进正文且分页正确。

    实际文档里参考文献跨页时，后一页只有 ref_text 块；漏掉它那页就只剩
    页尾的基金项目，知识卡片上参考文献凭空消失。
    """
    pages, _ = _run(mineru, tmp_path, 2, [[
        {"type": "text", "text": "参考文献：", "text_level": 2, "page_idx": 0},
        {"type": "ref_text", "text": "[1] 甲", "page_idx": 0},
        {"type": "ref_text", "text": "[2] 乙", "page_idx": 1},
        {"type": "text", "text": "基金项目：", "text_level": 2, "page_idx": 1},
    ]])

    assert pages[0].page_text == "## 参考文献：\n\n[1] 甲"
    assert pages[1].page_text == "[2] 乙\n\n## 基金项目："


def test_table_body_drops_html_wrapper():
    """切分侧的表格保护只认 <table>，外层 html/body 必须剥掉。"""
    rendered = mineru_client._render_block({
        "type": "table",
        "table_caption": ["表 1"],
        "table_body": "<html><body><table><tr><td>1</td></tr></table></body></html>",
    })

    assert rendered == "表 1\n\n<table><tr><td>1</td></tr></table>"
    assert "html" not in rendered and "body" not in rendered


def test_footnotes_of_images_and_tables_are_kept():
    """期刊末页的作者简介挂在头像的 image_footnote 里，漏掉就整段消失。"""
    image = mineru_client._render_block({
        "type": "image",
        "img_path": "images/a.jpg",
        "image_caption": [],
        "image_footnote": ["薛迪(2001—),男,硕士研究生。"],
    })
    table = mineru_client._render_block({
        "type": "table",
        "table_body": "<table><tr><td>1</td></tr></table>",
        "table_footnote": ["注：数据来自实验"],
    })

    assert image == '<img src="images/a.jpg">\n薛迪(2001—),男,硕士研究生。'
    assert table.endswith("\n\n注：数据来自实验")


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
