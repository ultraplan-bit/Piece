"""
可复用 UI 组件

职责:
- 定义可在多个页面复用的组件
- 保持 UI 风格一致性
"""

from copy import deepcopy
from typing import Callable, Optional
from functools import cache
from html import unescape
import inspect
import logging
import re
from urllib.parse import quote

import markdown2
from nicegui import context, ui
from app.i18n import t

logger = logging.getLogger(__name__)


CATALOG_KEY_HANDLER = """(event) => {
    if (event.isComposing || event.altKey ||
        event.target.closest('input, textarea, select, button, a, [contenteditable=true], [role=menuitem]')) return;
    const row = event.target.closest('[data-catalog-row]');
    const catalog = row?.closest('[data-catalog]');
    if (!catalog) return;
    const rows = [...catalog.querySelectorAll('[data-catalog-row]')];
    const index = rows.indexOf(row);
    const focus = next => {
        if (!next) return;
        rows.forEach(item => item.tabIndex = item === next ? 0 : -1);
        next.focus({preventScroll: true}); next.scrollIntoView({block: 'nearest'});
        if (!event.ctrlKey && !event.metaKey &&
            (catalog.dataset.multiselect !== 'true' || event.shiftKey)) {
            next.dispatchEvent(new MouseEvent('click', {bubbles: true, shiftKey: event.shiftKey}));
        }
    };
    if (catalog.dataset.tree === 'true' && ['ArrowLeft', 'ArrowRight'].includes(event.key)) {
        event.preventDefault(); event.stopPropagation();
        const expanded = row.getAttribute('aria-expanded') === 'true';
        if (row.dataset.collectionId && ((event.key === 'ArrowRight' && !expanded) ||
            (event.key === 'ArrowLeft' && expanded))) {
            row.dispatchEvent(new MouseEvent('dblclick', {bubbles: true}));
        } else {
            focus(rows.find(item => event.key === 'ArrowRight'
                ? item.dataset.parentRow === row.dataset.catalogRow
                : item.dataset.catalogRow === row.dataset.parentRow));
        }
        return;
    }
    if (event.key === 'F2' && row.dataset.collectionId) {
        event.preventDefault(); row.dispatchEvent(new CustomEvent('rename-collection')); return;
    }
    if (catalog.dataset.catalog === 'files' && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a') {
        event.preventDefault(); catalog.dispatchEvent(new CustomEvent('catalog-select-all')); return;
    }
    const directions = {ArrowUp: index - 1, ArrowDown: index + 1, Home: 0, End: rows.length - 1};
    if (event.key in directions) {
        event.preventDefault(); event.stopPropagation();
        focus(rows[Math.max(0, Math.min(rows.length - 1, directions[event.key]))]);
    } else if ((event.key === 'Enter' || event.key === ' ') && !event.repeat &&
               !event.ctrlKey && !event.metaKey) {
        event.preventDefault(); event.stopPropagation();
        const type = row.dataset.collectionId || (event.key === 'Enter' && row.dataset.openOnEnter === 'true') ? 'dblclick' : 'click';
        row.dispatchEvent(new MouseEvent(type, {bubbles: true, shiftKey: event.shiftKey}));
    } else if (event.key === 'Escape' && catalog.dataset.multiselect === 'true') {
        event.preventDefault(); event.stopPropagation();
        catalog.dispatchEvent(new CustomEvent('catalog-cancel'));
    }
}"""


def _open_dialog(dialog) -> None:
    """把对话框挂到页面根容器后再打开。

    NiceGUI 在"事件发起者的父 slot"里执行事件处理器，所以从菜单项或右键菜单
    里创建的对话框会成为该菜单的子元素：菜单一关对话框跟着隐藏（点了没反应），
    下次打开菜单又会把上一次的对话框一并弹出来。
    """
    dialog.move(context.client.content)
    dialog.open()


# 只处理渲染后的 img 标签；跳过引号内的属性文本，不误改代码示例或普通链接。
_IMG_SRC = re.compile(
    r'''(<img\b(?:[^>"']|"[^"]*"|'[^']*')*?\ssrc\s*=\s*)(["'])(.*?)\2''',
    re.IGNORECASE | re.DOTALL,
)


