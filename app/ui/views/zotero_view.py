"""Zotero 文献工作区：先查阅，再选择附件；写入只执行确认过的服务端计划。"""

import json
from functools import partial
from html.parser import HTMLParser

from nicegui import ui

from app.i18n import t
from app.ui.components import _open_dialog
from app.ui.import_dialogs import _dialog_frame
from indexing.services import collection_service, file_service, task_service
from indexing.services import zotero_service as zotero
from indexing.services import zotero_write_service as writes
from indexing.services.errors import BusinessError
from indexing.settings import get_settings
from indexing.utils import run_sync


def _dialog(title, width="w-[520px] max-w-[94vw]"):
    dialog, body = _dialog_frame(title, width)
    _open_dialog(dialog)
    return dialog, body


class _NoteText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "div", "br", "li", "h1", "h2", "h3", "blockquote"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in {"p", "div", "li", "blockquote"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def note_text(value):
    """只展示文本，不执行笔记 HTML，也不自动加载内嵌图片或外部链接。"""
    parser = _NoteText()
    parser.feed(str(value or ""))
    return "".join(parser.parts).strip()


def _title(item):
    data = item.get("data", {})
    return str(data.get("title") or note_text(data.get("note"))[:90]
               or data.get("annotationText") or data.get("annotationComment") or item.get("key") or "—")


def _authors(item):
    return ", ".join(person.get("name") or " ".join(filter(None, (person.get("firstName"), person.get("lastName"))))
                     for person in item.get("data", {}).get("creators", []))


def _lines(value):
    return list(dict.fromkeys(line.strip() for line in str(value or "").splitlines() if line.strip()))


def render_zotero_sources(sources, on_open=None):
    """结构化来源只读展示，不把来源数组压成可编辑的字符串。"""
    if not isinstance(sources, list):
        return
    for source in sources:
        if not isinstance(source, dict):
            continue
        key = source.get("item_key")
        with ui.column().classes("w-full gap-1 py-2"):
            ui.label(t("zotero.source_title")).classes("text-xs font-semibold theme-text")
            ui.label(t("zotero.source_keys", item=key or "—", attachment=source.get("attachment_key") or "—")).classes(
                "text-xs theme-text-secondary break-all")
            ui.label(t("zotero.source_versions", item=source.get("item_version", "—"),
                       attachment=source.get("attachment_version", "—"))).classes("text-xs theme-text-muted")
            ui.label(f"{source.get('library_type', 'user')}/{source.get('library_id', 0)} · {source.get('server_id') or '—'}").classes(
                "text-xs theme-text-muted font-mono break-all")
            if on_open and key and source.get("server_id"):
                ui.button(t("zotero.open_item"), color=None,
                          on_click=partial(on_open, key, source["server_id"])).props("flat dense no-caps")


class ZoteroWorkbench:
    def __init__(self, *, open_file=None, refresh_files=None, open_settings=None):
        self.open_file = open_file
        self.refresh_files = refresh_files
        self.open_settings = open_settings
        self.busy = False
        self.error = ""
        self.last_error = None
        self.info = {}
        self.query = ""
        self.collection_key = None
        self.tag = ""
        self.collections = {}
        self.collection_next = None
        self.results = None
        self.selected = {}
        self.current = None
        self.panel = "metadata"
        self.children = None
        self.fulltext = None
        self.links = None
        self.plan = None
        self.write_result = None
        self.last_import = None
        self._plan_dialog = None
        self._displayed_plan_id = None

    @property
    def server_id(self):
        return self.info.get("server_id")

    @property
    def can_read(self):
        return bool(self.info.get("connected") and not self.busy)

    @property
    def can_write(self):
        return self.can_read and bool(self.info.get("write_supported") and get_settings().zotero_write_enabled)

    @property
    def has_selection(self):
        return self.can_read and bool(self.selected)

    @property
    def selection_text(self):
        return t("zotero.selected", count=len(self.selected))

    async def call(self, function, *args, **kwargs):
        if self.busy:
            return None
        self.busy = True
        self.error = ""
        self.last_error = None
        try:
            return await run_sync(function, *args, **kwargs)
        except BusinessError as exc:
            self.last_error = exc
            self.error = f"{exc.code} · {exc}"
            if exc.code in {"ZOTERO_INSTANCE_CHANGED", "ZOTERO_UNAVAILABLE", "ZOTERO_DISABLED"}:
                self.info["connected"] = False
            ui.notify(self.error, type="negative")
            return None
        finally:
            self.busy = False

    def refresh(self):
        self.connection.refresh()
        self.library.refresh()
        self.detail.refresh()

    async def connect(self):
        info = await self.call(zotero.status)
        if info is None:
            return
        self.info = info
        self.results = self.current = self.children = self.fulltext = self.links = None
        self.selected.clear()
        self.collections.clear()
        self.collection_key = None
        self.collection_next = None
        if info.get("connected"):
            await self.load_collections()
            await self.search()
        else:
            error = info.get("error") or {}
            self.error = f"{error.get('code', '')} · {error.get('message', '')}"
        self.refresh()

