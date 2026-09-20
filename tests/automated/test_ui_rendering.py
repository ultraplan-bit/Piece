"""知识卡片渲染与冻结资源回归；只使用隔离的临时知识库。"""

import html
import logging
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def components(knowledge_base):
    from nicegui import ui
    from nicegui.elements.markdown import prepare_content
    from app.ui import components

    # ui.markdown 按正文缓存渲染结果，用例之间可能复用同一段正文
    prepare_content.cache_clear()
    components._warn_formula_support_unavailable.cache_clear()
    with ui.column() as container:
        yield components
    container.delete()
    prepare_content.cache_clear()
    components._warn_formula_support_unavailable.cache_clear()


def _render(components, text: str) -> str:
    return html.unescape(components.chunk_markdown(text)._props["innerHTML"])


@pytest.mark.parametrize(("text", "expected"), [
    (r"行内 $\frac{1}{2} + \alpha$", ['display="inline"', "<mfrac>", "α"]),
    (r"$$\nabla f \neq 0$$", ['display="block"', "∇", "≠"]),
    ("$$\n\\begin{pmatrix}1 & 2 \\\\ 3 & 4\\end{pmatrix}\n$$", ["<mtable>", "<mtr>"]),
    ("# 标题\n\n**加粗**与*斜体*\n\n> 引用\n\n- 列表", ["<h1>", "<strong>", "<em>", "<blockquote>", "<ul>"]),
    ("| A | B |\n| --- | --- |\n| 1 | 2 |", ["<table>", "<thead>", "<td>1</td>"]),
    ('<table><tr><td colspan="2">合并单元格</td></tr></table>', ['colspan="2"', "合并单元格"]),
    ("```python\ndef square(x):\n    return x * x\n```", ['class="codehilite"', '<span class="k">def</span>']),
    ("`$x$` 与 $x^2$", ["<code>$x$</code>", "<msup>"]),
    ("![插图](book/figure.png)", ['src="/working/book/figure.png"', 'alt="插图"']),
    ('<img src="book/figure.png">', ['src="/working/book/figure.png"']),
    # 文档名里的 # % ? 和空格必须按 URL 编码，否则浏览器当成片段或查询串而 404
    ('<img src="报告 #1 (50%)/图 a.jpg">', ['src="/working/%E6%8A%A5%E5%91%8A%20%231%20%2850%25%29/%E5%9B%BE%20a.jpg"']),
    ("[网站](https://example.com)\n\n![远程](https://example.com/a.png)", ['href="https://example.com"', 'src="https://example.com/a.png"']),
])
def test_card_renders_supported_markup(components, text, expected):
    rendered = _render(components, text)
    for fragment in expected:
        assert fragment in rendered


@pytest.mark.parametrize("formula", [
    # OCR 常见错误：双下标（latex2mathml 自有异常 DoubleSubscriptsError）
    r"$P_{BERT} = \frac{1}{|\hat{x}|}\sum_{\hat{x}_j \in \hat{x}_x_i \in x} \max x_i^T\hat{x}_j$",
    # 缺参数（NoAvailableTokensError）
    r"$\frac{ $",
    # latex2mathml 漏出的非自有异常：StopIteration / IndexError / ValueError
    r"$\text$",
    r"$\genfrac{}{}{0pt}{9}{a}{b}$",
    r"$\begin{align$",
])
def test_invalid_formula_is_shown_verbatim_and_keeps_the_rest_of_the_card(components, caplog, formula):
    caplog.set_level(logging.INFO, logger=components.__name__)
    rendered = _render(components, f"**正文** {formula} 与 $y^2$")
    assert "<strong>正文</strong>" in rendered
    # 坏公式按原文放进 <code>，其中的 _ 和 * 不能被当成斜体/加粗
    assert f"<code>{formula}</code>" in rendered
    assert "<em>" not in rendered
    # 同一张卡片里的其余公式照常转成 MathML
    assert "<msup>" in rendered
    assert "公式无法转换" in caplog.text


def test_invalid_block_formula_is_collapsed_to_one_line(components):
    rendered = _render(components, "$$\nx_a_b\n\\\\ y\n$$")
    assert "<code>$$ x_a_b \\\\ y $$</code>" in rendered
    assert "<math" not in rendered


