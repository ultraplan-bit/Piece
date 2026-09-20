"""
可复用 UI 组件

职责:
- 定义可在多个页面复用的组件
- 保持 UI 风格一致性
"""

from typing import Callable, Optional
from functools import cache
import inspect
import logging
import re
from urllib.parse import quote

import markdown2
from nicegui import context, ui
from app.i18n import t

logger = logging.getLogger(__name__)


def _open_dialog(dialog) -> None:
    """把对话框挂到页面根容器后再打开。

    NiceGUI 在"事件发起者的父 slot"里执行事件处理器，所以从菜单项或右键菜单
    里创建的对话框会成为该菜单的子元素：菜单一关对话框跟着隐藏（点了没反应），
    下次打开菜单又会把上一次的对话框一并弹出来。
    """
    dialog.move(context.client.content)
    dialog.open()


# 匹配 Markdown/HTML 图片引用中不带协议和根前缀的相对路径，
# 如 src="文档名/img_xxx.jpg" 或 ![alt](文档名/img_xxx.jpg)（OCR 解析产出的工作文件内图片）
_RELATIVE_IMG_SRC = re.compile(r'src="(?!https?://|/|data:)([^"]+)"')
_RELATIVE_IMG_MD = re.compile(r'(\]\()(?!https?://|/|data:)([^)\s]+)(\))')


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


def _absolutize_image_srcs(markdown_text: str, base_url: str = "/working/") -> str:
    """将切片文本中的相对图片引用重写为静态目录绝对路径。

    浏览器按站点根解析相对路径，而工作文件图片挂载在 /working 下，
    需要补前缀才能命中 app.add_static_files 注册的路由。
    路径按 URL 编码：文档名里的 #、%、? 不编码会被浏览器当成片段或查询串。
    """
    text = _RELATIVE_IMG_SRC.sub(lambda m: f'src="{base_url}{quote(m[1], safe="/")}"', markdown_text)
    return _RELATIVE_IMG_MD.sub(lambda m: f'{m[1]}{base_url}{quote(m[2], safe="/")}{m[3]}', text)