    async def open_item(self, key, server_id=None):
        if server_id is not None and self.server_id != server_id:
            info = await self.call(zotero.status, server_id=server_id)
            if not info or not info.get("connected"):
                if info:
                    self.error = (info.get("error") or {}).get("message", "")
                self.refresh()
                return
            self.info = info
            self.results = None
            self.selected.clear()
            self.collections.clear()
            self.collection_key = None
        result = await self.call(zotero.get_item, key, server_id=self.server_id)
        if result is not None:
            self.current = result["item"]
            self.panel = "metadata"
            self.children = self.fulltext = self.links = None
        self.refresh()

    async def load_collections(self, offset=0):
        result = await self.call(zotero.list_collections, limit=100, offset=offset, server_id=self.server_id)
        if result is not None:
            if offset == 0:
                self.collections.clear()
            self.collections.update({item["key"]: item for item in result["items"]})
            self.collection_next = result.get("next_offset")
            self.filters.refresh()

    async def search(self, offset=0):
        if not self.can_read:
            return
        result = await self.call(zotero.search, query=str(self.query or "").strip(), collection_key=self.collection_key,
                                 tag=str(self.tag or "").strip() or None, limit=20, offset=offset, server_id=self.server_id)
        if result is not None:
            self.results = result
            self.selected.clear()
        self.library.refresh()

    def toggle_selection(self, item, checked):
        if checked:
            self.selected[item["key"]] = item
        else:
            self.selected.pop(item["key"], None)

    def collection_options(self):
        return {key: f"{item['data'].get('name', key)} · {key}" for key, item in self.collections.items()}

    def render_middle(self):
        with ui.column().classes("w-full h-full theme-panel gap-0 overflow-hidden").props("data-zotero-browser"):
            with ui.row().classes("workspace-toolbar w-full justify-between"):
                ui.label(t("zotero.title")).classes("text-sm font-medium theme-text")
                ui.spinner(size="xs").bind_visibility_from(self, "busy")
                ui.button(t("zotero.connect"), icon="refresh", color=None, on_click=self.connect).props(
                    "flat dense no-caps").bind_enabled_from(self, "busy", backward=lambda value: not value)
            self.connection()
            self.filters()
            with ui.scroll_area().classes("w-full flex-1 min-h-0 scroll-flush"):
                self.library()
            with ui.column().classes("w-full p-3 gap-2 library-footer"):
                ui.label().classes("text-xs theme-text-muted").bind_text_from(self, "selection_text")
                with ui.row().classes("w-full gap-1"):
                    ui.button(t("zotero.preview_import"), icon="file_download", color=None,
                              on_click=lambda: self.import_dialog(item_keys=list(self.selected))).props(
                                  "outline dense no-caps").bind_enabled_from(self, "has_selection")
                    with ui.button(t("zotero.actions"), color=None).props("flat dense no-caps").bind_enabled_from(self, "can_read"):
                        with ui.menu().props("auto-close"):
                            ui.menu_item(t("zotero.export"), on_click=lambda: self.citations_dialog(list(self.selected))).props("role=menuitem").bind_enabled_from(
                                self, "has_selection")
                            ui.menu_item(t("zotero.organize"), on_click=self.organize_dialog).props("role=menuitem").bind_enabled_from(self, "can_write")
                            ui.menu_item(t("zotero.create_note"), on_click=self.note_dialog).props("role=menuitem").bind_enabled_from(self, "can_write")
                            ui.menu_item(t("zotero.resume_plan"), on_click=self.plan_dialog).props("role=menuitem").bind_enabled_from(self, "plan", backward=bool)
                if self.open_settings:
                    ui.button(t("zotero.settings"), on_click=self.open_settings, color=None).props("flat dense no-caps")

    @ui.refreshable
    def connection(self):
        with ui.column().classes("w-full px-3 py-2 gap-1"):
            if not self.info.get("connected"):
                ui.label(t("zotero.local_hint")).classes("text-xs theme-text-muted leading-relaxed")
            else:
                ui.label(f"Zotero {self.info.get('zotero_version') or '?'} · {t('zotero.personal_library')}").classes(
                    "text-xs theme-text-secondary")
                ui.label(self.server_id or t("zotero.legacy_identity")).classes("text-xs font-mono theme-text-muted break-all")
                grant = self.info.get("authorization", {}).get("state")
                state = "write_off" if not get_settings().zotero_write_enabled else (
                    "write_unsupported" if not self.info.get("write_supported") else "auth_cached" if grant == "cached" else "auth_required")
                ui.label(t(f"zotero.{state}")).classes("text-xs theme-text-muted")
            ui.label().classes("text-xs theme-danger break-words").props('role="alert"').bind_text_from(self, "error").bind_visibility_from(
                self, "error", backward=bool)