class _PieceLatex(markdown2.Latex):
    """在 markdown2 2.5.5 的 latex 扩展基础上做三处修正。

    - 块公式不再删除 ``\\n`` 字面量：上游 ``_convert_double_match`` 会把
      ``\\nabla``、``\\neq`` 等命令删成 ``abla``、``eq``。
    - 逐个公式兜底：OCR 产出的公式常有 ``x_a_b`` 双下标、``\\frac{`` 缺参数等错误，
      latex2mathml 会抛错（除自有异常外还会漏出 StopIteration/ValueError/IndexError）。
      转换失败的公式按原文放进行内代码显示，同一张卡片里的其余公式照常转成 MathML。
    - 每次渲染前清空 ``code_blocks``：上游把它写成类属性，占位符会跨渲染累积，
      既泄漏内存也让每次回填都遍历历史条目。
    """

    name = "piece-latex"

    def test(self, text: str) -> bool:
        # 上游每次渲染会调用 run 四次；没有 $ 的正文（多数切片）直接跳过
        return "$" in text

    def run(self, text: str) -> str:
        self.code_blocks = {}
        try:
            return super().run(text)
        except Exception:
            # 单个公式的转换异常已在 _convert_match 内兜底，能走到这里的只有 latex2mathml
            # 装载失败（如冻结包漏掉 unimathsymbols.txt），此时整段退回普通 Markdown
            _warn_formula_support_unavailable()
            return text

    def _convert_single_match(self, match):
        return self._convert_match(match, display="inline")

    def _convert_double_match(self, match):
        return self._convert_match(match, display="block")

    def _convert_match(self, match, display: str) -> str:
        from latex2mathml.converter import convert

        try:
            return convert(match.group(1), display=display)
        except Exception as exc:
            # 块公式跨行时压成一行，日志和回退显示都更紧凑
            source = " ".join(match.group(0).split())
            logger.info(
                "[Markdown] 公式无法转换 (%s)，已按原文显示: %.120s",
                type(exc).__name__, source,
            )
            # 借用 markdown2 处理反引号代码段的编码：转义 HTML 并把内容哈希占位，
            # 其中的 _、* 不再被当成斜体/加粗，输出前统一还原。不能直接输出反引号：
            # 段落首尾都是 <math> 时 markdown2 会把整行当作 HTML 块，行内语法不再解析
            return f"<code>{self.md._encode_code(source)}</code>"


_PieceLatex.register()


@cache
def _warn_formula_support_unavailable() -> None:
    """latex2mathml 装载失败会让每张卡片都退回普通 Markdown，堆栈只记一次。"""
    logger.warning("[Markdown] 公式转换不可用，已回退为普通 Markdown", exc_info=True)


# 切片正文的 Markdown 扩展：表格、代码块，以及把 $...$ / $$...$$ 转为 MathML
# （OCR 解析的文档常含公式，latex2mathml 在服务端完成转换无需联网）
_MARKDOWN_EXTRAS = ["fenced-code-blocks", "tables", _PieceLatex.name]


def _absolutize_image_srcs(html_text: str, base_url: str = "/working/") -> str:
    """将已渲染 HTML 的相对图片地址补成工作目录 URL，并编码文件名中的特殊字符。"""
    def replace(match: re.Match) -> str:
        src = unescape(match[3])
        if not src or src.lower().startswith(("https://", "http://", "/", "data:")):
            return match[0]
        return f'{match[1]}{match[2]}{base_url}{quote(src, safe="/")}{match[2]}'

    return _IMG_SRC.sub(replace, html_text)


def help_hint(text: str, *, label: str | None = None):
    """低频帮助不占正文；鼠标、键盘和触屏使用同一提示。"""
    with ui.button(icon="help_outline", color=None).props("flat dense round no-ripple").classes("help-hint") as button:
        tooltip = ui.tooltip(text).props('anchor="bottom middle" self="top middle"').classes("help-tooltip")
    button._props.update({"aria-label": label or t("workspace.help"), "aria-describedby": f"c{tooltip.id}"})
    for event in ("focus", "click"):
        button.on(event, js_handler=f"() => getElement({tooltip.id}).show()")
    for event in ("blur", "keydown.escape.stop"):
        button.on(event, js_handler=f"() => getElement({tooltip.id}).hide()")
    return button


def workspace_controls(callbacks: dict, *, include_navigation: bool = True):
    """目录常驻；工具栏只提供回到目录的焦点入口。"""
    for key, icon, label in (
        ("toggle_navigation", "menu", "workspace.navigation"),
        ("focus_catalog", "view_sidebar", "workspace.results"),
    ):
        if key == "toggle_navigation" and not include_navigation:
            continue
        ui.button(icon=icon, color=None, on_click=callbacks.get(key)).props(
            f'flat dense round size=sm aria-label="{t(label)}"'
        ).classes("theme-text-muted").tooltip(t(label))


def status_badge(status: str):
    """状态徽章组件（紧凑样式）"""
    color_map = {
        "pending": "orange",
        "processing": "blue",
        "completed": "grey",
        "indexed": "grey",
        "failed": "red",
        "error": "red",
        "empty": "grey",
    }
    color = color_map.get(status, "gray")
    label = t("file_status." + status) if status in color_map else status
    ui.badge(label, color=None if color == "grey" else color,
             text_color="var(--text-muted)" if color == "grey" else None).props("dense outline")


