"""
网页与 EPUB 转换测试

验证 convert_to_markdown 对另存网页和电子书的处理：
- 网页只取主内容容器，导航/页眉/页脚/脚本不进正文；元数据写成 frontmatter
- 懒加载图片取真实地址；单文件网页内嵌的 data URI 图片落盘
- EPUB 按 spine 逐章成 ## 段，章名取自目录，章内标题降级，代码块不受影响
- EPUB 插图解压到落盘目录并改写引用；加密电子书明确拒绝
"""

import base64
import io
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image as PILImage

sys.path.insert(0, str(Path(__file__).parent.parent))

from indexing.services.chunking import ChunkerFactory
from indexing.services.converter import convert_to_markdown
from indexing.services.metadata_service import parse_frontmatter

WECHAT_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>页面标题 - 站点</title>
<meta property="og:title" content="文章标题">
<meta name="author" content="作者名">
<meta property="og:url" content="https://mp.weixin.qq.com/s/abc">
<meta name="description" content="摘要
一句话">
</head><body>
<nav>首页 分类 关于</nav><header>站点头部</header>
<div id="js_content"><section><p>正文第一段。</p></section>
<img data-src="https://mmbiz.qpic.cn/a.jpg" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">
<section><p>正文第二段。</p></section></div>
<footer>版权信息</footer><script>var x = 1;</script></body></html>"""

PLAIN_HTML = """<html><head><title>无容器页面</title></head><body>
<header>页眉导航</header><h1>正文标题</h1><p>只有 body 的页面正文。</p><footer>页脚</footer></body></html>"""

SECTIONED_HTML = """<html><head><title>分节页面</title></head><body><main>
<h2>第一节</h2><p>一</p><h2>第二节</h2><p>二</p></main></body></html>"""


def _png_bytes(color=(10, 20, 30)) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (300, 200), color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_supported_extensions_include_web_and_ebook_formats():
    assert {".html", ".htm", ".epub"} <= set(ChunkerFactory.get_supported_extensions())


def test_wechat_page_keeps_main_content_and_metadata(tmp_path):
    """公众号另存页：只取 #js_content，元数据成 frontmatter，无小节时标题补成 ## 首行"""
    source = tmp_path / "文章.html"
    source.write_text(WECHAT_HTML, encoding="utf-8")

    metadata, body = parse_frontmatter(convert_to_markdown(source))

    assert metadata["title"] == "文章标题" and metadata["author"] == "作者名"
    assert metadata["source_url"] == "https://mp.weixin.qq.com/s/abc"
    assert metadata["description"] == "摘要 一句话"
    assert body.lstrip().startswith("## 文章标题")
    assert "正文第一段" in body and "正文第二段" in body
    for noise in ("首页 分类", "站点头部", "版权信息", "var x"):
        assert noise not in body
    # 懒加载图片取 data-src，占位 data URI 不进正文
    assert "(https://mmbiz.qpic.cn/a.jpg)" in body and "data:image" not in body
    chunks = ChunkerFactory.get_chunker(".html").chunk(body, "文章")
    assert [chunk["heading_path"] for chunk in chunks] == ["文章 / 文章标题"]


def test_page_without_main_container_falls_back_to_body(tmp_path):
    source = tmp_path / "page.htm"
    source.write_text(PLAIN_HTML, encoding="utf-8")

    metadata, body = parse_frontmatter(convert_to_markdown(source))

    assert metadata["title"] == "无容器页面"
    assert "只有 body 的页面正文" in body
    assert "页眉导航" not in body and "页脚" not in body
    # 页面自带 h1 时把它升成 ##，不再另补页面标题
    assert body.lstrip().startswith("## 正文标题") and "无容器页面" not in body


def test_sectioned_page_is_left_untouched(tmp_path):
    source = tmp_path / "sections.html"
    source.write_text(SECTIONED_HTML, encoding="utf-8")

    _, body = parse_frontmatter(convert_to_markdown(source))

    assert body.lstrip().startswith("## 第一节") and "分节页面" not in body