def status_badge(status: str):
    """状态徽章组件（紧凑样式）"""
    color_map = {
        "pending": "orange",
        "processing": "blue",
        "completed": "green",
        "indexed": "green",
        "failed": "red",
        "error": "red",
        "empty": "grey",
    }
    color = color_map.get(status, "gray")
    ui.badge(status, color=color).props("dense outline")


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
    with ui.card().tight().props("flat bordered").classes("w-full mb-2 theme-card theme-card-shadow overflow-hidden").style("border: 1px solid var(--border-color)") as card:
        # w-full 不能省：NiceGUI 的 .nicegui-card 是 align-items:flex-start 的 flex 容器，
        # 内容区默认按正文宽度收缩，标题行的分隔线会随正文长短忽长忽短
        with ui.card_section().classes("w-full py-2 px-3 min-w-0"):
            # 标题行：标题 + 操作按钮 + 序号
            # w-full 不能省：否则这一行按内容宽度收缩，justify-between 失效、
            # 分隔线也只画半截
            with ui.row().classes("w-full items-center justify-between gap-2 mb-2 theme-card-divider pb-2 flex-nowrap"):
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
                        ).props("flat dense round size=xs").classes(
                            "theme-text-muted"
                        ).tooltip(t("chunks.view_source_page", page=source_page))
                    # 编辑按钮
                    if on_edit:
                        ui.button(
                            icon="edit",
                            on_click=lambda: on_edit(chunk_id)
                        ).props("flat dense round size=xs").classes("theme-text-muted")
                    # 删除按钮
                    if on_delete:
                        ui.button(
                            icon="delete",
                            on_click=lambda: on_delete(chunk_id)
                        ).props("flat dense round size=xs").classes("theme-text-muted")
                    # ID 徽章
                    if chunk_id:
                        ui.badge(f"#{chunk_id}", color="grey").props(
                            "dense outline"
                        ).classes("text-xs")

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
    content = _absolutize_image_srcs(chunk_text, base_url)
    return ui.markdown(
        content,
        extras=_MARKDOWN_EXTRAS,
    ).classes("chunk-content text-sm leading-relaxed")


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
        result = on_save(
            owner_id,
            form_data["doc_title"],
            form_data["chunk_text"],
        )
        if inspect.isawaitable(result):
            await result
        dialog.close()

    with ui.dialog() as dialog, ui.card().classes("w-[600px] theme-card theme-card-shadow"):
        # 标题栏
        with ui.row().classes(
            "w-full items-center justify-between pb-2"
        ).style("border-bottom: 1px solid var(--border-color)"):
            ui.label(title).classes("text-base font-semibold theme-text")
            ui.button(icon="close", on_click=dialog.close).props("flat dense round").classes("theme-text-muted")

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
        with ui.row().classes("w-full justify-end gap-2 pt-2").style("border-top: 1px solid var(--border-color)"):
            ui.button(t("chunk_dialog.btn_cancel"), on_click=dialog.close).props("flat").classes("theme-text-muted")
            ui.button(
                save_text,
                on_click=handle_save,
            ).props("color=primary").classes("theme-card-shadow")

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

    # 包装回调函数，支持异步。
    # 判断返回值而不是 iscoroutinefunction(on_confirm)：调用方常传
    # `lambda: self._do_xxx(id)`，lambda 本身不是协程函数，但返回协程。
    async def handle_confirm():
        result = on_confirm()
        if inspect.isawaitable(result):
            await result
        dialog.close()

    with ui.dialog() as dialog, ui.card().classes("w-[400px] theme-card theme-card-shadow"):
        with ui.row().classes(
            "w-full items-center justify-between pb-2"
        ).style("border-bottom: 1px solid var(--border-color)"):
            ui.label(title).classes("text-base font-semibold theme-text")
            ui.button(icon="close", on_click=dialog.close).props("flat dense round").classes("theme-text-muted")

        with ui.column().classes("w-full py-4"):
            ui.label(message).classes("theme-text-secondary")

        with ui.row().classes("w-full justify-end gap-2 pt-2").style("border-top: 1px solid var(--border-color)"):
            ui.button(cancel_text, on_click=dialog.close).props("flat").classes("theme-text-muted")
            btn_props = "color=red" if danger else "color=primary"
            ui.button(
                confirm_text,
                on_click=handle_confirm,
            ).props(btn_props).classes("theme-card-shadow")

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
            ui.button(icon="close", on_click=dialog.close).props("flat dense round").classes("theme-text-muted")

        # 表单内容
        with ui.column().classes("w-full gap-4 py-4"):
            ui.input(
                label=t("file_dialog.label_filename"),
                placeholder=t("file_dialog.placeholder_filename"),
                on_change=lambda e: form_data.update({"filename": e.value}),
            ).props("dense outlined").classes("w-full")

            ui.label(t("file_dialog.create_hint")).classes(
                "text-xs theme-text-muted"
            )

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
    with ui.row().classes("items-center gap-1 min-w-0"):
        ui.icon("folder", size="10px").classes("theme-text-muted")
        ui.label(" · ".join(names)).classes(
            "text-xs theme-text-muted truncate"
        ).tooltip(" · ".join(names))