def test_invalid_formula_between_formulas_at_line_edges(components):
    # 段落首尾都是 <math> 时 markdown2 把整行当作 HTML 块跳过行内解析，
    # 回退结果必须已经是 HTML 而不是依赖 Markdown 再解析的反引号
    rendered = _render(components, "$x^2$ 与 $x_a_b$ 及 $y^2$")
    assert rendered.count("<math") == 2
    assert "<code>$x_a_b$</code>" in rendered
    assert "<em>" not in rendered


def test_unavailable_formula_support_is_logged_once(components, monkeypatch, caplog):
    # 冻结包漏掉 latex2mathml 资源时整段退回普通 Markdown；只在首次记录堆栈，
    # 后续每张卡片不再刷屏
    monkeypatch.setitem(sys.modules, "latex2mathml.converter", None)
    for text in ("$x$ 与 **加粗**", "$y$ 单独"):
        rendered = _render(components, text)
        assert "<math" not in rendered
        assert "$" in rendered
    assert "<strong>加粗</strong>" in _render(components, "$x$ 与 **加粗**")
    warnings = [
        record for record in caplog.records
        if record.name == components.__name__ and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "公式转换不可用" in warnings[0].getMessage()


def test_formula_code_placeholders_do_not_accumulate(components):
    import markdown2

    for _ in range(3):
        _render(components, "`code` 与 $x^2$")
    # 上游把 code_blocks 写成类属性，占位符会跨渲染累积；这里必须保持为空
    assert markdown2.Latex.code_blocks == {}


def _find_elements(element, type_name):
    """按 NiceGUI 元素类型名递归查找子元素。"""
    found = []
    for child in getattr(getattr(element, "default_slot", None), "children", []):
        if type(child).__name__ == type_name:
            found.append(child)
        found.extend(_find_elements(child, type_name))
    return found


def test_ocr_provider_toggle_keeps_label_case():
    """Quasar 按钮默认 text-transform: uppercase（PaddleOCR 会显示成 PADDLEOCR）。

    解析后端选项是产品名，必须保持原样，故带上 no-caps。
    """
    from nicegui import ui
    from app.ui.views import settings_view

    handlers = SimpleNamespace(
        test_ocr_connection=lambda *args: None,
        save_settings_form=lambda *args: None,
    )
    with ui.column() as container:
        settings_view._render_ocr_settings({"ocr_provider": "mineru"}, handlers)

    toggles = _find_elements(container, "Toggle")
    assert len(toggles) == 1, "解析后端应是一个三选一开关"
    toggle = toggles[0]
    assert toggle._props.get("no-caps") is True
    # 选项顺序与取值由 _values 维护，展示文案即用户看到的原文
    assert toggle._values == ["paddle", "vlm", "mineru"]
    labels = [option["label"] for option in toggle._props["options"]]
    assert "PaddleOCR" in labels[0] and "PADDLEOCR" not in labels[0]


def test_bundle_collects_formula_symbols(monkeypatch):
    """执行真实 spec 的收集逻辑，但不运行 Analysis/EXE 等耗时构建阶段。"""
    hooks = pytest.importorskip("PyInstaller.utils.hooks")
    monkeypatch.setattr(hooks, "copy_metadata", lambda *args, **kwargs: [])
    root = Path(__file__).resolve().parents[2]

    def skip_build(*args, **kwargs):
        return SimpleNamespace(pure=[], zipped_data=[], scripts=[], binaries=[], zipfiles=[], datas=[])

    spec = runpy.run_path(str(root / "Piece.spec"), init_globals={
        "SPECPATH": str(root),
        **dict.fromkeys(("Analysis", "PYZ", "EXE", "COLLECT"), skip_build),
    })
    assert any(
        Path(source).name == "unimathsymbols.txt"
        and Path(source).is_file()
        and Path(destination).as_posix() == "latex2mathml"
        for source, destination in spec["datas"]
    ), "冻结包缺少公式转换必需的 latex2mathml/unimathsymbols.txt"
    assert {"latex2mathml", "latex2mathml.converter"} <= set(spec["hiddenimports"])