def chunk_card(
    doc_title: str,
    chunk_text: str,
    chunk_id: int = None,
    on_edit: Optional[Callable] = None,
    on_delete: Optional[Callable] = None,
    parent_path: Optional[str] = None,
    source_page: Optional[int] = None,
    on_view_source: Optional[Callable] = None,
):
    """
    切片卡片组件（支持编辑/删除）

    Args:
        doc_title: 文档标题
        chunk_text: 切片文本内容（支持Markdown格式）
        chunk_id: 切片 ID
        on_edit: 编辑回调函数，传入 chunk_id
        on_delete: 删除回调函数，传入 chunk_id
        parent_path: 上级标题路径，显示为面包屑；顶层切片无上级则不显示
        source_page: 切片在原始 PDF 中的页码，非 PDF 文档为 None
        on_view_source: 查看原页的回调，传入 source_page

    Returns:
        卡片根元素，供调用方追加定位用的 class
    """
    with ui.card().tight().props("flat").classes("w-full mb-2 chunk-sheet overflow-hidden") as card:
        # w-full 不能省：NiceGUI 的 .nicegui-card 是 align-items:flex-start 的 flex 容器，
        # 内容区默认按正文宽度收缩，标题行的分隔线会随正文长短忽长忽短
        with ui.card_section().classes("w-full py-2 px-3 min-w-0"):
            # 标题行：标题 + 操作按钮 + 序号
            # w-full 不能省：否则这一行按内容宽度收缩，justify-between 失效、
            # 分隔线也只画半截
            with ui.row().classes("w-full items-center justify-between gap-2 mb-2 chunk-heading flex-nowrap"):
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label(doc_title).classes(
                        "font-semibold text-sm theme-text truncate"
                    ).tooltip(doc_title)
                    # 面包屑只给上级路径：末段就是标题本身，重复显示没有信息量
                    if parent_path:
                        ui.label(parent_path).classes(
                            "text-xs theme-text-muted truncate"
                        ).tooltip(parent_path)
                with ui.row().classes("items-center gap-1 shrink-0"):
                    # 原页按钮：PDF 切片是 OCR/文本层加工后的结果，
                    # 校对时需要能回看原件对应页
                    if source_page and on_view_source:
                        ui.button(
                            icon="image",
                            on_click=lambda: on_view_source(source_page)
                        ).props(f'flat dense round size=xs aria-label="{t("chunks.view_source_page", page=source_page)}"').classes(
                            "theme-text-muted"
                        ).tooltip(t("chunks.view_source_page", page=source_page))
                    if on_edit:
                        ui.button(
                            icon="edit", on_click=lambda: on_edit(chunk_id)
                        ).props(f'flat dense round size=xs aria-label="{t("chunks.edit")}"').classes(
                            "theme-text-muted"
                        ).tooltip(t("chunks.edit"))
                    if on_delete:
                        with ui.button(icon="more_horiz").props(
                            f'flat dense round size=xs aria-label="{t("workspace.more")}"'
                        ).classes("theme-text-muted").tooltip(t("workspace.more")):
                            with ui.menu():
                                ui.menu_item(t("chunks.delete_confirm_title"), on_click=lambda: on_delete(chunk_id)).classes("theme-danger")
                    if chunk_id:
                        ui.label(f"#{chunk_id}").classes("text-xs theme-text-muted")

            # 正文内容
            chunk_markdown(chunk_text, chunk_id=chunk_id)

    return card


def chunk_markdown(chunk_text: str, chunk_id: int | None = None, file_path: str | None = None):
    """
    切片正文的 Markdown 渲染（相对图片引用重写为 /working/ 绝对路径，
    公式转 MathML，转换失败的公式按原文显示）

    Args:
        chunk_text: 切片文本内容

    Returns:
        ui.markdown 元素，供调用方控制显隐
    """
    base_url = "/working/"
    if chunk_id is not None and file_path is None:
        from indexing.services.chunk_service import get_chunk_by_id
        from indexing.services.file_service import get_file_by_id
        chunk = get_chunk_by_id(chunk_id)
        file = get_file_by_id(chunk["file_id"]) if chunk else None
        file_path = file["file_path"] if file else None
    if file_path:
        from pathlib import Path
        from indexing.services.file_service import get_working_dir
        relative = Path(file_path).resolve().parent.relative_to(get_working_dir().resolve())
        if relative.parts:
            base_url += quote(relative.as_posix()) + "/"
    # 先让 Markdown 解析器处理空格、配对括号和引用式图片，再统一改写 HTML 地址。
    # 同步更新元素属性，确保首次发给浏览器的就是正确 URL，不先触发一次错误请求。
    rendered = ui.markdown(chunk_text, extras=_MARKDOWN_EXTRAS)
    rendered._props["innerHTML"] = _absolutize_image_srcs(rendered._props["innerHTML"], base_url)
    return rendered.classes("chunk-content text-sm leading-relaxed")


