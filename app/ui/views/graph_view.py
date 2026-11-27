"""知识图谱工作台：独立维护实体、语义关系与证据。

图谱节点没有 Wiki 正文，也不管理页面链接；图谱直接依据原始资料构建。
"""
from copy import deepcopy
from functools import partial
import inspect
import json
import logging
import math

from nicegui import run, ui

from app.ui.views.graph_presenter import (
    BASES, KINDS, PREDICATES, graph_options, merge_graph,
)
from app.ui.components import _open_dialog, help_hint
from app.ui.views.knowledge_common import STATUSES, WorkbenchBase, local_source
from indexing.services import knowledge_service as service
from indexing.services.errors import BusinessError

logger = logging.getLogger(__name__)


class _GraphServiceProxy:
    """给共享工作台的写入、冲突读回和历史请求补上当前图谱身份。"""

    _payload_methods = {"apply", "delete"}
    _scoped_methods = {"get_record", "history"}

    def __init__(self, workbench):
        self.workbench = workbench

    def __getattr__(self, name):
        function = getattr(service, name)
        if name == "request_result":
            def request_result(request_key, graph_id=None):
                graph_id = graph_id or self.workbench._request_graphs.get(request_key) or self.workbench.graph_id
                return function(request_key, graph_id=graph_id) if graph_id else function(request_key)
            return request_result
        if name not in self._payload_methods | self._scoped_methods:
            return function

        def scoped(*args, **kwargs):
            graph_id = kwargs.pop("graph_id", None) or self.workbench.graph_id
            if name in self._payload_methods:
                payload = dict(args[0] if args else kwargs.pop("data"))
                graph_id = payload.setdefault("graph_id", graph_id)
                request_key = payload.get("request_key")
                if request_key and graph_id:
                    self.workbench._request_graphs[request_key] = graph_id
                if args:
                    args = (payload, *args[1:])
                else:
                    kwargs["data"] = payload
            elif graph_id:
                kwargs["graph_id"] = graph_id
            return function(*args, **kwargs)
        return scoped


def finite_pair(value):
    """布局坐标与视口中心必须是有限的二元组。

    非有限值（NaN/Infinity）会让节点或整张图被渲染到画布之外，表现为「点了缩放
    图就不见了」。这类值只可能来自浏览器端的浮点运算，所以读回时就挡掉。
    """
    # bool 是 int 的子类，但不是坐标；显式排除，避免 True 被当成 1。
    return (isinstance(value, list) and len(value) == 2
            and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                    and math.isfinite(item) for item in value))


