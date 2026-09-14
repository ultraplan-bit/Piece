"""
切片插图提取测试

验证 get-docs 的 include_images 支持：
- HTML 与 Markdown 两种引用形态都能识别
- 相对路径按工作文件所在目录解析
- 页眉 logo 等小图被尺寸过滤
- 越出工作目录的引用被拒绝
- 超出上限的图片被计入截断数
"""

import sys
from pathlib import Path

import pytest
from PIL import Image as PILImage

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from retrieval.tools.chunk_images import MIN_IMAGE_EDGE, collect_chunk_images


def _make_workspace(tmp_path: Path) -> Path:
    """构造 working/<文档名>/ 布局，返回工作文件路径。"""
    working = tmp_path / "working"
    image_dir = working / "论文"
    image_dir.mkdir(parents=True)

    PILImage.new("RGB", (640, 386)).save(image_dir / "fig-1.jpg")
    PILImage.new("RGB", (509, 314)).save(image_dir / "fig-2.jpg")
    # 页眉 logo：任一边小于阈值
    PILImage.new("RGB", (59, 62)).save(image_dir / "header-logo.jpg")

    md = working / "论文.md"
    md.write_text("# 论文", encoding="utf-8")
    # 目录外文件，用于验证路径穿越防护
    (tmp_path / "secret.jpg").write_bytes(b"")
    return md


def _doc(md: Path, chunk_text: str) -> dict:
    return {"chunk_text": chunk_text, "file_path": str(md)}


def test_html_and_markdown_refs(tmp_path):
    """OCR 的 <img src> 和用户 md 的 ![]() 都应被识别"""
    md = _make_workspace(tmp_path)
    documents = {
        "论文_第1页": _doc(md, '<img src="论文/fig-1.jpg" alt="Image" width="52%" />'),
        "论文_第2页": _doc(md, "![图2](论文/fig-2.jpg)"),
    }

    images, skipped = collect_chunk_images(documents)

    assert skipped == 0
    assert [img["ref"] for img in images] == ["论文/fig-1.jpg", "论文/fig-2.jpg"]
    assert [img["doc_title"] for img in images] == ["论文_第1页", "论文_第2页"]
    assert Path(images[0]["path"]).is_file()
    print("[OK] HTML 与 Markdown 引用均被识别")


def test_small_images_filtered(tmp_path):
    """页眉 logo 类小图不返回"""
    md = _make_workspace(tmp_path)
    documents = {
        "论文_第1页": _doc(
            md,
            '<img src="论文/header-logo.jpg" />\n<img src="论文/fig-1.jpg" />',
        )
    }

    images, _ = collect_chunk_images(documents)

    assert [img["ref"] for img in images] == ["论文/fig-1.jpg"]
    print(f"[OK] 小于 {MIN_IMAGE_EDGE}px 的装饰图被过滤")


def test_external_and_missing_refs_ignored(tmp_path):
    """外链、data URI、根路径和不存在的文件都不产出图片"""
    md = _make_workspace(tmp_path)
    documents = {
        "论文_第1页": _doc(
            md,
            '<img src="https://example.com/a.jpg" />\n'
            '<img src="data:image/png;base64,AAAA" />\n'
            '<img src="/static/b.jpg" />\n'
            '<img src="论文/not-exist.jpg" />\n'
            "![外链](https://example.com/c.jpg)",
        )
    }

    images, skipped = collect_chunk_images(documents)

    assert images == [] and skipped == 0
    print("[OK] 外链/根路径/缺失文件均被忽略")


def test_path_traversal_rejected(tmp_path):
    """引用不得逃出工作文件所在目录"""
    md = _make_workspace(tmp_path)
    documents = {"论文_第1页": _doc(md, '<img src="../secret.jpg" />')}

    images, _ = collect_chunk_images(documents)

    assert images == []
    print("[OK] 目录穿越引用被拒绝")


def test_max_images_truncates(tmp_path):
    """超出上限的图片计入截断数而非静默丢弃"""
    md = _make_workspace(tmp_path)
    documents = {
        "论文_第1页": _doc(
            md, '<img src="论文/fig-1.jpg" />\n<img src="论文/fig-2.jpg" />'
        )
    }

    images, skipped = collect_chunk_images(documents, max_images=1)

    assert len(images) == 1 and skipped == 1
    print("[OK] 超限图片被截断并计数")


@pytest.mark.parametrize("offset, expected, remaining", [
    (0, [("第一张", "论文/fig-1.jpg")], 1),
    (1, [("第二张", "论文/fig-2.jpg")], 0),
    (2, [], 0),
    (20, [], 0),
])
def test_image_pagination_filters_before_offset(tmp_path, offset, expected, remaining):
    md = _make_workspace(tmp_path)
    documents = {
        "第一张": _doc(
            md,
            '<img src="论文/header-logo.jpg" />\n'
            '<img src="论文/not-exist.jpg" />\n'
            '<img src="../secret.jpg" />\n'
            '<img src="论文/fig-1.jpg" />\n'
            '![重复引用](论文/fig-1.jpg)',
        ),
        "第二张": _doc(md, "![图2](论文/fig-2.jpg)"),
    }

    images, skipped = collect_chunk_images(
        documents, max_images=1, image_offset=offset
    )

    assert [(img["doc_title"], img["ref"]) for img in images] == expected
    assert skipped == remaining


@pytest.mark.parametrize("arguments, message", [
    ({"image_offset": -1}, "image_offset"),
    ({"max_images": 0}, "max_images"),
    ({"max_images": -1}, "max_images"),
])
def test_image_pagination_rejects_invalid_bounds(arguments, message):
    with pytest.raises(ValueError, match=message):
        collect_chunk_images({}, **arguments)


def test_doc_without_file_path():
    """缺少 file_path（如未找到的文档）不应报错"""
    images, skipped = collect_chunk_images(
        {"未知": {"chunk_text": '<img src="a/b.jpg" />'}}
    )

    assert images == [] and skipped == 0
    print("[OK] 缺少 file_path 时安全跳过")


def test_vlm_style_description_yields_nothing(tmp_path):
    """VLM 后端产出的 ![描述] 无链接目标，不应误判"""
    md = _make_workspace(tmp_path)
    documents = {"论文_第1页": _doc(md, "![图：甲状腺结节超声图像，包含A-G共7个子图]")}

    images, _ = collect_chunk_images(documents)

    assert images == []
    print("[OK] VLM 纯文字描述不产出图片")
