"""知识卡片渲染与冻结资源回归；只使用隔离的临时知识库。"""

import html
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def components(knowledge_base):
    from nicegui import ui
    from app.ui import components

    components._markdown_extras_for.cache_clear()
    with ui.column() as container:
        yield components
    container.delete()
    components._markdown_extras_for.cache_clear()


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
    ("[网站](https://example.com)\n\n![远程](https://example.com/a.png)", ['href="https://example.com"', 'src="https://example.com/a.png"']),
])
def test_card_renders_supported_markup(components, text, expected):
    rendered = html.unescape(components.chunk_markdown(text)._props["innerHTML"])
    for fragment in expected:
        assert fragment in rendered


def test_invalid_formula_keeps_the_rest_of_the_card(components, caplog):
    rendered = components.chunk_markdown(r"**正文** $\frac{ $")._props["innerHTML"]
    assert "<strong>正文</strong>" in rendered
    assert "<math" not in rendered
    assert "公式渲染失败" in caplog.text


def test_missing_formula_resource_is_logged_once_per_content(components, monkeypatch, caplog):
    import markdown2

    def missing_resource(*args, **kwargs):
        raise FileNotFoundError("unimathsymbols.txt")

    monkeypatch.setattr(markdown2, "markdown", missing_resource)
    for _ in range(2):
        assert components._markdown_extras_for("$x$") == components._MARKDOWN_EXTRAS_NO_LATEX
    warnings = [record for record in caplog.records if record.name == components.__name__]
    assert len(warnings) == 1
    assert "unimathsymbols.txt" in caplog.text


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
