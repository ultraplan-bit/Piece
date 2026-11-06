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
from app.ui.components import help_hint
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
        self.chart = None
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

    async def load_graphs(self):
        if self._graphs_loaded or self._graphs_loading:
            return
        self._graphs_loading = True
        try:
            data = await self.call(service.list_graphs)
            if not data:
                return
            self.graphs = data["graphs"]
            self._graphs_loaded = True
            selected = next((item for item in self.graphs if item["id"] == self.graph_id), None)
            if selected is None:
                selected = next((item for item in self.graphs if item["is_default"]),
                                self.graphs[0] if self.graphs else None)
            self.refresh_modes()
            if selected and selected["id"] != self.graph_id:
                await self.select_graph(selected["id"])
        finally:
            self._graphs_loading = False

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
        dialog.open()

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
        dialog.open()

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
        dialog.open()

    async def select_graph(self, graph_id):
        if graph_id == self.graph_id:
            return
        self.graph_generation += 1
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
        self.refresh_workspace()

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
                               auto_upload=True, max_files=1, max_file_size=10_000_000)\
                .props("accept=.json,.graphml,application/json,application/graphml+xml").classes("max-w-full")
            self.import_upload = upload
            preview = ui.column().classes("w-full gap-2")
            message = ui.label().classes("text-sm whitespace-pre-wrap theme-text-muted")
            with ui.row().classes("w-full justify-end"):
                ui.button(self.text("cancel"), on_click=dialog.close).props("flat")
                confirm = ui.button(self.text("confirm_import"), on_click=lambda: self.confirm_graph_import(
                    state, graph_id, dialog, message)).props("unelevated no-caps")
                confirm.disable()
            state.update(dialog=dialog, preview=preview, confirm=confirm, message=message)
        dialog.open()

    async def preview_graph_import(self, event, state, graph_id):
        if graph_id != self.graph_id or state["busy"]:
            return
        try:
            document = await event.file.text()
        except (UnicodeDecodeError, ValueError):
            state["message"].set_text(self.text("import_invalid"))
            return
        suffix = event.file.name.rsplit(".", 1)[-1].lower()
        format = "graphml" if suffix == "graphml" else "json"
        if suffix not in ("json", "graphml"):
            state["message"].set_text(self.text("import_format_invalid"))
            return
        state.update(document=document, format=format, preview_data=None)
        state["busy"] = True
        state["confirm"].disable()
        try:
            data = await self.call(service.import_graph, {
                "format": format, "document": document, "target_graph_id": graph_id,
                "dry_run": True, "confirmed": False,
            })
            if graph_id != self.graph_id:
                return
            state["preview"].clear()
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
        finally:
            state["busy"] = False

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
        if self._polling or self._searching or self.graph_id is None:
            return
        timer, generation = self._poll_timer, self._search_generation

        def current():
            return (timer is self._poll_timer and (timer is None or not timer.is_deleted)
                    and generation == self._search_generation)

        if not current():
            return
        self._polling = True
        selection, detail, graph_id = self._selection, self.detail, self.graph_id
        try:
            params = dict(self._search_params or self.search_params())
            function = self._list_function(params)
            results = await run.io_bound(function, **params)
            if not current() or results.get("graph_id") != self.graph_id:
                return
            last_offset = max(0, (results["total"] - 1) // params["limit"]) * params["limit"]
            if params["offset"] > last_offset:
                params["offset"] = last_offset
                results = await run.io_bound(function, **params)
                if not current() or results.get("graph_id") != self.graph_id:
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
                graph_id = self.graph_id
                incoming = await run.io_bound(service.get_record, kind=kind, id=ident, graph_id=graph_id,
                                             limit=evidence.get("limit", 25), offset=evidence.get("offset", 0))
                local = await run.io_bound(service.graph, root_id=ident, depth=1, edge_types=["relation"],
                                           graph_id=graph_id) if kind == "object" else None
            except BusinessError as exc:
                if exc.code != "NOT_FOUND":
                    raise
                incoming, local = None, None
            if (not current() or selection != self._selection or self.detail is not detail
                    or incoming and incoming.get("graph_id") != self.graph_id):
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

    async def select(self, kind, ident, *, offset=0, reveal=False):
        """读取并选中一条记录。

        reveal 只用于画布点击：图谱标签下点画布节点或边时切到「实体」标签读详情。
        从左栏目录选中实体（reveal=False）时留在图谱标签，并把图谱以该实体为根刷新。
        """
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
        self.highlight_selection()
        if kind == "object":
            self.selected_object = ident
            self.local_relations = await self._scoped_call(service.graph, root_id=ident, depth=1,
                                                           edge_types=["relation"], graph_id=graph_id)
            if generation != self._selection or graph_id != self.graph_id:
                return
        if self.mode == "graph":
            if kind == "object" and (not reveal or self.overview):
                # 从目录或全局概览选中实体：切回局部探索并以它为根刷新图谱。
                self.overview = False
                await self.load_graph(center=True)
                self.refresh_workspace()
            else:
                # 画布点选节点或边：图不动，切到「实体」标签读详情。
                await self.set_mode("objects")
            return data
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
        def create_object():
            if self.graph_id:
                self.edit_object()

        self.render_catalog(KINDS, "objects", "object", create_object)
        self.query_input = self._catalog_search
        self.filter_controls = self._catalog_filters

    def render_right(self, controls=None):
        if not self._graphs_loaded and not self._graphs_loading:
            ui.timer(0.1, self.load_graphs, once=True)
        with ui.column().classes("w-full h-full flex-1 min-h-0 min-w-0 theme-content gap-0"):
            @ui.refreshable
            def modes():
                with ui.row().classes("w-full items-center library-heading workspace-toolbar overflow-x-auto"):
                    if controls:
                        controls()
                    async def choose_graph(event):
                        await self.load_graphs()
                        await self.select_graph(event.value)

                    ui.select({item["id"]: item["name"] for item in self.graphs}, value=self.graph_id,
                              label=self.text("graph_space"), on_change=choose_graph)\
                        .props("dense outlined").classes("graph-space-picker shrink-0")
                    ui.button(self.text("create_graph"), icon="add", color=None, on_click=self.create_graph_dialog)\
                        .props("flat dense no-caps")
                    ui.button(self.text("rename_graph"), icon="edit", color=None, on_click=self.rename_graph_dialog)\
                        .props("flat dense no-caps").set_enabled(bool(self.graph_id))
                    current_graph = next((item for item in self.graphs if item["id"] == self.graph_id), None)
                    if current_graph and not current_graph.get("is_default"):
                        ui.button(self.text("delete_graph"), icon="delete", on_click=self.delete_graph_dialog)\
                            .props("flat dense no-caps color=negative")
                    if self.graph_id:
                        with ui.button(self.text("graph_exchange"), icon="more_vert", color=None).props("flat dense no-caps"):
                            with ui.menu():
                                ui.menu_item(self.text("export_graph_json"), on_click=partial(self.export_graph, "json"))
                                ui.menu_item(self.text("export_graphml"), on_click=partial(self.export_graph, "graphml"))
                                ui.separator()
                                ui.menu_item(self.text("import_graph"), on_click=self.import_graph_dialog)
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
                    # 图谱标签不放进滚动区：画布要吃掉整栏剩余高度，套在滚动区里
                    # 高度会被内容撑成固定值，下方就留出大片空白。
                    self.render_graph()
                    if self.references_data is not None:
                        with ui.column().classes("graph-references w-full max-h-[220px] overflow-y-auto px-3 py-2 gap-1"):
                            self.render_references()
                    # 图谱标签只显示图与关系表：实体详情在「实体」标签读，不在图下方再叠一份。
                    self.refresh_detail = lambda: None
                    return
                with ui.scroll_area(on_scroll=lambda e: self._scroll_positions.update(detail=e.vertical_position)).classes("w-full flex-1") as area:
                    self._scroll_areas["detail"] = area
                    with ui.column().classes("w-full min-w-0 p-5 gap-4"):
                        if self.mode == "objects":
                            with ui.expansion(self.text("review"), icon="fact_check", value=self.review_open,
                                              on_value_change=lambda e: setattr(self, "review_open", e.value)).classes("w-full"):
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
            elif self.selected_object and (
                    center or self.graph is None or self.graph["root_id"] != self.selected_object):
                await self.load_graph(center=True)
        self.refresh_workspace()

    # ---- 局部图探索 ----
    async def remember_positions(self):
        if not self.chart or self.chart.is_deleted or (self.graph and self.graph.get("overview")):
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

    async def load_graph(self, *, center=False, expand=False):
        self.overview = False
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
        filters = (tuple(self.edge_types), tuple(self.predicates or ()), tuple(self.relation_statuses or ()))
        incoming = await self._scoped_call(service.graph, root_id=self.selected_object if expand else root,
                                           depth=1 if expand else self.depth, edge_types=self.edge_types,
                                           predicates=self.predicates, statuses=self.relation_statuses,
                                           max_nodes=100, max_edges=300, graph_id=graph_id)
        if incoming and generation == self._graph_generation and graph_id == self.graph_id:
            self.graph = merge_graph(self.graph, incoming) if expand and self.graph and filters == self._graph_filters else incoming
            self._graph_filters = filters
            if center:
                self.positions = {}
                self.viewport = {}
            self.refresh_graph()

    async def load_overview(self, *, center=False):
        self._graph_generation += 1
        generation = self._graph_generation
        graph_id = self.graph_id
        await self.remember_positions()
        if graph_id != self.graph_id or generation != self._graph_generation:
            return
        incoming = await self._scoped_call(service.graph_overview, predicates=self.predicates,
                                           statuses=self.relation_statuses, graph_id=graph_id)
        if incoming and generation == self._graph_generation and graph_id == self.graph_id:
            self.graph = incoming
            self._graph_filters = (tuple(self.predicates or ()), tuple(self.relation_statuses or ()))
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
            # 全局概览点选节点直接切入以该节点为根的局部图。
            if self.overview:
                await self.select("object", data["id"])
            else:
                # 画布点击是探索意图：切到「实体」标签看详情，不重排当前这张图。
                await self.select("object", data["id"], reveal=True)

    def render_graph(self):
        # 范围与视图控件收成一条工具条；画布吃掉整栏剩余高度，控件不再挤压图。
        with ui.row().classes("graph-toolbar w-full items-center gap-1 px-3 py-1 flex-nowrap overflow-x-auto"):
            if self.overview:
                ui.button(self.text("local_graph"), on_click=self.show_local_graph).props("flat dense no-caps shrink-0")
                ui.select(self.choices(PREDICATES), label=self.text("predicate"), value=self.predicates, multiple=True, clearable=True,
                          on_change=lambda e: self.reload_on_change("predicates", e.value or None)).props("dense outlined").classes("graph-control shrink-0")
                ui.select(self.choices(STATUSES), label=self.text("status"), value=self.relation_statuses, multiple=True, clearable=True,
                          on_change=lambda e: self.reload_on_change("relation_statuses", e.value or None)).props("dense outlined").classes("graph-control shrink-0")
                ui.button(self.text("refresh"), on_click=self.load_overview).props("flat dense no-caps shrink-0")
                ui.button(self.text("reset_view"), on_click=self.reset_graph_view).props("flat dense no-caps shrink-0")
            else:
                ui.button(self.text("global_graph"), on_click=self.show_global_graph).props("flat dense no-caps shrink-0")
                ui.select({1: self.text("one_hop"), 2: self.text("two_hops")}, value=self.depth,
                          on_change=lambda e: self.reload_on_change("depth", e.value)).props("dense outlined").classes("graph-control shrink-0")
                ui.select(self.choices(PREDICATES), label=self.text("predicate"), value=self.predicates, multiple=True, clearable=True,
                          on_change=lambda e: self.reload_on_change("predicates", e.value or None)).props("dense outlined").classes("graph-control shrink-0")
                ui.select(self.choices(STATUSES), label=self.text("status"), value=self.relation_statuses, multiple=True, clearable=True,
                          on_change=lambda e: self.reload_on_change("relation_statuses", e.value or None)).props("dense outlined").classes("graph-control shrink-0")
                ui.button(self.text("refresh"), on_click=self.load_graph).props("flat dense no-caps shrink-0")
                ui.button(self.text("center"), on_click=partial(self.load_graph, center=True)).props("flat dense no-caps shrink-0")
                ui.button(self.text("expand"), on_click=partial(self.load_graph, expand=True)).props("flat dense no-caps shrink-0")
                ui.button(self.text("reset_view"), on_click=self.reset_graph_view).props("flat dense no-caps shrink-0")
            # 缩放按倍率作用于画布；画布铺满后仍可放大看局部。
            ui.button(icon="zoom_in", on_click=partial(self.zoom_graph, 1.25)).props(
                f'flat dense round size=sm aria-label="{self.text("zoom_in")}"'
            ).tooltip(self.text("zoom_in"))
            ui.button(icon="zoom_out", on_click=partial(self.zoom_graph, 0.8)).props(
                f'flat dense round size=sm aria-label="{self.text("zoom_out")}"'
            ).tooltip(self.text("zoom_out"))
            help_hint(self.text("overview_scope" if self.overview else "graph_scope"))
        with ui.element("div").classes("graph-stage w-full flex-1 min-h-0"):
            @ui.refreshable
            def graph_body():
                if self.graph is None or not self.graph["nodes"]:
                    with ui.column().classes("absolute inset-0 items-center justify-center"):
                        ui.label(self.text("empty" if self.overview else "choose_root")).classes("theme-text-muted")
                    return
                data = self.graph
                with ui.column().classes("graph-stats items-end gap-0"):
                    if data.get("overview"):
                        ui.label(self.text("overview_count", nodes=len(data["nodes"]), edges=len(data["edges"]),
                                          total_nodes=data["total_nodes"], total_edges=data["total_edges"])).classes("text-xs theme-text-muted")
                        if data["truncated"]:
                            ui.label(self.text("overview_truncated")).classes("text-xs font-semibold theme-text")
                    else:
                        ui.label(self.text("graph_count", nodes=len(data["nodes"]), edges=len(data["edges"]),
                                          depth=data["depth"])).classes("text-xs theme-text-muted")
                        if data["truncated"]:
                            ui.label(self.text("truncated")).classes("text-xs font-semibold theme-text")
                options = graph_options(data, dark=bool(self.dark()), selected=self.selected_object,
                                        positions=self.positions, label=self.text)
                if not data.get("overview"):
                    options["series"][0].update(self.viewport)
                    self.positions.update({n["id"]: (n["x"], n["y"]) for n in options["series"][0]["data"]})
                # 3.3.1 的 on_point_click 强制读取 value；无数值的图节点/边须用原生事件。
                self.chart = ui.echart(options, enable_3d=bool(data.get("overview"))).classes("graph-canvas min-w-0")
                self.chart.on("chart:click", self.graph_click, ["data", "dataType"])
            graph_body_refresh = graph_body.refresh
            graph_body()
        # 关系表格默认收起，展开时才占用画布高度。
        with ui.expansion(self.text("table"), icon="table_rows").classes("graph-drawer w-full"):
            with ui.column().classes("w-full max-h-[240px] overflow-y-auto px-3 pb-2"):
                @ui.refreshable
                def table():
                    if self.graph:
                        self.edge_table(self.graph)
                self.refresh_edge_table = table.refresh
                table()

        def refresh_all():
            # 图与表是两个独立 refreshable，重绘图时必须一起更新表。
            graph_body_refresh()
            self.refresh_edge_table()
        self.refresh_graph = refresh_all

    async def show_global_graph(self):
        self.overview = True
        await self.load_overview(center=True)
        self.refresh_workspace()

    async def show_local_graph(self):
        self.overview = False
        if self.selected_object:
            await self.load_graph(center=True)
        else:
            self.graph = None
        self.refresh_workspace()

    async def reload_on_change(self, name, value):
        """筛选变化即重取当前全局概览或局部邻域。"""
        setattr(self, name, value)
        if self.overview:
            await self.load_overview()
        else:
            await self.load_graph()

    async def zoom_graph(self, factor):
        """按倍率缩放 2D 图或调整 3D 球体相机距离。"""
        if not self.chart or self.chart.is_deleted:
            return
        ratio = float(factor)
        try:
            if self.graph and self.graph.get("overview"):
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
        self.positions = {}
        self.viewport = {}
        if self.graph and self.graph.get("overview") and self.chart and not self.chart.is_deleted:
            try:
                ui.run_javascript(f"""(() => {{
                    const chart = getElement({self.chart.id}).chart;
                    chart.setOption({{globe: {{viewControl: {{alpha: 22, beta: 35,
                        distance: 170, center: [0, 0, 0]}}}}}});
                }})()""")
            except (TimeoutError, RuntimeError):
                pass
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
        # 详情里的「查看关联」总是以当前对象为根重排，即使图谱标签已有同根的图。
        await self.set_mode("graph", center=True)

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

        graph_id, generation = self.graph_id, self.graph_generation
        if graph_id:
            params["graph_id"] = graph_id
        self.checks = await self._scoped_call(service.lint, **params, graph_id=graph_id)
        if graph_id == self.graph_id and generation == self.graph_generation:
            self.refresh_workspace()

    async def check_page(self, delta):
        self.lint_offset = max(0, self.lint_offset + delta * 25)
        await self.run_checks(False)

    async def rebuild_index(self):
        """只重建查询索引；不改实体、关系、证据或历史，不调用模型。"""
        result = await self._scoped_call(service.rebuild_index, graph_id=self.graph_id)
        if result is None:
            return
        self.notify(self.text("rebuild_done"), type="positive")
        await self.search(False)

    async def file_references(self, file_id, offset=0):
        graph_id = self.graph_id
        info = await self._scoped_call(service.list_objects, limit=1, graph_id=graph_id)
        if not info or graph_id != self.graph_id:
            return
        self.reference_file = file_id
        self.references_data = await self._scoped_call(service.references, source_library_id=info["library_id"],
                                                       source_file_id=file_id, limit=25, offset=offset,
                                                       graph_id=graph_id)
        if graph_id != self.graph_id:
            return
        if self.show_view:
            await self.show_view()
        self.refresh_workspace()

    def render_references(self):
        data = self.references_data
        if data is None:
            return
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