def guard_unsaved(dialog, *, busy: Callable[[], bool] | None = None,
                  pending: Callable[[], bool] | None = None):
    """跟踪已渲染的表单值，返回统一的取消处理器；还原原值后不再提示。"""
    controls = [element for element in dialog.descendants()
                if isinstance(element, (ui.input, ui.select, ui.checkbox, ui.switch))]
    original = [deepcopy(element.value) for element in controls]
    status = ui.label(t("workspace.unsaved")).classes("text-xs theme-text-muted")
    status.set_visibility(False)

    def changed():
        return [element.value for element in controls] != original

    def update_status():
        status.set_visibility(changed())

    for element in controls:
        element.on_value_change(update_status)
    dialog.props("persistent")

    def request_close():
        if busy and busy():
            return
        if not changed() and not (pending and pending()):
            dialog.close()
            return
        confirm_dialog(
            title=t("workspace.discard_title"), message=t("workspace.discard_message"),
            on_confirm=dialog.close, confirm_text=t("workspace.discard"),
            cancel_text=t("workspace.keep_editing"),
        )

    return request_close


def chunk_dialog(
    owner_id: int,
    on_save: Callable,
    on_close: Callable,
    doc_title: str = "",
    chunk_text: str = "",
    is_edit: bool = False,
):
    """
    切片编辑/新增对话框

    Args:
        owner_id: 编辑时为 chunk_id，新增时为 file_id
        on_save: 保存回调，传入 (owner_id, doc_title, chunk_text)
        on_close: 关闭回调
        doc_title: 初始标题（编辑时为当前值）
        chunk_text: 初始内容（编辑时为当前值）
        is_edit: True 为编辑已有切片，False 为新增
    """
    form_data = {"doc_title": doc_title, "chunk_text": chunk_text}
    busy = {"value": False}

    title = (
        t("chunk_dialog.edit_title", id=owner_id)
        if is_edit
        else t("chunk_dialog.add_title")
    )
    hint = t("chunk_dialog.edit_hint") if is_edit else t("chunk_dialog.add_hint")
    save_text = (
        t("chunk_dialog.btn_save") if is_edit else t("chunk_dialog.btn_add")
    )

    # on_save 多为 async 方法，必须在 handler 内 await；
    # 若放进 lambda 的列表里返回，NiceGUI 拿到的是 list 而非 awaitable，协程不会被执行。
    async def handle_save():
        if busy["value"]:
            return
        busy["value"] = True
        try:
            result = on_save(owner_id, form_data["doc_title"], form_data["chunk_text"])
            if inspect.isawaitable(result):
                await result
            dialog.close()
        finally:
            busy["value"] = False

    with ui.dialog() as dialog, ui.card().classes("w-[780px] max-w-full max-h-[90vh] overflow-auto theme-card"):
        # 标题栏
        with ui.row().classes(
            "w-full items-center justify-between pb-2"
        ).style("border-bottom: 1px solid var(--border-color)"):
            ui.label(title).classes("text-base font-semibold theme-text")
            ui.button(icon="close", on_click=lambda: request_close()).props("flat dense round").classes("theme-text-muted")

        # 表单内容（编辑时 value 回填，新增时 value 为空以显示 placeholder）
        with ui.column().classes("w-full gap-4 py-4"):
            ui.input(
                label=t("chunk_dialog.label_title"),
                value=doc_title,
                placeholder=t("chunk_dialog.placeholder_title"),
                on_change=lambda e: form_data.update({"doc_title": e.value}),
            ).props("dense outlined").classes("w-full")

            ui.textarea(
                label=t("chunk_dialog.label_content"),
                value=chunk_text,
                placeholder=t("chunk_dialog.placeholder_content"),
                on_change=lambda e: form_data.update({"chunk_text": e.value}),
            ).props("outlined rows=12").classes("w-full")

            ui.label(hint).classes("text-xs theme-text-muted")

        # 底部按钮
        with ui.row().classes("w-full items-center gap-2 pt-2").style("border-top: 1px solid var(--border-color)"):
            request_close = guard_unsaved(dialog, busy=lambda: busy["value"])
            ui.space()
            ui.button(t("chunk_dialog.btn_cancel"), on_click=request_close).props("flat").classes("theme-text-muted")
            ui.button(
                save_text,
                on_click=handle_save,
            ).props("unelevated color=primary")

    dialog.on("close", on_close)
    _open_dialog(dialog)
    return dialog