    @ui.refreshable
    def filters(self):
        with ui.column().classes("w-full p-3 gap-2"):
            ui.input(t("zotero.query"), value=self.query).props("dense outlined clearable").classes(
                "w-full workspace-search").bind_value(self, "query").on("keydown.enter", self.search).bind_enabled_from(self, "can_read")
            ui.select(self.collection_options(), label=t("zotero.collection"), value=self.collection_key,
                      with_input=True, clearable=True).props("dense outlined").classes("w-full").bind_value(
                          self, "collection_key").bind_enabled_from(self, "can_read")
            if self.collection_next is not None:
                ui.button(t("zotero.more_collections"), on_click=partial(self.load_collections, self.collection_next), color=None).props(
                    "flat dense no-caps").bind_enabled_from(self, "can_read")
            with ui.row().classes("w-full gap-1 flex-nowrap items-center"):
                ui.input(t("zotero.tag"), value=self.tag).props("dense outlined clearable").classes("flex-1 min-w-0").bind_value(
                    self, "tag").on("keydown.enter", self.search).bind_enabled_from(self, "can_read")
                ui.button(t("zotero.tags"), on_click=self.tags_dialog, color=None).props("flat dense no-caps").bind_enabled_from(self, "can_read")
            ui.label(t("zotero.search_hint")).classes("text-xs theme-text-muted leading-relaxed")
            ui.button(t("zotero.search"), icon="search", on_click=self.search).props("dense unelevated no-caps").classes(
                "w-full").bind_enabled_from(self, "can_read")

    @ui.refreshable
    def library(self):
        if self.results is None:
            return
        with ui.column().classes("w-full gap-0"):
            if not self.results["items"]:
                ui.label(t("zotero.empty")).classes("p-4 text-sm theme-text-muted")
            for item in self.results["items"]:
                key = item["key"]
                selected = self.current and self.current["key"] == key
                with ui.row().classes("w-full gap-1 items-start flex-nowrap px-2 py-1 zotero-item" + (" zotero-item-active" if selected else "")):
                    ui.checkbox(value=key in self.selected,
                                on_change=lambda event, row=item: self.toggle_selection(row, event.value)).props(
                                    "dense").bind_enabled_from(self, "can_read")._props["aria-label"] = t("zotero.select_item", title=_title(item))
                    with ui.button(on_click=partial(self.open_item, key), color=None).props(
                        "flat dense no-caps no-ripple align=left").classes("flex-1 min-w-0 text-left").bind_enabled_from(self, "can_read"):
                        with ui.column().classes("w-full gap-1 min-w-0"):
                            ui.label(_title(item)).classes("text-sm theme-text text-left leading-snug line-clamp-2")
                            subtitle = " · ".join(filter(None, (_authors(item), item["data"].get("date"), key)))
                            ui.label(subtitle).classes("text-xs theme-text-muted truncate max-w-full")
            self.pagination(self.results, self.search)

    def pagination(self, result, callback):
        offset, count = result.get("offset", 0), len(result.get("items", result.get("links", [])))
        with ui.row().classes("w-full items-center justify-between gap-2 px-3 py-2"):
            ui.label(t("zotero.page", start=offset + 1 if count else 0, end=offset + count, total=result.get("total", "—"))).classes(
                "text-xs theme-text-muted")
            with ui.row().classes("gap-1"):
                ui.button(t("knowledge.previous"), on_click=partial(callback, max(0, offset - result.get("limit", 20))), color=None).props(
                    "flat dense no-caps").set_enabled(offset > 0 and not self.busy)
                next_offset = result.get("next_offset")
                ui.button(t("knowledge.next"), on_click=partial(callback, next_offset or 0), color=None).props(
                    "flat dense no-caps").set_enabled(next_offset is not None and not self.busy)

    def render_right(self):
        with ui.column().classes("w-full h-full min-w-0 theme-content gap-0").props("data-zotero-detail"):
            with ui.row().classes("workspace-toolbar w-full"):
                ui.label(t("zotero.details")).classes("text-sm font-medium theme-text")
                ui.space()
                ui.label(t("zotero.read_hint")).classes("text-xs theme-text-muted")
            with ui.scroll_area().classes("w-full flex-1 min-h-0"):
                with ui.column().classes("w-full p-5 gap-4 min-w-0"):
                    self.detail()

