"""离线知识阅读工作台。全部业务读写通过共享服务及 io_bound。"""
from copy import deepcopy
import inspect
import json
import logging
from functools import partial

import markdown2
from nicegui import run, ui

from app.i18n import t
from app.ui.components import _MARKDOWN_EXTRAS, _open_dialog
from app.ui.views.knowledge_presenter import (
    BASES, KINDS, PREDICATES, STATUSES, PendingWrite, graph_options,
    local_source, merge_graph, safe_destination, safe_knowledge_html,
)
from indexing.services import knowledge_service as service
from indexing.services.errors import BusinessError

logger = logging.getLogger(__name__)


def text(message_key, **kwargs):
    return t("knowledge." + message_key, **kwargs)


def choices(values):
    return {value: text(value) for value in values}


class KnowledgeWorkbench:
    def __init__(self, *, open_source=None, show_knowledge=None, dark=None):
        self.open_source = open_source
        self.show_knowledge = show_knowledge
        self.dark = dark or (lambda: False)
        self.mode = "pages"
        self.query = ""
        self.kind = None
        self.status = None
        self.offset = 0
        self.results = None
        self.detail = None
        self.page_links = None
        self.selected_object = None
        self.graph = None
        self.chart = None
        self.positions = {}
        self.viewport = {}
        self.depth = 1
        self.edge_types = ["link", "relation"]
        self.predicates = None
        self.relation_statuses = None
        self.lint_scope = "all"
        self.lint_offset = 0
        self.checks = None
        self.references_data = None
        self.reference_file = None
        self.error = None
        self._selection = 0
        self._search_generation = 0
        self._search_params = None
        self._searching = False
        self._polling = False
        self._poll_timer = None
        self._scroll_areas = {}
        self._scroll_positions = {}
        self._graph_filters = None
        self._graph_generation = 0
        self.refresh_modes = lambda: None
        self.refresh_edge_table = lambda: None
        self.refresh_list = lambda: None
        self.refresh_workspace = lambda: None
        self.refresh_detail = lambda: None
        self.refresh_graph = lambda: None

    async def call(self, function, *args, **kwargs):
        try:
            return await run.io_bound(function, *args, **kwargs)
        except BusinessError as exc:
            ui.notify(text("error", code=exc.code), type="warning")
            return None

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
        function = service.search_objects if "query" in params else service.list_objects
        self._searching = True
        try:
            results = await self.call(function, **params)
            if generation == self._search_generation:
                self.results = results
                self.refresh_list()
        finally:
            if generation == self._search_generation:
                self._searching = False

    async def _refresh_preserving_scroll(self, callback, section):
        area = self._scroll_areas.get(section)
        position = self._scroll_positions.get(section, 0)
        result = callback()
        if inspect.isawaitable(result):
            await result
        if area is not None and not area.is_deleted:
            area.scroll_to(pixels=position)

    async def poll(self):
        """只读当前页和当前详情；无变化不重绘，不触碰独立对话框中的草稿。"""
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
            # 沿用已提交的查询，不自动提交用户尚在输入的新搜索词。
            params = dict(self._search_params or self.search_params())
            function = service.search_objects if "query" in params else service.list_objects
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
            try:
                incoming = await run.io_bound(service.get_record, kind=kind, id=ident,
                                             limit=evidence.get("limit", 25), offset=evidence.get("offset", 0))
                links = await run.io_bound(service.graph, root_id=ident, depth=1) if kind == "object" else self.page_links
            except BusinessError as exc:
                if exc.code != "NOT_FOUND":
                    raise
                incoming, links = None, None
            if not current() or selection != self._selection or self.detail is not detail:
                return
            if incoming != detail or links != self.page_links:
                self.detail, self.page_links = incoming, links
                if incoming is None:
                    self.error = text("broken_link")
                    if kind == "object":
                        self.selected_object = None
                await self._refresh_preserving_scroll(self.refresh_detail, "detail")
        except Exception as exc:
            # 自动检查失败保留当前画面，下次重试；不每两秒弹一次错误。
            logger.debug("知识库自动更新暂未完成 (%s)", type(exc).__name__)
        finally:
            self._polling = False

    async def list_page(self, delta):
        self.offset = max(0, self.offset + delta * 25)
        await self.search(False)

    async def select(self, kind, ident, *, offset=0):
        self._selection += 1
        generation = self._selection
        data = await self.call(service.get_record, kind=kind, id=ident, limit=25, offset=offset)
        if generation != self._selection:
            return
        if data is None:
            self.error = text("broken_link")
            self.refresh_detail()
            return
        self.error = None
        self.detail = data
        if kind == "object":
            self.selected_object = ident
            links = await self.call(service.graph, root_id=ident, depth=1)
            if generation != self._selection:
                return
            self.page_links = links
        self.refresh_detail()
        self.refresh_edge_table()
        if self.chart and not self.chart.is_deleted and self.graph:
            ids = [n["id"] for n in self.graph["nodes"]]
            self.chart.run_chart_method("dispatchAction", {"type": "downplay", "seriesIndex": 0})
            if kind == "object" and ident in ids:
                self.chart.run_chart_method("dispatchAction", {"type": "highlight", "seriesIndex": 0, "dataIndex": ids.index(ident)})
            elif kind in ("relation", "link"):
                edges = [edge["id"] for edge in self.graph["edges"]]
                if ident in edges:
                    self.chart.run_chart_method("dispatchAction", {"type": "highlight", "seriesIndex": 0,
                                                                  "dataType": "edge", "dataIndex": edges.index(ident)})
                    self.chart.run_chart_method("dispatchAction", {"type": "highlight", "seriesIndex": 0,
                                                                  "dataIndex": [ids.index(data["record"][key]) for key in ("source_id", "target_id")]})
        return data

    async def internal_link(self, event):
        ident = event.args
        destination = safe_destination("piece://knowledge/" + ident) if isinstance(ident, str) else None
        if destination:
            await self.select("object", destination[1])

    def markdown(self, body):
        rendered = markdown2.markdown(body, extras=_MARKDOWN_EXTRAS)
        element = ui.html(rendered, sanitize=safe_knowledge_html).classes("chunk-content w-full break-words knowledge-prose")
        element.on("click", self.internal_link, js_handler="""(event) => {
            const a = event.target.closest('a[data-knowledge-id]');
            if (a) { event.preventDefault(); event.stopPropagation(); emit(a.dataset.knowledgeId); }
        }""")
        return element

    def render_middle(self):
        with ui.column().classes("w-full h-full gap-3 p-4 theme-panel overflow-hidden"):
            ui.label(text("title")).classes("text-xl font-semibold theme-text")
            ui.label(text("intro")).classes("text-xs theme-text-muted")
            ui.label(text("auto_refresh")).classes("text-xs theme-text-muted")
            ui.input(text("search"), value=self.query, on_change=lambda e: setattr(self, "query", e.value)).props("outlined dense").classes("w-full").on("keydown.enter", self.search)
            with ui.row().classes("w-full gap-2"):
                ui.select(choices(KINDS), label=text("kind"), value=self.kind, clearable=True,
                          on_change=lambda e: setattr(self, "kind", e.value)).props("dense outlined").classes("flex-1")
                ui.select(choices(STATUSES), label=text("status"), clearable=True).bind_value(self, "status").props("dense outlined").classes("flex-1")
            with ui.row().classes("gap-2"):
                ui.button(text("search"), on_click=self.search).props("unelevated no-caps")
                ui.button(text("create"), icon="add", on_click=lambda: self.edit_object()).props("flat no-caps")
            with ui.scroll_area(on_scroll=lambda e: self._scroll_positions.update(list=e.vertical_position)).classes("w-full flex-1") as area:
                self._scroll_areas["list"] = area
                @ui.refreshable
                def listing():
                    data = self.results
                    if not data:
                        ui.label(text("search_hint")).classes("theme-text-muted")
                        return
                    if not data["objects"]:
                        ui.label(text("empty")).classes("theme-text-muted")
                    for item in data["objects"]:
                        with ui.column().classes("w-full gap-1 py-3 knowledge-list-item"):
                            ui.button(item["title"], on_click=partial(self.select, "object", item["id"])).props("flat no-caps align=left").classes("w-full text-left theme-text")
                            ui.label(text(item["kind"]) + " · " + text(item["status"])).classes("text-xs theme-text-muted")
                            ui.label(item["summary"]).classes("text-sm theme-text-secondary line-clamp-3 break-words")
                            ui.label(item["updated_at"]).classes("text-xs theme-text-muted")
                    self.pager(self.offset, 25, data["total"], self.list_page)
                self.refresh_list = listing.refresh
                listing()
            # 随知识库面板创建/销毁，离开页面后不继续查询；重入时立即检查一次。
            self._poll_timer = ui.timer(2.0, self.poll)

    def pager(self, offset, limit, total, callback):
        with ui.row().classes("items-center gap-2"):
            previous = ui.button(text("previous"), on_click=partial(callback, -1)).props("flat dense no-caps")
            previous.set_enabled(offset > 0)
            ui.label(text("page_count", start=min(offset + 1, total), end=min(offset + limit, total), total=total)).classes("text-xs theme-text-muted")
            following = ui.button(text("next"), on_click=partial(callback, 1)).props("flat dense no-caps")
            following.set_enabled(offset + limit < total)

    async def set_mode(self, mode):
        await self.remember_positions()
        self.mode = mode
        self.refresh_modes()
        self.refresh_workspace()

    def render_right(self):
        with ui.column().classes("w-full flex-1 min-h-0 min-w-0 theme-panel gap-0"):
            @ui.refreshable
            def modes():
                with ui.row().classes("w-full items-center gap-2 px-4 py-2 library-heading"):
                    for mode, icon in (("pages", "article"), ("graph", "hub"), ("review", "fact_check")):
                        ui.button(text(mode), icon=icon, on_click=partial(self.set_mode, mode)).props("flat no-caps").classes("theme-selected" if self.mode == mode else "")
            self.refresh_modes = modes.refresh
            modes()
            with ui.scroll_area(on_scroll=lambda e: self._scroll_positions.update(detail=e.vertical_position)).classes("w-full flex-1") as area:
                self._scroll_areas["detail"] = area
                @ui.refreshable
                def workspace():
                    self.chart = None
                    with ui.column().classes("w-full min-w-0 p-5 gap-4"):
                        if self.mode == "graph":
                            self.render_graph()
                        elif self.mode == "review":
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

    async def remember_positions(self):
        if not self.chart or self.chart.is_deleted:
            return
        try:
            # 唯一插值为 NiceGUI 分配的整数元素 ID，绝不插入模型文本。
            view = await ui.run_javascript(f"""(() => {{
                const chart = getElement({self.chart.id}).chart;
                const data = chart.getModel().getSeriesByIndex(0).getData();
                const points = {{}};
                for (let i = 0; i < data.count(); i++) points[data.getId(i)] = data.getItemLayout(i);
                const series = chart.getOption().series[0];
                return {{points, zoom: series.zoom, center: series.center}};
            }})()""")
            if isinstance(view, dict):
                self.positions.update({key: tuple(value) for key, value in view.get("points", {}).items()
                                       if isinstance(value, list) and len(value) == 2})
                self.viewport = {key: view[key] for key in ("zoom", "center") if view.get(key) is not None}
        except (TimeoutError, RuntimeError):
            # 断线时保留上次位置；布局从不触发业务写入。
            pass

    async def load_graph(self, *, center=False, expand=False):
        self._graph_generation += 1
        generation = self._graph_generation
        await self.remember_positions()
        root = self.selected_object if center or self.graph is None else self.graph["root_id"]
        if not root:
            ui.notify(text("choose_root"), type="info")
            return
        if not self.edge_types:
            ui.notify(text("edge_required"), type="warning")
            return
        filters = (tuple(self.edge_types), tuple(self.predicates or ()), tuple(self.relation_statuses or ()))
        incoming = await self.call(service.graph, root_id=self.selected_object if expand else root,
                                   depth=1 if expand else self.depth, edge_types=self.edge_types,
                                   predicates=self.predicates, statuses=self.relation_statuses,
                                   max_nodes=100, max_edges=300)
        if incoming and generation == self._graph_generation:
            self.graph = merge_graph(self.graph, incoming) if expand and self.graph and filters == self._graph_filters else incoming
            self._graph_filters = filters
            if center:
                self.positions = {}
                self.viewport = {}
            self.refresh_graph()

    async def graph_click(self, event):
        data = event.args.get("data")
        if not isinstance(data, dict) or not self.graph:
            return
        if event.args.get("dataType") == "edge":
            edge = next((edge for edge in self.graph["edges"] if edge["id"] == data.get("id")), None)
            if edge:
                await self.select(edge["kind"], edge["id"])
        elif any(n["id"] == data.get("id") for n in self.graph["nodes"]):
            await self.select("object", data["id"])

    def render_graph(self):
        ui.label(text("graph_scope")).classes("text-xs theme-text-muted")
        with ui.row().classes("w-full items-center gap-2"):
            ui.select({1: text("one_hop"), 2: text("two_hops")}, value=self.depth,
                      on_change=lambda e: setattr(self, "depth", e.value)).props("dense outlined")
            ui.select(choices(("link", "relation")), value=self.edge_types, multiple=True,
                      on_change=lambda e: setattr(self, "edge_types", e.value)).props("dense outlined").classes("min-w-32")
            ui.select(choices(PREDICATES), label=text("predicate"), value=self.predicates, multiple=True, clearable=True,
                      on_change=lambda e: setattr(self, "predicates", e.value or None)).props("dense outlined").classes("min-w-32")
            ui.select(choices(STATUSES), label=text("status"), value=self.relation_statuses, multiple=True, clearable=True,
                      on_change=lambda e: setattr(self, "relation_statuses", e.value or None)).props("dense outlined").classes("min-w-32")
        with ui.row().classes("gap-2"):
            ui.button(text("refresh"), on_click=self.load_graph).props("flat no-caps")
            ui.button(text("center"), on_click=partial(self.load_graph, center=True)).props("flat no-caps")
            ui.button(text("expand"), on_click=partial(self.load_graph, expand=True)).props("flat no-caps")
            ui.button(text("reset_view"), on_click=self.reset_graph_view).props("flat no-caps")
            ui.button(text("pages"), on_click=partial(self.set_mode, "pages")).props("flat no-caps")
        @ui.refreshable
        def graph_body():
            if self.graph is None:
                ui.label(text("choose_root")).classes("theme-text-muted")
                return
            data = self.graph
            ui.label(text("graph_count", nodes=len(data["nodes"]), edges=len(data["edges"]), depth=data["depth"])).classes("text-xs theme-text-muted")
            if data["truncated"]:
                ui.label(text("truncated")).classes("font-semibold theme-text")
            options = graph_options(data, dark=bool(self.dark()), selected=self.selected_object,
                                    positions=self.positions, label=text)
            options["series"][0].update(self.viewport)
            self.positions.update({n["id"]: (n["x"], n["y"]) for n in options["series"][0]["data"]})
            height = "h-[520px]" if len(data["nodes"]) > 50 else "h-[420px]"
            # 3.3.1 的 on_point_click 强制读取 value；无数值的图节点/边须用原生事件。
            self.chart = ui.echart(options).classes(f"w-full {height} min-w-0")
            self.chart.on("chart:click", self.graph_click, ["data", "dataType"])
            with ui.expansion(text("table"), icon="table_rows", value=True).classes("w-full"), \
                    ui.column().classes("w-full max-h-[280px] overflow-y-auto"):
                @ui.refreshable
                def table():
                    self.edge_table(data)
                self.refresh_edge_table = table.refresh
                table()
        self.refresh_graph = graph_body.refresh
        graph_body()

    def reset_graph_view(self):
        self.positions = {}
        self.viewport = {}
        self.refresh_graph()

    def edge_table(self, graph, edges=None):
        names = {node["id"]: node["title"] for node in graph["nodes"]}
        edges = graph["edges"] if edges is None else edges
        if not edges:
            ui.label(text("no_edges")).classes("text-sm theme-text-muted")
        for edge in edges:
            selected = self.detail and self.detail["record"]["id"] == edge["id"]
            with ui.row().classes("w-full items-center gap-1 knowledge-list-item py-2" + (" theme-selected" if selected else "")):
                ui.button(names[edge["source_id"]], on_click=partial(self.select, "object", edge["source_id"])).props("flat dense no-caps").classes("max-w-full break-words")
                ui.label("↔" if edge.get("symmetric") else "→")
                ui.button(names[edge["target_id"]], on_click=partial(self.select, "object", edge["target_id"])).props("flat dense no-caps").classes("max-w-full break-words")
                ui.button(text("link" if edge["kind"] == "link" else edge["predicate"]),
                          on_click=partial(self.select, edge["kind"], edge["id"])).props("outline dense no-caps")
                if edge["kind"] == "relation":
                    ui.label(text("evidence_count", count=edge["evidence_count"])).classes("text-xs theme-text-muted")

    def render_detail(self):
        if self.error:
            ui.label(self.error).classes("theme-text-muted")
            ui.button(text("back"), on_click=self.clear_error).props("flat")
        if not self.detail:
            ui.label(text("choose_object")).classes("text-sm theme-text-muted py-8")
            return
        data, kind = self.detail["record"], self.detail["kind"]
        with ui.column().classes("knowledge-detail w-full max-w-[1000px] mx-auto gap-3 min-w-0"):
            ui.label(data.get("title", text(kind))).classes("text-2xl font-semibold theme-text break-words")
            if kind in ("object", "relation"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("help_outline" if data["status"] == "disputed" else "schedule" if data["status"] == "outdated" else "label_outline", size="xs")
                    ui.label(text(data["status"])).classes("text-xs theme-text-muted")
                    ui.label(text("revision", revision=data["revision"], updated=data["updated_at"])).classes("text-xs theme-text-muted")
                    ui.button(text("edit"), on_click=self.edit_current).props("flat dense no-caps")
                    ui.button(text("history"), on_click=partial(self.show_history, kind, data["id"])).props("flat dense no-caps")
                    ui.button(text("add_evidence"), on_click=partial(self.edit_evidence, kind, data["id"])).props("flat dense no-caps")
            ui.button(text("delete"), icon="delete_outline", on_click=partial(self.preview_delete, kind, deepcopy(data))).props("flat dense no-caps")
            if kind == "object":
                ui.label(text(data["kind"]) + (" · " + " / ".join(data["aliases"]) if data["aliases"] else "")).classes("text-sm theme-text-muted")
                if data["summary"]:
                    ui.label(data["summary"]).classes("text-base theme-text-secondary break-words")
                self.markdown(data["body"]) if data["body"] else ui.label(text("no_body")).classes("theme-text-muted")
                with ui.row().classes("gap-2"):
                    ui.button(text("create_link"), on_click=partial(self.edit_edge, "link", data["id"])).props("flat no-caps")
                    ui.button(text("create_relation"), on_click=partial(self.edit_edge, "relation", data["id"])).props("flat no-caps")
                    ui.button(text("view_graph"), on_click=self.view_graph).props("flat no-caps")
                with ui.expansion(text("backlinks"), icon="link").classes("w-full"):
                    if self.page_links:
                        if self.page_links["truncated"]:
                            ui.label(text("truncated"))
                        edges = self.page_links["edges"]
                        for caption, selected in (
                            ("outgoing_links", [e for e in edges if e["kind"] == "link" and e["source_id"] == data["id"]]),
                            ("incoming_links", [e for e in edges if e["kind"] == "link" and e["target_id"] == data["id"]]),
                            ("relations", [e for e in edges if e["kind"] == "relation"]),
                        ):
                            ui.label(text(caption)).classes("text-sm font-semibold theme-text")
                            self.edge_table(self.page_links, selected)
            elif kind in ("relation", "link"):
                with ui.row().classes("gap-2"):
                    ui.button(text("source_object"), on_click=partial(self.select, "object", data["source_id"])).props("flat no-caps")
                    ui.label("↔" if data.get("symmetric") else "→")
                    ui.button(text("target_object"), on_click=partial(self.select, "object", data["target_id"])).props("flat no-caps")
                if kind == "relation":
                    for key in ("predicate", "description", "qualifier", "basis"):
                        ui.label(text(key) + " · " + (text(data[key]) if key in ("basis", "predicate") else data[key])).classes("text-sm break-words theme-text-secondary")
            elif kind == "evidence":
                self.render_evidence(data)
            if "evidence" in self.detail:
                evidence = self.detail["evidence"]
                ui.label(text("evidence_count", count=evidence["total"])).classes("font-semibold theme-text")
                if not evidence["items"]:
                    ui.label(text("no_evidence")).classes("text-sm theme-text-muted")
                for item in evidence["items"]:
                    self.render_evidence(item)
                self.pager(evidence["offset"], evidence["limit"], evidence["total"],
                           partial(self.evidence_page, kind, data["id"], evidence["offset"]))

    def clear_error(self):
        self.error = None
        self.refresh_detail()

    async def evidence_page(self, kind, ident, offset, delta):
        await self.select(kind, ident, offset=max(0, offset + delta * 25))

    async def view_graph(self):
        await self.set_mode("graph")
        await self.load_graph(center=True)

    def render_evidence(self, evidence):
        with ui.column().classes("w-full gap-2 p-3 knowledge-evidence theme-card"):
            ui.label(text(evidence["stance"]) + " · " + text(evidence["source_kind"])).classes("text-sm font-semibold theme-text")
            ui.label(evidence.get("source_title") or "").classes("text-sm break-words")
            ui.label(text("location_" + evidence["location_status"])).classes("text-xs theme-text-muted")
            ui.label(evidence["quote"]).classes("text-sm whitespace-pre-wrap break-words theme-text-secondary")
            if evidence["source_kind"] == "piece":
                ui.label(text("source_ids", library=evidence["source_library_id"], file=evidence["source_file_id"], chunk=evidence["source_chunk_id"])).classes("text-xs break-all theme-text-muted")
                if local_source(evidence, self.detail["library_id"]):
                    ui.button(text("compare_source"), on_click=partial(self.compare_source, deepcopy(evidence))).props("flat dense no-caps")
            elif evidence["source_kind"] == "external" and safe_destination(evidence.get("source_url")):
                ui.link(text("external_source"), evidence["source_url"], new_tab=True)
            ui.button(text("delete_evidence"), on_click=partial(self.preview_delete, "evidence", deepcopy(evidence))).props("flat dense no-caps")

    async def compare_source(self, evidence):
        fresh = await self.call(service.get_record, kind="evidence", id=evidence["id"])
        if not fresh or not local_source(fresh["record"], fresh["library_id"]):
            ui.notify(text("source_unavailable"), type="warning")
            return
        evidence = fresh["record"]
        from indexing.services.chunk_service import get_chunk_by_id
        chunk = await self.call(get_chunk_by_id, evidence["source_chunk_id"])
        if not chunk or chunk["file_id"] != evidence["source_file_id"]:
            ui.notify(text("source_unavailable"), type="warning")
            return
        with ui.dialog() as dialog, ui.card().classes("w-[1000px] max-w-full theme-card"):
            ui.label(text("compare_source")).classes("text-lg font-semibold")
            with ui.row().classes("w-full gap-4 items-start"):
                with ui.column().classes("flex-1 min-w-[240px]"):
                    ui.label(text("snapshot"))
                    ui.label(evidence["quote"]).classes("whitespace-pre-wrap break-words")
                with ui.column().classes("flex-1 min-w-[240px]"):
                    ui.label(text("current_body"))
                    self.markdown(chunk["chunk_text"])
            if self.open_source:
                async def open_file():
                    dialog.close()
                    await self.open_source(evidence)
                ui.button(text("open_file"), on_click=open_file).props("flat no-caps")
            ui.button(text("close"), on_click=dialog.close).props("flat")
        _open_dialog(dialog)

    def edit_current(self):
        if self.detail["kind"] == "object":
            self.edit_object(deepcopy(self.detail["record"]))
        else:
            self.edit_relation(deepcopy(self.detail["record"]))

    def field(self, name, values, *, multiline=False, options=None):
        caption = text("title_field" if name == "title" else name)
        if options:
            element = ui.select(choices(options), label=caption, value=values.get(name))
        elif multiline:
            element = ui.textarea(caption, value=values.get(name, "")).props("autogrow")
        else:
            element = ui.input(caption, value=values.get(name, ""))
        element.bind_value(values, name).props("outlined dense").classes("w-full")
        return element

    def edit_object(self, record=None):
        draft = {key: deepcopy(record[key]) for key in ("kind", "title", "summary", "body", "aliases", "status")} if record else {
            "kind": "concept", "title": "", "summary": "", "body": "", "aliases": [], "status": "active"}
        draft["aliases_text"] = "\n".join(draft.pop("aliases"))
        def payload():
            item = {k: v for k, v in draft.items() if k != "aliases_text"}
            item["aliases"] = [a.strip() for a in draft["aliases_text"].splitlines() if a.strip()]
            item.update({"id": record["id"], "expected_revision": record["revision"]} if record else {"ref": "new"})
            return {"objects": [item]}
        def fields():
            for name in ("kind", "title", "summary", "body", "aliases_text", "status"):
                self.field(name, draft, multiline=name in ("summary", "body", "aliases_text"),
                           options=KINDS if name == "kind" else STATUSES if name == "status" else None)
        self.write_dialog(text("edit" if record else "create"), fields, payload, "object", record)

    def edit_relation(self, record):
        draft = {key: record[key] for key in ("description", "qualifier", "basis", "status")}
        def fields():
            self.relation_fields(draft)
        def payload():
            return {"relations": [{"id": record["id"], "expected_revision": record["revision"], **draft}]}
        self.write_dialog(text("edit"), fields, payload, "relation", record)

    def relation_fields(self, draft):
        for name in ("description", "qualifier", "basis", "status"):
            self.field(name, draft, multiline=name in ("description", "qualifier"),
                       options=BASES if name == "basis" else STATUSES if name == "status" else None)

    def edit_edge(self, kind, source_id):
        draft = {"target": None, "predicate": "related_to", "description": "", "qualifier": "", "basis": "inference", "status": "active"}
        def fields():
            search = ui.input(text("target_search")).props("outlined dense").classes("w-full")
            target = ui.select({}, label=text("target_object"), on_change=lambda e: draft.update(target=e.value)).props("outlined dense").classes("w-full")
            async def lookup():
                if not search.value or not search.value.strip():
                    return
                found = await self.call(service.search_objects, query=search.value, limit=50)
                if found:
                    target.set_options({item["id"]: item["title"] + " · " + text(item["kind"]) + " · " + item["summary"][:60] + " · " + item["id"][:8]
                                        for item in found["objects"] if item["id"] != source_id})
                    ui.notify(text("target_limit"), type="info")
            ui.button(text("search"), on_click=lookup).props("flat")
            if kind == "relation":
                self.field("predicate", draft, options=PREDICATES)
                self.relation_fields(draft)
        def payload():
            if not draft["target"]:
                raise ValueError(text("choose_target"))
            item = {"source": {"id": source_id}, "target": {"id": draft["target"]}}
            if kind == "relation":
                item.update({k: v for k, v in draft.items() if k != "target"}, ref="new")
            return {"links" if kind == "link" else "relations": [item]}
        self.write_dialog(text("create_link" if kind == "link" else "create_relation"), fields, payload, kind, None)

    def edit_evidence(self, kind, owner):
        draft = {"source_kind": "user", "stance": "supports", "source_title": "", "source_url": "", "quote": "",
                 "source_library_id": self.detail["library_id"], "source_file_id": "", "source_chunk_id": ""}
        def fields():
            self.field("source_kind", draft, options=("user", "external", "piece"))
            self.field("stance", draft, options=("supports", "contradicts", "context"))
            for name in ("source_title", "source_url", "source_library_id", "source_file_id", "source_chunk_id", "quote"):
                self.field(name, draft, multiline=name == "quote")
            ui.label(text("evidence_hint")).classes("text-xs theme-text-muted")
        def payload():
            item = {kind: {"id": owner}, **{k: draft[k] for k in ("source_kind", "stance", "quote")}}
            if draft["source_title"]:
                item["source_title"] = draft["source_title"]
            if draft["source_kind"] == "external":
                item["source_url"] = draft["source_url"]
            elif draft["source_kind"] == "piece":
                item.update(source_library_id=draft["source_library_id"], source_file_id=int(draft["source_file_id"]), source_chunk_id=int(draft["source_chunk_id"]))
            return {"evidence": [item]}
        self.write_dialog(text("add_evidence"), fields, payload, "evidence", None)

    def write_dialog(self, title, fields, payload, kind, record):
        pending = {"value": None}
        reason = {"reason": ""}
        with ui.dialog().props("persistent") as dialog, ui.card().classes("w-[780px] max-w-full max-h-[90vh] overflow-auto theme-card"):
            ui.label(title).classes("text-xl font-semibold")
            fields()
            self.field("reason", reason)
            message = ui.label().classes("text-sm whitespace-pre-wrap theme-text-muted")
            comparison = ui.column().classes("w-full")
            busy = {"value": False}
            async def save():
                if busy["value"]:
                    return
                busy["value"] = True
                try:
                    if pending["value"] is None:
                        pending["value"] = PendingWrite({**payload(), "reason": reason["reason"]})
                    request = pending["value"]
                    if request.uncertain:
                        try:
                            result = await run.io_bound(service.request_result, request.key)
                        except BusinessError as exc:
                            if exc.code != "NOT_FOUND":
                                raise
                            # 原键原内容重试，不采用输入框可能已变化的草稿。
                            result = await run.io_bound(service.apply, request.payload, actor="gui")
                    else:
                        result = await run.io_bound(service.apply, request.payload, actor="gui")
                    dialog.close()
                    await self.after_write(result)
                except BusinessError as exc:
                    pending["value"] = None
                    message.set_text(text("error", code=exc.code))
                    if exc.code == "VERSION_CONFLICT" and record:
                        latest = await self.call(service.get_record, kind=kind, id=record["id"])
                        if latest:
                            comparison.clear()
                            with comparison:
                                ui.label(text("conflict_draft")).classes("font-semibold")
                                ui.label(json.dumps(latest["record"], ensure_ascii=False, indent=2)).classes("text-xs whitespace-pre-wrap break-words")
                                def use_revision():
                                    record["revision"] = latest["record"]["revision"]
                                    message.set_text(text("compare_then_save"))
                                    comparison.clear()
                                ui.button(text("use_revision"), on_click=use_revision).props("outline no-caps")
                except ValueError as exc:
                    pending["value"] = None
                    message.set_text(str(exc))
                except Exception:
                    if pending["value"]:
                        pending["value"].uncertain = True
                        message.set_text(text("uncertain", request_key=pending["value"].key))
                finally:
                    busy["value"] = False
            ui.button(text("save"), on_click=save).props("unelevated no-caps")
            ui.button(text("close"), on_click=dialog.close).props("flat")
        _open_dialog(dialog)

    async def after_write(self, result):
        readback = None
        for name, kind in (("objects", "object"), ("relations", "relation"), ("links", "link"), ("evidence", "evidence")):
            if result.get(name):
                readback = await self.select(kind, result[name][0]["id"])
                break
        await self.search(False)
        if self.graph:
            await self.load_graph()
        ui.notify(text("saved" if readback else "saved_unread"), type="positive" if readback else "warning")

    async def preview_delete(self, kind, record):
        payload = {"kind": kind, "id": record["id"]}
        if kind in ("object", "relation"):
            payload["expected_revision"] = record["revision"]
        preview = await self.call(service.delete, {**payload, "dry_run": True}, actor="gui")
        if preview is None:
            return
        pending = PendingWrite({**payload, "impact_token": preview["impact_token"], "dry_run": False, "confirmed": True})
        with ui.dialog().props("persistent") as dialog, ui.card().classes("w-[600px] max-w-full theme-card"):
            ui.label(text("delete_preview")).classes("text-xl font-semibold")
            ui.label(record.get("title", record["id"])).classes("break-all")
            for key, count in preview["counts"].items():
                ui.label(text("impact_count", kind=text(key), count=count))
            ui.label(text("delete_scope")).classes("text-sm theme-text-muted")
            message = ui.label().classes("text-sm break-all")
            busy = {"value": False}
            async def confirm():
                if busy["value"]:
                    return
                busy["value"] = True
                try:
                    if pending.uncertain:
                        try:
                            await run.io_bound(service.request_result, pending.key)
                        except BusinessError as exc:
                            if exc.code != "NOT_FOUND":
                                raise
                            await run.io_bound(service.delete, pending.payload, actor="gui")
                    else:
                        await run.io_bound(service.delete, pending.payload, actor="gui")
                    dialog.close()
                    self.detail = None
                    self.graph = None
                    self.refresh_workspace()
                    await self.search(False)
                except BusinessError as exc:
                    message.set_text(text("delete_conflict", code=exc.code))
                    button.disable()
                except Exception:
                    pending.uncertain = True
                    message.set_text(text("uncertain", request_key=pending.key))
                finally:
                    busy["value"] = False
            button = ui.button(text("confirm_delete"), on_click=confirm).props("unelevated color=negative no-caps")
            ui.button(text("cancel"), on_click=dialog.close).props("flat")
        _open_dialog(dialog)

    async def show_history(self, kind, ident, offset=0):
        data = await self.call(service.history, kind=kind, id=ident, limit=10, offset=offset)
        if not data:
            return
        with ui.dialog() as dialog, ui.card().classes("w-[900px] max-w-full max-h-[90vh] overflow-auto theme-card"):
            ui.label(text("history")).classes("text-xl font-semibold")
            ui.label(text("history_readonly")).classes("text-xs theme-text-muted")
            for item in data["history"]:
                with ui.expansion(str(item["after_revision"]) + " · " + item["reason"]).classes("w-full"):
                    ui.label(json.dumps({"before": item["before"], "after": item["after"]}, ensure_ascii=False, indent=2)).classes("text-xs whitespace-pre-wrap break-words")
            async def page(delta):
                dialog.close()
                await self.show_history(kind, ident, max(0, offset + delta * 10))
            self.pager(offset, 10, data["total"], page)
            ui.button(text("close"), on_click=dialog.close).props("flat")
        _open_dialog(dialog)

    def render_review(self):
        ui.label(text("review_intro")).classes("text-sm theme-text-muted")
        ui.select(choices(("all", "selected", "filtered")), label=text("scope"), value=self.lint_scope,
                  on_change=lambda e: setattr(self, "lint_scope", e.value)).props("outlined dense")
        with ui.row():
            ui.button(text("run_checks"), on_click=self.run_checks).props("outline no-caps")
            ui.button(text("disputed"), on_click=partial(self.show_status, "disputed")).props("flat no-caps")
            ui.button(text("outdated"), on_click=partial(self.show_status, "outdated")).props("flat no-caps")
        if self.checks:
            data = self.checks
            ui.label(text("checked_count", count=data["checked_objects"])).classes("text-xs theme-text-muted")
            if data["truncated"]:
                ui.label(text("truncated"))
            for issue in data["issues"]:
                with ui.row().classes("w-full items-center gap-2 py-2 knowledge-list-item"):
                    ui.label(text("lint_" + issue["code"])).classes("text-sm break-words")
                    for kind in ("object", "relation", "evidence"):
                        if issue.get(kind + "_id"):
                            ui.button(text("open_" + kind), on_click=partial(self.select, kind, issue[kind + "_id"])).props("flat dense no-caps")
                    if issue.get("target_id"):
                        ui.label(text("target_object") + " · " + issue["target_id"]).classes("text-xs break-all theme-text-muted")
                    for candidate in issue.get("candidates", []):
                        with ui.column().classes("w-full gap-1"):
                            ui.button(candidate["title"] + " · " + text(candidate["kind"]) + " · " + candidate["id"][:8],
                                      on_click=partial(self.select, "object", candidate["id"])).props("flat dense no-caps")
                            ui.label(candidate["summary"]).classes("text-xs break-words theme-text-muted")
            self.pager(data["offset"], data["limit"], data["total_objects"], self.check_page)

    async def show_status(self, status):
        self.status = status
        await self.search()
        await self.set_mode("pages")

    async def run_checks(self, reset=True):
        if reset:
            self.lint_offset = 0
        params = {"limit": 25, "offset": self.lint_offset}
        if self.lint_scope == "selected":
            params["object_ids"] = [self.selected_object] if self.selected_object else []
        elif self.lint_scope == "filtered":
            params["object_ids"] = [item["id"] for item in (self.results or {}).get("objects", [])]
        self.checks = await self.call(service.lint, **params)
        self.refresh_workspace()

    async def check_page(self, delta):
        self.lint_offset = max(0, self.lint_offset + delta * 25)
        await self.run_checks(False)

    async def file_references(self, file_id, offset=0):
        info = await self.call(service.list_objects, limit=1)
        if not info:
            return
        self.reference_file = file_id
        self.references_data = await self.call(service.references, source_library_id=info["library_id"], source_file_id=file_id, limit=25, offset=offset)
        if self.show_knowledge:
            await self.show_knowledge()
        self.refresh_workspace()

    def render_references(self):
        data = self.references_data
        with ui.expansion(text("references"), icon="format_quote", value=True).classes("w-full"):
            if not data["evidence"]:
                ui.label(text("empty"))
            for item in data["evidence"]:
                owner = item["owner"]
                ui.button(owner.get("title") or owner.get("description") or owner["id"],
                          on_click=partial(self.select, item["owner_kind"], owner["id"])).props("flat no-caps").classes("w-full break-words")
            async def page(delta):
                await self.file_references(self.reference_file, max(0, data["offset"] + delta * 25))
            self.pager(data["offset"], 25, data["total"], page)
            ui.button(text("close"), on_click=self.close_references).props("flat")

    def close_references(self):
        self.references_data = None
        self.refresh_workspace()