class GraphWorkbench(WorkbenchBase):
    service = service
    prefix = "graph."
    result_readback = (("objects", "object"), ("relations", "relation"), ("evidence", "evidence"))
    revision_kinds = ("object", "relation")
    hash_kinds = ()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._request_graphs = {}
        self.service = _GraphServiceProxy(self)
        self.graph_id = None
        self.graphs = []
        self._graphs_loaded = False
        self._graphs_loading = False
        self.graph_generation = 0
        self.query_input = None
        self.filter_controls = []
        self.import_upload = None
        self.mode = "objects"
        self.review_open = False
        self.kind = None
        self.status = None
        self.selected_object = None
        self.local_relations = None
        self.graph = None
        self.chart: ui.echart | None = None
        self.positions = {}
        self.viewport = {}
        self.depth = 1
        self.overview = False
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
        self.refresh_workspace = lambda: None
        self.refresh_edge_table = lambda: None
        self._graph_filters = None
        self._graph_generation = 0
        self._graph_queries = []
        self._space_picker: ui.select | None = None
        self.refresh_graph_stats = lambda: None
        self.layout = "auto"
        self.expanded_groups = set()
        self.structure_groups = []
        self.isolated_only = False
        self.inspector_open = False
        self.inspector = None
        self.inspection = None
        self.table_open = False
        self.edge_page = 0
        self._table_signature = None
        self.exploration_history = []
        self._overview_queries = []
        self._hierarchy_return_state = None
        self._projection = {}
        self._endpoint_names = {}
        self.refresh_graph_toolbar = lambda: None
        self._graph_busy = False
        self._graph_reads = 0
        self._space_switching = False
        self.canvas_size = (900, 650)
        self._canvas_window_size = None
        self._hierarchy_empty = None
        self._stats_signature = None

    async def remember_exploration(self):
        """会话内的探索历史；与实体修订历史分开，不写入业务数据。"""
        token = (self.graph_id, self._selection, self._graph_generation)
        await self.remember_positions()
        if token != (self.graph_id, self._selection, self._graph_generation):
            return False
        snapshot = {name: deepcopy(getattr(self, name)) for name in (
            "graph", "overview", "layout", "selected_object", "detail", "local_relations",
            "positions", "viewport", "canvas_size", "depth", "predicates", "relation_statuses", "isolated_only",
            "expanded_groups", "structure_groups", "_graph_queries", "_overview_queries", "_graph_filters",
            "_hierarchy_return_state",
            "inspection", "inspector_open", "_endpoint_names",
        )}
        if not self.exploration_history or snapshot != self.exploration_history[-1]:
            self.exploration_history.append(snapshot)
            self.exploration_history = self.exploration_history[-30:]
        return True

    async def begin_exploration(self):
        # 在首次 await 前记录操作次序，慢的位置读回不能覆盖后一次点击。
        self._selection += 1
        return await self.remember_exploration()

    async def back_exploration(self):
        if not self.exploration_history:
            return
        self._selection += 1
        self._graph_generation += 1
        for name, value in self.exploration_history.pop().items():
            setattr(self, name, value)
        self.error = None
        self.refresh_workspace()
        self.highlight_selection()

    def graph_chart_options(self):
        selected_edge = self.detail["record"]["id"] if self.detail and self.detail["kind"] == "relation" else None
        options = graph_options(self.graph, dark=bool(self.dark()), selected=self.selected_object,
                                selected_edge=selected_edge, positions=self.positions, label=self.text,
                                layout=self.layout, expanded_groups=self.expanded_groups, grouping=self.structure_groups,
                                zoom=self.viewport.get("zoom", 1.0), canvas_size=self.canvas_size)
        self._projection = options.pop("piece_meta", {})
        self.structure_groups = self._projection.get("groups", [])
        if self.layout != "sphere3d":
            options["series"][0].update({"zoom": options["series"][0].get("zoom", 1), "center": None, **self.viewport})
            self.positions.update({n["id"]: (n["x"], n["y"]) for n in options["series"][0]["data"]})
        return options

    def update_chart(self):
        """更新同一张画布，保留用户正在看的位置和焦点。"""
        if not self.graph or not self.chart or self.chart.is_deleted:
            self.refresh_graph()
            return
        options = self.graph_chart_options()
        previous_series = self.chart.options.get("series", [{}])[0]
        next_series = options["series"][0]
        presentation = ("type", "pieceHierarchy", "pieceTopology")
        if tuple(previous_series.get(key) for key in presentation) != tuple(next_series.get(key) for key in presentation):
            # 展示模式改变时清除 ECharts 合并保留的旧标签/形状设置；点选与刷新仍复用画布。
            self.refresh_graph()
            return
        if self._hierarchy_empty is not None and not self._hierarchy_empty.is_deleted:
            self._hierarchy_empty.set_visibility(self.layout == "hierarchy" and not self._projection.get("visible_nodes"))
        self.chart.run_chart_method("setOption", {"series": options["series"], "legend": options.get("legend", {})}, {"replaceMerge": ["series"]})
        with self.chart._props.suspend_updates():
            self.chart.options["series"] = options["series"]
            self.chart.options["legend"] = options.get("legend", {})
        signature = (self.overview, len(self.graph["nodes"]), len(self.graph["edges"]), self.graph.get("total_nodes"),
                     self.graph.get("total_edges"), self.graph.get("truncated"), self.graph.get("next_offset"),
                     self.graph.get("next_edge_offset"), self.graph.get("next_neighbor_offset"), self.graph.get("depth"),
                     len(self._projection.get("groups", [])), bool(self._projection.get("cycles")),
                     self._projection.get("excluded_count"), self._projection.get("layout_warning"),
                     self._projection.get("visible_nodes"), self._projection.get("visible_edges"))
        if signature != self._stats_signature:
            self._stats_signature = signature
            self.refresh_graph_stats()
        table_signature = tuple((e["id"], e.get("revision"), e.get("evidence_count")) for e in self.graph["edges"])
        if self.table_open and table_signature != self._table_signature:
            self._table_signature = table_signature
            self.refresh_edge_table()

    def close_inspector(self):
        self.inspector_open = False
        if self.inspector is not None and not self.inspector.is_deleted:
            self.inspector.set_visibility(False)

    def show_inspector(self):
        self.inspector_open = True
        if self.inspector is not None and not self.inspector.is_deleted:
            self.inspector.set_visibility(True)
        self.refresh_detail()

    def escape_graph(self):
        if self.inspector_open:
            self.close_inspector()
        else:
            self.detail = self.inspection = None
            self.selected_object = None
            if self.chart and not self.chart.is_deleted:
                self.chart.run_chart_method("dispatchAction", {"type": "downplay", "seriesIndex": 0})

    @staticmethod
    def combine_overview(previous, incoming):
        """分页结果按真实 ID 合并；分页游标使用最新响应，旧关系不丢失。"""
        combined = dict(incoming)
        for key in ("nodes", "edges"):
            records = {item["id"]: item for item in (previous or {}).get(key, [])}
            records.update({item["id"]: item for item in incoming[key]})
            combined[key] = list(records.values())
        if incoming.get("load_nodes") is False and previous:
            combined["next_offset"] = previous.get("next_offset")
        ids = {node["id"] for node in combined["nodes"]}
        combined["edges"] = [edge for edge in combined["edges"] if edge["source_id"] in ids and edge["target_id"] in ids]
        expected_edges = 0 if incoming.get("isolated_only") else combined["total_edges"]
        combined["truncated"] = (len(combined["nodes"]) < combined.get("filtered_nodes", combined["total_nodes"])
                                 or len(combined["edges"]) < expected_edges)
        return combined

    def _scoped_call(self, function, *args, graph_id=None, **kwargs):
        """把当前（或调用方捕获的）图谱身份传到支持作用域的服务请求。"""
        if graph_id is None:
            graph_id = self.graph_id
        if graph_id is not None:
            try:
                parameters = inspect.signature(function).parameters.values()
                accepts_graph_id = any(parameter.name == "graph_id" or
                                       parameter.kind == inspect.Parameter.VAR_KEYWORD
                                       for parameter in parameters)
            except (TypeError, ValueError):
                accepts_graph_id = False
            if accepts_graph_id:
                kwargs["graph_id"] = graph_id
        return self.call(function, *args, **kwargs)

    async def read_graph(self, function, **kwargs):
        self._graph_reads += 1
        self._graph_busy = True
        try:
            return await self._scoped_call(function, **kwargs)
        finally:
            self._graph_reads -= 1
            self._graph_busy = bool(self._graph_reads)

    async def load_graphs(self):
        if self._graphs_loaded or self._graphs_loading:
            return
        self._graphs_loading = True
        try:
            data = await self.call(service.list_graphs)
            if not data:
                return
            self._graphs_loaded = True
            await self.adopt_graphs(data["graphs"], select_default=True)
        finally:
            self._graphs_loading = False
            if self._space_picker is not None and not self._space_picker.is_deleted:
                self._space_picker.set_enabled(self._graphs_loaded)

    async def adopt_graphs(self, graphs, *, select_default=False):
        changed = graphs != self.graphs
        self.graphs = graphs
        if not any(item["id"] == self.graph_id for item in graphs) and (self.graph_id is not None or select_default):
            fallback = next((item for item in graphs if item["is_default"]), graphs[0] if graphs else None)
            # 先切换再重建选择器，避免 set_options 把已删除的值置空后触发迟到的切换事件。
            await self.select_graph(fallback["id"] if fallback else None)
            return
        if changed:
            if self._space_picker is not None and not self._space_picker.is_deleted:
                self._space_picker.set_options({item["id"]: item["name"] for item in graphs})
            else:
                self.refresh_modes()

    def create_graph_dialog(self):
        name = {"value": ""}
        with ui.dialog() as dialog, ui.card().classes("w-96 max-w-full"):
            ui.label(self.text("create_graph"))
            ui.input(self.text("graph_name"), on_change=lambda event: name.update(value=event.value))\
                .props("outlined dense autofocus")
            with ui.row().classes("w-full justify-end"):
                ui.button(self.text("cancel"), on_click=dialog.close).props("flat")

                async def create():
                    value = name["value"].strip()
                    if not value:
                        return
                    graph = await self.call(service.create_graph, value)
                    if graph:
                        dialog.close()
                        self.graphs.append(graph)
                        self.refresh_modes()
                        await self.select_graph(graph["id"])

                ui.button(self.text("create"), on_click=create).props("unelevated")
        _open_dialog(dialog)

    def rename_graph_dialog(self):
        if not self.graph_id:
            return
        current_id = self.graph_id
        current = next((item for item in self.graphs if item["id"] == current_id), None)
        if current is None:
            return
        name = {"value": current["name"]}
        with ui.dialog() as dialog, ui.card().classes("w-96 max-w-full"):
            ui.label(self.text("rename_graph"))
            ui.input(self.text("graph_name"), value=name["value"],
                     on_change=lambda event: name.update(value=event.value))\
                .props("outlined dense autofocus")
            with ui.row().classes("w-full justify-end"):
                ui.button(self.text("cancel"), on_click=dialog.close).props("flat")

                async def rename():
                    value = name["value"].strip()
                    if not value:
                        return
                    graph = await self.call(service.rename_graph, current_id, value)
                    if graph:
                        current.update(graph)
                        dialog.close()
                        self.refresh_modes()

                ui.button(self.text("save"), on_click=rename).props("unelevated")
        _open_dialog(dialog)

    def delete_graph_dialog(self):
        graph_id = self.graph_id
        current = next((item for item in self.graphs if item["id"] == graph_id), None)
        if not graph_id or current is None or current.get("is_default"):
            return
        with ui.dialog().props("persistent") as dialog, ui.card().classes("w-[520px] max-w-full theme-card"):
            ui.label(self.text("delete_graph_title")).classes("text-xl font-semibold")
            ui.label(self.text("delete_graph_warning", name=current["name"])).classes("break-words theme-text")
            preview = ui.label().classes("text-sm whitespace-pre-wrap theme-text-muted")
            message = ui.label().classes("text-sm theme-danger")
            with ui.row().classes("w-full justify-end"):
                ui.button(self.text("cancel"), on_click=dialog.close).props("flat")
                confirm = ui.button(self.text("confirm_delete_graph"), on_click=lambda: confirm_delete())
                confirm.props("unelevated color=negative no-caps")
                confirm.disable()

            state = {"preview": None, "busy": False}

            async def load_preview():
                result = await self.call(service.delete_graph, graph_id)
                if not result or graph_id != self.graph_id:
                    return
                state["preview"] = result
                counts = result["counts"]
                preview.set_text(self.text("delete_graph_counts", entities=counts["entities"],
                                           relations=counts["relations"], evidence=counts["evidence"],
                                           history=counts["history"], requests=counts["requests"]))
                confirm.set_enabled(True)

            async def confirm_delete():
                result = state["preview"]
                if not result or state["busy"] or graph_id != self.graph_id:
                    return
                state["busy"] = True
                confirm.disable()
                try:
                    deleted = await self.call(service.delete_graph, graph_id, confirmed=True,
                                              impact_token=result["impact_token"])
                    if graph_id != self.graph_id:
                        return
                    if not deleted:
                        state["preview"] = None
                        message.set_text(self.text("delete_graph_retry"))
                        return
                    if not deleted.get("committed"):
                        message.set_text(self.text("delete_graph_retry"))
                        return
                    dialog.close()
                    self.graphs = [item for item in self.graphs if item["id"] != graph_id]
                    self._graphs_loaded = True
                    fallback = next((item for item in self.graphs if item.get("is_default")),
                                    self.graphs[0] if self.graphs else None)
                    self.graph_id = None
                    self.refresh_modes()
                    if fallback:
                        await self.select_graph(fallback["id"])
                except BusinessError as exc:
                    message.set_text(self.text("delete_graph_conflict", code=exc.code))
                finally:
                    state["busy"] = False
                    if state["preview"] and not dialog.is_deleted:
                        confirm.set_enabled(True)

            ui.timer(0.1, load_preview, once=True)
        _open_dialog(dialog)

    async def select_graph(self, graph_id):
        if graph_id == self.graph_id:
            return
        self._space_switching = True
        self.graph_generation += 1
        generation = self.graph_generation
        self.graph_id = graph_id
        self._selection += 1
        self._search_generation += 1
        self._graph_generation += 1
        self.offset = 0
        self.query = ""
        self.kind = self.status = None
        self.depth = 1
        self.mode = "objects"
        self.review_open = False
        self.selected_object = None
        self.local_relations = None
        self.detail = None
        self.error = None
        self.graph = None
        self.chart = None
        self.positions = {}
        self.viewport = {}
        self.overview = False
        self.edge_types = ["relation"]
        self.predicates = self.relation_statuses = None
        self._graph_filters = None
        self._graph_queries = []
        self._references_params = None
        self._overview_queries = []
        self._hierarchy_return_state = None
        self.exploration_history = []
        self.expanded_groups.clear()
        self.structure_groups = []
        self.inspection = None
        self.inspector_open = False
        self.isolated_only = False
        self.layout = "auto"
        self._checks_generation += 1
        self.checks = None
        self.lint_scope = "all"
        self.lint_offset = 0
        self.references_data = None
        self.reference_file = None
        self._search_params = None
        self._applied_query = None
        self.results = {"library_id": "", "graph_id": graph_id, "objects": [], "total": 0, "limit": 25, "offset": 0}
        if self.query_input is not None and not self.query_input.is_deleted:
            self.query_input.set_value("")
        for control in self.filter_controls:
            if not control.is_deleted:
                control.set_value(None)
        self.refresh_modes()
        self.refresh_list()
        await self.search()
        if generation != self.graph_generation or graph_id != self.graph_id:
            return
        self.mode = "graph"
        self.overview = True
        await self.load_overview(center=True)
        if generation != self.graph_generation or graph_id != self.graph_id:
            return
        self.refresh_modes()
        self.refresh_workspace()
        self._space_switching = False

    async def export_graph(self, format):
        graph_id = self.graph_id
        if not graph_id:
            return
        result = await self._scoped_call(service.export_graph, format=format, graph_id=graph_id)
        if result and graph_id == self.graph_id:
            extension = "json" if format == "json" else "graphml"
            media_type = "application/json" if format == "json" else "application/graphml+xml"
            ui.download.content(result["content"], f"piece-graph-{graph_id}.{extension}", media_type)

    def import_graph_dialog(self):
        if not self.graph_id:
            return
        state = {"document": None, "format": None, "preview": None, "busy": False}
        graph_id = self.graph_id
        with ui.dialog().props("persistent") as dialog, ui.card().classes("w-[700px] max-w-full max-h-[90vh] overflow-auto theme-card"):
            ui.label(self.text("import_graph")).classes("text-lg font-semibold theme-text")
            ui.label(self.text("import_graph_hint")).classes("text-sm theme-text-muted")
            upload = ui.upload(on_upload=lambda event: self.preview_graph_import(event, state, graph_id),
                               on_begin_upload=lambda: self.clear_graph_import_preview(state),
                               on_rejected=lambda: self.clear_graph_import_preview(state),
                               auto_upload=True, max_files=1)\
                .props("accept=.json,.graphml,application/json,application/graphml+xml").classes("max-w-full")
            for event in ("added", "removed"):
                upload.on(event, lambda: self.clear_graph_import_preview(state), args=[])
            self.import_upload = upload
            preview = ui.column().classes("w-full gap-2")
            message = ui.label().classes("text-sm whitespace-pre-wrap theme-text-muted")
            with ui.row().classes("w-full justify-end"):
                ui.button(self.text("cancel"), on_click=dialog.close).props("flat")
                confirm = ui.button(self.text("confirm_import"), on_click=lambda: self.confirm_graph_import(
                    state, graph_id, dialog, message)).props("unelevated no-caps")
                confirm.disable()
            state.update(dialog=dialog, preview=preview, confirm=confirm, message=message)
        _open_dialog(dialog)

    def clear_graph_import_preview(self, state):
        if state["busy"]:
            return
        state.update(document=None, format=None, preview_data=None, generation=state.get("generation", 0) + 1)
        state["confirm"].disable()
        state["preview"].clear()
        state["message"].set_text("")

    async def preview_graph_import(self, event, state, graph_id):
        if graph_id != self.graph_id or state["busy"]:
            return
        self.clear_graph_import_preview(state)
        generation = state["generation"]
        try:
            document = await event.file.text()
        except (UnicodeDecodeError, ValueError):
            if generation == state["generation"]:
                state["message"].set_text(self.text("import_invalid"))
            return
        if graph_id != self.graph_id or generation != state["generation"]:
            return
        suffix = event.file.name.rsplit(".", 1)[-1].lower()
        format = "graphml" if suffix == "graphml" else "json"
        if suffix not in ("json", "graphml"):
            state["message"].set_text(self.text("import_format_invalid"))
            return
        state.update(document=document, format=format)
        data = await self.call(service.import_graph, {
            "format": format, "document": document, "target_graph_id": graph_id,
            "dry_run": True, "confirmed": False,
        })
        if graph_id != self.graph_id or generation != state["generation"]:
            return
        if not data:
            return
        state["preview_data"] = data
        with state["preview"]:
            if data.get("conflicts"):
                ui.label(self.text("import_conflicts", count=len(data["conflicts"]))).classes("theme-text")
                ui.label(json.dumps(data["conflicts"], ensure_ascii=False, indent=2)).classes(
                    "text-xs whitespace-pre-wrap break-words theme-text-muted")
            else:
                counts = data.get("counts", {})
                ui.label(self.text("import_preview_counts", entities=counts.get("entities", 0),
                                   relations=counts.get("relations", 0), evidence=counts.get("evidence", 0)))
                ui.label(self.text("import_target", name=data.get("graph_name", ""))).classes("text-sm theme-text-muted")
        state["confirm"].set_enabled(not data.get("conflicts"))

    async def confirm_graph_import(self, state, graph_id, dialog, message):
        preview = state.get("preview_data")
        if not preview or preview.get("conflicts") or state["busy"] or graph_id != self.graph_id:
            return
        state["busy"] = True
        state["confirm"].disable()
        try:
            result = await self.call(service.import_graph, {
                "format": state["format"], "document": state["document"],
                "target_graph_id": graph_id, "dry_run": False, "confirmed": True,
            })
            if graph_id != self.graph_id:
                return
            if not result:
                return
            if result.get("conflicts"):
                message.set_text(self.text("import_conflicts", count=len(result["conflicts"])))
                state["confirm"].set_enabled(True)
                return
            dialog.close()
            counts = result.get("counts", {})
            self.notify(self.text("import_done", entities=counts.get("entities", 0),
                                  relations=counts.get("relations", 0), evidence=counts.get("evidence", 0)),
                        type="positive")
            self.graph = None
            self.detail = None
            self.selected_object = None
            self._selection += 1
            self._graph_generation += 1
            await self.search()
            self.refresh_workspace()
        finally:
            state["busy"] = False

    def _list_function(self, params):
        return service.search_objects if "query" in params else service.list_objects

    def search_params(self):
        params = {"kind": self.kind, "status": self.status, "limit": 25, "offset": self.offset}
        if self.graph_id:
            params["graph_id"] = self.graph_id
        if self.query.strip():
            params["query"] = self.query.strip()
        return params

    async def search(self, reset=True):
        self._search_generation += 1
        generation = self._search_generation
        if reset:
            self.offset = 0
        self._search_params = params = self.search_params()
        graph_id = self.graph_id
        function = self._list_function(params)
        self._searching = True
        try:
            results = await self.call(function, **params)
            if generation == self._search_generation and graph_id == self.graph_id:
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
        timer, generation, graph_id = self._poll_timer, self._search_generation, self.graph_id

        def current():
            return (timer is self._poll_timer and (timer is None or not timer.is_deleted)
                    and generation == self._search_generation and graph_id == self.graph_id)

        if not current():
            return
        self._polling = True
        selection, detail = self._selection, self.detail
        try:
            previous_graphs = deepcopy(self.graphs)
            spaces = await run.io_bound(service.list_graphs)
            if not current():
                return
            if previous_graphs == self.graphs:
                await self.adopt_graphs(spaces["graphs"])
            if not current():
                return
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

            if detail and current() and selection == self._selection and self.detail is detail:
                kind, ident = detail["kind"], detail["record"]["id"]
                evidence = detail.get("evidence", {})
                try:
                    incoming = await run.io_bound(service.get_record, kind=kind, id=ident, graph_id=graph_id,
                                                 limit=evidence.get("limit", 25), offset=evidence.get("offset", 0))
                    local = await run.io_bound(service.graph, root_id=ident, depth=1, edge_types=["relation"],
                                               graph_id=graph_id) if kind == "object" else None
                except BusinessError as exc:
                    if exc.code != "NOT_FOUND":
                        raise
                    incoming, local = None, None
                if not current():
                    return
                if selection == self._selection and self.detail is detail:
                    if incoming != detail or local != self.local_relations:
                        self.detail, self.local_relations = incoming, local
                        if incoming is None:
                            self.error = self.text("broken_link")
                            if kind == "object":
                                self.selected_object = None
                        await self._refresh_preserving_scroll(self.refresh_detail, "detail")
            await self.poll_graph(current)
            await self.poll_auxiliary(current, checks_visible=self.mode == "objects" and self.review_open)
        except Exception as exc:
            # 自动检查失败保留当前画面，下次重试；不每两秒弹一次错误。
            logger.debug("图谱自动更新暂未完成 (%s)", type(exc).__name__)
        finally:
            self._polling = False

    async def poll_graph(self, current):
        if not current() or self.mode != "graph" or (self.graph is None and not self.overview):
            return
        previous, generation, overview = self.graph, self._graph_generation, self.overview

        def unchanged():
            return (current() and self.mode == "graph" and overview == self.overview
                    and generation == self._graph_generation and previous is self.graph)

        if overview:
            incoming = None
            for params in deepcopy(self._overview_queries or [{"predicates": self.predicates,
                         "statuses": self.relation_statuses, "isolated_only": self.isolated_only}]):
                # 只在页面仍停留于同一探索状态时应用后台更新。
                if not unchanged():
                    return
                if "known_node_ids" in params:
                    params["known_node_ids"] = [node["id"] for node in (incoming or {}).get("nodes", [])]
                part = await run.io_bound(service.graph_overview, graph_id=self.graph_id, **params)
                incoming = self.combine_overview(incoming, part)
        else:
            # 重读每次探索的范围后重新合并，既保留展开范围，也去掉已被外部删除的记录。
            queries = self._graph_queries or [{"root_id": previous["root_id"], "depth": self.depth,
                        "edge_types": self.edge_types, "predicates": self.predicates,
                        "statuses": self.relation_statuses, "max_nodes": 100, "max_edges": 300}]
            incoming = None
            for index, params in enumerate(deepcopy(queries)):
                try:
                    part = await run.io_bound(service.graph, **params, graph_id=self.graph_id)
                except BusinessError as exc:
                    if exc.code != "NOT_FOUND":
                        raise
                    if index == 0:
                        break
                    continue
                if not unchanged():
                    return
                if incoming:
                    incoming["max_nodes"] = previous["max_nodes"]
                    incoming["max_edges"] = previous["max_edges"]
                    incoming = merge_graph(incoming, part)
                else:
                    incoming = part
        if not unchanged() or incoming == previous:
            return
        await self.remember_positions()
        if not unchanged():
            return
        self.graph = incoming
        if incoming is None or not incoming["nodes"] or self.chart is None or self.chart.is_deleted:
            self.refresh_graph()
            return
        self.update_chart()

    async def select(self, kind, ident, *, offset=0, reveal=False):
        """点选只检查详情；改变探索中心必须由明确操作触发。"""
        if self.mode == "graph" and (not self.detail or self.detail["record"]["id"] != ident):
            if not await self.begin_exploration():
                return
        self._selection += 1
        generation = self._selection
        graph_id = self.graph_id
        data = await self._scoped_call(service.get_record, kind=kind, id=ident,
                                       limit=25, offset=offset, graph_id=graph_id)
        if generation != self._selection or graph_id != self.graph_id:
            return
        if data is None:
            self.error = self.text("broken_link")
            self.refresh_detail()
            return
        self.error = None
        self.detail = data
        self.inspection = None
        if kind != "object":
            self.local_relations = None
        self.highlight_selection()
        if kind == "object":
            self.selected_object = ident
            local_relations = await self._scoped_call(service.graph, root_id=ident, depth=1,
                                                      edge_types=["relation"], graph_id=graph_id)
            if generation != self._selection or graph_id != self.graph_id:
                return
            self.local_relations = local_relations
        elif kind == "relation":
            names = {node["id"]: node["title"] for node in (self.graph or {}).get("nodes", [])}
            for endpoint in ("source_id", "target_id"):
                endpoint_id = data["record"][endpoint]
                if endpoint_id not in names:
                    record = await self._scoped_call(service.get_record, kind="object", id=endpoint_id, graph_id=graph_id)
                    if generation != self._selection or graph_id != self.graph_id:
                        return
                    if record:
                        names[endpoint_id] = record["record"]["title"]
            self._endpoint_names = names
        if self.mode == "graph":
            await self.remember_positions()
            group = self._projection.get("node_to_group", {}).get(ident) if kind == "object" else None
            if group:
                self.expanded_groups.add(group)
            self.update_chart()
            self.show_inspector()
        else:
            self.refresh_detail()
        self.refresh_edge_table()
        if self.chart and not self.chart.is_deleted and self.graph and self.layout != "sphere3d":
            series = self.chart.options["series"][0]
            ids = [n["id"] for n in series.get("data", [])]
            self.chart.run_chart_method("dispatchAction", {"type": "downplay", "seriesIndex": 0})
            if kind == "object" and ident in ids:
                self.chart.run_chart_method("dispatchAction", {"type": "highlight", "seriesIndex": 0, "dataIndex": ids.index(ident)})
            elif kind == "relation":
                edges = [edge["id"] for edge in series.get("links", series.get("edges", []))]
                if ident in edges:
                    self.chart.run_chart_method("dispatchAction", {"type": "highlight", "seriesIndex": 0,
                                                                  "dataType": "edge", "dataIndex": edges.index(ident)})
        return data

    def render_middle(self):
        def create_object():
            if self.graph_id:
                self.edit_object()

        self.render_catalog(KINDS, "objects", "object", create_object)
        self.query_input = self._catalog_search
        self.filter_controls = self._catalog_filters

    def render_right(self, controls=None):
        if not self._graphs_loaded and not self._graphs_loading:
            ui.timer(0.1, self.load_graphs, once=True)
        with ui.column().classes("graph-workbench w-full h-full flex-1 min-h-0 min-w-0 theme-content gap-0") as workbench:
            workbench.on("graph-escape", self.escape_graph, args=[])
            workbench.on("graph-back", self.back_exploration, args=[])
            @ui.refreshable
            def modes():
                with ui.row().classes("graph-header w-full items-center library-heading workspace-toolbar"):
                    if controls:
                        controls()
                    async def choose_graph(event):
                        if event.sender is not self._space_picker or self._graphs_loading:
                            return
                        if any(item["id"] == event.value for item in self.graphs):
                            await self.select_graph(event.value)

                    self._space_picker = ui.select({item["id"]: item["name"] for item in self.graphs}, value=self.graph_id,
                                                   on_change=choose_graph)\
                        .props(f'dense outlined options-dense aria-label="{self.text("graph_space")}"')\
                        .classes("graph-space-picker").tooltip(self.text("graph_space"))
                    self._space_picker.set_enabled(self._graphs_loaded and not self._graphs_loading)
                    with ui.button(icon="more_horiz", color=None).props(
                            f'flat dense aria-label="{self.text("space_actions")}"').classes("graph-space-actions").tooltip(self.text("space_actions")):
                        with ui.menu():
                            ui.menu_item(self.text("create_graph"), on_click=self.create_graph_dialog)
                            ui.menu_item(self.text("rename_graph"), on_click=self.rename_graph_dialog).set_enabled(bool(self.graph_id))
                            if self.graph_id:
                                ui.separator()
                                ui.menu_item(self.text("export_graph_json"), on_click=partial(self.export_graph, "json"))
                                ui.menu_item(self.text("export_graphml"), on_click=partial(self.export_graph, "graphml"))
                                ui.menu_item(self.text("import_graph"), on_click=self.import_graph_dialog)
                            current_graph = next((item for item in self.graphs if item["id"] == self.graph_id), None)
                            if current_graph and not current_graph.get("is_default"):
                                ui.separator()
                                ui.menu_item(self.text("delete_graph"), on_click=self.delete_graph_dialog).classes("theme-danger")
                    self.query_input = self.query_input if self.query_input and not self.query_input.is_deleted else None
                    self.filter_controls = [item for item in self.filter_controls if not item.is_deleted]
                    with ui.row().classes("graph-mode-switch items-center flex-nowrap").props(
                            f'role=group aria-label="{self.text("view_mode")}"'):
                        for mode, icon in (("objects", "account_tree"), ("graph", "hub")):
                            ui.button(self.text(mode), icon=icon, color=None,
                                      on_click=partial(self.set_mode, mode)).props(
                                f'flat dense no-caps no-ripple aria-pressed={str(self.mode == mode).lower()}')\
                                .classes("mode-active" if self.mode == mode else "theme-text-muted")
            self.refresh_modes = modes.refresh
            modes()
            @ui.refreshable
            def workspace():
                self.chart = None
                if self.mode == "graph":
                    with ui.row().classes("graph-exploration w-full flex-1 min-h-0 min-w-0 gap-0 flex-nowrap"):
                        with ui.column().classes("graph-map flex-1 h-full min-w-0 min-h-0 gap-0"):
                            self.render_graph()
                        with ui.column().classes("graph-inspector h-full gap-0").props(
                                f'role=complementary aria-label="{self.text("inspector")}"') as self.inspector:
                            self.inspector.set_visibility(self.inspector_open)
                            with ui.row().classes("workspace-toolbar w-full items-center"):
                                ui.label(self.text("inspector")).classes("text-sm theme-text-secondary")
                                ui.space()
                                ui.button(icon="close", on_click=self.close_inspector, color=None).props(
                                    f'flat dense round aria-label="{self.text("close")}"').tooltip(self.text("close"))
                            with ui.scroll_area(on_scroll=lambda e: self._scroll_positions.update(detail=e.vertical_position)).classes(
                                    "w-full flex-1 min-h-0 scroll-flush") as area:
                                self._scroll_areas["detail"] = area
                                with ui.column().classes("graph-inspector-content w-full min-w-0"):
                                    @ui.refreshable
                                    def inspector_detail():
                                        self.render_inspection()
                                    self.refresh_detail = inspector_detail.refresh
                                    inspector_detail()
                                if self._scroll_positions.get("detail"):
                                    area.scroll_to(pixels=self._scroll_positions["detail"])
                    if self.references_data is not None:
                        with ui.column().classes("graph-references w-full max-h-[220px] overflow-y-auto px-3 py-2 gap-1"):
                            self.render_references()
                    return
                with ui.scroll_area(on_scroll=lambda e: self._scroll_positions.update(detail=e.vertical_position)).classes("w-full flex-1 scroll-flush") as area:
                    self._scroll_areas["detail"] = area
                    with ui.column().classes("graph-paper w-full min-w-0 mx-auto"):
                        if self.references_data is not None:
                            self.render_references()

                        @ui.refreshable
                        def detail():
                            self.render_detail()
                        self.refresh_detail = detail.refresh
                        detail()
                        with ui.expansion(self.text("review"), icon="fact_check", value=self.review_open,
                                          on_value_change=lambda e: setattr(self, "review_open", e.value)).classes("graph-review w-full"):
                            self.render_review()
            self.refresh_workspace = workspace.refresh
            workspace()

    async def set_mode(self, mode, *, center=False):
        await self.remember_positions()
        self.mode = "objects" if mode == "review" else mode
        if mode == "review":
            self.review_open = True
        self.refresh_modes()
        # 进入图谱标签时，让图与当前选中实体一致（尚无图或根不同都算），
        # 否则切过来只看到「先选一个实体」，还要手动点一次刷新范围。
        if mode == "graph":
            if self.overview:
                if center or self.graph is None or not self.graph.get("overview"):
                    await self.load_overview(center=True)
            elif self.selected_object and (center or self.graph is None):
                await self.load_graph(center=True)
            elif self.graph is None:
                self.overview = True
                await self.load_overview(center=True)
        self.refresh_workspace()

    # ---- 局部图探索 ----
    async def remember_positions(self):
        if not self.chart or self.chart.is_deleted or self.layout == "sphere3d":
            return
        chart, graph_id, generation, layout = self.chart, self.graph_id, self._graph_generation, self.layout
        try:
            # 唯一插值为 NiceGUI 分配的整数元素 ID，绝不插入模型文本。
            view = await ui.run_javascript(f"""(() => {{
                const chart = getElement({self.chart.id})?.chart;
                if (!chart || chart.isDisposed()) return null;
                const data = chart.getModel().getSeriesByIndex(0).getData();
                const points = {{}};
                for (let i = 0; i < data.count(); i++) points[data.getId(i)] = data.getItemLayout(i);
                const series = chart.getOption().series[0];
                return {{points, zoom: series.zoom, center: series.center}};
            }})()""")
            if (self.chart is not chart or graph_id != self.graph_id or generation != self._graph_generation
                    or layout != self.layout):
                return
            if isinstance(view, dict):
                self.positions.update({key: tuple(value) for key, value in view.get("points", {}).items()
                                       if finite_pair(value)})
                center, zoom = view.get("center"), view.get("zoom")
                self.viewport = {}
                if finite_pair(center):
                    self.viewport["center"] = center
                if isinstance(zoom, (int, float)) and math.isfinite(zoom):
                    self.viewport["zoom"] = zoom
        except (TimeoutError, RuntimeError):
            # 断线时保留上次位置；布局从不触发业务写入。
            pass

    async def load_graph(self, *, center=False, expand=False, predicate=None, neighbor_offset=0, expand_root=None):
        if expand and self.graph and (len(self.graph["nodes"]) >= 2000 or len(self.graph["edges"]) >= 6000):
            self.notify(self.text("canvas_capacity"), type="info")
            return
        self._graph_generation += 1
        generation = self._graph_generation
        graph_id = self.graph_id
        await self.remember_positions()
        if graph_id != self.graph_id or generation != self._graph_generation:
            return
        root = self.selected_object if center or self.graph is None or self.graph.get("overview") else self.graph["root_id"]
        if not root:
            self.notify(self.text("choose_root"), type="info")
            return
        self.overview = False
        if self.layout == "sphere3d":
            self.layout = "auto"
        filters = (tuple(self.edge_types), tuple(self.predicates or ()), tuple(self.relation_statuses or ()))
        params = {"root_id": (expand_root or self.selected_object) if expand else root, "depth": 1 if expand else self.depth,
                  "edge_types": self.edge_types, "predicates": [predicate] if predicate else self.predicates, "statuses": self.relation_statuses,
                  "max_nodes": 100, "max_edges": 300}
        if neighbor_offset:
            params["neighbor_offset"] = neighbor_offset
        incoming = await self.read_graph(service.graph, **params, graph_id=graph_id)
        if incoming and generation == self._graph_generation and graph_id == self.graph_id:
            if expand and self.graph and filters == self._graph_filters:
                self.graph["max_nodes"] = min(2000, len(self.graph["nodes"]) + incoming["max_nodes"])
                self.graph["max_edges"] = min(6000, len(self.graph["edges"]) + incoming["max_edges"])
                self.graph = merge_graph(self.graph, incoming)
                if params not in self._graph_queries:
                    self._graph_queries.append(deepcopy(params))
            else:
                self.graph = incoming
                self._graph_queries = [deepcopy(params)]
            self._graph_filters = filters
            if center:
                self.positions = {}
                self.viewport = {}
            self.update_chart()

    async def load_overview(self, *, center=False, more=False, more_edges=False):
        self._graph_generation += 1
        generation = self._graph_generation
        graph_id = self.graph_id
        await self.remember_positions()
        if graph_id != self.graph_id or generation != self._graph_generation:
            return
        params = {"predicates": self.predicates, "statuses": self.relation_statuses,
                  "isolated_only": self.isolated_only}
        if (more or more_edges) and self.graph:
            params["known_node_ids"] = [node["id"] for node in self.graph["nodes"]]
            if not more_edges:
                params["max_nodes"] = min(400, max(1, 10000 - len(params["known_node_ids"])))
            if more_edges:
                params.update(load_nodes=False, edge_offset=self.graph.get("next_edge_offset") or 0)
            else:
                params["offset"] = self.graph.get("next_offset") or 0
        incoming = await self.read_graph(service.graph_overview, **params, graph_id=graph_id)
        if incoming and generation == self._graph_generation and graph_id == self.graph_id:
            self.overview = True
            if more or more_edges:
                if more_edges and self.graph:
                    incoming["next_offset"] = self.graph.get("next_offset")
                self.graph = self.combine_overview(self.graph, incoming)
                self._overview_queries.append(deepcopy(params))
            else:
                self.graph = incoming
                self._overview_queries = [deepcopy(params)]
            self._graph_filters = (tuple(self.predicates or ()), tuple(self.relation_statuses or ()))
            if center:
                self.positions = {}
                self.viewport = {}
            self.update_chart()

    async def graph_click(self, event):
        data = event.args.get("data")
        if not isinstance(data, dict) or not self.graph:
            return
        if data.get("kind") == "group" or data.get("aggregated"):
            if not await self.begin_exploration():
                return
            self.inspection = deepcopy(data)
            self.show_inspector()
            self.refresh_graph_toolbar()
        elif event.args.get("dataType") == "edge":
            edge = next((edge for edge in self.graph["edges"] if edge["id"] == data.get("id")), None)
            if edge:
                await self.select("relation", edge["id"])
        elif any(n["id"] == data.get("id") for n in self.graph["nodes"]):
            await self.select("object", data["id"], reveal=True)

    def render_graph(self):
        def toolbar():
            buttons = {}
            with ui.row().classes("graph-toolbar w-full items-center"):
                ui.button(icon="arrow_back", on_click=self.back_exploration, color=None).props(
                    f'flat dense aria-label="{self.text("exploration_back")}"').tooltip(self.text("exploration_back")).bind_enabled_from(self, "exploration_history", backward=bool)
                scope = "hierarchy" if self.layout == "hierarchy" else "global" if self.overview else "local"
                with ui.row().classes("graph-scope-switch gap-1").props(f'role=group aria-label="{self.text("view_mode")}"'):
                    for value, name, callback in (("global", "global_graph", self.show_global_graph),
                                                  ("local", "local_graph", self.show_local_graph),
                                                  ("hierarchy", "hierarchy", self.show_hierarchy)):
                        buttons[value] = ui.button(self.text(name), color=None, on_click=callback).props(
                            f'flat dense no-caps aria-pressed={str(scope == value).lower()}').classes("mode-active" if scope == value else "").bind_enabled_from(self, "_space_switching", backward=lambda value: not value)
                ui.space()
                with ui.element("div").classes("graph-loading-slot"):
                    ui.spinner(size="sm").props(f'role=status aria-label="{self.text("loading_graph")}"').bind_visibility_from(self, "_graph_busy")
                with ui.button(self.text("filters"), icon="filter_list", color=None).props("flat dense no-caps").bind_enabled_from(self, "_space_switching", backward=lambda value: not value):
                    with ui.menu().classes("graph-filter-menu"):
                        ui.label(self.text("relation_filters")).classes("text-sm font-medium")
                        depth_control = ui.select({1: self.text("one_hop"), 2: self.text("two_hops")}, value=self.depth, label=self.text("depth"),
                                      on_change=lambda e: self.reload_on_change("depth", e.value)).props("dense outlined").classes("w-full")
                        depth_control.bind_visibility_from(self, "overview", backward=lambda value: not value)
                        predicate_control = ui.select(self.choices(PREDICATES), label=self.text("predicate"), value=self.predicates,
                                  multiple=True, clearable=True, on_change=lambda e: self.reload_on_change("predicates", e.value or None)).props("dense outlined").classes("w-full")
                        predicate_control.bind_visibility_from(self, "layout", backward=lambda value: value != "hierarchy")
                        status_control = ui.select(self.choices(STATUSES), label=self.text("relation_status"), value=self.relation_statuses,
                                  multiple=True, clearable=True, on_change=lambda e: self.reload_on_change("relation_statuses", e.value or None)).props("dense outlined").classes("w-full")
                        isolated_control = ui.switch(self.text("isolated_only"), value=self.isolated_only,
                                      on_change=lambda e: self.reload_on_change("isolated_only", e.value)).bind_visibility_from(self, "overview")
                        ui.button(self.text("clear_filters"), on_click=self.clear_graph_filters).props("flat dense no-caps")
                ui.button(icon="refresh", color=None, on_click=lambda: self.load_overview() if self.overview else self.refresh_local()).props(
                    f'flat dense aria-label="{self.text("refresh")}"').tooltip(self.text("refresh")).bind_enabled_from(self, "_space_switching", backward=lambda value: not value)
                ui.button(icon="view_sidebar", color=None, on_click=lambda: self.close_inspector() if self.inspector_open else self.show_inspector()).props(
                    f'flat dense aria-label="{self.text("inspector")}"').tooltip(self.text("inspector"))
                with ui.button(icon="more_horiz", color=None).props(f'flat dense aria-label="{self.text("more")}"'):
                    with ui.menu():
                        ui.menu_item(self.text("layout_2d"), on_click=partial(self.change_layout, "auto")).bind_enabled_from(self, "overview")
                        ui.menu_item(self.text("layout_3d"), on_click=partial(self.change_layout, "sphere3d")).bind_enabled_from(self, "overview")
                help_hint(self.text("exploration_help"))
            with ui.row().classes("graph-filter-summary w-full items-center").bind_visibility_from(self, "layout", backward=lambda value: value == "hierarchy"):
                hierarchy_control = ui.select(self.choices(("is_a", "part_of", "depends_on")), label=self.text("hierarchy_relation"),
                          value=self.predicates[0] if self.predicates and self.predicates[0] in ("is_a", "part_of", "depends_on") else "is_a",
                          on_change=lambda e: self.reload_on_change("predicates", [e.value])).props("dense outlined").classes("graph-hierarchy-select")
                ui.label(self.text("hierarchy_direction")).classes("text-xs theme-text-muted")
            with ui.row().classes("graph-filter-summary w-full items-center") as summary_row:
                summary_label = ui.label().classes("text-xs theme-text-secondary")
                ui.button(self.text("clear_filters"), color=None, on_click=self.clear_graph_filters).props("flat dense no-caps")

            def refresh_toolbar():
                scope = "hierarchy" if self.layout == "hierarchy" else "global" if self.overview else "local"
                for value, button in buttons.items():
                    if button.is_deleted:
                        return
                    active = scope == value
                    button.props(f'aria-pressed={str(active).lower()}')
                    button.classes(add="mode-active" if active else "", remove="mode-active" if not active else "")
                depth_control.set_value(self.depth)
                predicate_control.set_value(self.predicates)
                status_control.set_value(self.relation_statuses)
                isolated_control.set_value(self.isolated_only)
                if self.layout == "hierarchy" and self.predicates:
                    hierarchy_control.set_value(self.predicates[0])
                summary_row.set_visibility(self.layout != "hierarchy" and bool(self.predicates or self.relation_statuses or self.isolated_only))
                if summary_row.visible:
                    summary = [self.text(v) for v in [*(self.predicates or []), *(self.relation_statuses or [])]]
                    if self.isolated_only:
                        summary.append(self.text("isolated_only"))
                    summary_label.set_text(" · ".join(summary))
            self.refresh_graph_toolbar = refresh_toolbar
            refresh_toolbar()
        toolbar()
        with ui.element("div").classes("graph-stage w-full flex-1 min-h-0"):
            @ui.refreshable
            def graph_body():
                if self.graph is None or not self.graph["nodes"]:
                    with ui.column().classes("graph-empty absolute inset-0 items-center justify-center"):
                        ui.icon("hub").classes("graph-empty-icon").props("aria-hidden=true")
                        ui.label(self.text("global_graph" if self.overview else "local_graph")).classes("graph-empty-title")
                        ui.label(self.text("empty" if self.overview else "choose_root")).classes("graph-empty-hint")
                    return
                data = self.graph
                options = self.graph_chart_options()
                with ui.column().classes("graph-stats items-end gap-0"):
                    @ui.refreshable
                    def stats():
                        data = self.graph
                        if data is None:
                            return
                        if data.get("overview"):
                            ui.label(self.text("hierarchy_loaded" if self.layout == "hierarchy" else "overview_count", nodes=len(data["nodes"]), edges=len(data["edges"]),
                                              total_nodes=data["total_nodes"], total_edges=data["total_edges"])).classes("text-xs theme-text-muted")
                            if data["truncated"]:
                                ui.label(self.text("overview_truncated")).classes("text-xs font-semibold theme-text")
                            if len(data["nodes"]) >= 10000 and data.get("next_offset") is not None:
                                ui.label(self.text("canvas_capacity")).classes("text-xs theme-text-secondary")
                            with ui.row().classes("graph-load-actions gap-1"):
                                if data.get("next_offset") is not None and len(data["nodes"]) < 10000:
                                    ui.button(self.text("load_more_nodes"), on_click=partial(self.load_overview, more=True), color=None).props("flat dense no-caps")
                                if data.get("next_edge_offset") is not None:
                                    ui.button(self.text("load_more_edges"), on_click=partial(self.load_overview, more_edges=True), color=None).props("flat dense no-caps")
                            if data.get("total_isolated") and self.layout != "hierarchy":
                                ui.label(self.text("isolated_count", count=data["total_isolated"])).classes("text-xs theme-text-muted")
                        else:
                            ui.label(self.text("graph_count", nodes=len(data["nodes"]), edges=len(data["edges"]),
                                              depth=data["depth"])).classes("text-xs theme-text-muted")
                            if data["truncated"]:
                                ui.label(self.text("truncated")).classes("text-xs font-semibold theme-text")
                            if data.get("next_neighbor_offset") is not None:
                                ui.button(self.text("load_more_neighbors"), color=None,
                                          on_click=partial(self.load_graph, expand=True, neighbor_offset=data["next_neighbor_offset"],
                                                           expand_root=data.get("neighbor_root_id", data["root_id"]))).props("flat dense no-caps").set_enabled(len(data["nodes"]) < 2000 and len(data["edges"]) < 6000)
                            if len(data["nodes"]) >= 2000 or len(data["edges"]) >= 6000:
                                ui.label(self.text("canvas_capacity")).classes("text-xs theme-text-secondary")
                        if self._projection.get("groups"):
                            ui.label(self.text("group_count", count=len(self._projection["groups"]))).classes("text-xs theme-text-muted")
                        if self._projection.get("cycles"):
                            ui.label(self.text("cycle_detected")).classes("text-xs theme-text-secondary")
                        if self._projection.get("layout_warning"):
                            ui.label(self.text(self._projection["layout_warning"])).classes("text-xs theme-text-secondary")
                        if self._projection.get("excluded_count"):
                            ui.label(self.text("hierarchy_excluded", count=self._projection["excluded_count"])).classes("text-xs theme-text-secondary")
                        if self.layout == "hierarchy":
                            ui.label(self.text("hierarchy_visible", count=self._projection.get("visible_nodes", 0))).classes("text-xs theme-text-secondary")
                            if self._projection.get("hierarchy_overflow"):
                                ui.label(self.text("hierarchy_pan_hint")).classes("text-xs theme-text-muted")
                    self.refresh_graph_stats = stats.refresh
                    stats()
                # 3.3.1 的 on_point_click 强制读取 value；无数值的图节点/边须用原生事件。
                self.chart = ui.echart(options, enable_3d=self.layout == "sphere3d").classes("graph-canvas min-w-0")
                self.chart.on("chart:click", self.graph_click, js_handler="(event) => { if (window.pieceGraphDraggedUntil > performance.now()) return; emit({data: event.data, dataType: event.dataType}); }")
                self.chart.on("hierarchy-size", self.resize_hierarchy, ["detail"])
                with ui.column().classes("graph-empty absolute inset-0 items-center justify-center pointer-events-none") as self._hierarchy_empty:
                    self._hierarchy_empty.set_visibility(self.layout == "hierarchy" and not options["series"][0]["data"])
                    ui.label(self.text("hierarchy_empty")).classes("graph-empty-title")
                chart = self.chart
                with chart.client.content:
                    ui.timer(0.05, lambda: self.install_chart_interactions(chart) if not chart.is_deleted else None, once=True)
            graph_body_refresh = graph_body.refresh
            graph_body()
            with ui.row().classes("graph-canvas-tools items-center").props(f'role=group aria-label="{self.text("view_mode")}"'):
                for name, icon, action in (("zoom_out", "remove", partial(self.zoom_graph, 0.8)),
                                           ("zoom_in", "add", partial(self.zoom_graph, 1.25)),
                                           ("reset_view", "fit_screen", self.reset_graph_view)):
                    ui.button(icon=icon, color=None, on_click=action).props(
                        f'flat dense aria-label="{self.text(name)}"').tooltip(self.text(name))
        # 关系表格默认收起，展开时才占用画布高度。
        def toggle_table(event):
            self.table_open = event.value
            self.refresh_edge_table()

        with ui.expansion(self.text("table"), icon="table_rows", value=self.table_open,
                          on_value_change=toggle_table).classes("graph-drawer w-full"):
            with ui.column().classes("w-full max-h-[240px] overflow-y-auto px-3 pb-2"):
                @ui.refreshable
                def table():
                    if self.graph and self.table_open:
                        total = len(self.graph["edges"])
                        self.edge_page = min(self.edge_page, max(0, (total - 1) // 25))
                        self.edge_table(self.graph, self.graph["edges"][self.edge_page * 25:(self.edge_page + 1) * 25])
                        self.pager(self.edge_page * 25, 25, total, self.change_edge_page)
                self.refresh_edge_table = table.refresh
                table()

        def refresh_all():
            # 图与表是两个独立 refreshable，重绘图时必须一起更新表。
            graph_body_refresh()
            self.refresh_edge_table()
        self.refresh_graph = refresh_all

    def change_edge_page(self, delta):
        self.edge_page = max(0, self.edge_page + delta)
        self.refresh_edge_table()

    async def show_global_graph(self):
        rebuild = self.mode != "graph" or self.layout == "sphere3d"
        if not await self.begin_exploration():
            return
        self.restore_hierarchy_filters()
        self.overview = True
        self.layout = "auto"
        self.expanded_groups.clear()
        await self.load_overview(center=True)
        self.mode = "graph"
        self.refresh_workspace() if rebuild else self.refresh_graph_toolbar()

    async def show_local_graph(self):
        rebuild = self.mode != "graph" or self.layout == "sphere3d"
        if not await self.begin_exploration():
            return
        self.restore_hierarchy_filters()
        self.overview = False
        self.layout = "auto"
        self.isolated_only = False
        if self.selected_object:
            await self.load_graph(center=True)
        else:
            self.graph = None
            self.refresh_graph()
        self.mode = "graph"
        self.refresh_workspace() if rebuild else self.refresh_graph_toolbar()

    async def reload_on_change(self, name, value):
        """筛选变化即重取当前全局概览或局部邻域。"""
        if getattr(self, name) == value:
            return
        if not await self.begin_exploration():
            return
        setattr(self, name, value)
        self.structure_groups = []
        self.expanded_groups.clear()
        if self.overview:
            await self.load_overview(center=self.layout == "hierarchy" and name == "predicates")
            if self.layout == "hierarchy" and name == "predicates":
                self.refresh_graph()
        else:
            await self.load_graph()
        self.refresh_graph_toolbar()

    async def clear_graph_filters(self):
        if not await self.begin_exploration():
            return
        self.predicates = self.relation_statuses = None
        self.isolated_only = False
        self._hierarchy_return_state = None
        self.structure_groups = []
        self.expanded_groups.clear()
        reset_layout = self.layout == "hierarchy"
        if reset_layout:
            self.layout = "auto"
        if self.overview:
            await self.load_overview(center=reset_layout)
        else:
            await self.load_graph(center=reset_layout)
        self.refresh_graph_toolbar()

    async def change_layout(self, layout):
        if not await self.begin_exploration():
            return
        left_hierarchy = self.restore_hierarchy_filters()
        self.layout = layout
        self.positions, self.viewport = {}, {}
        if left_hierarchy and self.overview:
            await self.load_overview(center=True)
        self.refresh_workspace()

    async def show_hierarchy(self):
        if self.layout == "hierarchy":
            return
        rebuild = self.mode != "graph" or self.layout == "sphere3d"
        if not await self.begin_exploration():
            return
        self._hierarchy_return_state = {
            "predicates": deepcopy(self.predicates),
            "relation_statuses": deepcopy(self.relation_statuses),
            "isolated_only": self.isolated_only,
        }
        self.layout = "hierarchy"
        self.positions, self.viewport = {}, {}
        self.predicates = [self.predicates[0]] if self.predicates and len(self.predicates) == 1 and self.predicates[0] in (
            "is_a", "part_of", "depends_on") else ["is_a"]
        self.isolated_only = False
        await self.load_overview(center=True)
        self.mode = "graph"
        self.refresh_workspace() if rebuild else self.refresh_graph_toolbar()

    def restore_hierarchy_filters(self):
        """Leave the hierarchy-only relation filter at the scope boundary."""
        if self.layout != "hierarchy" or self._hierarchy_return_state is None:
            return False
        for name, value in self._hierarchy_return_state.items():
            setattr(self, name, deepcopy(value))
        self._hierarchy_return_state = None
        return True

    async def expand_selected(self, predicate=None):
        if not await self.begin_exploration():
            return
        if self.overview:
            rebuild = self.layout == "sphere3d"
            self.restore_hierarchy_filters()
            self.layout = "auto"
            await self.load_graph(center=True, predicate=predicate)
            self.refresh_workspace() if rebuild else self.refresh_graph_toolbar()
        else:
            await self.load_graph(expand=True, predicate=predicate)
        self.refresh_detail()

    async def collapse_selected(self):
        if not await self.begin_exploration():
            return
        self._graph_queries = [query for index, query in enumerate(self._graph_queries)
                               if index == 0 or query["root_id"] != self.selected_object]
        await self.refresh_local()
        self.refresh_detail()

    async def refresh_local(self):
        if not self._graph_queries:
            return await self.load_graph()
        self._graph_generation += 1
        generation, graph_id = self._graph_generation, self.graph_id
        await self.remember_positions()
        incoming = None
        for params in deepcopy(self._graph_queries):
            part = await self._scoped_call(service.graph, **params, graph_id=graph_id)
            if graph_id != self.graph_id or generation != self._graph_generation:
                return
            if part:
                if incoming:
                    incoming["max_nodes"] = (self.graph or incoming)["max_nodes"]
                    incoming["max_edges"] = (self.graph or incoming)["max_edges"]
                    incoming = merge_graph(incoming, part)
                else:
                    incoming = part
        self.graph = incoming
        self.update_chart()

    async def toggle_group(self, ident):
        if not await self.begin_exploration():
            return
        if ident in self.expanded_groups:
            self.expanded_groups.remove(ident)
        else:
            self.expanded_groups.add(ident)
        self.update_chart()
        self.refresh_detail()

    def render_inspection(self):
        if not self.inspection:
            self.render_detail()
            return
        data = self.inspection
        if data.get("kind") == "group":
            ui.label(data.get("name") or data.get("title") or self.text("structure_group")).classes("graph-detail-title")
            ui.label(self.text("structure_group_hint")).classes("text-sm theme-text-secondary")
            ui.label(self.text("group_members", count=data.get("member_count", len(data.get("member_ids", []))))).classes("text-sm")
            ui.button(self.text("collapse_group" if data["id"] in self.expanded_groups else "expand_group"),
                      on_click=partial(self.toggle_group, data["id"])).props("flat no-caps")
            members = set(data.get("member_ids", []))
            for node in (self.graph or {}).get("nodes", []):
                if node["id"] in members:
                    ui.button(node["title"], on_click=partial(self.select, "object", node["id"]), color=None).props("flat dense no-caps").classes("w-full justify-start")
            internal_ids = set(data.get("internal_edge_ids", []))
            if internal_ids:
                with ui.expansion(self.text("internal_relations", count=len(internal_ids))).classes("w-full"):
                    self.edge_table(self.graph, [edge for edge in self.graph["edges"] if edge["id"] in internal_ids])
        elif data.get("aggregated"):
            ids = set(data.get("relation_ids", []))
            ui.label(self.text("aggregated_relations", count=len(ids))).classes("graph-detail-title")
            self.edge_table(self.graph, [edge for edge in self.graph["edges"] if edge["id"] in ids])

    def install_chart_interactions(self, chart):
        """真实画布尺寸下的标签避让及拖动保护，交互不触发业务写入。"""
        from pathlib import Path
        script = Path(__file__).with_name("graph_interactions.js").read_text(encoding="utf-8")
        chart.client.run_javascript(script.replace("__CHART_ID__", str(chart.id)))

    def resize_hierarchy(self, event):
        if event.sender is not self.chart or self.layout != "hierarchy":
            return
        size = event.args.get("detail", {})
        window_size = (size.get("windowWidth"), size.get("windowHeight"))
        resized_window = self._canvas_window_size is not None and window_size != self._canvas_window_size
        self._canvas_window_size = window_size
        if self.inspector_open and not resized_window:
            return
        width, height = size.get("width"), size.get("height")
        if not all(isinstance(value, (int, float)) and math.isfinite(value) and value >= 100 for value in (width, height)):
            return
        dimensions = (min(8000, int(width)), min(8000, int(height)))
        if dimensions == self.canvas_size:
            return
        self.canvas_size = dimensions
        self.positions, self.viewport = {}, {}
        self.update_chart()

    async def zoom_graph(self, factor):
        """按倍率缩放 2D 图或调整 3D 球体相机距离。"""
        if not self.chart or self.chart.is_deleted:
            return
        ratio = float(factor)
        try:
            if self.layout == "sphere3d":
                await ui.run_javascript(f"""(() => {{
                    const chart = getElement({self.chart.id}).chart;
                    const current = chart.getOption().globe[0].viewControl.distance || 170;
                    chart.setOption({{globe: {{viewControl: {{
                        distance: Math.max(30, Math.min(400, current / {ratio}))
                    }}}}}});
                }})()""")
            else:
                # graphRoam 的 zoom 是相对倍率，必须同时给缩放原点。
                await ui.run_javascript(f"""(() => {{
                    const chart = getElement({self.chart.id}).chart;
                    chart.dispatchAction({{type: 'graphRoam', zoom: {ratio},
                                           originX: chart.getWidth() / 2, originY: chart.getHeight() / 2}});
                }})()""")
        except (TimeoutError, RuntimeError):
            # 断线时放弃这次缩放；缩放从不触发业务写入。
            pass

    def reset_graph_view(self):
        self.viewport = {}
        if self.layout == "sphere3d" and self.chart and not self.chart.is_deleted:
            try:
                ui.run_javascript(f"""(() => {{
                    const chart = getElement({self.chart.id}).chart;
                    chart.setOption({{globe: {{viewControl: {{alpha: 22, beta: 35,
                        distance: 170, center: [0, 0, 0]}}}}}});
                }})()""")
            except (TimeoutError, RuntimeError):
                pass
        self.update_chart()

    def edge_table(self, graph, edges=None):
        names = {node["id"]: node["title"] for node in graph["nodes"]}
        edges = graph["edges"] if edges is None else edges
        if not edges:
            ui.label(self.text("no_edges")).classes("text-sm theme-text-muted")
        for edge in edges:
            selected = self.detail and self.detail["record"]["id"] == edge["id"]
            with ui.row().classes("graph-edge-row w-full items-center gap-1 knowledge-list-item py-2" + (" theme-selected" if selected else "")):
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
            with ui.column().classes("graph-empty w-full items-center"):
                ui.icon("account_tree").classes("graph-empty-icon").props("aria-hidden=true")
                ui.label(self.text("heading")).classes("graph-empty-title")
                ui.label(self.text("choose_node")).classes("graph-empty-hint")
                ui.button(self.text("create"), icon="add", color=None, on_click=self.edit_object)\
                    .props("outline no-caps").set_enabled(bool(self.graph_id))
            return
        data, kind = self.detail["record"], self.detail["kind"]
        with ui.column().classes("knowledge-detail graph-detail w-full min-w-0"):
            if kind in ("object", "relation"):
                with ui.row().classes("graph-detail-meta w-full items-center justify-between"):
                    with ui.row().classes("items-center gap-2 knowledge-metadata"):
                        ui.label(self.text(data["kind"] if kind == "object" else kind)).classes("graph-kind-label")
                        ui.icon("help_outline" if data["status"] == "disputed" else "schedule" if data["status"] == "outdated" else "check_circle_outline", size="xs").props("aria-hidden=true")
                        ui.label(self.text(data["status"])).classes("text-xs theme-text-muted").tooltip(
                            self.text("revision", revision=data["revision"], updated=data["updated_at"]))
                    with ui.row().classes("graph-detail-actions items-center gap-1"):
                        ui.button(self.text("edit"), icon="edit", color=None, on_click=self.edit_current).props("flat dense no-caps")
                        with ui.button(icon="more_horiz", color=None).props(f'flat dense aria-label="{self.text("more")}"').tooltip(self.text("more")):
                            with ui.menu():
                                ui.menu_item(self.text("add_evidence"), on_click=partial(self.edit_evidence, kind, data["id"]))
                                ui.menu_item(self.text("history"), on_click=partial(self.show_history, kind, data["id"]))
                                ui.separator()
                                ui.menu_item(self.text("delete"), on_click=partial(self.preview_delete, kind, deepcopy(data))).classes("theme-danger")
            ui.label(data.get("title", self.text(kind))).classes("graph-detail-title").props("role=heading aria-level=1")
            if kind == "object":
                if data.get("aliases"):
                    ui.label(" / ".join(data["aliases"])).classes("graph-aliases text-sm theme-text-muted")
                if data["summary"]:
                    ui.label(data["summary"]).classes("graph-summary")
                with ui.row().classes("graph-detail-primary w-full items-center gap-2"):
                    ui.button(self.text("center" if self.mode == "graph" else "view_graph"), icon="my_location", color=None, on_click=self.view_graph).props("unelevated no-caps").classes("graph-primary-action")
                    if self.mode == "graph":
                        with ui.button(self.text("expand"), icon="open_in_full", color=None).props("flat no-caps"):
                            with ui.menu():
                                ui.menu_item(self.text("expand_all_neighbors"), on_click=self.expand_selected)
                                counts = {}
                                for edge in (self.local_relations or {}).get("edges", []):
                                    counts[edge["predicate"]] = counts.get(edge["predicate"], 0) + 1
                                for predicate, count in counts.items():
                                    ui.menu_item(f"{self.text(predicate)} · {count}", on_click=partial(self.expand_selected, predicate))
                                if (self.local_relations or {}).get("truncated"):
                                    ui.label(self.text("neighbor_counts_partial")).classes("text-xs px-4 theme-text-muted")
                        if any(query["root_id"] == data["id"] for query in self._graph_queries[1:]):
                            ui.button(self.text("collapse_neighbors"), icon="close_fullscreen", on_click=self.collapse_selected, color=None).props("flat dense no-caps")
                    ui.button(self.text("create_relation"), icon="add_link", color=None, on_click=partial(self.edit_edge, data["id"])).props("flat no-caps")
                with ui.expansion(self.text("relations"), icon="share").classes("graph-section graph-relations w-full"):
                    if self.local_relations:
                        if self.local_relations["truncated"]:
                            ui.label(self.text("truncated"))
                        self.edge_table(self.local_relations)
            elif kind == "relation":
                with ui.row().classes("graph-relation-path w-full items-center gap-2"):
                    ui.button(self._endpoint_names.get(data["source_id"], self.text("source_object")), color=None, on_click=partial(self.select, "object", data["source_id"])).props("flat no-caps")
                    ui.label("↔" if data.get("symmetric") else "→")
                    ui.button(self._endpoint_names.get(data["target_id"], self.text("target_object")), color=None, on_click=partial(self.select, "object", data["target_id"])).props("flat no-caps")
                for key in ("predicate", "description", "qualifier", "basis"):
                    value = self.text(data[key]) if key in ("basis", "predicate") else data[key]
                    ui.label(self.text(key) + " · " + value).classes("text-sm break-words theme-text-secondary")
            elif kind == "evidence":
                self.render_evidence(data)
            if "evidence" in self.detail:
                evidence = self.detail["evidence"]
                with ui.column().classes("graph-section graph-evidence-section w-full"):
                    with ui.row().classes("graph-section-heading w-full items-center gap-2"):
                        ui.icon("format_quote", size="xs").props("aria-hidden=true")
                        ui.label(self.text("evidence_count", count=evidence["total"])).props("role=heading aria-level=2")
                    if not evidence["items"]:
                        ui.label(self.text("no_evidence")).classes("graph-evidence-empty text-sm theme-text-muted")
                    for item in evidence["items"]:
                        self.render_evidence(item)
                    self.pager(evidence["offset"], evidence["limit"], evidence["total"],
                               partial(self.evidence_page, kind, data["id"], evidence["offset"]))

    def clear_error(self):
        self.error = None
        self.refresh_detail()

    async def view_graph(self):
        rebuild = self.mode != "graph" or self.layout == "sphere3d"
        if not await self.begin_exploration():
            return
        self.restore_hierarchy_filters()
        self.mode = "graph"
        self.overview = False
        self.layout = "auto"
        await self.load_graph(center=True)
        self.refresh_modes()
        self.refresh_workspace() if rebuild else self.refresh_graph_toolbar()

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
        if not self.graph_id:
            return
        draft = {"target": None, "predicate": "related_to", "description": "", "qualifier": "", "basis": "inference", "status": "active"}

        graph_id = self.graph_id
        generation = self.graph_generation

        def fields():
            search = ui.input(self.text("target_search")).props("outlined dense").classes("w-full")
            target = ui.select({}, label=self.text("target_object"), on_change=lambda e: draft.update(target=e.value)).props("outlined dense").classes("w-full")

            async def lookup():
                if not search.value or not search.value.strip():
                    return
                found = await self._scoped_call(service.search_objects, query=search.value, limit=50,
                                                graph_id=graph_id)
                if found and graph_id == self.graph_id and generation == self.graph_generation:
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
        if not self.detail or not self.graph_id:
            return
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
        @ui.refreshable
        def checks():
            self.render_check_results()
        self.refresh_checks = checks.refresh
        checks()

    def render_check_results(self):
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

    def check_params(self):
        params: dict[str, object] = {"limit": 25, "offset": self.lint_offset}
        if self.graph_id:
            params["graph_id"] = self.graph_id
        if self.lint_scope == "selected":
            params["object_ids"] = [self.selected_object] if self.selected_object else []
        elif self.lint_scope == "filtered":
            params["object_ids"] = [item["id"] for item in (self.results or {}).get("objects", [])]
        return params

    async def run_checks(self, reset=True):
        self._checks_generation += 1
        generation = self._checks_generation
        if reset:
            self.lint_offset = 0
        params = self.check_params()
        checks = await self.call(service.lint, **params)
        if generation == self._checks_generation and params == self.check_params():
            self.checks = checks
            self.refresh_checks()

    async def check_page(self, delta):
        self.lint_offset = max(0, self.lint_offset + delta * 25)
        await self.run_checks(False)

    async def rebuild_index(self):
        """只重建查询索引；不改实体、关系、证据或历史，不调用模型。"""
        graph_id, generation = self.graph_id, self.graph_generation
        result = await self._scoped_call(service.rebuild_index, graph_id=graph_id)
        if result is None or graph_id != self.graph_id or generation != self.graph_generation:
            return
        self.notify(self.text("rebuild_done"), type="positive")
        await self.search(False)

    async def file_references(self, file_id, offset=0):
        await self.load_graphs()
        graph_id, generation = self.graph_id, self.graph_generation
        self._references_params = token = {}
        info = await self._scoped_call(service.list_objects, limit=1, graph_id=graph_id)
        if (not info or graph_id != self.graph_id or generation != self.graph_generation
                or self._references_params is not token):
            return
        self._references_params = params = {"source_library_id": info["library_id"], "source_file_id": file_id,
                                           "limit": 25, "offset": offset, "graph_id": info["graph_id"]}
        references = await self.call(service.references, **params)
        if (graph_id != self.graph_id or generation != self.graph_generation
                or self._references_params is not params or references is None):
            return
        self.reference_file, self.references_data = file_id, references
        if self.show_view:
            await self.show_view()
        self.refresh_workspace()

    def render_references(self):
        with ui.expansion(self.text("references"), icon="format_quote", value=True).classes("w-full"):
            @ui.refreshable
            def references():
                data = self.references_data
                if data is None:
                    return
                if not data["evidence"]:
                    ui.label(self.text("empty"))
                for item in data["evidence"]:
                    owner = item["owner"]
                    ui.button(owner.get("title") or owner.get("description") or owner["id"],
                              on_click=partial(self.select, item["owner_kind"], owner["id"])).props("flat no-caps").classes("w-full break-words")
                async def page(delta):
                    await self.file_references(self.reference_file, max(0, data["offset"] + delta * 25))
                self.pager(data["offset"], 25, data["total"], page)
            self.refresh_references = references.refresh
            references()
            ui.button(self.text("close"), on_click=self.close_references).props("flat")

    def close_references(self):
        self._references_params = None
        self.references_data = None
        self.refresh_workspace()