def confirm_dialog(
    title: str,
    message: str,
    on_confirm: Callable,
    confirm_text: str = None,
    cancel_text: str = None,
    danger: bool = False,
):
    """
    确认对话框（支持同步和异步回调）

    Args:
        title: 对话框标题
        message: 提示信息
        on_confirm: 确认回调（支持同步和异步函数）
        confirm_text: 确认按钮文字
        cancel_text: 取消按钮文字
        danger: 是否为危险操作（红色按钮）
    """
    # 使用默认翻译文本
    if confirm_text is None:
        confirm_text = t("confirm_dialog.btn_confirm")
    if cancel_text is None:
        cancel_text = t("confirm_dialog.btn_cancel")

    # 判断返回值而不是回调本身，兼容返回协程的 lambda。
    busy = False

    async def handle_confirm():
        nonlocal busy
        if busy:
            return
        busy = True
        dialog.props("persistent")
        confirm_button.props("loading")
        for button in (confirm_button, cancel_button, close_button):
            button.disable()
        try:
            result = on_confirm()
            if inspect.isawaitable(result):
                await result
            dialog.close()
        finally:
            busy = False
            if not dialog.is_deleted:
                dialog.props(remove="persistent")
                confirm_button.props(remove="loading")
                for button in (confirm_button, cancel_button, close_button):
                    button.enable()

    with ui.dialog() as dialog, ui.card().classes("w-[440px] max-w-full theme-card theme-card-shadow"):
        with ui.row().classes(
            "w-full items-center justify-between pb-2"
        ).style("border-bottom: 1px solid var(--border-color)"):
            ui.label(title).classes("text-base font-semibold theme-text")
            close_button = ui.button(icon="close", on_click=dialog.close).props(
                f'flat dense round aria-label="{cancel_text}"'
            ).classes("theme-text-muted")

        with ui.column().classes("w-full py-4"):
            ui.label(message).classes("text-sm theme-text-secondary whitespace-pre-line")

        with ui.row().classes("w-full justify-end gap-2 pt-2").style("border-top: 1px solid var(--border-color)"):
            cancel_button = ui.button(cancel_text, on_click=dialog.close).props("flat no-caps autofocus").classes("theme-text-muted")
            confirm_button = ui.button(confirm_text, on_click=handle_confirm).props(
                "unelevated no-caps color=red" if danger else "unelevated no-caps color=primary"
            )

    _open_dialog(dialog)
    return dialog


def file_create_dialog(
    on_create: Callable,
    on_close: Callable = None,
):
    """
    新建文件对话框

    Args:
        on_create: 创建回调，传入 filename
        on_close: 关闭回调
    """
    form_data = {"filename": ""}

    # 同 chunk_dialog：on_create 为 async 方法，需在 handler 内 await
    async def handle_create():
        result = on_create(form_data["filename"])
        if inspect.isawaitable(result):
            await result
        dialog.close()

    with ui.dialog() as dialog, ui.card().classes("w-[400px] theme-card theme-card-shadow"):
        # 标题栏
        with ui.row().classes(
            "w-full items-center justify-between pb-2"
        ).style("border-bottom: 1px solid var(--border-color)"):
            ui.label(t("file_dialog.create_title")).classes("text-base font-semibold theme-text")
            help_hint(t("file_dialog.create_hint"), label=t("file_dialog.create_title"))
            ui.space()
            ui.button(icon="close", on_click=dialog.close).props("flat dense round").classes("theme-text-muted")

        # 表单内容
        with ui.column().classes("w-full gap-4 py-4"):
            ui.input(
                label=t("file_dialog.label_filename"),
                placeholder=t("file_dialog.placeholder_filename"),
                on_change=lambda e: form_data.update({"filename": e.value}),
            ).props("dense outlined").classes("w-full")

        # 底部按钮
        with ui.row().classes("w-full justify-end gap-2 pt-2").style("border-top: 1px solid var(--border-color)"):
            ui.button(t("file_dialog.btn_cancel"), on_click=dialog.close).props("flat").classes("theme-text-muted")
            ui.button(
                t("file_dialog.btn_create"),
                on_click=handle_create,
            ).props("color=primary").classes("theme-card-shadow")

    if on_close:
        dialog.on("close", on_close)
    _open_dialog(dialog)
    return dialog


def collection_labels(names: list):
    """文件列表中的集合标签（紧凑，避免撑破 256px 的中栏）"""
    if not names:
        return
    with ui.row().classes("w-full items-center gap-1 min-w-0 flex-nowrap"):
        ui.icon("folder", size="10px").classes("theme-text-muted shrink-0").props("aria-hidden=true")
        ui.label(" · ".join(names)).classes(
            "text-xs theme-text-muted truncate flex-1 min-w-0"
        ).tooltip(" · ".join(names))


def collection_path_label(item: dict) -> str:
    """路径仅用于展示，永远不将显示字符串解析回集合身份。"""
    return " › ".join(part["name"] for part in item.get("path", [])) or item["name"]


