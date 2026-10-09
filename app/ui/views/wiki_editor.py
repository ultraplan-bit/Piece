"""Wiki 文档编辑会话：草稿与请求键独立于可重建的界面元素。"""
from copy import deepcopy
import json

from nicegui import context, ui

from app.i18n import t
from app.ui.components import help_hint
from app.ui.views.knowledge_common import CONFLICT_CODES, PendingWrite, STATUSES
from app.ui.views.wiki_presenter import KINDS
from indexing.services.errors import BusinessError


# 在浏览器内一次完成选区变换，避免网络往返覆盖期间的新输入；索引保持 UTF-16 语义。
MARKDOWN_ACTION_JS = r"""event => {
    const ta = event.currentTarget.closest('.wiki-editor').querySelector('.wiki-editor-body textarea');
    if (!ta || ta.disabled || ta.readOnly || ta.dataset.composing === 'true') return;
    const action = __ACTION__, value = ta.value, start = ta.selectionStart, end = ta.selectionEnd;
    const scroll = ta.scrollTop;
    ta.focus();
    if (action === 'link') {
        const text = value.slice(start, end);
        ta.setRangeText('[' + text + '](url)', start, end, 'select');
        const cursor = text ? start + text.length + 3 : start + 1;
        ta.setSelectionRange(cursor, text ? cursor + 3 : cursor);
    } else {
        const prefix = {heading: '# ', list: '- ', quote: '> '}[action];
        if (!prefix) return;
        const from = start === 0 ? 0 : value.lastIndexOf('\n', start - 1) + 1;
        const edge = end > start && value[end - 1] === '\n' ? end - 1 : end;
        let to = value.indexOf('\n', edge);
        if (to < 0) to = value.length;
        const lines = value.slice(from, to).split('\n');
        const remove = lines.some(line => line.trim()) && lines.every(line => !line.trim() || line.startsWith(prefix));
        const replacement = from === to ? prefix : lines.map(line =>
            remove ? (line.startsWith(prefix) ? line.slice(prefix.length) : line) : (line.trim() ? prefix + line : line)
        ).join('\n');
        ta.setRangeText(replacement, from, to, 'select');
        if (start === end) {
            const cursor = Math.min(from + replacement.length, Math.max(from, start + (remove ? -prefix.length : prefix.length)));
            ta.setSelectionRange(cursor, cursor);
        }
    }
    ta.dispatchEvent(new Event('input', {bubbles: true}));
    ta.scrollTop = scroll;
}"""


# 编辑器聚焦时由浏览器拦截 Ctrl/Cmd+S：阻止“保存网页”，改用同一保存流程。
# 组字交给输入法；长按仍阻止浏览器另存页面，但不重复提交。
SAVE_SHORTCUT_JS = r"""event => {
    if (event.key !== 's' && event.key !== 'S') return;
    if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return;
    if (event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    event.stopPropagation();
    if (!event.repeat) emit(null);
}"""

# 状态名到本地化短标签的映射；失败/冲突/未确认都表示草稿仍保留，等待用户处置。
STATUS_KEYS = {
    "saving": "saving",
    "unsaved": "draft_unsaved",
    "unchanged": "draft_unchanged",
    "saved": "saved",
    "failed": "draft_failed",
    "conflict": "draft_conflict",
    "uncertain": "draft_uncertain",
}