def test_single_file_page_images_are_saved(tmp_path):
    """SingleFile 等内嵌 data URI 图片的网页：给落盘目录时图片落盘并改写引用"""
    encoded = base64.b64encode(_png_bytes()).decode()
    source = tmp_path / "单文件.html"
    source.write_text(
        f'<html><body><article><p>正文</p><img src="data:image/png;base64,{encoded}"></article></body></html>',
        encoding="utf-8",
    )
    image_dir = tmp_path / "working" / "单文件"

    content = convert_to_markdown(source, image_dir)

    assert [p.suffix for p in image_dir.iterdir()] == [".png"]
    assert "(单文件/image-" in content and "base64" not in content
    # 不给落盘目录时整个 data URI 引用剥掉，正文不残留截断的 base64 前缀
    assert "data:image" not in convert_to_markdown(source)


OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:1234</dc:identifier>
    <dc:title>示例书</dc:title>
    <dc:creator>作者甲</dc:creator>
    <dc:creator>作者乙</dc:creator>
    <dc:publisher>示例出版社</dc:publisher>
    <dc:language>zh</dc:language>
    <dc:description>&lt;p&gt;一本&lt;b&gt;示例&lt;/b&gt;书&lt;/p&gt;</dc:description>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ch1" href="Text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="Text/ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="fig" href="Images/figure.png" media-type="image/png"/>
  </manifest>
  <spine>
    <itemref idref="nav" linear="no"/>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>"""

NAV = """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
<nav epub:type="toc"><ol>
<li><a href="Text/ch1.xhtml">第一章 开始</a></li>
<li><a href="Text/ch1.xhtml#sec">小节锚点</a></li>
<li><a href="Text/ch2.xhtml">第二章 继续</a></li>
</ol></nav></body></html>"""

CH1 = """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1>第一章 开始</h1><p>第一章正文。</p>
<h2 id="sec">小节</h2><p>小节正文。</p>
<img src="../Images/figure.png" alt="图一"/>
<pre><code class="language-python"># 注释
print(1)
</code></pre></body></html>"""

CH2 = """<html xmlns="http://www.w3.org/1999/xhtml"><body><p>第二章没有标题，直接正文。</p></body></html>"""


def _make_epub(tmp_path: Path, *, encrypted: bool = False) -> Path:
    path = tmp_path / "示例书.epub"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        if encrypted:
            archive.writestr("META-INF/encryption.xml", "<encryption/>")
        archive.writestr("OEBPS/content.opf", OPF)
        archive.writestr("OEBPS/nav.xhtml", NAV)
        archive.writestr("OEBPS/Text/ch1.xhtml", CH1)
        archive.writestr("OEBPS/Text/ch2.xhtml", CH2)
        archive.writestr("OEBPS/Images/figure.png", _png_bytes())
    return path


def test_epub_chapters_become_h2_sections_with_metadata(tmp_path):
    source = _make_epub(tmp_path)
    image_dir = tmp_path / "working" / "示例书"

    metadata, body = parse_frontmatter(convert_to_markdown(source, image_dir))

    assert metadata["title"] == "示例书" and metadata["authors"] == ["作者甲", "作者乙"]
    assert metadata["publisher"] == "示例出版社" and metadata["description"] == "一本 示例 书"
    # 章名取自目录；同一文件的锚点条目不另成章
    assert body.count("## 第一章 开始") == 1 and "## 第二章 继续" in body
    assert "小节锚点" not in body
    # 与章名重复的 h1 去掉，章内 h2 降为 ####
    assert body.count("第一章 开始") == 1 and "#### 小节" in body
    # 代码块里的 # 行不当标题处理
    assert "# 注释" in body and "### 注释" not in body
    # 插图解压落盘，引用形态与 Office/OCR 解析一致
    assert [p.suffix for p in image_dir.iterdir()] == [".png"]
    assert "![图一](示例书/image-" in body

    chunks = ChunkerFactory.get_chunker(".epub").chunk(body, "示例书")
    assert [chunk["heading_path"] for chunk in chunks] == ["示例书 / 第一章 开始", "示例书 / 第二章 继续"]


def test_epub_without_image_dir_strips_images_and_rejects_drm(tmp_path):
    body = convert_to_markdown(_make_epub(tmp_path))
    assert "示例书/image-" not in body and "figure.png" not in body

    drm_dir = tmp_path / "drm"
    drm_dir.mkdir()
    with pytest.raises(ValueError, match="DRM"):
        convert_to_markdown(_make_epub(drm_dir, encrypted=True))