def collection_manage_dialog(
    collections: list,
    on_create: Callable,
    on_rename: Callable,
    on_move: Callable,
    on_delete: Callable,
    on_preview_delete: Callable,
    selected_id: int | None = None,
    on_close: Callable | None = None,
):
    """按稳定 ID 管理集合；删除先预览，最终写入仍由服务重新校验。"""
    items = {"list": list(collections), "selected_id": selected_id}

    async def run(callback, *args):
        result = callback(*args)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, list):
            items["list"] = result
            if items["selected_id"] not in {item["id"] for item in result}:
                items["selected_id"] = None
            editor.refresh()

    def select(value):
        items["selected_id"] = value
        editor.refresh()

    async def delete(collection_id):
        preview = await on_preview_delete(collection_id)
        if preview is None:
            return
        confirm_dialog(
            title=t("collections.delete"),
            message=t("collections.delete_preview", count=preview["direct_file_count"],
                      unclassified=preview["unclassified_file_count"]),
            on_confirm=lambda: run(on_delete, collection_id),
            danger=True,
        )

    with ui.dialog() as dialog, ui.card().classes("w-[520px] max-w-full theme-card theme-card-shadow"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.label(t("collections.manage_title")).classes("text-base font-semibold theme-text")
            help_hint(t("collections.manage_hint"), label=t("collections.manage_title"))
            ui.space()
            ui.button(icon="close", on_click=dialog.close).props("flat dense round").classes("theme-text-muted")

        @ui.refreshable
        def editor():
            options = {item["id"]: collection_path_label(item) for item in items["list"]}
            ui.select(options, value=items["selected_id"], label=t("collections.tree_title"),
                      on_change=lambda e: select(e.value)).props("outlined dense options-dense clearable").classes("w-full")
            item = next((item for item in items["list"] if item["id"] == items["selected_id"]), None)
            if item:
                cid = item["id"]
                with ui.row().classes("w-full items-center flex-nowrap"):
                    name = ui.input(value=item["name"], label=t("collections.rename")).props("dense outlined").classes("flex-1 min-w-0")
                    ui.button(icon="check", on_click=lambda: run(on_rename, cid, name.value)).props("flat dense round").tooltip(t("chunk_dialog.btn_save"))
                    name.on("keydown.enter", lambda: run(on_rename, cid, name.value))
                ui.label(t("collections.counts", direct=item["direct_file_count"],
                           subtree=item["subtree_file_count"])).classes("text-xs theme-text-muted")
                # 只展示合法父候选；服务端仍会在事务内再次检查循环。
                parents = {0: t("collections.root")}
                parents.update({candidate["id"]: collection_path_label(candidate)
                                for candidate in items["list"]
                                if cid not in {part["id"] for part in candidate["path"]}})
                with ui.row().classes("w-full items-center flex-nowrap"):
                    parent = ui.select(parents, value=item["parent_id"] or 0,
                                       label=t("collections.parent")).props("outlined dense options-dense").classes("flex-1 min-w-0")
                    ui.button(t("collections.move"), on_click=lambda: run(on_move, cid, parent.value or None)).props("flat dense")
                ui.button(t("collections.delete"), icon="delete", on_click=lambda: delete(cid)).props("flat color=red")
            ui.separator()
            new_name = ui.input(label=t("collections.create"), placeholder=t("collections.new_placeholder")).props("dense outlined").classes("w-full")
            new_parent = ui.select({0: t("collections.root"), **options}, value=items["selected_id"] or 0,
                                   label=t("collections.parent")).props("dense outlined options-dense").classes("w-full")

            async def create():
                if (new_name.value or "").strip():
                    await run(on_create, new_name.value, new_parent.value or None)

            new_name.on("keydown.enter", create)
            ui.button(t("collections.create_child") if item else t("collections.create"),
                      icon="create_new_folder", on_click=create).props("color=primary")

        editor()
    if on_close:
        dialog.on("close", on_close)
    _open_dialog(dialog)
    return dialog


def file_collections_dialog(
    filename: str,
    collections: list,
    selected_ids: set,
    on_save: Callable,
    on_create: Callable = None,
    on_close: Callable = None,
    *,
    batch: bool = False,
    append: bool = False,
):
    """
    设置文件所属集合的对话框（一个文件可属于多个集合）

    Args:
        filename: 文件名（标题展示用），批量归类时传"已选 N 个文件"
        collections: 全部集合 [{"id", "name"}]
        selected_ids: 当前已归属的集合 ID 集合
        on_save: 保存回调，传入 collection_ids 列表
        on_create: 新建集合回调，传入 name 并返回最新集合列表（None 则不显示新建入口）
        on_close: 关闭回调
    """
    chosen = set(selected_ids)
    items = {"list": list(collections)}

    def toggle(collection_id: int, checked: bool):
        if checked:
            chosen.add(collection_id)
        else:
            chosen.discard(collection_id)

    busy = False

    async def handle_save():
        nonlocal busy
        if busy:
            return
        busy = True
        controls = [element for element in dialog.descendants() if isinstance(element, (ui.button, ui.checkbox, ui.input))]
        for element in controls:
            element.disable()
        dialog.props("persistent")
        try:
            result = on_save(list(chosen))
            if inspect.isawaitable(result):
                result = await result
            if result is not False:
                dialog.close()
        finally:
            busy = False
            dialog.props(remove="persistent")
            for element in controls:
                if not element.is_deleted:
                    element.enable()

    with ui.dialog() as dialog, ui.card().classes("w-[420px] theme-card theme-card-shadow"):
        with ui.row().classes(
            "w-full items-center justify-between pb-2"
        ).style("border-bottom: 1px solid var(--border-color)"):
            ui.label(t("collections.add_title" if append else "collections.assign_title")).classes("text-base font-semibold theme-text")
            ui.button(icon="close", on_click=dialog.close).props("flat dense round").classes("theme-text-muted")

        with ui.column().classes("w-full gap-2 py-3"):
            ui.label(filename).classes("text-sm theme-text-secondary truncate")
            if append or batch:
                ui.label(t("collections.append_hint" if append else "collections.batch_overwrite_hint")).classes("text-xs theme-text-secondary")

            @ui.refreshable
            def checkbox_list():
                if not items["list"]:
                    ui.label(t("collections.empty")).classes("text-xs theme-text-muted")
                    return
                with ui.column().classes("w-full gap-1 max-h-64 overflow-auto"):
                    for item in items["list"]:
                        ui.checkbox(
                            collection_path_label(item),
                            value=item["id"] in chosen,
                            on_change=lambda e, cid=item["id"]: toggle(cid, e.value),
                        ).props("dense").classes("text-sm")

            checkbox_list()

            # 没有集合时不必先去"管理集合"，这里直接建并自动勾上
            if on_create:
                with ui.row().classes("w-full items-center gap-2 pt-1"):
                    new_input = ui.input(
                        placeholder=t("collections.new_placeholder")
                    ).props("dense outlined").classes("flex-1 text-sm")

                    async def add_new():
                        name = (new_input.value or "").strip()
                        if not name:
                            return
                        result = on_create(name)
                        if inspect.isawaitable(result):
                            result = await result
                        if isinstance(result, list):
                            items["list"] = result
                            for item in result:
                                if item["name"] == name:
                                    chosen.add(item["id"])
                            checkbox_list.refresh()
                        new_input.value = ""

                    new_input.on("keydown.enter", add_new)
                    ui.button(icon="add", on_click=add_new).props(
                        "flat dense round size=sm"
                    ).classes("shrink-0 theme-text-accent")

        with ui.row().classes("w-full justify-end gap-2 pt-2").style("border-top: 1px solid var(--border-color)"):
            ui.button(t("chunk_dialog.btn_cancel"), on_click=dialog.close).props("flat").classes("theme-text-muted")
            ui.button(t("chunk_dialog.btn_save"), on_click=handle_save).props("color=primary").classes("theme-card-shadow")

    if on_close:
        dialog.on("close", on_close)
    _open_dialog(dialog)
    return dialog


def file_properties_dialog(
    filename: str,
    properties: dict,
    on_save: Callable,
    on_close: Callable = None,
):
    """
    文件属性编辑对话框（来源、作者、日期等自定义键值）

    Args:
        filename: 文件名（标题展示用）
        properties: 当前属性字典
        on_save: 保存回调，传入属性字典
        on_close: 关闭回调
    """
    # 用列表保存，键可重复编辑，保存时再收敛为字典
    entries = [{"key": key, "value": _property_text(value)} for key, value in properties.items()]

    async def handle_save():
        payload = {}
        for entry in entries:
            key = (entry["key"] or "").strip()
            value = (entry["value"] or "").strip()
            if key and value:
                payload[key] = value
        result = on_save(payload)
        if inspect.isawaitable(result):
            await result
        dialog.close()

    with ui.dialog() as dialog, ui.card().classes("w-[520px] theme-card theme-card-shadow"):
        with ui.row().classes(
            "w-full items-center justify-between pb-2"
        ).style("border-bottom: 1px solid var(--border-color)"):
            ui.label(t("properties.dialog_title")).classes("text-base font-semibold theme-text")
            help_hint(t("properties.hint"), label=t("properties.dialog_title"))
            ui.space()
            ui.button(icon="close", on_click=dialog.close).props("flat dense round").classes("theme-text-muted")

        with ui.column().classes("w-full gap-2 py-3"):
            ui.label(filename).classes("text-sm theme-text-secondary truncate")

            @ui.refreshable
            def rows():
                if not entries:
                    ui.label(t("properties.empty")).classes("text-xs theme-text-muted")
                for entry in entries:
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.input(
                            value=entry["key"],
                            placeholder=t("properties.key_placeholder"),
                            on_change=lambda e, item=entry: item.update({"key": e.value}),
                        ).props("dense outlined").classes("w-32 text-sm")
                        ui.input(
                            value=entry["value"],
                            placeholder=t("properties.value_placeholder"),
                            on_change=lambda e, item=entry: item.update({"value": e.value}),
                        ).props("dense outlined").classes("flex-1 text-sm")
                        ui.button(
                            icon="delete",
                            on_click=lambda _, item=entry: (
                                entries.remove(item),
                                rows.refresh(),
                            ),
                        ).props("flat dense round size=xs").classes("text-red-400")

            rows()

            ui.button(
                t("properties.add_row"),
                icon="add",
                on_click=lambda: (entries.append({"key": "", "value": ""}), rows.refresh()),
            ).props("flat dense size=sm").classes("theme-text-accent self-start")

        with ui.row().classes("w-full justify-end gap-2 pt-2").style("border-top: 1px solid var(--border-color)"):
            ui.button(t("chunk_dialog.btn_cancel"), on_click=dialog.close).props("flat").classes("theme-text-muted")
            ui.button(t("chunk_dialog.btn_save"), on_click=handle_save).props("color=primary").classes("theme-card-shadow")

    if on_close:
        dialog.on("close", on_close)
    _open_dialog(dialog)
    return dialog


def _property_text(value) -> str:
    """属性值统一转为可编辑的单行文本"""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return "" if value is None else str(value)


def card_dialog(
    note_files: list,
    default_box_name: str,
    on_save: Callable,
    on_close: Callable = None,
):
    """
    新建知识卡片对话框

    卡片存放在"卡片盒"（应用内新建的 Markdown 笔记文件）中，
    因此天然享有同步、导出和检索能力。

    Args:
        note_files: 可选卡片盒 [{"id", "filename"}]
        default_box_name: 新建卡片盒时的默认名称
        on_save: 保存回调，传入 (file_id | None, new_box_name, doc_title, chunk_text)
        on_close: 关闭回调
    """
    NEW_BOX = "__new__"
    options = {item["id"]: item["filename"] for item in note_files}
    options[NEW_BOX] = t("cards.new_box_option")
    initial = note_files[0]["id"] if note_files else NEW_BOX

    form = {
        "box": initial,
        "new_box": default_box_name,
        "doc_title": "",
        "chunk_text": "",
    }

    busy = {"value": False}

    async def handle_save():
        if busy["value"]:
            return
        busy["value"] = True
        try:
            file_id = None if form["box"] == NEW_BOX else form["box"]
            new_box_name = form["new_box"] if form["box"] == NEW_BOX else None
            result = on_save(file_id, new_box_name, form["doc_title"], form["chunk_text"])
            if inspect.isawaitable(result):
                await result
            dialog.close()
        finally:
            busy["value"] = False

    with ui.dialog() as dialog, ui.card().classes("w-[780px] max-w-full max-h-[90vh] overflow-auto theme-card"):
        with ui.row().classes(
            "w-full items-center justify-between pb-2"
        ).style("border-bottom: 1px solid var(--border-color)"):
            ui.label(t("cards.dialog_title")).classes("text-base font-semibold theme-text")
            help_hint(t("cards.hint"), label=t("cards.dialog_title"))
            ui.space()
            ui.button(icon="close", on_click=lambda: request_close()).props("flat dense round").classes("theme-text-muted")

        with ui.column().classes("w-full gap-4 py-4"):
            box_select = ui.select(
                options,
                value=initial,
                label=t("cards.label_box"),
                on_change=lambda e: form.update({"box": e.value}),
            ).props("dense outlined").classes("w-full")

            ui.input(
                label=t("cards.label_new_box"),
                value=default_box_name,
                on_change=lambda e: form.update({"new_box": e.value}),
            ).props("dense outlined").classes("w-full").bind_visibility_from(
                box_select, "value", lambda value: value == NEW_BOX
            )

            ui.input(
                label=t("cards.label_title"),
                placeholder=t("cards.placeholder_title"),
                on_change=lambda e: form.update({"doc_title": e.value}),
            ).props("dense outlined").classes("w-full")

            ui.textarea(
                label=t("cards.label_content"),
                placeholder=t("cards.placeholder_content"),
                on_change=lambda e: form.update({"chunk_text": e.value}),
            ).props("outlined rows=10").classes("w-full")

        with ui.row().classes("w-full items-center gap-2 pt-2").style("border-top: 1px solid var(--border-color)"):
            request_close = guard_unsaved(dialog, busy=lambda: busy["value"])
            ui.space()
            ui.button(t("chunk_dialog.btn_cancel"), on_click=request_close).props("flat").classes("theme-text-muted")
            ui.button(t("cards.btn_add"), on_click=handle_save).props("unelevated color=primary")

    if on_close:
        dialog.on("close", on_close)
    _open_dialog(dialog)
    return dialog