def collection_manage_dialog(
    collections: list,
    on_create: Callable,
    on_rename: Callable,
    on_delete: Callable,
    on_close: Callable = None,
):
    """
    集合管理对话框（新建 / 重命名 / 删除）

    Args:
        collections: 集合列表 [{"id", "name", "file_count"}]
        on_create: 新建回调，传入 name，返回最新集合列表
        on_rename: 重命名回调，传入 (collection_id, name)，返回最新集合列表
        on_delete: 删除回调，传入 collection_id，返回最新集合列表
        on_close: 关闭回调
    """
    items = {"list": list(collections)}

    async def run(callback, *args):
        """执行回调并用其返回的最新集合列表刷新列表"""
        result = callback(*args)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, list):
            items["list"] = result
            rows.refresh()

    with ui.dialog() as dialog, ui.card().classes("w-[460px] theme-card theme-card-shadow"):
        with ui.row().classes(
            "w-full items-center justify-between pb-2"
        ).style("border-bottom: 1px solid var(--border-color)"):
            ui.label(t("collections.manage_title")).classes("text-base font-semibold theme-text")
            ui.button(icon="close", on_click=dialog.close).props("flat dense round").classes("theme-text-muted")

        with ui.column().classes("w-full gap-2 py-3"):
            @ui.refreshable
            def rows():
                if not items["list"]:
                    ui.label(t("collections.empty")).classes("text-xs theme-text-muted")
                    return
                for item in items["list"]:
                    with ui.row().classes("w-full items-center gap-2"):
                        name_input = ui.input(value=item["name"]).props(
                            "dense outlined"
                        ).classes("flex-1 text-sm")
                        # 回车提交重命名，避免每敲一个字都写库
                        name_input.on(
                            "keydown.enter",
                            lambda _, cid=item["id"], field=name_input: run(
                                on_rename, cid, field.value
                            ),
                        )
                        ui.label(
                            t("collections.file_count", count=item["file_count"])
                        ).classes("text-xs theme-text-muted whitespace-nowrap")
                        ui.button(
                            icon="delete",
                            on_click=lambda _, cid=item["id"]: run(on_delete, cid),
                        ).props("flat dense round size=xs").classes("text-red-400")

            rows()

            ui.label(t("collections.manage_hint")).classes("text-xs theme-text-muted")

            with ui.row().classes("w-full items-center gap-2 pt-2").style(
                "border-top: 1px solid var(--border-color)"
            ):
                new_input = ui.input(
                    placeholder=t("collections.new_placeholder")
                ).props("dense outlined").classes("flex-1 text-sm")

                async def add_collection():
                    name = (new_input.value or "").strip()
                    if not name:
                        return
                    await run(on_create, name)
                    new_input.value = ""

                new_input.on("keydown.enter", add_collection)
                ui.button(icon="add", on_click=add_collection).props(
                    "flat dense round size=sm"
                ).classes("theme-text-accent")

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

    async def handle_save():
        result = on_save(list(chosen))
        if inspect.isawaitable(result):
            await result
        dialog.close()

    with ui.dialog() as dialog, ui.card().classes("w-[420px] theme-card theme-card-shadow"):
        with ui.row().classes(
            "w-full items-center justify-between pb-2"
        ).style("border-bottom: 1px solid var(--border-color)"):
            ui.label(t("collections.assign_title")).classes("text-base font-semibold theme-text")
            ui.button(icon="close", on_click=dialog.close).props("flat dense round").classes("theme-text-muted")

        with ui.column().classes("w-full gap-2 py-3"):
            ui.label(filename).classes("text-sm theme-text-secondary truncate")

            @ui.refreshable
            def checkbox_list():
                if not items["list"]:
                    ui.label(t("collections.empty")).classes("text-xs theme-text-muted")
                    return
                with ui.column().classes("w-full gap-1 max-h-64 overflow-auto"):
                    for item in items["list"]:
                        ui.checkbox(
                            item["name"],
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

            ui.label(t("properties.hint")).classes("text-xs theme-text-muted")

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

    async def handle_save():
        file_id = None if form["box"] == NEW_BOX else form["box"]
        new_box_name = form["new_box"] if form["box"] == NEW_BOX else None
        result = on_save(file_id, new_box_name, form["doc_title"], form["chunk_text"])
        if inspect.isawaitable(result):
            await result
        dialog.close()

    with ui.dialog() as dialog, ui.card().classes("w-[600px] theme-card theme-card-shadow"):
        with ui.row().classes(
            "w-full items-center justify-between pb-2"
        ).style("border-bottom: 1px solid var(--border-color)"):
            ui.label(t("cards.dialog_title")).classes("text-base font-semibold theme-text")
            ui.button(icon="close", on_click=dialog.close).props("flat dense round").classes("theme-text-muted")

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

            ui.label(t("cards.hint")).classes("text-xs theme-text-muted")

        with ui.row().classes("w-full justify-end gap-2 pt-2").style("border-top: 1px solid var(--border-color)"):
            ui.button(t("chunk_dialog.btn_cancel"), on_click=dialog.close).props("flat").classes("theme-text-muted")
            ui.button(t("cards.btn_add"), on_click=handle_save).props("color=primary").classes("theme-card-shadow")

    if on_close:
        dialog.on("close", on_close)
    _open_dialog(dialog)
    return dialog