class WikiEditor:
    def __init__(self, workbench, record=None):
        self.workbench = workbench
        self.record = deepcopy(record)
        self.draft = {key: deepcopy(record.get(key)) for key in ("kind", "title", "summary", "body", "aliases", "status")} if record else {
            "kind": "concept", "title": "", "summary": "", "body": "", "aliases": [], "status": "active"}
        self.draft["aliases_text"] = "\n".join(self.draft.pop("aliases") or [])
        self.original = deepcopy(self.draft)
        self.reason = {"reason": ""}
        self.pending = None
        self.busy = False
        self.preview = False
        self.message = ""
        self.latest = None
        self.outcome = None
        self.state = "unchanged"
        self.root = None
        self.controls = []
        self.buttons = []
        self.status_label = None
        self.feedback = None

    @property
    def dirty(self):
        return self.draft != self.original or bool(self.reason["reason"])

    def payload(self):
        item = {key: value for key, value in self.draft.items() if key != "aliases_text"}
        item["aliases"] = [value.strip() for value in self.draft["aliases_text"].splitlines() if value.strip()]
        if self.record:
            item.update(id=self.record["id"], expected_revision=self.record["revision"],
                        expected_content_hash=self.record["content_hash"])
        else:
            item["ref"] = "new"
        return {"pages": [item], "reason": self.reason["reason"]}

    async def can_close(self):
        if self.busy:
            ui.notify(self.workbench.text("saving"), type="info")
            return False
        if not self.dirty and self.pending is None:
            return True
        with ui.dialog().props("persistent") as dialog, ui.card().classes("w-[420px] max-w-full theme-card"):
            ui.label(t("workspace.discard_title")).classes("text-base font-semibold theme-text")
            ui.label(t("workspace.discard_message")).classes("text-sm theme-text-secondary")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(t("workspace.keep_editing"), color=None, on_click=lambda: dialog.submit(False)).props("flat no-caps")
                ui.button(t("workspace.discard"), on_click=lambda: dialog.submit(True)).props("unelevated no-caps")
        dialog.move(context.client.content)
        return bool(await dialog)

    async def cancel(self):
        if await self.can_close():
            self.workbench.finish_editing()

    def status_state(self):
        # 优先级：保存中 > 结果未确认 > 冲突/失败/已保存 > 有无改动。
        # 冲突、失败、未确认都保留草稿，必须在标签上与普通“未保存”区分开。
        if self.busy:
            return "saving"
        if (self.pending is not None and self.pending.uncertain) or self.outcome == "uncertain":
            return "uncertain"
        if self.outcome in ("conflict", "failed", "saved"):
            return self.outcome
        return "unsaved" if self.dirty else "unchanged"

    def update_status(self):
        self.state = self.status_state()
        if self.status_label and not self.status_label.is_deleted:
            # 只更新标签文本与状态属性，绝不重建输入控件，避免打字时丢失光标/选区。
            self.status_label.set_text(self.workbench.text(STATUS_KEYS[self.state]))
            self.status_label.props(f"data-state={self.state}")

    async def save_shortcut(self, _event=None):
        # 与保存按钮走同一条路径：不同步改草稿，冲突与未知结果仍按原请求键处理。
        if self.busy:
            return
        await self.save()

    def update_controls(self):
        uncertain = self.pending is not None and self.pending.uncertain
        for element in self.controls:
            if not element.is_deleted:
                element.set_enabled(not self.busy and not uncertain)
        for button in self.buttons:
            if not button.is_deleted:
                button.set_enabled(not self.busy)
        self.update_status()

    def show_feedback(self):
        if self.feedback is None or self.feedback.is_deleted:
            return
        self.feedback.clear()
        if not self.message:
            return
        with self.feedback:
            ui.label(self.message).classes("text-sm theme-text-secondary whitespace-pre-wrap")
            if self.latest:
                with ui.expansion(self.workbench.text("conflict_draft"), icon="difference", value=True).classes("w-full"):
                    ui.label(json.dumps(self.latest, ensure_ascii=False, indent=2)).classes("text-xs whitespace-pre-wrap break-words")
                    ui.button(self.workbench.text("use_revision"), on_click=self.adopt_latest).props("outline no-caps")

    def adopt_latest(self):
        self.workbench.adopt_latest(self.record, self.latest)
        self.latest = None
        # 采用最新版本后冲突已消解；草稿仍是未保存内容。
        self.outcome = None
        self.message = self.workbench.text("compare_then_save")
        self.show_feedback()
        self.update_status()

    async def failure(self, code):
        self.message = self.workbench.text("error", code=code)
        self.outcome = "conflict" if code in CONFLICT_CODES else "failed"
        if code in CONFLICT_CODES and self.record:
            result = await self.workbench.call(self.workbench.service.get_record, kind="page", id=self.record["id"])
            self.latest = result["record"] if result else None
        self.show_feedback()

    async def save(self):
        if self.busy:
            return
        client = self.workbench._workspace_host.client
        self.busy = True
        self.outcome = None
        self.update_controls()
        try:
            if self.pending is None:
                self.pending = PendingWrite(self.payload())
            result = await self.workbench.submit(self.pending)
            code = self.workbench.result_error(result)
            if code is not None:
                self.pending = None
                await self.failure(code)
                return
            # 提交成功：编辑器随后关闭，标签在关闭前如实显示“已保存”。
            self.outcome = "saved"
        except BusinessError as exc:
            self.pending = None
            await self.failure(exc.code)
            return
        except ValueError as exc:
            self.pending = None
            self.outcome = "failed"
            self.message = str(exc)
            self.show_feedback()
            return
        except Exception:
            if self.pending:
                self.pending.uncertain = True
                self.outcome = "uncertain"
                self.message = self.workbench.text("uncertain", request_key=self.pending.key)
                self.show_feedback()
            return

        finally:
            self.busy = False
            self.update_controls()
        # 提交结果与读回分开：读回失败不能把已经成功的写入误报成未知提交。
        # 编辑器会被移除；后续读回/通知必须运行在仍然存活的页面 slot 下。
        with client.content:
            self.workbench.finish_editing()
            await self.workbench.after_write(result)

    def toggle_preview(self):
        if not self.busy:
            self.preview = not self.preview
            self.workbench.rebuild_workspace()

    def render(self):
        self.controls, self.buttons = [], []
        with ui.column().classes("wiki-editor w-full h-full min-h-0 gap-0") as root:
            with ui.row().classes("w-full items-center gap-2 editor-command-bar"):
                ui.icon("edit_note", size="sm").classes("theme-text-muted")
                ui.label(self.workbench.text("edit" if self.record else "create")).classes("text-sm font-medium theme-text")
                self.status_label = ui.label().classes("text-xs theme-text-muted editor-draft-status").props("role=status")
                help_hint(self.workbench.text("draft_retained"))
                ui.space()
                self.buttons.append(ui.button(self.workbench.text("edit_source" if self.preview else "preview"),
                                              color=None, on_click=self.toggle_preview).props("flat dense no-caps"))
                self.buttons.append(ui.button(self.workbench.text("close"), color=None, on_click=self.cancel).props("flat dense no-caps"))
                save_button = ui.button(self.workbench.text("save"), icon="check", on_click=self.save).props("unelevated dense no-caps")
                save_button._props["aria-keyshortcuts"] = "Control+S Meta+S"
                self.buttons.append(save_button)
            with ui.scroll_area().classes("w-full flex-1 min-h-0 scroll-flush"):
                with ui.element("div").classes("wiki-editor-layout"):
                    with ui.column().classes("wiki-editor-paper w-full min-w-0"):
                        if self.preview:
                            ui.label(self.draft["title"] or self.workbench.text("untitled")).classes("knowledge-title theme-text")
                            self.workbench.markdown(self.draft["body"])
                        else:
                            title = ui.input(self.workbench.text("title_field"), value=self.draft["title"]).props("borderless").classes("w-full wiki-title-input")
                            title.bind_value(self.draft, "title")
                            summary = ui.textarea(self.workbench.text("summary"), value=self.draft["summary"]).props("borderless autogrow").classes("w-full wiki-summary-input")
                            summary.bind_value(self.draft, "summary")
                            with ui.row().classes("w-full gap-1 items-center markdown-toolbar"):
                                for icon, action in (("title", "heading"), ("format_list_bulleted", "list"),
                                                     ("link", "link"), ("format_quote", "quote")):
                                    caption = self.workbench.text("md_" + action)
                                    button = ui.button(icon=icon, color=None).props(
                                        f'flat dense round size=sm aria-label="{caption}"'
                                    ).classes("theme-text-muted").tooltip(caption)
                                    button.on("click", js_handler=MARKDOWN_ACTION_JS.replace("__ACTION__", json.dumps(action)))
                                    self.controls.append(button)
                            body = ui.textarea(self.workbench.text("body"), value=self.draft["body"]).props("borderless autogrow").classes("w-full wiki-editor-body")
                            body.bind_value(self.draft, "body")
                            body.on("compositionstart", js_handler="e => { e.target.dataset.composing = 'true'; }")
                            body.on("compositionend", js_handler="e => { delete e.target.dataset.composing; }")
                            self.controls.extend((title, summary, body))
                        self.feedback = ui.column().classes("w-full editor-feedback gap-2")
                    with ui.column().classes("wiki-editor-properties min-w-0"):
                        ui.label(self.workbench.text("page_properties")).classes("workspace-section-label")
                        for name, options in (("kind", KINDS), ("status", STATUSES), ("aliases_text", None)):
                            self.controls.append(self.workbench.field(name, self.draft, options=options, multiline=name == "aliases_text"))
                        self.controls.append(self.workbench.field("reason", self.reason))
        self.root = root
        # 快捷键挂在编辑器根节点：只有焦点在编辑器内（含正文、字段、按钮）时才触发。
        root.on("keydown", self.save_shortcut, js_handler=SAVE_SHORTCUT_JS)
        for element in self.controls:
            if not isinstance(element, ui.button):
                element.on_value_change(self.update_status)
        self.update_controls()
        self.show_feedback()
