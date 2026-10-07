"""Wiki 工作台：独立维护 Markdown 知识页、页面链接与证据。

页面正文以 Markdown 文件为正式存储；页面链接由正文中的 piece://wiki/<UUID> 派生，
不进入图谱，也不在图谱工作台出现。
"""
from copy import deepcopy
from functools import partial
import logging

import markdown2
from nicegui import run, ui

from app.ui.components import _MARKDOWN_EXTRAS
from app.ui.views.knowledge_common import STATUSES, WorkbenchBase
from app.ui.views.wiki_presenter import KINDS, safe_wiki_html

from indexing.services.errors import BusinessError

logger = logging.getLogger(__name__)


class WikiWorkbench(WorkbenchBase):
    prefix = "wiki."
    result_readback = (("evidence", "evidence"), ("pages", "page"))
    revision_kinds = ("page",)
    hash_kinds = ("page", "evidence")

    @property
    def service(self):
        # wiki_service 由后端并行实现；延迟导入让两个功能各自可独立启动。
        from indexing.services import wiki_service
        return wiki_service

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.mode = "pages"
        self.kind = None
        self.status = None
        self.links = None
        self.backlinks = None
        self.lint_scope = "all"
        self.lint_offset = 0
        self.checks = None
        self.references_data = None
        self.reference_file = None
        self.refresh_modes = lambda: None

    def _list_function(self, params):
        return self.service.search_pages if "query" in params else self.service.list_pages

    def search_params(self):
        params = {"kind": self.kind, "status": self.status, "limit": 25, "offset": self.offset}
        if self.query.strip():
            params["query"] = self.query.strip()
        return params

    async def search(self, reset=True):
        self._search_generation += 1
        generation = self._search_generation
        if reset:
            self.offset = 0
        self._search_params = params = self.search_params()
        function = self._list_function(params)
        self._searching = True
        try:
            results = await self.call(function, **params)
            if generation == self._search_generation:
                self.results = results
                self.refresh_list()
        finally:
            if generation == self._search_generation:
                self._searching = False

    async def list_page(self, delta):
        self.offset = max(0, self.offset + delta * 25)
        await self.search(False)

    async def poll(self):
        """只读页面列表与当前详情；外部改动 Markdown 文件后由此反映真实内容。"""
        if self._polling or self._searching:
            return
        timer, generation = self._poll_timer, self._search_generation

        def current():
            return (timer is self._poll_timer and (timer is None or not timer.is_deleted)
                    and generation == self._search_generation)

        if not current():
            return
        self._polling = True
        selection, detail = self._selection, self.detail
        try:
            params = dict(self._search_params or self.search_params())
            function = self._list_function(params)
            results = await run.io_bound(function, **params)
            if not current():
                return
            last_offset = max(0, (results["total"] - 1) // params["limit"]) * params["limit"]
            if params["offset"] > last_offset:
                params["offset"] = last_offset
                results = await run.io_bound(function, **params)
                if not current():
                    return
            self._search_params, self.offset = params, params["offset"]
            if results != self.results:
                self.results = results
                await self._refresh_preserving_scroll(self.refresh_list, "list")

            if not detail or not current() or selection != self._selection or self.detail is not detail:
                return
            kind, ident = detail["kind"], detail["record"]["id"]
            evidence = detail.get("evidence", {})
            read_error = None
            try:
                incoming = await run.io_bound(self.service.get_record, kind=kind, id=ident,
                                             limit=evidence.get("limit", 25), offset=evidence.get("offset", 0))
            except BusinessError as exc:
                if exc.code not in {"NOT_FOUND", "FILE_MISSING", "FILE_INVALID", "UNSAFE_PATH",
                                    "DUPLICATE_PAGE_ID", "DUPLICATE_EVIDENCE_ID"}:
                    raise
                incoming = None
                read_error = self.text("error", code=exc.code)
            if not current() or selection != self._selection or self.detail is not detail:
                return
            links = incoming.get("links") if incoming and incoming.get("kind") == "page" else None
            backlinks = incoming.get("backlinks") if incoming and incoming.get("kind") == "page" else None
            if incoming != detail or links != self.links or backlinks != self.backlinks:
                self.detail, self.links, self.backlinks = incoming, links, backlinks
                if incoming is None:
                    self.error = read_error
                await self._refresh_preserving_scroll(self.refresh_detail, "detail")
        except Exception as exc:
            logger.debug("Wiki 自动更新暂未完成 (%s)", type(exc).__name__)
        finally:
            self._polling = False

    async def select(self, kind, ident, *, offset=0):
        self._selection += 1
        generation = self._selection
        data = await self.call(self.service.get_record, kind=kind, id=ident, limit=25, offset=offset)
        if generation != self._selection:
            return
        if data is None:
            self.error = self.text("broken_link")
            if self.detail and (kind, ident) == (self.detail["kind"], self.detail["record"]["id"]):
                self.detail = self.links = self.backlinks = None
                self.error = self.text("lint_FILE_MISSING_OR_INVALID")
            self.refresh_detail()
            return
        self.error = None
        self.detail = data
        self.links = data.get("links") if kind == "page" else None
        self.backlinks = data.get("backlinks") if kind == "page" else None
        self.refresh_detail()
        return data

    async def internal_link(self, event):
        ident = event.args
        if isinstance(ident, str):
            await self.select("page", ident)

    def markdown(self, body):
        rendered = markdown2.markdown(body, extras=_MARKDOWN_EXTRAS)
        element = ui.html(rendered, sanitize=safe_wiki_html).classes("chunk-content w-full break-words knowledge-prose")
        element.on("click", self.internal_link, js_handler="""(event) => {
            const a = event.target.closest('a[data-wiki-id]');
            if (a) { event.preventDefault(); event.stopPropagation(); emit(a.dataset.wikiId); }
        }""")
        return element

    def render_middle(self):
        with ui.column().classes("w-full h-full gap-3 p-4 theme-panel overflow-hidden"):
            ui.label(self.text("heading")).classes("text-xl font-semibold theme-text")
            ui.label(self.text("intro")).classes("text-xs theme-text-muted")
            ui.label(self.text("auto_refresh")).classes("text-xs theme-text-muted")
            ui.input(self.text("search"), value=self.query, on_change=lambda e: setattr(self, "query", e.value)).props("outlined dense").classes("w-full").on("keydown.enter", self.search)
            with ui.row().classes("w-full gap-2"):
                ui.select(self.choices(KINDS), label=self.text("kind"), value=self.kind, clearable=True,
                          on_change=lambda e: setattr(self, "kind", e.value)).props("dense outlined").classes("flex-1")
                ui.select(self.choices(STATUSES), label=self.text("status"), value=self.status, clearable=True,
                          on_change=lambda e: setattr(self, "status", e.value)).props("dense outlined").classes("flex-1")
            with ui.row().classes("gap-2"):
                ui.button(self.text("search"), on_click=self.search).props("unelevated no-caps")
                ui.button(self.text("create"), icon="add", on_click=lambda: self.edit_page()).props("flat no-caps")
            with ui.scroll_area(on_scroll=lambda e: self._scroll_positions.update(list=e.vertical_position)).classes("w-full flex-1") as area:
                self._scroll_areas["list"] = area
                @ui.refreshable
                def listing():
                    data = self.results
                    if not data:
                        ui.label(self.text("search_hint")).classes("theme-text-muted")
                        return
                    if not data["pages"]:
                        ui.label(self.text("empty")).classes("theme-text-muted")
                    for item in data["pages"]:
                        with ui.column().classes("w-full gap-1 py-3 knowledge-list-item"):
                            ui.button(item["title"], on_click=partial(self.select, "page", item["id"])).props("flat no-caps align=left").classes("w-full text-left theme-text")
                            ui.label(self.text(item["kind"]) + " · " + self.text(item["status"])).classes("text-xs theme-text-muted")
                            ui.label(item["summary"]).classes("text-sm theme-text-secondary line-clamp-3 break-words")
                            ui.label(item["updated_at"]).classes("text-xs theme-text-muted")
                    self.pager(self.offset, 25, data["total"], self.list_page)
                self.refresh_list = listing.refresh
                listing()
            self._poll_timer = ui.timer(2.0, self.poll)

    def render_right(self):
        with ui.column().classes("w-full flex-1 min-h-0 min-w-0 theme-panel gap-0"):
            @ui.refreshable
            def modes():
                with ui.row().classes("w-full items-center gap-2 px-4 py-2 library-heading"):
                    for mode, icon in (("pages", "article"), ("review", "fact_check")):
                        ui.button(self.text(mode), icon=icon, on_click=partial(self.set_mode, mode)).props("flat no-caps").classes("theme-selected" if self.mode == mode else "")
            self.refresh_modes = modes.refresh
            modes()
            with ui.scroll_area(on_scroll=lambda e: self._scroll_positions.update(detail=e.vertical_position)).classes("w-full flex-1") as area:
                self._scroll_areas["detail"] = area
                @ui.refreshable
                def workspace():
                    with ui.column().classes("w-full min-w-0 p-5 gap-4"):
                        if self.mode == "review":
                            self.render_review()
                        if self.references_data is not None:
                            self.render_references()
                        @ui.refreshable
                        def detail():
                            self.render_detail()
                        self.refresh_detail = detail.refresh
                        detail()
                self.refresh_workspace = workspace.refresh
                workspace()

    async def set_mode(self, mode):
        self.mode = mode
        self.refresh_modes()
        self.refresh_workspace()

    def render_detail(self):
        if self.error:
            ui.label(self.error).classes("theme-text-muted")
            ui.button(self.text("back"), on_click=self.clear_error).props("flat")
        if not self.detail:
            ui.label(self.text("choose_page")).classes("text-sm theme-text-muted py-8")
            return
        data, kind = self.detail["record"], self.detail["kind"]
        with ui.column().classes("knowledge-detail w-full max-w-[1000px] mx-auto gap-3 min-w-0"):
            ui.label(data.get("title", self.text(kind))).classes("text-2xl font-semibold theme-text break-words")
            if kind == "page":
                with ui.row().classes("items-center gap-2"):
                    ui.icon("help_outline" if data["status"] == "disputed" else "schedule" if data["status"] == "outdated" else "label_outline", size="xs")
                    ui.label(self.text(data["status"])).classes("text-xs theme-text-muted")
                    ui.label(self.text("revision", revision=data["revision"], updated=data.get("updated_at", ""))).classes("text-xs theme-text-muted")
                    if data.get("index_status") and data["index_status"] != "current":
                        ui.label(self.text("index_" + data["index_status"])).classes("text-xs theme-text-muted")
                    ui.button(self.text("edit"), on_click=self.edit_current).props("flat dense no-caps")
                    ui.button(self.text("history"), on_click=partial(self.show_history, "page", data["id"])).props("flat dense no-caps")
                    ui.button(self.text("add_evidence"), on_click=partial(self.edit_evidence, data["id"])).props("flat dense no-caps")
                ui.button(self.text("delete"), icon="delete_outline", on_click=partial(self.preview_delete, "page", deepcopy(data))).props("flat dense no-caps")
                ui.label(self.text(data["kind"]) + (" · " + " / ".join(data["aliases"]) if data.get("aliases") else "")).classes("text-sm theme-text-muted")
                if data["summary"]:
                    ui.label(data["summary"]).classes("text-base theme-text-secondary break-words")
                self.markdown(data["body"]) if data.get("body") else ui.label(self.text("no_body")).classes("theme-text-muted")
                self.render_page_links()
            elif kind == "evidence":
                if data.get("page_id"):
                    ui.button(self.text("open_page"), on_click=partial(self.select, "page", data["page_id"])).props("flat dense no-caps")
                self.render_evidence(data)
            if self.detail.get("evidence"):
                evidence = self.detail["evidence"]
                ui.label(self.text("evidence_count", count=evidence["total"])).classes("font-semibold theme-text")
                if not evidence["items"]:
                    ui.label(self.text("no_evidence")).classes("text-sm theme-text-muted")
                for item in evidence["items"]:
                    self.render_evidence(item)
                self.pager(evidence["offset"], evidence["limit"], evidence["total"],
                           partial(self.evidence_page, kind, data["id"], evidence["offset"]))

    def render_page_links(self):
        # 服务返回 {items,total,limit,offset} 的导航对象：出链取 target，反向链接取 source。
        for caption, nav in (("outgoing_links", self.links), ("backlinks", self.backlinks)):
            with ui.expansion(self.text(caption), icon="link", value=True).classes("w-full"):
                items = (nav or {}).get("items") or []
                if not items:
                    ui.label(self.text("empty")).classes("text-sm theme-text-muted")
                for item in items:
                    ident = item.get("target_id") if caption == "outgoing_links" else item.get("source_id")
                    label = item.get("title") or ident
                    if item.get("exists"):
                        ui.button(label, on_click=partial(self.select, "page", ident)).props("flat no-caps").classes("w-full break-words")
                    else:
                        ui.label(self.text("link_missing") + " · " + label).classes("text-sm theme-text-muted break-words")
                if nav and self.detail:
                    self.pager(nav["offset"], nav["limit"], nav["total"],
                               partial(self.evidence_page, "page", self.detail["record"]["id"], nav["offset"]))

    def clear_error(self):
        self.error = None
        self.refresh_detail()

    # ---- 写入 ----
    def edit_current(self):
        self.edit_page(deepcopy(self.detail["record"]))

    def edit_page(self, record=None):
        draft = {key: deepcopy(record.get(key)) for key in ("kind", "title", "summary", "body", "aliases", "status")} if record else {
            "kind": "concept", "title": "", "summary": "", "body": "", "aliases": [], "status": "active"}
        draft["aliases_text"] = "\n".join(draft.pop("aliases") or [])

        def payload():
            item = {k: v for k, v in draft.items() if k != "aliases_text"}
            item["aliases"] = [a.strip() for a in draft["aliases_text"].splitlines() if a.strip()]
            if record:
                item.update({"id": record["id"], "expected_revision": record["revision"],
                             "expected_content_hash": record["content_hash"]})
            else:
                item["ref"] = "new"
            return {"pages": [item]}

        def fields():
            for name in ("kind", "title", "summary", "body", "aliases_text", "status"):
                self.field(name, draft, multiline=name in ("summary", "body", "aliases_text"),
                           options=KINDS if name == "kind" else STATUSES if name == "status" else None)
        self.write_dialog(self.text("edit" if record else "create"), fields, payload, "page", record)

    def adopt_latest(self, record, latest):
        record["revision"] = latest["revision"]
        record["content_hash"] = latest["content_hash"]

    def result_error(self, result):
        # Wiki 写入按页面独立提交：失败写入 result.errors 而不是抛异常，必须当作失败处理。
        if result.get("errors") and not result.get("committed"):
            return (result["errors"][0] or {}).get("code")
        return None

    def recovery_note(self):
        # Wiki 更新/删除会把被替换文件留作本地恢复副本，不能暗示彻底擦除。
        return "delete_local_recovery"

    def edit_evidence(self, page_id):
        record = self.detail["record"]
        draft = {"source_kind": "user", "stance": "supports", "source_title": "", "source_url": "", "quote": "",
                 "source_library_id": self.detail["library_id"], "source_file_id": "", "source_chunk_id": ""}

        def fields():
            self.field("source_kind", draft, options=("user", "external", "piece"))
            self.field("stance", draft, options=("supports", "contradicts", "context"))
            for name in ("source_title", "source_url", "source_library_id", "source_file_id", "source_chunk_id", "quote"):
                self.field(name, draft, multiline=name == "quote")
            ui.label(self.text("evidence_hint")).classes("text-xs theme-text-muted")

        def payload():
            item = {"page": {"id": page_id}, **{k: draft[k] for k in ("source_kind", "stance", "quote")}}
            if draft["source_title"]:
                item["source_title"] = draft["source_title"]
            if draft["source_kind"] == "external":
                item["source_url"] = draft["source_url"]
            elif draft["source_kind"] == "piece":
                item.update(source_library_id=draft["source_library_id"], source_file_id=int(draft["source_file_id"]), source_chunk_id=int(draft["source_chunk_id"]))
            # 追加证据也会写回 Markdown；已有页用并发条件条目显式声明期望版本与内容哈希。
            condition = {"id": page_id, "expected_revision": record["revision"], "expected_content_hash": record["content_hash"]}
            return {"pages": [condition], "evidence": [item]}
        self.write_dialog(self.text("add_evidence"), fields, payload, "evidence", None)

    def lint_label(self, code):
        label = self.text("lint_" + code)
        return code if label == self.prefix + "lint_" + code else label

    # ---- 只读结构检查与索引重建 ----
    def render_review(self):
        ui.label(self.text("review_intro")).classes("text-sm theme-text-muted")
        with ui.row().classes("items-center gap-2"):
            ui.select(self.choices(("all", "selected", "filtered")), label=self.text("scope"), value=self.lint_scope,
                      on_change=lambda e: setattr(self, "lint_scope", e.value)).props("outlined dense")
            ui.button(self.text("rebuild_index"), on_click=self.rebuild_index).props("outline no-caps").tooltip(self.text("rebuild_intro"))
        with ui.row():
            ui.button(self.text("run_checks"), on_click=self.run_checks).props("outline no-caps")
            ui.button(self.text("disputed"), on_click=partial(self.show_status, "disputed")).props("flat no-caps")
            ui.button(self.text("outdated"), on_click=partial(self.show_status, "outdated")).props("flat no-caps")
        if self.checks:
            data = self.checks
            ui.label(self.text("checked_count", count=data.get("checked_pages", data.get("checked_objects", 0)))).classes("text-xs theme-text-muted")
            if data["truncated"]:
                ui.label(self.text("truncated"))
            for issue in data["issues"]:
                with ui.row().classes("w-full items-center gap-2 py-2 knowledge-list-item"):
                    ui.label(self.lint_label(issue["code"])).classes("text-sm break-words")
                    for kind in ("page", "evidence"):
                        if issue.get(kind + "_id"):
                            ui.button(self.text("open_" + kind), on_click=partial(self.select, kind, issue[kind + "_id"])).props("flat dense no-caps")
                    if issue.get("target_id"):
                        ui.label(self.text("target_page") + " · " + issue["target_id"]).classes("text-xs break-all theme-text-muted")
                    for candidate in issue.get("candidates", []):
                        with ui.column().classes("w-full gap-1"):
                            ui.button(candidate["title"] + " · " + self.text(candidate["kind"]) + " · " + candidate["id"][:8],
                                      on_click=partial(self.select, "page", candidate["id"])).props("flat dense no-caps")
                            ui.label(candidate["summary"]).classes("text-xs break-words theme-text-muted")
            self.pager(data["offset"], data["limit"], data.get("total_pages", data.get("total_objects", 0)), self.check_page)

    async def show_status(self, status):
        self.status = status
        await self.search()
        await self.set_mode("pages")

    async def run_checks(self, reset=True):
        if reset:
            self.lint_offset = 0
        params = {"limit": 25, "offset": self.lint_offset}
        if self.lint_scope == "selected":
            current = (self.detail or {}).get("record", {}).get("id") if (self.detail or {}).get("kind") == "page" else None
            params["page_ids"] = [current] if current else []
        elif self.lint_scope == "filtered":
            params["page_ids"] = [item["id"] for item in (self.results or {}).get("pages", [])]
        self.checks = await self.call(self.service.lint, **params)
        self.refresh_workspace()

    async def check_page(self, delta):
        self.lint_offset = max(0, self.lint_offset + delta * 25)
        await self.run_checks(False)

    async def rebuild_index(self):
        result = await self.call(self.service.rebuild_index)
        if result is None:
            return
        ui.notify(self.text("rebuild_done"), type="positive")
        await self.search(False)

    async def file_references(self, file_id, offset=0):
        info = await self.call(self.service.list_pages, limit=1)
        if not info:
            return
        self.reference_file = file_id
        self.references_data = await self.call(self.service.references, source_library_id=info["library_id"], source_file_id=file_id, limit=25, offset=offset)
        if self.show_view:
            await self.show_view()
        self.refresh_workspace()

    def render_references(self):
        data = self.references_data
        with ui.expansion(self.text("references"), icon="format_quote", value=True).classes("w-full"):
            if not data["evidence"]:
                ui.label(self.text("empty"))
            for item in data["evidence"]:
                owner = item["owner"]
                ui.button(owner.get("title") or owner["id"],
                          on_click=partial(self.select, "page", owner["id"])).props("flat no-caps").classes("w-full break-words")
            async def page(delta):
                await self.file_references(self.reference_file, max(0, data["offset"] + delta * 25))
            self.pager(data["offset"], 25, data["total"], page)
            ui.button(self.text("close"), on_click=self.close_references).props("flat")

    def close_references(self):
        self.references_data = None
        self.refresh_workspace()