    @ui.refreshable
    def detail(self):
        if self.current is None:
            ui.label(t("zotero.choose_item")).classes("text-sm theme-text-muted py-8")
            return
        item, data = self.current, self.current["data"]
        kind = data.get("itemType")
        with ui.column().classes("w-full gap-3 min-w-0"):
            ui.label(_title(item)).classes("text-xl font-semibold theme-text break-words")
            ui.label(f"{kind} · {item['key']} · v{item.get('version', 0)}").classes("text-xs font-mono theme-text-muted")
            with ui.row().classes("gap-1 flex-wrap"):
                if data.get("parentItem"):
                    ui.button(t("zotero.parent"), icon="arrow_back", on_click=partial(self.open_item, data["parentItem"]), color=None).props(
                        "flat dense no-caps").bind_enabled_from(self, "can_read")
                ui.link(t("zotero.open_desktop"), f"zotero://select/library/items/{item['key']}").classes("text-xs theme-text-accent self-center")
                if kind not in {"attachment", "annotation", "note"}:
                    ui.button(t("zotero.edit_metadata"), on_click=self.metadata_dialog, color=None).props("flat dense no-caps").bind_enabled_from(self, "can_write")
                    ui.button(t("zotero.create_child_note"), on_click=partial(self.note_dialog, parent_key=item["key"]), color=None).props(
                        "flat dense no-caps").bind_enabled_from(self, "can_write")
                    ui.button(t("zotero.export"), on_click=partial(self.citations_dialog, [item["key"]]), color=None).props(
                        "flat dense no-caps").bind_enabled_from(self, "can_read")
                elif kind == "note":
                    ui.button(t("zotero.edit_note"), on_click=partial(self.note_dialog, existing=item), color=None).props(
                        "flat dense no-caps").bind_enabled_from(self, "can_write")
                elif kind == "attachment" and data.get("contentType") == "application/pdf":
                    ui.button(t("zotero.preview_import"), on_click=lambda: self.import_dialog(attachment_keys=[item["key"]]), color=None).props(
                        "outline dense no-caps").bind_enabled_from(self, "can_read")
            with ui.row().classes("w-full gap-1 zotero-tabs"):
                panels = ["metadata", "children", "links"]
                if kind == "attachment":
                    panels.insert(2, "fulltext")
                for panel in panels:
                    ui.button(t(f"zotero.{panel}"), on_click=partial(self.show_panel, panel), color=None).props(
                        "flat dense no-caps").classes("zotero-tab-active" if self.panel == panel else "theme-text-secondary").bind_enabled_from(self, "can_read")
            if self.panel == "metadata":
                self.metadata(item)
            elif self.panel == "children" and self.children is not None:
                if not self.children["items"]:
                    ui.label(t("zotero.no_children")).classes("text-sm theme-text-muted")
                for child in self.children["items"]:
                    with ui.row().classes("w-full items-start gap-2 py-2 zotero-child"):
                        ui.label(child["data"].get("itemType", "")).classes("text-xs theme-text-muted w-20 shrink-0")
                        with ui.column().classes("flex-1 min-w-0 gap-1"):
                            ui.button(_title(child), on_click=partial(self.open_item, child["key"]), color=None).props(
                                "flat dense no-caps align=left").classes("text-left break-words").bind_enabled_from(self, "can_read")
                            ui.label(child["key"]).classes("text-xs theme-text-muted font-mono")
                            if child.get("truncated_fields"):
                                ui.label(t("zotero.truncated")).classes("text-xs theme-text-muted")
                self.pagination(self.children, self.load_children)
            elif self.panel == "fulltext" and self.fulltext is not None:
                ui.label(t("zotero.fulltext_hint")).classes("text-xs theme-text-muted leading-relaxed")
                ui.label(t("zotero.coverage", coverage=json.dumps(self.fulltext["coverage"], ensure_ascii=False),
                           complete=t("zotero.complete") if self.fulltext["complete"] else t("zotero.incomplete"))).classes("text-xs theme-text-secondary")
                ui.label(self.fulltext["content"]).classes("text-sm theme-text whitespace-pre-wrap break-words leading-relaxed")
                with ui.row().classes("gap-2"):
                    offset = self.fulltext["offset"]
                    ui.button(t("knowledge.previous"), on_click=partial(self.load_fulltext, max(0, offset - 12000)), color=None).props(
                        "flat dense no-caps").set_enabled(offset > 0)
                    next_offset = self.fulltext.get("next_offset")
                    ui.button(t("knowledge.next"), on_click=partial(self.load_fulltext, next_offset or 0), color=None).props(
                        "flat dense no-caps").set_enabled(next_offset is not None)
            elif self.panel == "links" and self.links is not None:
                if not self.links.get("identity_verified"):
                    ui.label(t("zotero.legacy_identity")).classes("text-sm theme-text-muted")
                if not self.links["links"]:
                    ui.label(t("zotero.no_links")).classes("text-sm theme-text-muted")
                for link in self.links["links"]:
                    with ui.column().classes("w-full gap-1 py-2"):
                        ui.label(f"#{link['file_id']} · {link['filename']}").classes("text-sm theme-text")
                        ui.label(t(f"file_status.{link['status']}")).classes("text-xs theme-text-muted")
                        render_zotero_sources([link["source"]])
                        if self.open_file:
                            ui.button(t("zotero.open_piece"), on_click=partial(self.open_file, link["file_id"]), color=None).props("flat dense no-caps")
                self.pagination(self.links, self.load_links)

