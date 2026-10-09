"""知识图谱工作台：独立维护实体、语义关系与证据。

图谱节点没有 Wiki 正文，也不管理页面链接；图谱直接依据原始资料构建。
"""
from copy import deepcopy
from functools import partial
import logging

from nicegui import run, ui

from app.ui.views.graph_presenter import (
    BASES, KINDS, PREDICATES, graph_options, merge_graph,
)
from app.ui.components import help_hint
from app.ui.views.knowledge_common import STATUSES, WorkbenchBase, local_source
from indexing.services import knowledge_service as service
from indexing.services.errors import BusinessError

logger = logging.getLogger(__name__)


class GraphWorkbench(WorkbenchBase):
    service = service
    prefix = "graph."
    result_readback = (("objects", "object"), ("relations", "relation"), ("evidence", "evidence"))
    revision_kinds = ("object", "relation")
    hash_kinds = ()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.mode = "objects"
        self.kind = None
        self.status = None
        self.selected_object = None
        self.local_relations = None
        self.graph = None
        self.chart = None
        self.positions = {}
        self.viewport = {}
        self.depth = 1
        self.edge_types = ["relation"]
        self.predicates = None
        self.relation_statuses = None
        self.lint_scope = "all"
        self.lint_offset = 0
        self.checks = None
        self.references_data = None
        self.reference_file = None
        self.refresh_modes = lambda: None
        self.refresh_graph = lambda: None
        self._graph_filters = None
        self._graph_generation = 0

    def _list_function(self, params):
        return service.search_objects if "query" in params else service.list_objects

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
        """只读当前列表和当前详情；无变化不重绘，不触碰对话框草稿。"""
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
            try:
                incoming = await run.io_bound(service.get_record, kind=kind, id=ident,
                                             limit=evidence.get("limit", 25), offset=evidence.get("offset", 0))
                local = await run.io_bound(service.graph, root_id=ident, depth=1, edge_types=["relation"]) if kind == "object" else None
            except BusinessError as exc:
                if exc.code != "NOT_FOUND":
                    raise
                incoming, local = None, None
            if not current() or selection != self._selection or self.detail is not detail:
                return
            if incoming != detail or local != self.local_relations:
                self.detail, self.local_relations = incoming, local
                if incoming is None:
                    self.error = self.text("broken_link")
                    if kind == "object":
                        self.selected_object = None
                await self._refresh_preserving_scroll(self.refresh_detail, "detail")
        except Exception as exc:
            # 自动检查失败保留当前画面，下次重试；不每两秒弹一次错误。
            logger.debug("图谱自动更新暂未完成 (%s)", type(exc).__name__)
        finally:
            self._polling = False

    async def select(self, kind, ident, *, offset=0):
        self._selection += 1
        generation = self._selection
        data = await self.call(service.get_record, kind=kind, id=ident, limit=25, offset=offset)
        if generation != self._selection:
            return
        if data is None:
            self.error = self.text("broken_link")
            self.refresh_detail()
            return
        self.error = None
        self.detail = data
        self.highlight_selection()
        if kind == "object":
            self.selected_object = ident
            self.local_relations = await self.call(service.graph, root_id=ident, depth=1, edge_types=["relation"])
            if generation != self._selection:
                return
        self.refresh_detail()
        self.refresh_edge_table()
        if self.chart and not self.chart.is_deleted and self.graph:
            ids = [n["id"] for n in self.graph["nodes"]]
            self.chart.run_chart_method("dispatchAction", {"type": "downplay", "seriesIndex": 0})
            if kind == "object" and ident in ids:
                self.chart.run_chart_method("dispatchAction", {"type": "highlight", "seriesIndex": 0, "dataIndex": ids.index(ident)})
            elif kind == "relation":
                edges = [edge["id"] for edge in self.graph["edges"]]
                if ident in edges:
                    self.chart.run_chart_method("dispatchAction", {"type": "highlight", "seriesIndex": 0,
                                                                  "dataType": "edge", "dataIndex": edges.index(ident)})
        return data

    def render_middle(self):
        self.render_catalog(KINDS, "objects", "object", lambda: self.edit_object())

    def render_right(self, controls=None):
        with ui.column().classes("w-full h-full flex-1 min-h-0 min-w-0 theme-content gap-0"):
            @ui.refreshable
            def modes():
                with ui.row().classes("w-full items-center library-heading workspace-toolbar overflow-x-auto"):
                    if controls:
                        controls()
                    for mode, icon in (("objects", "account_tree"), ("graph", "hub"), ("review", "fact_check")):
                        ui.button(self.text(mode), icon=icon, color=None, on_click=partial(self.set_mode, mode)).props("flat dense no-caps").classes("theme-selected" if self.mode == mode else "theme-text-secondary")
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

    async def set_mode(self, mode):
        await self.remember_positions()
        self.mode = mode
        self.refresh_modes()
        self.refresh_workspace()

    # ---- 局部图探索 ----
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
            self.notify(self.text("choose_root"), type="info")
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
                await self.select("relation", edge["id"])
        elif any(n["id"] == data.get("id") for n in self.graph["nodes"]):
            await self.select("object", data["id"])

    def render_graph(self):
        with ui.row().classes("w-full items-center gap-2"):
            ui.label(self.text("graph")).classes("text-sm font-medium theme-text")
            help_hint(self.text("graph_scope"))
        with ui.row().classes("w-full items-center gap-2"):
            ui.select({1: self.text("one_hop"), 2: self.text("two_hops")}, value=self.depth,
                      on_change=lambda e: setattr(self, "depth", e.value)).props("dense outlined")
            ui.select(self.choices(PREDICATES), label=self.text("predicate"), value=self.predicates, multiple=True, clearable=True,
                      on_change=lambda e: setattr(self, "predicates", e.value or None)).props("dense outlined").classes("min-w-32")
            ui.select(self.choices(STATUSES), label=self.text("status"), value=self.relation_statuses, multiple=True, clearable=True,
                      on_change=lambda e: setattr(self, "relation_statuses", e.value or None)).props("dense outlined").classes("min-w-32")
        with ui.row().classes("gap-2"):
            ui.button(self.text("refresh"), on_click=self.load_graph).props("flat no-caps")
            ui.button(self.text("center"), on_click=partial(self.load_graph, center=True)).props("flat no-caps")
            ui.button(self.text("expand"), on_click=partial(self.load_graph, expand=True)).props("flat no-caps")
            ui.button(self.text("reset_view"), on_click=self.reset_graph_view).props("flat no-caps")
        @ui.refreshable
        def graph_body():
            if self.graph is None:
                ui.label(self.text("choose_root")).classes("theme-text-muted")
                return
            data = self.graph
            ui.label(self.text("graph_count", nodes=len(data["nodes"]), edges=len(data["edges"]), depth=data["depth"])).classes("text-xs theme-text-muted")
            if data["truncated"]:
                ui.label(self.text("truncated")).classes("font-semibold theme-text")
            options = graph_options(data, dark=bool(self.dark()), selected=self.selected_object,
                                    positions=self.positions, label=self.text)
            options["series"][0].update(self.viewport)
            self.positions.update({n["id"]: (n["x"], n["y"]) for n in options["series"][0]["data"]})
            height = "h-[520px]" if len(data["nodes"]) > 50 else "h-[420px]"
            # 3.3.1 的 on_point_click 强制读取 value；无数值的图节点/边须用原生事件。
            self.chart = ui.echart(options).classes(f"w-full {height} min-w-0")
            self.chart.on("chart:click", self.graph_click, ["data", "dataType"])
            with ui.expansion(self.text("table"), icon="table_rows", value=True).classes("w-full"), \
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
            ui.label(self.text("no_edges")).classes("text-sm theme-text-muted")
        for edge in edges:
            selected = self.detail and self.detail["record"]["id"] == edge["id"]
            with ui.row().classes("w-full items-center gap-1 knowledge-list-item py-2" + (" theme-selected" if selected else "")):
                ui.button(names[edge["source_id"]], on_click=partial(self.select, "object", edge["source_id"])).props("flat dense no-caps").classes("max-w-full break-words")
                ui.label("↔" if edge.get("symmetric") else "→")
                ui.button(names[edge["target_id"]], on_click=partial(self.select, "object", edge["target_id"])).props("flat dense no-caps").classes("max-w-full break-words")
                ui.button(self.text(edge["predicate"]), on_click=partial(self.select, "relation", edge["id"])).props("outline dense no-caps")
                ui.label(self.text("evidence_count", count=edge["evidence_count"])).classes("text-xs theme-text-muted")

    # ---- 详情 ----
    def render_detail(self):
        if self.error:
            ui.label(self.error).classes("theme-text-muted")
            ui.button(self.text("back"), on_click=self.clear_error).props("flat")
        if not self.detail:
            ui.label(self.text("choose_node")).classes("text-sm theme-text-muted py-8")
            return
        data, kind = self.detail["record"], self.detail["kind"]
        with ui.column().classes("knowledge-detail w-full max-w-[1000px] mx-auto gap-3 min-w-0"):
            ui.label(data.get("title", self.text(kind))).classes("text-2xl font-semibold theme-text break-words")
            if kind in ("object", "relation"):
                with ui.row().classes("w-full items-center gap-2 knowledge-metadata"):
                    ui.icon("help_outline" if data["status"] == "disputed" else "schedule" if data["status"] == "outdated" else "label_outline", size="xs")
                    ui.label(self.text(data["status"])).classes("text-xs theme-text-muted").tooltip(
                        self.text("revision", revision=data["revision"], updated=data["updated_at"])
                    )
                    ui.space()
                    ui.button(self.text("edit"), icon="edit", on_click=self.edit_current).props("flat dense no-caps")
                    with ui.button(icon="more_horiz").props(f'flat dense round size=sm aria-label="{self.text("more")}"').tooltip(self.text("more")):
                        with ui.menu():
                            ui.menu_item(self.text("add_evidence"), on_click=partial(self.edit_evidence, kind, data["id"]))
                            ui.menu_item(self.text("history"), on_click=partial(self.show_history, kind, data["id"]))
                            ui.separator()
                            ui.menu_item(self.text("delete"), on_click=partial(self.preview_delete, kind, deepcopy(data))).classes("theme-danger")
            if kind == "object":
                ui.label(self.text(data["kind"]) + (" · " + " / ".join(data["aliases"]) if data.get("aliases") else "")).classes("text-sm theme-text-muted")
                if data["summary"]:
                    ui.label(data["summary"]).classes("text-base theme-text-secondary break-words")
                with ui.row().classes("gap-2"):
                    ui.button(self.text("create_relation"), on_click=partial(self.edit_edge, data["id"])).props("flat no-caps")
                    ui.button(self.text("view_graph"), on_click=self.view_graph).props("flat no-caps")
                with ui.expansion(self.text("relations"), icon="share").classes("w-full"):
                    if self.local_relations:
                        if self.local_relations["truncated"]:
                            ui.label(self.text("truncated"))
                        self.edge_table(self.local_relations)
            elif kind == "relation":
                with ui.row().classes("gap-2"):
                    ui.button(self.text("source_object"), on_click=partial(self.select, "object", data["source_id"])).props("flat no-caps")
                    ui.label("↔" if data.get("symmetric") else "→")
                    ui.button(self.text("target_object"), on_click=partial(self.select, "object", data["target_id"])).props("flat no-caps")
                for key in ("predicate", "description", "qualifier", "basis"):
                    value = self.text(data[key]) if key in ("basis", "predicate") else data[key]
                    ui.label(self.text(key) + " · " + value).classes("text-sm break-words theme-text-secondary")
            elif kind == "evidence":
                self.render_evidence(data)
            if "evidence" in self.detail:
                evidence = self.detail["evidence"]
                ui.label(self.text("evidence_count", count=evidence["total"])).classes("font-semibold theme-text")
                if not evidence["items"]:
                    ui.label(self.text("no_evidence")).classes("text-sm theme-text-muted")
                for item in evidence["items"]:
                    self.render_evidence(item)
                self.pager(evidence["offset"], evidence["limit"], evidence["total"],
                           partial(self.evidence_page, kind, data["id"], evidence["offset"]))

    def clear_error(self):
        self.error = None
        self.refresh_detail()

    async def view_graph(self):
        await self.set_mode("graph")
        await self.load_graph(center=True)

    # ---- 写入 ----
    def edit_current(self):
        if self.detail["kind"] == "object":
            self.edit_object(deepcopy(self.detail["record"]))
        else:
            self.edit_relation(deepcopy(self.detail["record"]))

    def edit_object(self, record=None):
        draft = {key: deepcopy(record.get(key)) for key in ("kind", "title", "summary", "aliases", "status")} if record else {
            "kind": "concept", "title": "", "summary": "", "aliases": [], "status": "active"}
        draft["aliases_text"] = "\n".join(draft.pop("aliases") or [])

        def payload():
            item = {k: v for k, v in draft.items() if k != "aliases_text"}
            item["aliases"] = [a.strip() for a in draft["aliases_text"].splitlines() if a.strip()]
            item.update({"id": record["id"], "expected_revision": record["revision"]} if record else {"ref": "new"})
            return {"objects": [item]}

        def fields():
            for name in ("kind", "title", "summary", "aliases_text", "status"):
                self.field(name, draft, multiline=name in ("summary", "aliases_text"),
                           options=KINDS if name == "kind" else STATUSES if name == "status" else None)
        self.write_dialog(self.text("edit" if record else "create"), fields, payload, "object", record)

    def edit_relation(self, record):
        draft = {key: record[key] for key in ("description", "qualifier", "basis", "status")}

        def fields():
            self.relation_fields(draft)

        def payload():
            return {"relations": [{"id": record["id"], "expected_revision": record["revision"], **draft}]}
        self.write_dialog(self.text("edit"), fields, payload, "relation", record)

    def relation_fields(self, draft):
        for name in ("description", "qualifier", "basis", "status"):
            self.field(name, draft, multiline=name in ("description", "qualifier"),
                       options=BASES if name == "basis" else STATUSES if name == "status" else None)

    def edit_edge(self, source_id):
        draft = {"target": None, "predicate": "related_to", "description": "", "qualifier": "", "basis": "inference", "status": "active"}

        def fields():
            search = ui.input(self.text("target_search")).props("outlined dense").classes("w-full")
            target = ui.select({}, label=self.text("target_object"), on_change=lambda e: draft.update(target=e.value)).props("outlined dense").classes("w-full")

            async def lookup():
                if not search.value or not search.value.strip():
                    return
                found = await self.call(service.search_objects, query=search.value, limit=50)
                if found:
                    target.set_options({item["id"]: item["title"] + " · " + self.text(item["kind"]) + " · " + item["summary"][:60] + " · " + item["id"][:8]
                                        for item in found["objects"] if item["id"] != source_id})
                    self.notify(self.text("target_limit"), type="info")
            ui.button(self.text("search"), on_click=lookup).props("flat")
            self.field("predicate", draft, options=PREDICATES)
            self.relation_fields(draft)

        def payload():
            if not draft["target"]:
                raise ValueError(self.text("choose_target"))
            item = {"source": {"id": source_id}, "target": {"id": draft["target"]}, "ref": "new"}
            item.update({k: v for k, v in draft.items() if k != "target"})
            return {"relations": [item]}
        self.write_dialog(self.text("create_relation"), fields, payload, "relation", None)

    def edit_evidence(self, kind, owner):
        draft = {"source_kind": "user", "stance": "supports", "source_title": "", "source_url": "", "quote": "",
                 "source_library_id": self.detail["library_id"], "source_file_id": "", "source_chunk_id": ""}

        def fields():
            self.field("source_kind", draft, options=("user", "external", "piece"))
            self.field("stance", draft, options=("supports", "contradicts", "context"))
            for name in ("source_title", "source_url", "source_library_id", "source_file_id", "source_chunk_id", "quote"):
                self.field(name, draft, multiline=name == "quote")
            ui.label(self.text("evidence_hint")).classes("text-xs theme-text-muted")

        def payload():
            item = {kind: {"id": owner}, **{k: draft[k] for k in ("source_kind", "stance", "quote")}}
            if draft["source_title"]:
                item["source_title"] = draft["source_title"]
            if draft["source_kind"] == "external":
                item["source_url"] = draft["source_url"]
            elif draft["source_kind"] == "piece":
                item.update(source_library_id=draft["source_library_id"], source_file_id=int(draft["source_file_id"]), source_chunk_id=int(draft["source_chunk_id"]))
            return {"evidence": [item]}
        self.write_dialog(self.text("add_evidence"), fields, payload, "evidence", None)

    # ---- 只读结构检查 ----
    def render_review(self):
        with ui.row().classes("items-center gap-2"):
            ui.label(self.text("review")).classes("text-sm font-medium theme-text")
            help_hint(self.text("review_intro"))
        with ui.row().classes("items-center gap-2"):
            ui.select(self.choices(("all", "selected", "filtered")), label=self.text("scope"), value=self.lint_scope,
                      on_change=lambda e: setattr(self, "lint_scope", e.value)).props("outlined dense")
            ui.button(self.text("rebuild_index"), on_click=self.rebuild_index).props("outline no-caps")
            help_hint(self.text("rebuild_intro"))
        with ui.row():
            ui.button(self.text("run_checks"), on_click=self.run_checks).props("outline no-caps")
            ui.button(self.text("disputed"), on_click=partial(self.show_status, "disputed")).props("flat no-caps")
            ui.button(self.text("outdated"), on_click=partial(self.show_status, "outdated")).props("flat no-caps")
        if self.checks:
            data = self.checks
            ui.label(self.text("checked_count", count=data["checked_objects"])).classes("text-xs theme-text-muted")
            if data["truncated"]:
                ui.label(self.text("truncated"))
            for issue in data["issues"]:
                with ui.row().classes("w-full items-center gap-2 py-2 knowledge-list-item"):
                    ui.label(self.text("lint_" + issue["code"])).classes("text-sm break-words")
                    for kind in ("object", "relation", "evidence"):
                        if issue.get(kind + "_id"):
                            ui.button(self.text("open_" + kind), on_click=partial(self.select, kind, issue[kind + "_id"])).props("flat dense no-caps")
                    if issue.get("target_id"):
                        ui.label(self.text("target_object") + " · " + issue["target_id"]).classes("text-xs break-all theme-text-muted")
                    for candidate in issue.get("candidates", []):
                        with ui.column().classes("w-full gap-1"):
                            ui.button(candidate["title"] + " · " + self.text(candidate["kind"]) + " · " + candidate["id"][:8],
                                      on_click=partial(self.select, "object", candidate["id"])).props("flat dense no-caps")
                            ui.label(candidate["summary"]).classes("text-xs break-words theme-text-muted")
            self.pager(data["offset"], data["limit"], data["total_objects"], self.check_page)

    async def show_status(self, status):
        self.status = status
        await self.search()
        await self.set_mode("objects")

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

    async def rebuild_index(self):
        """只重建查询索引；不改实体、关系、证据或历史，不调用模型。"""
        result = await self.call(service.rebuild_index)
        if result is None:
            return
        self.notify(self.text("rebuild_done"), type="positive")
        await self.search(False)

    async def file_references(self, file_id, offset=0):
        info = await self.call(service.list_objects, limit=1)
        if not info:
            return
        self.reference_file = file_id
        self.references_data = await self.call(service.references, source_library_id=info["library_id"], source_file_id=file_id, limit=25, offset=offset)
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
                ui.button(owner.get("title") or owner.get("description") or owner["id"],
                          on_click=partial(self.select, item["owner_kind"], owner["id"])).props("flat no-caps").classes("w-full break-words")
            async def page(delta):
                await self.file_references(self.reference_file, max(0, data["offset"] + delta * 25))
            self.pager(data["offset"], 25, data["total"], page)
            ui.button(self.text("close"), on_click=self.close_references).props("flat")

    def close_references(self):
        self.references_data = None
        self.refresh_workspace()
