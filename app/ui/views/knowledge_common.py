"""Wiki 与知识图谱工作台共享的展示与写入基础设施；业务读写仍由各自 service 承担。

两个功能各自独立（服务、身份、生命周期），这里只复用与业务无关的通用能力：
请求键不可变重试、证据卡片与来源比对、删除影响预览、修订历史、分页与滚动位置。
"""
from contextlib import nullcontext
from copy import deepcopy
import inspect
import json
import logging
from functools import partial
from urllib.parse import urlsplit
from uuid import uuid4

from nicegui import context, run, ui

from app.i18n import t
from app.ui.components import _open_dialog, guard_unsaved, CATALOG_KEY_HANDLER
from indexing.services.errors import BusinessError

logger = logging.getLogger(__name__)

STATUSES = ("active", "disputed", "outdated")
CONFLICT_CODES = ("VERSION_CONFLICT", "CONTENT_CONFLICT")


def safe_external_url(value):
    """仅接受不含凭据、不含控制字符的安全 http/https 地址。"""
    if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password:
            parsed.port
            return value
    except (ValueError, TypeError):
        pass
    return None


def local_source(evidence, library_id):
    """只有本库、定位状态仍可用且 ID 为整数时，才允许打开原页。"""
    if (evidence.get("source_kind") == "piece" and evidence.get("source_library_id") == library_id
            and evidence.get("location_status") in ("current", "changed")
            and type(evidence.get("source_file_id")) is int and type(evidence.get("source_chunk_id")) is int):
        return evidence["source_file_id"], evidence["source_chunk_id"]
    return None


class PendingWrite:
    """提交不可变副本；未知结果只能查原键/原样重试。"""
    def __init__(self, payload):
        self.payload = deepcopy(payload)
        self.payload["request_key"] = str(uuid4())
        self.uncertain = False

    @property
    def key(self):
        return self.payload["request_key"]