    def metadata(self, item):
        data = item["data"]
        if _authors(item):
            ui.label(_authors(item)).classes("text-sm theme-text-secondary")
        if "note" in data:
            ui.label(t("zotero.user_note")).classes("text-xs theme-text-muted")
            ui.label(note_text(data["note"])).classes("text-sm theme-text whitespace-pre-wrap break-words leading-relaxed")
        if data.get("itemType") == "annotation":
            ui.label(t("zotero.user_annotation")).classes("text-xs theme-text-muted")
            for key in ("annotationPageLabel", "annotationText", "annotationComment"):
                if data.get(key):
                    ui.label(f"{key}: {data[key]}").classes("text-sm theme-text whitespace-pre-wrap break-words")
        if data.get("abstractNote"):
            ui.label(t("zotero.abstract")).classes("text-xs font-semibold theme-text-muted")
            ui.label(data["abstractNote"]).classes("text-sm theme-text whitespace-pre-wrap break-words leading-relaxed")
        excluded = {"key", "version", "title", "creators", "note", "abstractNote", "annotationText", "annotationComment", "annotationPosition"}
        for key, value in data.items():
            if key in excluded or value in (None, "", [], {}):
                continue
            text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            with ui.row().classes("w-full gap-3 flex-nowrap"):
                ui.label(key).classes("text-xs theme-text-muted w-32 shrink-0 break-words")
                ui.label(text).classes("text-sm theme-text-secondary whitespace-pre-wrap break-all min-w-0")

    async def show_panel(self, panel):
        if panel == "children":
            await self.load_children()
        elif panel == "fulltext":
            await self.load_fulltext()
        elif panel == "links":
            await self.load_links()
        else:
            self.panel = "metadata"
            self.detail.refresh()

    async def load_children(self, offset=0):
        if not self.current:
            return
        result = await self.call(zotero.get_children, self.current["key"], limit=20, offset=offset, server_id=self.server_id)
        if result is not None:
            self.children, self.panel = result, "children"
            self.detail.refresh()

    async def load_fulltext(self, offset=0):
        if not self.current:
            return
        result = await self.call(zotero.get_fulltext, self.current["key"], offset=offset, limit=12000, server_id=self.server_id)
        if result is not None:
            self.fulltext, self.panel = result, "fulltext"
            self.detail.refresh()

    async def load_links(self, offset=0):
        if not self.current:
            return
        data = self.current["data"]
        key = data.get("parentItem") if data.get("itemType") == "annotation" else self.current["key"]
        field = "attachment_keys" if data.get("itemType") in {"attachment", "annotation"} else "item_keys"
        result = await self.call(zotero.get_links, **{field: [key]}, limit=20, offset=offset, server_id=self.server_id)
        if result is not None:
            self.links, self.panel = result, "links"
            self.detail.refresh()

    async def tags_dialog(self):
        dialog, body = _dialog(t("zotero.tags"))
        collection_key, server_id = self.collection_key, self.server_id

        async def load(offset=0):
            result = await self.call(zotero.list_tags, collection_key=collection_key, limit=50, offset=offset, server_id=server_id)
            if result is None or dialog.is_deleted:
                return
            body.clear()
            with body:
                ui.label(t("zotero.tag_scope")).classes("text-xs theme-text-muted")
                if not result["items"]:
                    ui.label(t("zotero.empty")).classes("text-sm theme-text-muted")
                with ui.row().classes("w-full gap-1 flex-wrap"):
                    for entry in result["items"]:
                        tag = entry.get("tag", "")

                        async def choose(value=tag):
                            self.tag = value
                            self.filters.refresh()
                            dialog.close()
                            await self.search()

                        ui.button(tag, on_click=choose, color=None).props("flat dense no-caps")
                self.pagination(result, load)
        await load()

    async def citations_dialog(self, keys):
        if not keys:
            ui.notify(t("zotero.select_first"), type="warning")
            return
        server_id = self.server_id
        dialog, body = _dialog(t("zotero.export"), "w-[760px] max-w-[94vw]")
        with body:
            with ui.row().classes("w-full gap-2"):
                format_field = ui.select({"bibtex": "BibTeX", "ris": "RIS", "csljson": "CSL JSON", "bib": "HTML", "citation": t("zotero.citation")},
                                         value="bibtex", label=t("zotero.format")).props("dense outlined")
                style = ui.input(t("zotero.style"), value="apa").props("dense outlined")
                locale = ui.input(t("zotero.locale"), value="zh-CN").props("dense outlined")
            output = ui.column().classes("w-full gap-2 max-h-[55vh] overflow-auto")

            async def export():
                value = await self.call(zotero.export_citations, keys, format=format_field.value,
                                        style=style.value, locale=locale.value, server_id=server_id)
                if value is None or dialog.is_deleted:
                    return
                output.clear()
                content = value["content"]
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2)
                with output:
                    ui.code(text, language="json" if isinstance(content, (dict, list)) else "text").classes("w-full")
                    extension = {"bibtex": "bib", "ris": "ris", "bib": "html", "citation": "json", "csljson": "json"}[value["format"]]
                    ui.button(t("zotero.download"), on_click=lambda: ui.download.content(text, f"zotero-citations.{extension}"), color=None).props(
                        "flat dense no-caps")
            ui.button(t("zotero.export"), on_click=export).props("dense unelevated no-caps").bind_enabled_from(self, "busy", backward=lambda value: not value)

    async def import_dialog(self, *, item_keys=None, attachment_keys=None):
        keys = item_keys if item_keys is not None else attachment_keys
        if not keys:
            ui.notify(t("zotero.select_first"), type="warning")
            return
        scope = {"item_keys" if item_keys is not None else "attachment_keys": list(keys)}
        server_id = self.server_id
        dialog, body = _dialog(t("zotero.preview_import"), "w-[840px] max-w-[94vw]")
        state = {"preview": None, "selected": set(), "result": None}

        async def open_result(file_id):
            dialog.close()
            if self.open_file:
                await self.open_file(file_id)

        piece_collections = await self.call(collection_service.list_collections)
        if dialog.is_deleted:
            return
        with body:
            ui.label(t("zotero.import_hint")).classes("text-xs theme-text-muted leading-relaxed")
            with ui.column().classes("w-full gap-2") as options:
                mode = ui.select({"none": t("import.mode_none"), "path": t("import.mode_path"), "top": t("import.mode_top")},
                                 label=t("import.collection_mode"), value="none").props("dense outlined").classes("w-full")
                targets = ui.select([row["name"] for row in piece_collections or []], multiple=True, with_input=True,
                                    label=t("zotero.piece_collections"), value=[]).props("dense outlined use-chips").classes("w-full")
            output = ui.column().classes("w-full gap-2 max-h-[50vh] overflow-auto")

            async def preview():
                if state["result"] is not None or self.busy:
                    return
                collection_mode = mode.value
                value = await self.call(zotero.preview_import, **scope, collection_mode=collection_mode, server_id=server_id, max_items=50)
                if value is None or dialog.is_deleted:
                    return
                state["preview"] = value
                state["mode"] = collection_mode
                state["selected"] = {row["attachment_key"] for row in value["items"] if row["available"]}
                output.clear()
                with output:
                    ui.label(t("zotero.import_counts", available=value["importable_count"], duplicate=value["duplicate_count"],
                               unavailable=value["unavailable_count"], skipped=value["skipped_count"])).classes("text-sm theme-text")
                    for row in value["items"]:
                        key = row["attachment_key"]

                        def toggle(event, attachment=key):
                            if event.value:
                                state["selected"].add(attachment)
                            else:
                                state["selected"].discard(attachment)

                        with ui.column().classes("w-full gap-1 py-2 zotero-child"):
                            ui.checkbox(f"{row['filename']} · {key}", value=key in state["selected"], on_change=toggle).props(
                                "dense").set_enabled(row["available"])
                            detail = row.get("reason") or (t("zotero.existing_file", id=row["file_id"], status=row.get("file_status"))
                                                          if row.get("duplicate") else t("zotero.new_file"))
                            ui.label(detail).classes("text-xs theme-text-muted pl-8")
                    for row in value["skipped"]:
                        ui.label(f"{row.get('title') or row.get('key')} · {row.get('reason')}").classes("text-xs theme-text-muted")

            async def import_selected():
                if state["result"] is not None or self.busy:
                    return
                if not state["preview"] or state["mode"] != mode.value or not state["selected"]:
                    ui.notify(t("zotero.preview_first"), type="warning")
                    return
                selected = sorted(state["selected"])
                value = await self.call(zotero.import_library, attachment_keys=selected, collection_mode=state["mode"],
                                        collections=list(targets.value or []), server_id=server_id, max_items=50)
                if value is None:
                    return
                self.last_import = state["result"] = value
                if not dialog.is_deleted:
                    preview_button.disable()
                    import_button.disable()
                    options.set_visibility(False)
                    await refresh_status()
                if self.refresh_files:
                    await self.refresh_files()

            async def refresh_status():
                result = state["result"]
                if not result:
                    return
                tasks = await self.call(task_service.get_tasks_by_ids, result["task_ids"]) if result["task_ids"] else []
                files = {}
                for row in result["items"]:
                    file = await self.call(file_service.get_file_by_id, row["file_id"])
                    if file:
                        files[row["file_id"]] = file
                if dialog.is_deleted:
                    return
                output.clear()
                with output:
                    ui.label(t("import.zotero_done", accepted=result["accepted_count"], duplicates=result["duplicate_count"],
                               failed=result["failed_count"], skipped=result["skipped_count"])).classes("text-sm theme-text")
                    ui.label(t("zotero.accepted_hint")).classes("text-xs theme-text-muted")
                    for row in result["items"]:
                        status = files.get(row["file_id"], {}).get("status", row.get("file_status", "missing"))
                        ui.label(f"#{row['file_id']} · {row['filename']} · {t(f'file_status.{status}')}").classes("text-sm theme-text")
                        if self.open_file:
                            ui.button(t("zotero.open_piece"), on_click=partial(open_result, row["file_id"]), color=None).props("flat dense no-caps")
                    for task in tasks or []:
                        ui.label(f"#{task['id']} · {task['status']} · {task.get('progress', 0)}% · {task.get('error_message') or ''}").classes(
                            "text-xs theme-text-secondary break-words")
                    for failure in result["failures"]:
                        ui.label(f"{failure.get('filename') or failure.get('attachment_key')} · {failure['reason']}").classes("text-xs theme-danger")
                    ui.button(t("zotero.refresh_status"), on_click=refresh_status, color=None).props("flat dense no-caps")
            with ui.row().classes("w-full justify-end gap-2"):
                preview_button = ui.button(t("import.preview"), on_click=preview, color=None).props("flat dense no-caps")
                import_button = ui.button(t("zotero.import_selected"), on_click=import_selected).props("dense unelevated no-caps")
                for button in (preview_button, import_button):
                    button.bind_enabled_from(self, "busy", backward=lambda value: not value and state["result"] is None)
            mode.on_value_change(lambda: state.update(preview=None))
        await preview()

    def _can_edit(self):
        if not self.can_write:
            ui.notify(t("zotero.write_hint"), type="warning")
            return False
        if self.write_result and self.write_result.get("pending_count"):
            ui.notify(t("zotero.pending_plan"), type="warning")
            self.plan_dialog()
            return False
        return True

    async def preview_changes(self, changes, dialog=None, *, server_id):
        plan = await self.call(writes.preview_changes, changes, server_id=server_id)
        if plan is None:
            return
        self.plan, self.write_result = plan, None
        if dialog:
            dialog.close()
        self.plan_dialog()

    def organize_dialog(self):
        if not self._can_edit():
            return
        keys = [key for key, item in self.selected.items() if item["data"].get("itemType") not in {"attachment", "annotation", "note"}]
        if not keys or len(keys) != len(self.selected):
            ui.notify(t("zotero.select_papers"), type="warning")
            return
        server_id = self.server_id
        dialog, body = _dialog(t("zotero.organize"), "w-[640px] max-w-[94vw]")
        with body:
            ui.label(t("zotero.selected", count=len(keys))).classes("text-sm theme-text")
            add_tags = ui.textarea(t("zotero.add_tags")).props("dense outlined rows=2").classes("w-full")
            remove_tags = ui.textarea(t("zotero.remove_tags")).props("dense outlined rows=2").classes("w-full")
            add = ui.select(self.collection_options(), label=t("zotero.add_collections"), multiple=True, value=[]).props(
                "dense outlined use-chips").classes("w-full")
            remove = ui.select(self.collection_options(), label=t("zotero.remove_collections"), multiple=True, value=[]).props(
                "dense outlined use-chips").classes("w-full")
            ui.label(t("zotero.organize_hint")).classes("text-xs theme-text-muted")

            async def preview():
                changes = [{"action": "update_item", "item_key": key, "add_tags": _lines(add_tags.value),
                            "remove_tags": _lines(remove_tags.value), "add_collections": list(add.value or []),
                            "remove_collections": list(remove.value or [])} for key in keys]
                await self.preview_changes(changes, dialog, server_id=server_id)
            ui.button(t("zotero.preview_changes"), on_click=preview).props("dense unelevated no-caps").bind_enabled_from(
                self, "busy", backward=lambda value: not value)

    def metadata_dialog(self):
        if not self.current or not self._can_edit():
            return
        item = self.current
        server_id = self.server_id
        original = item["data"]
        dialog, body = _dialog(t("zotero.edit_metadata"), "w-[700px] max-w-[94vw]")
        fields = {}
        with body:
            ui.label(f"{_title(item)} · {item['key']}").classes("text-sm theme-text")
            with ui.column().classes("w-full gap-2 max-h-[60vh] overflow-auto"):
                for key in ("title", "date", "DOI", "url", "publicationTitle", "language", "abstractNote", "extra"):
                    if key not in original:
                        continue
                    widget = ui.textarea if key in {"abstractNote", "extra"} else ui.input
                    fields[key] = widget(key, value=original[key]).props("dense outlined").classes("w-full")

            async def preview():
                changed = {key: widget.value or "" for key, widget in fields.items() if (widget.value or "") != original[key]}
                if not changed:
                    ui.notify(t("settings.no_changes"), type="info")
                    return
                await self.preview_changes([{"action": "update_item", "item_key": item["key"], "fields": changed}], dialog,
                                           server_id=server_id)
            ui.button(t("zotero.preview_changes"), on_click=preview).props("dense unelevated no-caps").bind_enabled_from(
                self, "busy", backward=lambda value: not value)

    def note_dialog(self, *, parent_key=None, existing=None):
        if not self._can_edit():
            return
        server_id = self.server_id
        dialog, body = _dialog(t("zotero.edit_note" if existing else "zotero.create_note"), "w-[760px] max-w-[94vw]")
        with body:
            ui.label(t("zotero.note_replace_hint" if existing else "zotero.note_hint")).classes("text-xs theme-text-muted leading-relaxed")
            if parent_key:
                ui.label(t("zotero.note_parent", item=parent_key)).classes("text-sm theme-text")
            collections = None
            if not existing and not parent_key:
                collections = ui.select(self.collection_options(), label=t("zotero.note_collections"), multiple=True, value=[]).props(
                    "dense outlined use-chips").classes("w-full")
            content = ui.textarea(t("zotero.note_content"), value=note_text(existing["data"].get("note")) if existing else "").props(
                "outlined rows=12").classes("w-full")
            sources = [parent_key] if parent_key else [key for key, item in self.selected.items()
                                                      if item["data"].get("itemType") not in {"note", "attachment", "annotation"}]
            if sources and not existing:
                ui.label(t("zotero.note_sources", keys=", ".join(sources))).classes("text-xs theme-text-muted break-all")

            async def preview():
                change = {"action": "update_note" if existing else "create_note", "content": content.value or ""}
                if existing:
                    change["item_key"] = existing["key"]
                else:
                    change["source_item_keys"] = sources
                    if parent_key:
                        change["parent_item_key"] = parent_key
                    elif collections:
                        change["collection_keys"] = list(collections.value or [])
                await self.preview_changes([change], dialog, server_id=server_id)
            ui.button(t("zotero.preview_changes"), on_click=preview).props("dense unelevated no-caps").bind_enabled_from(
                self, "busy", backward=lambda value: not value)

    def plan_dialog(self):
        if not self.plan:
            return
        plan = self.plan
        if self._plan_dialog is not None and not self._plan_dialog.is_deleted:
            if self._displayed_plan_id == plan["plan_id"]:
                self._plan_dialog.open()
                return
            self._plan_dialog.close()
        dialog, body = _dialog(t("zotero.preview_changes"), "w-[920px] max-w-[96vw]")
        self._plan_dialog, self._displayed_plan_id = dialog, plan["plan_id"]
        with body:
            ui.label(t("zotero.plan_hint")).classes("text-xs theme-text-muted leading-relaxed")
            ui.label(f"{plan['plan_id']} · {plan.get('expires_at', '')}").classes("text-xs font-mono theme-text-muted")
            with ui.column().classes("w-full gap-3 max-h-[45vh] overflow-auto"):
                for change in plan["changes"]:
                    ui.label(f"{change['action']} · {change['key']} · v{change.get('version', 0)}").classes("text-sm font-medium theme-text")
                    with ui.row().classes("w-full gap-3 flex-wrap"):
                        for side in ("before", "after"):
                            with ui.column().classes("flex-1 min-w-[240px] gap-1"):
                                ui.label(t(f"zotero.{side}")).classes("text-xs theme-text-muted")
                                ui.code(json.dumps(change.get(side), ensure_ascii=False, indent=2), language="json").classes("w-full")
            status_box = ui.column().classes("w-full gap-1")

            def show_result():
                status_box.clear()
                with status_box:
                    result = self.write_result
                    if result:
                        ui.label(t("zotero.write_counts", success=result.get("successful_count", 0), unchanged=result.get("unchanged_count", 0),
                                   failed=result.get("failed_count", 0), pending=result.get("pending_count", 0))).classes("text-sm theme-text")
                        for row in result.get("items", []):
                            error = row.get("error") or {}
                            ui.label(f"{row['key']} · {row['status']} · {error.get('message', '')}").classes("text-xs theme-text-secondary break-words")
                    ui.label().classes("text-xs theme-danger break-words").props('role="alert"').bind_text_from(self, "error")
            show_result()
            ui.label(t("zotero.authorize_hint")).classes("text-xs theme-text-muted")
            ui.spinner(size="sm").bind_visibility_from(self, "busy")
            confirmation = ui.checkbox(t("zotero.confirm_plan"), value=False).props("dense")

            async def authorize():
                result = await self.call(writes.authorize, plan["zotero"]["server_id"])
                if result is not None:
                    self.info["authorization"] = {"state": "cached"}
                    ui.notify(t("zotero.authorized"), type="positive")
                    self.connection.refresh()
                show_result()

            async def apply():
                if not confirmation.value or self.busy:
                    return
                result = await self.call(writes.apply_changes, plan["plan_id"], confirmed=True)
                if result is None and self.last_error and isinstance(self.last_error.data, dict):
                    result = self.last_error.data
                if result is not None:
                    self.write_result = result
                self.info["authorization"] = writes.authorization_status(self.server_id)
                self.connection.refresh()
                show_result()
                if result and result.get("completed"):
                    confirmation.set_value(False)
                    ui.notify(t("zotero.write_verified"), type="positive")
                    if self.current:
                        await self.open_item(self.current["key"])
                else:
                    apply_button.set_text(t("zotero.retry_plan"))

            with ui.row().classes("w-full justify-end gap-2 flex-wrap"):
                ui.button(t("zotero.authorize"), on_click=authorize, color=None).props("outline dense no-caps").bind_enabled_from(
                    self, "busy", backward=lambda value: not value)
                apply_button = ui.button(t("zotero.apply"), on_click=apply).props("dense unelevated no-caps").bind_enabled_from(
                    confirmation, "value", backward=lambda value: bool(value) and not self.busy)