class WorkbenchBase:
    """两个工作台共享的 GUI 管道；子类负责自己的 service、查询与详情渲染。"""

    service = None
    prefix = ""
    # 写入结果读回顺序：(结果字段, 记录类型)；默认不含 wiki 的 page/refs 结构。
    result_readback = (("objects", "object"), ("relations", "relation"), ("evidence", "evidence"))
    # 正式删除需要 expected_revision 的记录类型。
    revision_kinds = ("object", "relation")
    # 正式删除需要 expected_content_hash 的记录类型（Wiki 页面与证据）。
    hash_kinds = ()

    def __init__(self, *, open_source=None, show_view=None, dark=None):
        self.open_source = open_source
        self.show_view = show_view
        self.dark = dark or (lambda: False)
        self.query = ""
        self.offset = 0
        self.results = None
        self.detail = None
        self.error = None
        self._client = None
        self._selection = 0
        self._search_generation = 0
        self._search_params = None
        self._applied_query = None
        self._searching = False
        self._polling = False
        self._poll_timer = None
        self._scroll_areas = {}
        self._scroll_positions = {}
        self._list_rows = {}
        self.refresh_list = lambda: None
        self.refresh_detail = lambda: None
        self.refresh_workspace = lambda: None
        self.refresh_edge_table = lambda: None

    # ---- i18n：功能命名空间优先，缺失时回退共享 knowledge 命名空间 ----
    def text(self, key, **kwargs):
        namespaced = self.prefix + key
        value = t(namespaced, **kwargs)
        if value == namespaced:
            value = t("knowledge." + key, **kwargs)
        return value

    def choices(self, values):
        return {value: self.text(value) for value in values}

    def notify(self, message, **options):
        # 异步服务返回时事件父容器可能已销毁，通知挂到渲染时记录的页面客户端。
        with self._client or nullcontext():
            ui.notify(message, **options)

    async def call(self, function, *args, **kwargs):
        try:
            return await run.io_bound(function, *args, **kwargs)
        except BusinessError as exc:
            self.notify(self.text("error", code=exc.code), type="warning")
            return None

    # ---- 查询输入：打字防抖刷新，回车/按钮立即执行 ----
    def query_changed(self, event):
        """值变化即记录查询串；列表刷新由防抖后的回调完成。"""
        self.query = (event.value or "").strip()

    async def apply_query(self, value=None, *, force=False):
        """同一查询串不重复请求；force 供回车或按钮显式重查。"""
        if value is not None:
            self.query = (value or "").strip()
        if not force and self.query == self._applied_query:
            return
        self._applied_query = self.query
        await self.search()

    async def query_committed(self):
        await self.apply_query()

    async def query_entered(self, event):
        await self.apply_query(getattr(event, "args", None), force=True)

    async def search_clicked(self):
        await self.apply_query(force=True)

    async def filters_changed(self):
        await self.search()

    async def clear_query(self):
        await self.apply_query("", force=True)

    # ---- 布局 ----
    def highlight_selection(self):
        selected = (self.detail["kind"], self.detail["record"]["id"]) if self.detail else None
        focused = selected if selected in self._list_rows else next(iter(self._list_rows), None)
        for identity, row in self._list_rows.items():
            if not row.is_deleted:
                row.classes(add="theme-selected" if identity == selected else "",
                            remove="" if identity == selected else "theme-selected")
                row.props(f'aria-pressed={str(identity == selected).lower()} tabindex={0 if identity == focused else -1}')

    def render_catalog(self, kinds, result_key, record_kind, create):
        """共享紧凑目录；搜索与选择仍调用各工作台自己的服务。"""
        self._client = context.client
        with ui.column().classes("w-full h-full gap-0 theme-panel overflow-hidden knowledge-catalog").props(
            f'data-catalog={self.prefix.rstrip(".")}'
        ):
            with ui.row().classes("w-full items-center justify-between library-heading workspace-toolbar"):
                ui.label(self.text("heading")).classes("text-sm font-medium theme-text")
                ui.button(self.text("create"), icon="add", on_click=create).props("flat dense no-caps size=sm")
            with ui.column().classes("w-full p-3 gap-2 workspace-search"):
                with ui.row().classes("w-full gap-1 items-center flex-nowrap"):
                    search = ui.input(self.text("search"), value=self.query,
                                      on_change=self.query_changed).props(
                        "outlined dense clearable debounce=300"
                    ).classes("flex-1 min-w-0")
                    search.on("update:model-value", self.query_committed)
                    search.on("keydown.enter", self.query_entered, js_handler="""(event) => {
                        const field = event.target.closest && event.target.closest('.q-field');
                        const el = (field && field.querySelector('input, textarea')) || event.target;
                        emit(((el && el.value) || '').trim());
                    }""")
                    search.on("clear", self.clear_query)
                    search.tooltip(self.text("search_hint"))
                    ui.button(icon="search", on_click=self.search_clicked).props(
                        f'flat dense round size=sm aria-label="{self.text("search")}"'
                    ).tooltip(self.text("search"))
                with ui.row().classes("w-full gap-2 flex-nowrap"):
                    ui.select(self.choices(kinds), label=self.text("kind"), value=self.kind, clearable=True,
                              on_change=lambda e: setattr(self, "kind", e.value)).props(
                        "dense outlined").classes("flex-1 min-w-0").on("update:model-value", self.filters_changed)
                    ui.select(self.choices(STATUSES), label=self.text("status"), value=self.status, clearable=True,
                              on_change=lambda e: setattr(self, "status", e.value)).props(
                        "dense outlined").classes("flex-1 min-w-0").on("update:model-value", self.filters_changed)
            with ui.scroll_area(on_scroll=lambda e: self._scroll_positions.update(list=e.vertical_position)).classes("w-full flex-1 scroll-flush") as area:
                self._scroll_areas["list"] = area
                @ui.refreshable
                def listing():
                    self._list_rows = {}
                    data = self.results
                    with ui.column().classes("w-full gap-0.5 px-2 pb-2"):
                        if not data or not data[result_key]:
                            ui.label(self.text("empty" if data else "search_hint")).classes("text-sm theme-text-muted px-2 py-6")
                        for item in (data or {}).get(result_key, []):
                            with ui.column().classes("w-full gap-1 workspace-list-item theme-hover").props(
                                f'role=button tabindex=-1 data-catalog-row={item["id"]}'
                            ) as row:
                                self._list_rows[(record_kind, item["id"])] = row
                                row._props["aria-label"] = item["title"]
                                row.on("click", partial(self.select, record_kind, item["id"]))
                                row.on("keydown", js_handler=CATALOG_KEY_HANDLER)
                                ui.label(item["title"]).classes("w-full text-left workspace-list-title theme-text truncate").tooltip(item["title"])
                                ui.label(self.text(item["kind"]) + " · " + self.text(item["status"])).classes("text-xs theme-text-muted")
                                if item.get("summary"):
                                    ui.label(item["summary"]).classes("text-xs theme-text-secondary line-clamp-2 break-words")
                                row.tooltip(self.text("updated_at", date=item.get("updated_at", "")))
                        if data:
                            self.pager(self.offset, 25, data["total"], self.list_page)
                    self.highlight_selection()
                self.refresh_list = listing.refresh
                listing()
            area.scroll_to(pixels=self._scroll_positions.get("list", 0))
            self._poll_timer = ui.timer(2.0, self.poll)

    async def _refresh_preserving_scroll(self, callback, section):
        area = self._scroll_areas.get(section)
        position = self._scroll_positions.get(section, 0)
        result = callback()
        if inspect.isawaitable(result):
            await result
        if area is not None and not area.is_deleted:
            area.scroll_to(pixels=position)

    def pager(self, offset, limit, total, callback):
        if total == 0 and offset == 0:
            return
        with ui.row().classes("items-center gap-2"):
            previous = ui.button(self.text("previous"), on_click=partial(callback, -1)).props("flat dense no-caps")
            previous.set_enabled(offset > 0)
            previous.set_visibility(total > limit or offset > 0)
            ui.label(self.text("page_count", start=min(offset + 1, total), end=min(offset + limit, total), total=total)).classes("text-xs theme-text-muted")
            following = ui.button(self.text("next"), on_click=partial(callback, 1)).props("flat dense no-caps")
            following.set_enabled(offset + limit < total)
            following.set_visibility(total > limit or offset > 0)

    def field(self, name, values, *, multiline=False, options=None):
        caption = self.text("title_field" if name == "title" else name)
        if options:
            element = ui.select(self.choices(options), label=caption, value=values.get(name))
        elif multiline:
            element = ui.textarea(caption, value=values.get(name, "")).props("autogrow")
            if name == "body":
                element.classes("editor-body")
        else:
            element = ui.input(caption, value=values.get(name, ""))
        element.bind_value(values, name).props("outlined dense").classes("w-full")
        return element

    # ---- 写入：请求键、冲突与诚实上报 ----
    async def submit(self, request):
        if request.uncertain:
            try:
                return await run.io_bound(self.service.request_result, request.key)
            except BusinessError as exc:
                if exc.code != "NOT_FOUND":
                    raise
        return await run.io_bound(self.service.apply, request.payload, actor="gui")

    def adopt_latest(self, record, latest):
        """用户显式采用最新版本后，把冲突条件写回草稿。子类可补充内容哈希。"""
        record["revision"] = latest["revision"]

    def result_error(self, result):
        """服务以结果字段报告失败（而非抛异常）时返回错误码；子类覆盖。"""
        return None

    def write_dialog(self, title, fields, payload, kind, record):
        pending = {"value": None}
        reason = {"reason": ""}
        with ui.dialog().props("persistent") as dialog, ui.card().classes("w-[900px] max-w-full max-h-[90vh] theme-card editor-dialog"):
            with ui.row().classes("w-full items-center justify-between editor-header"):
                ui.label(title).classes("text-base font-semibold theme-text")
                close_icon = ui.button(icon="close", on_click=lambda: request_close()).props(
                    f'flat dense round size=sm aria-label="{self.text("close")}"'
                ).classes("theme-text-muted")
            with ui.column().classes("w-full min-h-0 editor-fields") as form:
                fields()
                self.field("reason", reason)
                message = ui.label().classes("text-sm whitespace-pre-wrap theme-text-muted")
                comparison = ui.column().classes("w-full")
            controls = [element for element in form.descendants() if isinstance(element, (ui.input, ui.select))]
            busy = {"value": False}

            async def show_failure(code):
                # 版本/内容冲突：保留草稿，展示最新内容，由用户显式采用后再保存。
                message.set_text(self.text("error", code=code))
                if code in CONFLICT_CODES and record is not None:
                    latest = await self.call(self.service.get_record, kind=kind, id=record["id"])
                    if latest:
                        comparison.clear()
                        with comparison:
                            ui.label(self.text("conflict_draft")).classes("font-semibold")
                            ui.label(json.dumps(latest["record"], ensure_ascii=False, indent=2)).classes("text-xs whitespace-pre-wrap break-words")
                            def use_revision():
                                self.adopt_latest(record, latest["record"])
                                message.set_text(self.text("compare_then_save"))
                                comparison.clear()
                            ui.button(self.text("use_revision"), on_click=use_revision).props("outline no-caps")

            async def save():
                if busy["value"]:
                    return
                busy["value"] = True
                save_button.disable()
                close_button.disable()
                close_icon.disable()
                for element in controls:
                    element.disable()
                try:
                    if pending["value"] is None:
                        pending["value"] = PendingWrite({**payload(), "reason": reason["reason"]})
                    result = await self.submit(pending["value"])
                    code = self.result_error(result)
                    if code is not None:
                        pending["value"] = None
                        await show_failure(code)
                        return
                    dialog.close()
                    await self.after_write(result)
                except BusinessError as exc:
                    pending["value"] = None
                    await show_failure(exc.code)
                except ValueError as exc:
                    pending["value"] = None
                    message.set_text(str(exc))
                except Exception:
                    if pending["value"]:
                        pending["value"].uncertain = True
                        message.set_text(self.text("uncertain", request_key=pending["value"].key))
                finally:
                    busy["value"] = False
                    save_button.enable()
                    close_button.enable()
                    close_icon.enable()
                    # 响应未知时保留原提交；先核查原请求，避免让用户误以为新输入会被提交。
                    for element in controls:
                        element.set_enabled(pending["value"] is None or not pending["value"].uncertain)

            with ui.row().classes("w-full items-center gap-2 editor-footer"):
                request_close = guard_unsaved(dialog, busy=lambda: busy["value"],
                                              pending=lambda: pending["value"] is not None)
                ui.space()
                close_button = ui.button(self.text("close"), on_click=request_close).props("flat no-caps")
                save_button = ui.button(self.text("save"), on_click=save).props("unelevated no-caps")
        _open_dialog(dialog)

    async def after_write(self, result):
        readback = None
        for name, kind in self.result_readback:
            if result.get(name):
                readback = await self.select(kind, result[name][0]["id"])
                break
        await self.search(False)
        # 索引失败但文件已提交（index_status=stale）或部分页面失败，不能冒充全部成功。
        if result.get("errors") or result.get("partial") or result.get("index_status") == "stale":
            self.notify(self.text("partial_saved", count=len(result.get("errors") or []),
                                index=self.text("index_" + str(result.get("index_status") or "unknown"))), type="warning")
            return
        self.notify(self.text("saved" if readback else "saved_unread"), type="positive" if readback else "warning")

    # ---- 删除：预览影响范围，确认后重放同一请求键 ----
    def after_delete_extra(self):
        """子类清理自身缓存（如局部图）。"""

    def delete_error(self, outcome):
        """删除以结果字段报告失败时返回错误码；成功或抛异常路径返回 None。"""
        if (isinstance(outcome, dict) and outcome.get("errors")
                and not outcome.get("committed") and not outcome.get("dry_run")):
            return (outcome["errors"][0] or {}).get("code")
        return None

    def recovery_note(self):
        """删除预览中需要额外声明的本地恢复边界；默认无。子类可覆盖。"""
        return None

    async def preview_delete(self, kind, record):
        payload = {"kind": kind, "id": record["id"]}
        if kind in self.revision_kinds:
            payload["expected_revision"] = record["revision"]
        if kind in self.hash_kinds:
            # 证据记录同时带来源卡片 content_hash 与所属页面 page_content_hash；
            # 删除证据必须用页面哈希，不能用来源快照哈希。
            payload["expected_content_hash"] = record.get("page_content_hash") or record.get("content_hash")
        preview = await self.call(self.service.delete, {**payload, "dry_run": True}, actor="gui")
        if preview is None:
            return
        pending = PendingWrite({**payload, "impact_token": preview["impact_token"], "dry_run": False, "confirmed": True})
        with ui.dialog().props("persistent") as dialog, ui.card().classes("w-[600px] max-w-full theme-card"):
            ui.label(self.text("delete_preview")).classes("text-xl font-semibold")
            ui.label(record.get("title", record["id"])).classes("break-all")
            for key, count in preview["counts"].items():
                ui.label(self.text("impact_count", kind=self.text(key), count=count))
            # 历史是否保留由服务边界声明，GUI 不写死“删除即清历史”。
            if preview.get("retains_history"):
                ui.label(self.text("delete_retains_history")).classes("text-sm theme-text-muted")
            elif preview.get("clears_online_history"):
                ui.label(self.text("delete_clears_history")).classes("text-sm theme-text-muted")
            if self.recovery_note():
                ui.label(self.text(self.recovery_note())).classes("text-sm theme-text-muted")
            ui.label(self.text("delete_scope")).classes("text-sm theme-text-muted")
            message = ui.label().classes("text-sm break-all")
            busy = {"value": False}

            async def confirm():
                if busy["value"]:
                    return
                busy["value"] = True
                try:
                    outcome = None
                    if pending.uncertain:
                        try:
                            outcome = await run.io_bound(self.service.request_result, pending.key)
                        except BusinessError as exc:
                            if exc.code != "NOT_FOUND":
                                raise
                            outcome = await run.io_bound(self.service.delete, pending.payload, actor="gui")
                    else:
                        outcome = await run.io_bound(self.service.delete, pending.payload, actor="gui")
                    code = self.delete_error(outcome)
                    if code is not None:
                        message.set_text(self.text("delete_conflict", code=code))
                        button.disable()
                        return
                    dialog.close()
                    self.detail = None
                    self.after_delete_extra()
                    self.refresh_workspace()
                    await self.search(False)
                except BusinessError as exc:
                    message.set_text(self.text("delete_conflict", code=exc.code))
                    button.disable()
                except Exception:
                    pending.uncertain = True
                    message.set_text(self.text("uncertain", request_key=pending.key))
                finally:
                    busy["value"] = False

            button = ui.button(self.text("confirm_delete"), on_click=confirm).props("unelevated color=negative no-caps")
            ui.button(self.text("cancel"), on_click=dialog.close).props("flat")
        _open_dialog(dialog)

    # ---- 历史 ----
    async def show_history(self, kind, ident, offset=0):
        data = await self.call(self.service.history, kind=kind, id=ident, limit=10, offset=offset)
        if not data:
            return
        with ui.dialog() as dialog, ui.card().classes("w-[900px] max-w-full max-h-[90vh] overflow-auto theme-card"):
            ui.label(self.text("history")).classes("text-xl font-semibold")
            ui.label(self.text("history_readonly")).classes("text-xs theme-text-muted")
            for item in data["history"]:
                with ui.expansion(str(item["after_revision"]) + " · " + item["reason"]).classes("w-full"):
                    ui.label(json.dumps({"before": item["before"], "after": item["after"]}, ensure_ascii=False, indent=2)).classes("text-xs whitespace-pre-wrap break-words")
            async def page(delta):
                dialog.close()
                await self.show_history(kind, ident, max(0, offset + delta * 10))
            self.pager(offset, 10, data["total"], page)
            ui.button(self.text("close"), on_click=dialog.close).props("flat")
        _open_dialog(dialog)

    # ---- 证据卡片与来源比对 ----
    def render_evidence(self, evidence):
        with ui.column().classes("w-full gap-2 p-3 knowledge-evidence theme-card"):
            ui.label(self.text(evidence["stance"]) + " · " + self.text(evidence["source_kind"])).classes("text-sm font-semibold theme-text")
            ui.label(evidence.get("source_title") or "").classes("text-sm break-words")
            ui.label(self.text("location_" + evidence["location_status"])).classes("text-xs theme-text-muted")
            ui.label(evidence["quote"]).classes("text-sm whitespace-pre-wrap break-words theme-text-secondary")
            if evidence["source_kind"] == "piece":
                ui.label(self.text("source_ids", library=evidence["source_library_id"], file=evidence["source_file_id"], chunk=evidence["source_chunk_id"])).classes("text-xs break-all theme-text-muted")
                if local_source(evidence, self.detail["library_id"]):
                    ui.button(self.text("compare_source"), on_click=partial(self.compare_source, deepcopy(evidence))).props("flat dense no-caps")
            elif evidence["source_kind"] == "external" and safe_external_url(evidence.get("source_url")):
                ui.link(self.text("external_source"), evidence["source_url"], new_tab=True)
            ui.button(self.text("delete_evidence"), on_click=partial(self.preview_delete, "evidence", deepcopy(evidence))).props("flat dense no-caps")

    async def compare_source(self, evidence):
        fresh = await self.call(self.service.get_record, kind="evidence", id=evidence["id"])
        if not fresh or not local_source(fresh["record"], fresh["library_id"]):
            self.notify(self.text("source_unavailable"), type="warning")
            return
        evidence = fresh["record"]
        from indexing.services.chunk_service import get_chunk_by_id
        chunk = await self.call(get_chunk_by_id, evidence["source_chunk_id"])
        if not chunk or chunk["file_id"] != evidence["source_file_id"]:
            self.notify(self.text("source_unavailable"), type="warning")
            return
        with ui.dialog() as dialog, ui.card().classes("w-[1000px] max-w-full theme-card"):
            ui.label(self.text("compare_source")).classes("text-lg font-semibold")
            with ui.row().classes("w-full gap-4 items-start"):
                with ui.column().classes("flex-1 min-w-[240px]"):
                    ui.label(self.text("snapshot"))
                    ui.label(evidence["quote"]).classes("whitespace-pre-wrap break-words")
                with ui.column().classes("flex-1 min-w-[240px]"):
                    ui.label(self.text("current_body"))
                    self.markdown(chunk["chunk_text"])
            if self.open_source:
                async def open_file():
                    dialog.close()
                    await self.open_source(evidence)
                ui.button(self.text("open_file"), on_click=open_file).props("flat no-caps")
            ui.button(self.text("close"), on_click=dialog.close).props("flat")
        _open_dialog(dialog)

    def markdown(self, body):
        raise NotImplementedError

    async def evidence_page(self, kind, ident, offset, delta):
        await self.select(kind, ident, offset=max(0, offset + delta * 25))
