"""
文件操作处理器

职责:
- 文件列表加载和搜索
- 文件上传处理（支持批量）
- 文件删除
- 统计信息加载
"""

import asyncio
import json
import logging
from urllib.parse import urlencode

from nicegui import ui, events

from indexing.services import file_service, task_service, zotero_service
from indexing.utils import await_completion, run_sync
from indexing.services import chunk_service, collection_service, metadata_service, maintenance_service
from app.i18n import t
from app.import_scan import import_candidates
from app.ui.components import (
    card_dialog,
    collection_manage_dialog,
    collection_path_label,
    confirm_dialog,
    file_collections_dialog,
    file_create_dialog,
    file_properties_dialog,
)
from app.ui.import_dialogs import folder_import_dialog, zotero_import_dialog
from app.ui.resource_tree import branch_key, document_key, is_tree_view, visible_tree_rows
from app.utils import (
    format_size,
    MAX_TOTAL_UPLOAD_SIZE,
    MAX_UPLOAD_FILES,
)
from indexing.services.file_service import get_max_file_size

logger = logging.getLogger(__name__)

# 上传已改为流式落盘到临时文件，可并行处理多个文件而不叠加内存峰值。
_UPLOAD_CONCURRENCY = 3

# 文件列表排序方式：字段名 -> 是否倒序
SORT_OPTIONS = {
    "created_at": True,
    "updated_at": True,
    "filename": False,
}


class FileHandlers:
    """文件操作处理器"""

    def __init__(self, state: dict, ui_refs: dict):
        """
        初始化文件处理器

        任务创建后不需要回调登记：任务表由 TaskHandlers 统一订阅，
        任何来源写入的任务都会在下一次轮询出现在列表里。

        Args:
            state: 共享状态字典，包含 files_data, filtered_files, selected_file_id 等
            ui_refs: UI 组件引用字典
        """
        self.state = state
        self.ui_refs = ui_refs
        # 文档多选状态；导出与归类、删除共用同一组 ID。
        self.state["batch_mode"] = False
        self.state["file_exporting"] = False
        self.state["batch_selected_ids"] = set()
        self.state["selection_anchor"] = None
        self.state["focused_file_id"] = None
        # 正在向服务端传输的文件数（用于顶部上传提示）
        self.state["uploading_count"] = 0
        self.state["uploading_files"] = []
        self.state["upload_target_name"] = t("files.drop_library")
        # 集合状态：collections 为全部集合，collections_by_file 供列表渲染标签
        self.state["collections"] = []
        self.state["collections_by_file"] = {}
        # 界面一次只持有当前文件页和正在阅读的文件。
        self.state["active_collection_ids"] = []
        self.state["uncategorized"] = False
        self.state["include_descendants"] = True
        self.state["expanded_collection_ids"] = []
        self.state.setdefault("library_mode", "tree")
        self.state["tree_pages"] = {}
        self.state.setdefault("tree_page_numbers", {})
        self.state.setdefault("focused_tree_key", None)
        self.state["visible_file_rows"] = []
        self.state["selection_anchor_key"] = None
        self.state["collection_edit"] = None
        self.state["file_page"] = 1
        self.state["file_page_size"] = 50
        self.state["file_total"] = 0
        self.state["file_scroll"] = 0
        # 每个文件的阅读滚动位置：切走再切回时恢复（切片定位优先于旧位置）。
        self.state.setdefault("chunk_scroll_by_file", {})
        self.state.setdefault("chunk_page_by_file", {})
        self._chunk_revision = 0
        self.state["tree_scroll"] = 0
        self.state["sort_key"] = "created_at"
        # 阅读区默认"连续阅读"（像文档），可切到卡片管理
        self.state.setdefault("file_reading_mode", "reading")
        self._list_revision = 0
        # 上传并发控制
        self._upload_semaphore = asyncio.Semaphore(_UPLOAD_CONCURRENCY)
        self._upload_cancelled = False
        self._upload_received = False
        self._upload_collection_ids = []
        self.state["latest_file_tasks"] = {}
        # 切片处理器在本类之后构造，由 set_chunk_handlers 注入
        self.chunk_handlers = None

    def _notify(self, message, **options):
        """树的事件行可能已被刷新；通知始终发送给页面客户端，而非已销毁的行 slot。"""
        owner = self.ui_refs.get("file_catalog") or self.ui_refs.get("upload_input")
        if owner is not None:
            with owner.client:
                ui.notify(message, **options)
        else:
            ui.notify(message, **options)

    def set_chunk_handlers(self, chunk_handlers):
        """注入切片处理器，供切换文件后同步原页对比栏"""
        self.chunk_handlers = chunk_handlers

    async def open_reader(self, file_id: int):
        """加载该文件并切到连续阅读，保留资料表。

        单击资料表行保留当前文档模式；打开阅读由主布局回调切换右侧内容。
        回调由主布局注入，未提供时退化为普通预览。
        """
        self.state["file_reading_mode"] = self.state.get("file_reading_mode") or "reading"
        if file_id != self.state.get("selected_file_id") or not self.state.get("chunks_data"):
            await self.load_chunks(file_id)
        if self.state.get("selected_file_id") != file_id:
            return
        open_reader = self.ui_refs.get("open_reader")
        if open_reader:
            open_reader()

    def open_library(self):
        """展开资料表，保留当前文档模式。回调由主布局注入。"""
        open_library = self.ui_refs.get("open_library")
        if open_library:
            open_library()

    async def load_files(self):
        """刷新组织结构和当前文件页，不把全库文件装入界面。"""
        await self.load_collections(refresh=False)
        await self.load_file_page()
        await self.load_stats()

    async def load_collections(self, refresh: bool = True):
        collections = await run_sync(collection_service.list_collections)
        changed = collections != self.state["collections"]
        self.state["collections"] = collections
        valid_ids = {item["id"] for item in collections}
        if any(cid not in valid_ids for cid in self.state["active_collection_ids"]):
            self.state["active_collection_ids"] = []
            self.state["uncategorized"] = False
            self._reset_file_page()
            self._notify(t("collections.selection_deleted"), type="info")
        expanded = {cid for cid in self.state["expanded_collection_ids"] if int(cid) in valid_ids}
        if changed:
            for item in collections:
                if item["id"] in self.state["active_collection_ids"]:
                    expanded.update(str(part["id"]) for part in item["path"][:-1])
        self.state["expanded_collection_ids"] = sorted(expanded)
        if changed and self.ui_refs.get("collection_tree"):
            self.ui_refs["collection_tree"].refresh()
        if self.ui_refs.get("collection_filter"):
            self.ui_refs["collection_filter"].refresh()
        if refresh:
            await self.load_file_page()

    async def load_file_page(self, *, refresh_tree=True):
        """统一树按分支读取直系文档；平铺/搜索仍复用原有分页，迟到结果不得覆盖新视图。"""
        self._list_revision += 1
        revision = self._list_revision
        size = self.state["file_page_size"]
        sort_key = self.state["sort_key"]
        if is_tree_view(self.state):
            scopes = [row["scope"] for row in visible_tree_rows(self.state) if row["kind"] == "branch"]
            pages = {}
            for scope in scopes:
                key = branch_key(scope)
                page = self.state["tree_page_numbers"].get(key, 1)
                cached = self.state["tree_pages"].get(key)
                if not refresh_tree and cached and cached.get("page") == page and cached.get("sort") == sort_key:
                    pages[key] = cached
                    continue
                options = dict(limit=size, offset=(page - 1) * size,
                               collection_ids=[scope] if scope is not None else None,
                               include_descendants=False, uncategorized=scope is None,
                               sort_by=sort_key, descending=SORT_OPTIONS[sort_key])
                result = await run_sync(file_service.get_files_list_paginated, **options)
                last = max(1, (result["total"] + size - 1) // size)
                if page > last:
                    page = last
                    options["offset"] = (page - 1) * size
                    result = await run_sync(file_service.get_files_list_paginated, **options)
                pages[key] = {**result, "page": page, "sort": sort_key}
            if revision != self._list_revision:
                return
            self.state["tree_pages"] = pages
            self.state["tree_page_numbers"].update({key: value["page"] for key, value in pages.items()})
            visible = [row["file"] for row in visible_tree_rows(self.state) if row["kind"] == "file"]
            result = {"files": list({file["id"]: file for file in visible}.values()), "total": len({file["id"] for file in visible})}
        else:
            page = self.state["file_page"]
            result = await run_sync(file_service.get_files_list_paginated,
                limit=size, offset=(page - 1) * size,
                collection_ids=list(self.state["active_collection_ids"]) or None,
                include_descendants=True if self.state.get("library_mode") == "tree" else self.state["include_descendants"],
                uncategorized=self.state["uncategorized"], name=self.state.get("search_keyword") or None,
                sort_by=sort_key, descending=SORT_OPTIONS[sort_key])
            if revision != self._list_revision:
                return
            last_page = max(1, (result["total"] + size - 1) // size)
            if page > last_page:
                self.state["file_page"] = last_page
                await self.load_file_page()
                return

        files = list(result["files"])
        selected_id = self.state.get("selected_file_id")
        if selected_id is not None and not any(f["id"] == selected_id for f in files):
            selected_file = await run_sync(file_service.get_file_by_id, selected_id)
            if selected_file:
                files.append(selected_file)
        by_file = await run_sync(collection_service.get_collections_by_file, [f["id"] for f in files])
        latest_tasks = await run_sync(task_service.get_latest_file_tasks, [f["id"] for f in files])
        if revision != self._list_revision:
            return
        if self.state.get("selected_file_id") != selected_id:
            await self.load_file_page(refresh_tree=False)
            return
        if selected_id is not None and not any(f["id"] == selected_id for f in files):
            self.state["selected_file_id"] = None
            self.state["chunks_data"] = []
            self.state["source_page"] = None
            for key in ("chunk_inspector", "source_column"):
                if self.ui_refs.get(key):
                    self.ui_refs[key].refresh()
        # 阅读对象不依赖树的展开、分支分页或搜索结果。
        self.state["files_data"] = files
        self.state["filtered_files"] = result["files"]
        self.state["collections_by_file"] = by_file
        self.state["latest_file_tasks"] = latest_tasks
        self.state["file_total"] = result["total"]
        if self.ui_refs.get("selection_status"):
            self.ui_refs["selection_status"]()
        for key in ("file_list_container", "file_pagination", "file_info_panel", "collection_filter",
                    "chunk_toolbar_buttons", "reading_mode_toggle", "reader_title"):
            if key == "file_list_container" and self.state.get("collection_edit"):
                continue
            if self.ui_refs.get(key):
                self.ui_refs[key].refresh()
        if remember := self.ui_refs.get("remember_workspace"):
            remember()

    def _reset_file_page(self):
        self.state["file_page"] = 1
        self.state["file_scroll"] = 0
        if self.ui_refs.get("file_scroll"):
            self.ui_refs["file_scroll"].scroll_to(pixels=0)

    async def on_search_change(self, e):
        self.state["search_keyword"] = e.args or ""
        self._reset_file_page()
        await self.load_file_page()

    async def clear_search(self):
        tree_scroll = self.state.get("tree_scroll", 0)
        self.state["search_keyword"] = ""
        if search := self.ui_refs.get("file_search"):
            search.set_value("")
        self._reset_file_page()
        await self.load_file_page()
        if is_tree_view(self.state):
            self.state["tree_scroll"] = tree_scroll
            if scroll := self.ui_refs.get("file_scroll"):
                scroll.scroll_to(pixels=tree_scroll)

    async def on_sort_change(self, sort_key: str):
        self.state["sort_key"] = sort_key
        self._reset_file_page()
        await self.load_file_page()
        if self.ui_refs.get("list_toolbar"):
            self.ui_refs["list_toolbar"].refresh()

    async def go_to_file_page(self, page: int):
        self.state["file_page"] = max(1, page)
        self.state["file_scroll"] = 0
        if self.ui_refs.get("file_scroll"):
            self.ui_refs["file_scroll"].scroll_to(pixels=0)
        await self.load_file_page()

    # ==================== 集合 ====================

    async def set_library_mode(self, mode):
        if mode not in ("tree", "all", "uncategorized"):
            return False
        if self.state.get("collection_edit"):
            self._notify(t("collections.finish_edit"), type="info")
            return False
        tree_scroll = self.state.get("tree_scroll", 0)
        self.state["library_mode"] = mode
        if mode == "tree":
            self.state["search_keyword"] = ""
            if search := self.ui_refs.get("file_search"):
                search.set_value("")
        self.state["active_collection_ids"] = []
        self.state["uncategorized"] = mode == "uncategorized"
        self.state["focused_tree_key"] = None
        self._reset_file_page()
        await self.load_file_page()
        if mode == "tree":
            self.state["tree_scroll"] = tree_scroll
            if scroll := self.ui_refs.get("file_scroll"):
                scroll.scroll_to(pixels=tree_scroll)
        if self.ui_refs.get("collection_filter"):
            self.ui_refs["collection_filter"].refresh()
        return True

    def select_collection(self, collection_id):
        if self.state.get("collection_edit") or not is_tree_view(self.state):
            return
        self.state["active_collection_ids"] = [collection_id]
        self.state["uncategorized"] = False
        self.state["focused_tree_key"] = f"c:{collection_id}"
        self.refresh_file_selection()
        if self.ui_refs.get("collection_filter"):
            self.ui_refs["collection_filter"].refresh()

    async def toggle_collection(self, collection_id):
        if self.state.get("collection_edit") or not is_tree_view(self.state):
            return
        self.select_collection(collection_id)
        key = str(collection_id)
        expanded = set(self.state["expanded_collection_ids"])
        expanded.symmetric_difference_update({key})
        self.state["expanded_collection_ids"] = sorted(expanded)
        row = self.ui_refs.get("catalog_rows", {}).get(f"c:{collection_id}")
        expander = self.ui_refs.get("collection_expanders", {}).get(collection_id)
        if row is not None and not row.is_deleted:
            row.props(f'aria-expanded={str(key in expanded).lower()} aria-busy=true')
        if expander is not None and not expander.is_deleted:
            expander.props("loading")
        try:
            # 只在分支数据就绪后重绘一次，避免用户刚聚焦子项便被第二次刷新销毁。
            await self.load_file_page(refresh_tree=False)
        finally:
            if expander is not None and not expander.is_deleted:
                expander.props(remove="loading")
            if row is not None and not row.is_deleted:
                row.props(remove="aria-busy")
        self.focus_tree_row(self.state.get("focused_tree_key"))

    async def collapse_all_collections(self):
        if self.state.get("collection_edit") or not is_tree_view(self.state):
            return
        self.state["expanded_collection_ids"] = []
        self.state["active_collection_ids"] = []
        self.state["focused_tree_key"] = None
        self.state["tree_scroll"] = 0
        await self.load_file_page(refresh_tree=False)
        if scroll := self.ui_refs.get("file_scroll"):
            scroll.scroll_to(pixels=0)
        if self.ui_refs.get("collection_filter"):
            self.ui_refs["collection_filter"].refresh()

    async def go_to_tree_page(self, collection_id, page):
        if self.state.get("collection_edit") or not is_tree_view(self.state):
            return
        self.state["tree_page_numbers"][branch_key(collection_id)] = max(1, page)
        await self.load_file_page(refresh_tree=False)
        files = self.state["tree_pages"].get(branch_key(collection_id), {}).get("files", [])
        key = document_key(collection_id, files[0]["id"]) if files else f"c:{collection_id}"
        self.state["focused_tree_key"] = key
        self.refresh_file_selection()
        self.focus_tree_row(key)

    def focus_tree_row(self, key):
        from nicegui import core
        catalog = self.ui_refs.get("file_catalog")
        if self.state.get("focused_tree_key") != key or catalog is None or catalog.is_deleted or core.loop is None:
            return
        catalog.client.run_javascript(f'''requestAnimationFrame(() => {{
            const key = {json.dumps(key)};
            const rows = document.querySelectorAll('[data-catalog="files"] [data-catalog-row]');
            const row = [...rows].find(item => item.dataset.catalogRow === key);
            const active = document.activeElement;
            if (row && (active === document.body || (active?.closest('[data-catalog="files"]') &&
                !active.closest('input, textarea, select, button, a, [role=menuitem]')))) {{
                row.focus({{preventScroll: true}}); row.scrollIntoView({{block: 'nearest'}});
            }}
        }})''')

    async def on_collection_change(self, collection_ids, *, uncategorized=False):
        if self.state.get("collection_edit"):
            self._notify(t("collections.finish_edit"), type="info")
            return
        self.state["search_keyword"] = ""
        if search := self.ui_refs.get("file_search"):
            search.set_value("")
        self.state["active_collection_ids"] = list(collection_ids or [])
        self.state["uncategorized"] = uncategorized
        if uncategorized:
            self.state["library_mode"] = "uncategorized"
        elif not collection_ids:
            self.state["library_mode"] = "tree"
            self.state["focused_tree_key"] = None
        elif collection_ids:
            self.state["library_mode"] = "tree"
            expanded = set(self.state["expanded_collection_ids"])
            for item in self.state["collections"]:
                if item["id"] in collection_ids:
                    expanded.update(str(part["id"]) for part in item["path"])
            self.state["expanded_collection_ids"] = sorted(expanded)
            self.state["focused_tree_key"] = f"c:{collection_ids[0]}"
        self._reset_file_page()
        if self.ui_refs.get("collection_filter"):
            self.ui_refs["collection_filter"].refresh()
        await self.load_file_page()
        if self.state.get("focused_tree_key"):
            self.focus_tree_row(self.state["focused_tree_key"])

    async def on_tree_select(self, e):
        if e.value is None:
            return
        if e.value in ("all", "uncategorized"):
            await self.on_collection_change([], uncategorized=e.value == "uncategorized")
        else:
            await self.on_collection_change([int(e.value)])

    async def on_descendants_change(self, e):
        self.state["include_descendants"] = e.value
        self._reset_file_page()
        await self.load_file_page()

    async def _assign_active_collections(self, file_id: int) -> None:
        """把新建/上传的文件直接归入当前筛选的集合。

        否则在集合视图下上传的文件会因为"未归类"被立刻过滤掉，
        用户既看不到文件也看不到索引进度。
        """
        collection_ids = self.state.get("active_collection_ids") or []
        if not collection_ids:
            return

        await run_sync(
            collection_service.set_file_collections, file_id, list(collection_ids)
        )
        names = [
            item["name"]
            for item in self.state["collections"]
            if item["id"] in set(collection_ids)
        ]
        if names:
            self._notify(
                t("collections.auto_assigned", name=" · ".join(names)), type="info"
            )

    async def begin_collection_edit(self, collection_id=None, *, parent_id=None):
        if self.state.get("collection_edit"):
            self._notify(t("collections.finish_edit"), type="info")
            return
        item = next((item for item in self.state["collections"] if item["id"] == collection_id), None)
        if collection_id is not None and item is None:
            return
        if item:
            parent_id = item["parent_id"]
        self.state["library_mode"] = "tree"
        self.state["search_keyword"] = ""
        self.state["uncategorized"] = False
        expanded = set(self.state["expanded_collection_ids"])
        parent = next((item for item in self.state["collections"] if item["id"] == parent_id), None)
        if parent:
            expanded.update(str(part["id"]) for part in parent["path"])
        self.state["expanded_collection_ids"] = sorted(expanded)
        self.state["collection_edit"] = {"id": collection_id, "parent_id": parent_id,
                                         "name": item["name"] if item else "", "error": "", "busy": False}
        await self.load_file_page(refresh_tree=False)
        for key in ("file_list_container", "list_toolbar", "collection_filter"):
            if self.ui_refs.get(key):
                self.ui_refs[key].refresh()
        ui.run_javascript('''requestAnimationFrame(() => {
            const input = document.querySelector('[data-collection-editor] input');
            input?.focus(); input?.select();
        })''')

    async def commit_collection_edit(self, name=None, *, expected=None):
        edit = self.state.get("collection_edit")
        if not edit or edit["busy"] or (expected is not None and edit is not expected):
            return
        edit["name"] = edit["name"] if name is None else name
        edit["error"] = ""
        edit["busy"] = True
        for element in self.ui_refs.get("collection_edit_controls", []):
            if not element.is_deleted:
                element.disable()
        try:
            if edit["id"] is None:
                result = await run_sync(collection_service.create_collection, edit["name"], parent_id=edit["parent_id"])
            else:
                result = await run_sync(collection_service.rename_collection, edit["id"], edit["name"])
            if not result["success"]:
                edit["error"] = result["message"]
                return
            collection_id = edit["id"] if edit["id"] is not None else result["collection_id"]
        except ValueError as exc:
            edit["error"] = str(exc)
            return
        except Exception as exc:
            logger.warning("资料集名称保存失败 (%s)", type(exc).__name__)
            edit["error"] = t("collections.save_failed")
            return
        finally:
            edit["busy"] = False
            error = self.ui_refs.get("collection_edit_error")
            if error is not None and not error.is_deleted:
                error.set_text(edit["error"])
            for element in self.ui_refs.get("collection_edit_controls", []):
                if not element.is_deleted:
                    element.enable()
        self.state["collection_edit"] = None
        self.state["active_collection_ids"] = [collection_id]
        self.state["focused_tree_key"] = f"c:{collection_id}"
        if self.ui_refs.get("list_toolbar"):
            self.ui_refs["list_toolbar"].refresh()
        await self.load_files()
        self.focus_tree_row(f"c:{collection_id}")

    def cancel_collection_edit(self):
        edit = self.state.get("collection_edit")
        if edit and not edit["busy"]:
            self.state["collection_edit"] = None
            for key in ("file_list_container", "list_toolbar", "collection_filter"):
                if self.ui_refs.get(key):
                    self.ui_refs[key].refresh()

    async def confirm_delete_collection(self, collection_id):
        impact = await self._preview_delete_collection(collection_id)
        if impact:
            confirm_dialog(t("collections.delete"),
                t("collections.delete_preview", count=impact["direct_file_count"], unclassified=impact["unclassified_file_count"]),
                on_confirm=lambda: self._delete_collection(collection_id), danger=True,
                confirm_text=t("confirm_dialog.btn_delete"))

    def handle_move_collection(self, collection_id):
        from nicegui import context
        item = next((item for item in self.state["collections"] if item["id"] == collection_id), None)
        if not item:
            return
        options = {"root": t("collections.root")}
        options.update({str(candidate["id"]): collection_path_label(candidate) for candidate in self.state["collections"]
                        if collection_id not in [part["id"] for part in candidate["path"]]})
        with ui.dialog() as dialog, ui.card().classes("w-[400px] max-w-full theme-card"):
            ui.label(t("collections.move")).classes("text-base font-semibold")
            ui.label(collection_path_label(item)).classes("text-sm theme-text-secondary")
            parent = ui.select(options, value=str(item["parent_id"]) if item["parent_id"] else "root",
                               label=t("collections.parent"), with_input=True).classes("w-full")
            busy = False
            async def save():
                nonlocal busy
                if busy:
                    return
                busy = True
                try:
                    destination = parent.value
                    if destination is None:
                        self._notify(t("collections.choose_target"), type="warning")
                        return
                    await run_sync(collection_service.move_collection, collection_id,
                                   None if destination == "root" else int(destination))
                except (TypeError, ValueError) as exc:
                    self._notify(str(exc), type="warning")
                else:
                    dialog.close()
                    await self.load_files()
                finally:
                    busy = False
            with ui.row().classes("w-full justify-end"):
                ui.button(t("confirm_dialog.btn_cancel"), on_click=dialog.close).props("flat")
                ui.button(t("chunk_dialog.btn_save"), on_click=save)
        dialog.move(context.client.content)
        dialog.open()

    def handle_manage_collections(self):
        """打开集合管理对话框"""
        collection_manage_dialog(
            collections=self.state["collections"],
            on_create=self._create_collection,
            on_rename=self._rename_collection,
            on_move=self._move_collection,
            on_delete=self._delete_collection,
            on_preview_delete=self._preview_delete_collection,
            selected_id=next(iter(self.state["active_collection_ids"]), None),
        )

    async def _create_collection(self, name: str, parent_id: int | None = None) -> list:
        """新建集合，归类对话框按名新建时仍创建根集合。"""
        try:
            result = await run_sync(collection_service.create_collection, name, parent_id=parent_id)
            if not result["success"]:
                self._notify(result["message"], type="warning")
        except ValueError as exc:
            self._notify(str(exc), type="warning")
        await self.load_collections()
        return self.state["collections"]

    async def _rename_collection(self, collection_id: int, name: str) -> list:
        """重命名集合，返回刷新后的集合列表"""
        result = await run_sync(
            collection_service.rename_collection, collection_id, name
        )
        if not result["success"]:
            self._notify(result["message"], type="warning")
        await self.load_collections()
        return self.state["collections"]

    async def _move_collection(self, collection_id: int, parent_id: int | None) -> list:
        try:
            result = await run_sync(collection_service.move_collection, collection_id, parent_id)
            if not result["success"]:
                self._notify(result["message"], type="warning")
        except ValueError as exc:
            self._notify(str(exc), type="warning")
        await self.load_collections()
        return self.state["collections"]

    async def _preview_delete_collection(self, collection_id: int):
        try:
            return await run_sync(maintenance_service.delete_collection, collection_id, dry_run=True)
        except ValueError as exc:
            self._notify(str(exc), type="warning")
            return None

    async def _delete_collection(self, collection_id: int) -> list:
        """确认后重新校验，不使用过期预览作为删除依据。"""
        try:
            await run_sync(maintenance_service.delete_collection, collection_id, confirmed=True)
            self._notify(t("collections.deleted"), type="positive")
        except ValueError as exc:
            self._notify(str(exc), type="warning")
        await self.load_collections()
        return self.state["collections"]

    async def handle_locate_file(self, file_id: int):
        """一份文件可有多条直接归属，不能替用户猜一个文件夹。"""
        from nicegui import context
        from app.ui.components import collection_path_label

        collections = await run_sync(collection_service.get_file_collections, file_id)
        if not collections:
            await self.on_collection_change([], uncategorized=True)
            return

        async def locate(collection_id):
            item = next((c for c in self.state["collections"] if c["id"] == collection_id), None)
            if item:
                expanded = set(self.state["expanded_collection_ids"])
                expanded.update(str(part["id"]) for part in item["path"][:-1])
                self.state["expanded_collection_ids"] = sorted(expanded)
                if self.ui_refs.get("collection_tree"):
                    self.ui_refs["collection_tree"].refresh()
            await self.on_collection_change([collection_id])

        if len(collections) == 1:
            await locate(collections[0]["id"])
            return
        by_id = {c["id"]: c for c in self.state["collections"]}
        with ui.dialog() as dialog, ui.card().classes("w-[440px] max-w-full theme-card"):
            ui.label(t("files.locate_title")).classes("text-base font-semibold theme-text")
            for collection in collections:
                async def choose(cid=collection["id"]):
                    await locate(cid)
                    dialog.close()
                ui.button(collection_path_label(by_id.get(collection["id"], collection)), on_click=choose).props("flat no-caps").classes("w-full")
            ui.button(t("confirm_dialog.btn_cancel"), on_click=dialog.close).props("flat")
        dialog.move(context.client.content)
        dialog.open()

    async def handle_edit_file_collections(self, file_id: int):
        """打开"归入集合"对话框"""
        file_info = next(
            (f for f in self.state["files_data"] if f["id"] == file_id), None
        )
        if not file_info:
            return

        selected = await run_sync(
            collection_service.get_file_collections, file_id
        )
        file_collections_dialog(
            filename=file_info["filename"],
            collections=self.state["collections"],
            selected_ids={item["id"] for item in selected},
            on_save=lambda ids: self._save_file_collections(file_id, ids),
            on_create=self._create_collection,
        )

    async def _save_file_collections(self, file_id: int, collection_ids: list):
        """保存文件的集合归属"""
        await run_sync(
            collection_service.set_file_collections, file_id, collection_ids
        )
        await self.load_collections()
        if self.ui_refs.get("file_info_panel"):
            self.ui_refs["file_info_panel"].refresh()
        self._notify(t("collections.assigned"), type="positive")

    def handle_add_file_collections(self, file_id):
        self._show_collection_assignment([file_id], mode="add")

    def handle_batch_collections(self):
        """常用批量归类只追加，保留每份文档原有的其它归属。"""
        self._show_collection_assignment(list(self.state["batch_selected_ids"]), mode="add")

    def handle_replace_batch_collections(self):
        self._show_collection_assignment(list(self.state["batch_selected_ids"]), mode="replace")

    def _show_collection_assignment(self, file_ids, *, mode):
        if not file_ids:
            self._notify(t("files.batch_none_selected"), type="warning")
            return
        snapshot = tuple(file_ids)
        file_collections_dialog(
            filename=t("collections.batch_target", count=len(snapshot)),
            collections=self.state["collections"], selected_ids=set(),
            on_save=lambda ids: self._save_batch_collections(ids, file_ids=snapshot, mode=mode),
            on_create=self._create_collection, batch=True, append=mode == "add",
        )

    async def _save_batch_collections(self, collection_ids: list, *, file_ids=None, mode="add"):
        snapshot = list(file_ids if file_ids is not None else self.state["batch_selected_ids"])
        if mode == "add" and not collection_ids:
            self._notify(t("collections.choose_target"), type="warning")
            return False
        return await self._change_memberships(snapshot, collection_ids, mode=mode)

    async def _change_memberships(self, file_ids, collection_ids, *, mode):
        try:
            await run_sync(collection_service.update_file_collections, list(file_ids), list(collection_ids), mode=mode)
        except ValueError as exc:
            self._notify(str(exc), type="warning")
            return False
        if self.state.get("batch_mode"):
            self.exit_batch_mode()
        await self.load_files()
        key = {"add": "collections.added", "remove": "collections.removed", "replace": "collections.batch_assigned"}[mode]
        self._notify(t(key, count=len(set(file_ids))), type="positive")
        return True

    async def remove_from_collection(self, file_id, collection_id):
        await self._change_memberships([file_id], [collection_id], mode="remove")

    async def handle_document_drop(self, event):
        data = event.args if isinstance(event.args, dict) else {}
        if data.get("client_id") != event.client.id:
            self._notify(t("collections.drop_other_window"), type="warning")
            return
        file_ids = data.get("file_ids")
        if not isinstance(file_ids, list) or not file_ids or any(type(fid) is not int or fid <= 0 for fid in file_ids):
            return
        collection_id = data.get("collection_id")
        if type(collection_id) is not int or collection_id <= 0:
            return
        await self._change_memberships(file_ids, [collection_id], mode="add")

    # ==================== 单个文件删除 ====================

    async def confirm_delete_file(self, file_id: int):
        """确认删除单个文件并展示知识引用保留范围。"""
        from indexing.services.maintenance_service import delete_files
        try:
            impact = await run_sync(delete_files, [file_id], dry_run=True)
        except ValueError as exc:
            self._notify(str(exc), type="negative")
            return

        confirm_dialog(
            title=t("files.delete_confirm_title"),
            message=t("files.delete_confirm_msg", filename=impact["filenames"][0]) + "\n\n" + t(
                "files.knowledge_delete_warning", count=impact["knowledge_evidence_count"]),
            on_confirm=lambda: self._do_delete_file(file_id),
            confirm_text=t("confirm_dialog.btn_delete"),
            danger=True,
        )

    async def _do_delete_file(self, file_id: int):
        """执行删除单个文件"""
        from indexing.services.maintenance_service import delete_files
        try:
            result = await run_sync(delete_files, [file_id], confirmed=True)
        except ValueError as exc:
            self._notify(str(exc), type="negative")
            return
        if file_id not in result["deleted_file_ids"]:
            self._notify(t("files.delete_failed"), type="negative")
            return

        if self.state["selected_file_id"] == file_id:
            self.state["selected_file_id"] = None
            self.state["chunks_data"] = []
            if self.ui_refs.get("chunk_inspector"):
                self.ui_refs["chunk_inspector"].refresh()
            if self.ui_refs.get("file_info_panel"):
                self.ui_refs["file_info_panel"].refresh()

        await self.load_files()
        self._notify(t("files.deleted"), type="positive")

    # ==================== 文件属性 ====================

    async def handle_edit_properties(self, file_id: int):
        """打开文件属性编辑对话框"""
        file_info = next(
            (f for f in self.state["files_data"] if f["id"] == file_id), None
        )
        if not file_info:
            return

        file_properties_dialog(
            filename=file_info["filename"],
            properties=metadata_service.decode_metadata(file_info.get("metadata")),
            on_save=lambda properties: self._save_properties(file_id, properties),
        )

    async def _save_properties(self, file_id: int, properties: dict):
        """保存文件属性"""
        await run_sync(
            metadata_service.save_file_metadata, file_id, properties
        )
        await self.load_files()
        if self.ui_refs.get("file_info_panel"):
            self.ui_refs["file_info_panel"].refresh()
        self._notify(t("properties.saved"), type="positive")

    def get_export_file_ids(self):
        if self.state["batch_mode"]:
            return sorted(self.state["batch_selected_ids"])
        file_id = self.state.get("selected_file_id")
        focused = self.state.get("focused_tree_key") or ""
        return [file_id] if file_id is not None and not focused.startswith("c:") else []

    async def handle_export_files(self, format: str, file_ids=None):
        """固定本次选择；单文档沿用原下载地址，多文档只发起一次 ZIP 下载。"""
        file_ids = sorted(set(self.get_export_file_ids() if file_ids is None else file_ids))
        if not file_ids:
            self._notify(t("files.export_select_first"), type="warning")
            return
        if len(file_ids) > file_service.MAX_EXPORT_FILES:
            self._notify(t("files.export_limit", count=file_service.MAX_EXPORT_FILES), type="warning")
            return
        if self.state["file_exporting"]:
            return

        def prepare():
            ready, skipped = [], 0
            for file_id in file_ids:
                file_info = file_service.get_file_by_id(file_id)
                if format == "original" and file_info and not file_info.get("original_file_path"):
                    skipped += 1
                    continue
                try:
                    file_service.export_file(file_id, format)
                except ValueError as exc:
                    name = file_info["filename"] if file_info else str(file_id)
                    raise ValueError(f"{name}: {exc}") from exc
                ready.append(file_id)
            return ready, skipped

        self.state["file_exporting"] = True
        if update_status := self.ui_refs.get("selection_status"):
            update_status()
        try:
            ready, skipped = await run_sync(prepare)
            if not ready:
                self._notify(t("files.export_no_originals"), type="warning")
                return
            if skipped:
                self._notify(t("files.export_originals_skipped", count=skipped), type="warning")
            if len(file_ids) == 1:
                url = f"/gui/file/{ready[0]}/content?format={format}"
            else:
                url = "/gui/files/content?" + urlencode({"file_ids": ready, "format": format}, doseq=True)
            ui.download.from_url(url)
        except (ValueError, OSError) as exc:
            self._notify(t("files.export_failed", error=str(exc)), type="negative")
        finally:
            self.state["file_exporting"] = False
            if update_status := self.ui_refs.get("selection_status"):
                update_status()

    async def handle_reindex_file(self, file_id: int):
        """重新索引文件。

        换了 PDF 解析后端、Office 转换后端或分块参数之后，已入库的切片仍是
        旧配置的产物，需要重跑一遍才能生效。
        """
        file_info = next(
            (f for f in self.state["files_data"] if f["id"] == file_id), None
        )
        if not file_info:
            self._notify(t("files.original_missing"), type="warning")
            return

        # 同一文件两个任务并发会互相清空对方写入的切片
        active = await run_sync(task_service.get_active_tasks)
        if any(task.get("file_id") == file_id for task in active):
            self._notify(t("files.reindex_busy"), type="warning")
            return

        has_original = bool(file_info.get("original_file_path"))
        confirm_dialog(
            title=t("files.reindex_confirm_title"),
            message=t(
                # 有原件时从原件重新解析，UI 里编辑过的切片会被覆盖；
                # 应用内新建的文件解析的是工作文件本身，内容不会变
                "files.reindex_confirm_msg"
                if has_original
                else "files.reindex_confirm_msg_note",
                filename=file_info["filename"],
            ),
            on_confirm=lambda: self._do_reindex_file(file_id, file_info["filename"]),
            confirm_text=t("files.reindex"),
        )

    async def _do_reindex_file(self, file_id: int, filename: str):
        """执行重新索引"""
        try:
            await run_sync(file_service.reindex_file, file_id)
        except ValueError as exc:
            self._notify(str(exc), type="negative")
            return
        self._notify(t("files.reindex_started"), type="positive")
        await self.load_files()

    async def handle_cancel_task(self, task_id: int):
        try:
            task = await run_sync(task_service.cancel_task, task_id)
        except Exception as exc:
            logger.error("取消任务失败 (%s)", type(exc).__name__)
            self._notify(t("task.action_failed"), type="negative")
            return
        key = "task.cancelling" if task["status"] in task_service.ACTIVE_STATUSES else (
            "task.cancelled" if task["status"] == "cancelled" else "task.already_finished")
        self._notify(t(key), type="info")
        await self.load_files()

    async def handle_retry_task(self, task_id: int):
        try:
            await run_sync(task_service.retry_task, task_id)
        except ValueError as exc:
            self._notify(str(exc), type="warning")
            return
        except Exception as exc:
            logger.error("重试任务失败 (%s)", type(exc).__name__)
            self._notify(t("task.action_failed"), type="negative")
            return
        self._notify(t("files.reindex_started"), type="positive")
        await self.load_files()

    # ==================== 知识卡片 ====================

    async def handle_create_card(self):
        """打开新建知识卡片对话框"""
        note_files = await run_sync(file_service.get_note_files)
        card_dialog(
            note_files=note_files,
            default_box_name=t("cards.default_box"),
            on_save=self._save_card,
        )

    async def _save_card(
        self, file_id, new_box_name: str, doc_title: str, chunk_text: str
    ):
        """把卡片写入卡片盒（应用内笔记文件），走与新增切片相同的索引流程"""
        doc_title = (doc_title or "").strip()
        chunk_text = (chunk_text or "").strip()
        if not doc_title or not chunk_text:
            self._notify(t("chunks.title_content_required"), type="warning")
            return

        try:
            if file_id is None:
                created = await run_sync(
                    file_service.create_empty_file, new_box_name or t("cards.default_box")
                )
                file_id = created["file_id"]
                # 新建的卡片盒跟随当前集合视图
                await self._assign_active_collections(file_id)

            await run_sync(
                chunk_service.create_chunk_add_task,
                file_id=file_id,
                doc_title=doc_title,
                chunk_text=chunk_text,
            )
        except ValueError as exc:
            self._notify(str(exc), type="negative")
            return

        self._notify(t("cards.created"), type="positive")
        await self.load_files()
        await self.load_chunks(file_id)

    async def load_stats(self):
        """加载统计信息（异步）"""
        stats = await run_sync(file_service.get_storage_stats)
        total = stats["total_files"]
        indexed = stats["indexed_files"]
        size_str = format_size(stats["total_size"])

        self.state["stats_text"] = t("stats.indexed", size=size_str, indexed=indexed, total=total)
        if self.ui_refs.get("stats_label"):
            self.ui_refs["stats_label"].set_text(self.state["stats_text"])
            self.ui_refs["stats_label"].update()

    async def load_chunks(self, file_id: int, *, chunk_id=None):
        """加载选中文件的切片，可定位到指定卡片所在页。"""
        # 1. 在异步查询前保存页码和滚动，迟到的同文件请求也不能回写。
        self._chunk_revision += 1
        revision = self._chunk_revision
        previous_id = self.state.get("selected_file_id")
        positions = self.state["chunk_scroll_by_file"]
        pages = self.state["chunk_page_by_file"]
        if previous_id is not None:
            positions[previous_id] = self.state.get("chunk_scroll", 0)
            pages[previous_id] = self.state.get("chunk_page", 1)
        target_scroll = 0 if chunk_id is not None else positions.get(file_id, 0)
        target_page = 1 if chunk_id is not None else pages.get(file_id, 1)
        self.state["chunk_scroll"] = target_scroll
        self.state["chunk_page"] = target_page
        if self.chunk_handlers:
            self.chunk_handlers.invalidate_source_requests()
        self.state["selected_file_id"] = file_id
        # 新建文件或切换分页后，阅读对象可能不在当前结果页。
        if not any(f["id"] == file_id for f in self.state["files_data"]):
            file_info = await run_sync(file_service.get_file_by_id, file_id)
            collections = await run_sync(collection_service.get_file_collections, file_id)
            if self.state["selected_file_id"] != file_id or revision != self._chunk_revision:
                return
            if not file_info:
                self.state["selected_file_id"] = None
                return
            self.state["files_data"] = list(self.state["filtered_files"]) + [file_info]
            self.state["collections_by_file"][file_id] = [c["name"] for c in collections]

        # 文件选择立即保存；滚动位置仍由布局层合并写入。
        if remember := self.ui_refs.get("remember_workspace"):
            remember()

        # 2. 清空旧数据，保留本次要恢复的阅读位置。
        self.state["chunks_data"] = []
        self.state["total_chunks"] = 0
        self.state["total_chunk_pages"] = 1
        # 对比栏里的原页属于上一个文件，先清空，切片加载完再按新文件对齐
        self.state["source_page"] = None

        # 3. 原位更新行选中态，避免重建列表打断双击与键盘焦点。
        if not self.state.get("batch_mode"):
            self.state["focused_file_id"] = file_id
            self.refresh_file_selection()
        if self.ui_refs.get("file_info_panel"):
            self.ui_refs["file_info_panel"].refresh()
        # 阅读栏的重新索引等操作绑定当前文件，换文件时同步更新。
        if self.ui_refs.get("chunk_toolbar_buttons"):
            self.ui_refs["chunk_toolbar_buttons"].refresh()
        if self.ui_refs.get("chunk_inspector"):
            self.ui_refs["chunk_inspector"].refresh()
        if self.ui_refs.get("reading_mode_toggle"):
            self.ui_refs["reading_mode_toggle"].refresh()
        if self.ui_refs.get("reader_title"):
            self.ui_refs["reader_title"].refresh()
        if self.ui_refs.get("source_column"):
            self.ui_refs["source_column"].refresh()

        # 4. 加载上次阅读页，或引用卡片所在页；不按标题重定位。
        result = await run_sync(
            file_service.get_chunks_paginated,
            file_id,
            page=target_page,
            page_size=self.state["chunk_page_size"],
            **({"chunk_id": chunk_id} if chunk_id is not None else {}),
        )

        # 5. 更新状态，忽略迟到的查询（包括 A → B → A）。
        if self.state["selected_file_id"] != file_id or revision != self._chunk_revision:
            return
        if result:
            if chunk_id is None and result["page"] != target_page:
                target_scroll = 0
            self.state["chunk_page"] = result["page"]
            self.state["chunks_data"] = result["chunks"]
            self.state["total_chunks"] = result["total"]
            self.state["total_chunk_pages"] = result["total_pages"]
        else:
            self.state["chunks_data"] = []
            self.state["total_chunks"] = 0
            self.state["total_chunk_pages"] = 1

        # 6. 刷新 UI（显示数据），随后把滚动条放回该文件上次的阅读位置。
        self.state["chunk_scroll"] = target_scroll
        if self.ui_refs.get("chunk_inspector"):
            self.ui_refs["chunk_inspector"].refresh()
        scroll = self.ui_refs.get("chunk_scroll")
        if chunk_id is None and target_scroll and scroll is not None and not scroll.is_deleted:
            scroll.scroll_to(pixels=target_scroll)

        # 7. 展开着的对比栏跟到新文件（不是 PDF 则由该方法收起）
        if self.chunk_handlers:
            await self.chunk_handlers.sync_source_page()

    def set_upload_input(self, upload_input):
        """每批使用新组件身份，旧传输的迟到回调不能影响后续导入。"""
        self.ui_refs["upload_input"] = upload_input
        self._upload_cancelled = False
        self._upload_received = False
        self._upload_collection_ids = []

    def refresh_upload_limit(self):
        """解析配置可即时生效；只更新空闲控件的限制，不重建在途批次。"""
        upload = self.ui_refs.get("upload_input")
        if upload is None or upload.is_deleted or self.state.get("uploading_count"):
            return
        limit = get_max_file_size()
        if upload._props.get("max-file-size") != limit:
            upload._props["max-file-size"] = limit
            upload.update()

    async def open_file_picker(self):
        """一次接收一批；更换组件身份，取消后迟到的回调不能污染下一批。"""
        if self.state.get("uploading_count"):
            self._notify(t("files.upload_busy"), type="warning")
            return
        control = self.ui_refs.get("upload_control")
        if control:
            await control.refresh()
        upload_input = self.ui_refs.get("upload_input")
        if upload_input:
            self._upload_cancelled = False
            self._upload_received = False
            self._upload_collection_ids = list(self.state.get("active_collection_ids") or [])
            upload_input.run_method("pickFiles")

    def _upload_stopped(self, sender):
        return self._upload_cancelled or sender is not self.ui_refs.get("upload_input")

    def on_upload_added(self, e):
        if self._upload_stopped(e.sender) or self._upload_received or self.state.get("uploading_count"):
            return
        data = e.args if isinstance(e.args, dict) else {"count": e.args}
        target = data.get("collection_id")
        if target is not None:
            if target in ("all", "uncategorized"):
                self._upload_collection_ids = []
            else:
                try:
                    collection_id = int(target)
                    if collection_id not in {item["id"] for item in self.state["collections"]}:
                        raise ValueError
                except (TypeError, ValueError):
                    self._upload_cancelled = True
                    e.sender.reset()
                    if control := self.ui_refs.get("upload_control"):
                        control.refresh()
                    self._notify(t("files.drop_unavailable"), type="warning")
                    return
                self._upload_collection_ids = [collection_id]
        self.state["upload_target_name"] = " · ".join(
            collection_path_label(item) for item in self.state["collections"]
            if item["id"] in self._upload_collection_ids
        ) or t("files.drop_library")
        self.state["uploading_count"] = data["count"]
        self.state["uploading_files"] = list(data.get("files", []))
        self._refresh_upload_banner()
        # 先固定本批归属再启动 HTTP，不能依赖 added 与上传完成回调的到达顺序。
        e.sender.run_method("upload")

    def _refresh_upload_banner(self):
        banner = self.ui_refs.get("upload_banner")
        if banner:
            banner.refresh()
        if refresh := self.ui_refs.get("refresh_upload_activity"):
            refresh()

    def cancel_upload(self):
        """中止传输和未受理文件；已受理的任务在文件列表中单独取消。"""
        self._upload_cancelled = True
        upload_input = self.ui_refs.get("upload_input")
        if upload_input:
            upload_input.run_method("abort")
            upload_input.reset()
        self.state["uploading_count"] = 0
        self.state["uploading_files"] = []
        self._refresh_upload_banner()
        if control := self.ui_refs.get("upload_control"):
            control.refresh()
        self._notify(t("files.upload_cancelled"), type="info")

    def on_upload_failed(self, e):
        # QUploader 的 abort 同样触发 failed，不把主动取消误报成网络错误。
        if self._upload_stopped(e.sender):
            return
        self._upload_cancelled = True
        e.sender.reset()
        self.state["uploading_count"] = 0
        self.state["uploading_files"] = []
        self._refresh_upload_banner()
        if control := self.ui_refs.get("upload_control"):
            control.refresh()
        self._notify(t("files.upload_failed"), type="negative", timeout=0, close_button=True)

    async def handle_multi_upload(self, e: events.MultiUploadEventArguments):
        """NiceGUI 的批次回调不等待逐文件回调，由这里统一等待落盘和受理。"""
        if self._upload_stopped(e.sender):
            return
        self._upload_received = True
        collection_ids = self._upload_collection_ids[:]
        self.state["uploading_count"] = len(e.files)
        self.state["uploading_files"] = [file.name for file in e.files]
        self._refresh_upload_banner()

        async def process(file):
            # gather 子任务不继承 slot；绑定原客户端，上传控件取消后可能已被重建。
            with e.sender.client:
                try:
                    async with self._upload_semaphore:
                        if not self._upload_stopped(e.sender):
                            await self._process_single_upload(file, e.sender, collection_ids)
                finally:
                    if not self._upload_stopped(e.sender):
                        self.state["uploading_count"] = max(0, self.state["uploading_count"] - 1)
                        if file.name in self.state["uploading_files"]:
                            self.state["uploading_files"].remove(file.name)
                        self._refresh_upload_banner()

        await asyncio.gather(*(process(file) for file in e.files))
        if e.sender is self.ui_refs.get("upload_input"):
            e.sender.run_method("removeUploadedFiles")
        try:
            await self.load_files()
        except Exception as exc:
            logger.error("上传后刷新失败 (%s)", type(exc).__name__)
            self._notify(t("files.upload_refresh_failed"), type="warning")
        if e.sender is self.ui_refs.get("upload_input") and (control := self.ui_refs.get("upload_control")):
            await control.refresh()

    async def _process_single_upload(self, file, sender, collection_ids):
        """保存失败、导入失败和取消均收尾临时文件，不中断同批其他文件。"""
        import os
        import tempfile
        from pathlib import Path

        path = None
        try:
            descriptor, temp_name = tempfile.mkstemp(suffix=Path(file.name).suffix)
            path = Path(temp_name)
            os.close(descriptor)
            await await_completion(file.save(path))
            if self._upload_stopped(sender):
                return
            result = await run_sync(file_service.import_file, path, file.name, collection_ids)
            if self._upload_stopped(sender):
                # 同步受理不能强杀；等它返回，只取消本次新建任务，绝不取消查重命中的任务。
                if not result["duplicate"]:
                    await run_sync(task_service.cancel_task, result["task_id"])
                return
        except Exception as exc:
            logger.error("文件上传受理失败 (%s)", type(exc).__name__)
            reason = str(exc) if isinstance(exc, (ValueError, OSError)) else t("files.upload_error_generic")
            self._notify(t("files.upload_file_failed", filename=file.name, error=reason),
                      type="negative", timeout=0, close_button=True)
        else:
            self._notify(t("files.upload_exists" if result["duplicate"] else "files.upload_processing", filename=file.name),
                      type="warning" if result["duplicate"] else "positive")
        finally:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("上传临时文件清理失败 (%s)", type(exc).__name__)

    def on_upload_rejected(self, e):
        """显示逐文件拒绝原因；筛选出的其余文件仍可继续导入。"""
        entries = getattr(e, "args", None)
        if isinstance(entries, list) and entries:
            reasons = {
                "accept": t("files.reject_format"),
                "max-file-size": t("files.reject_size", size=format_size(get_max_file_size())),
                "max-total-size": t("files.reject_total", size=format_size(MAX_TOTAL_UPLOAD_SIZE)),
                "max-files": t("files.reject_count", count=MAX_UPLOAD_FILES),
                "duplicate": t("files.reject_duplicate"),
                "directory": t("files.reject_directory"),
            }
            message = "\n".join(t("files.upload_file_failed", filename=entry["name"],
                error=reasons.get(entry.get("reason"), t("files.upload_error_generic"))) for entry in entries)
            self._notify(message, type="negative", timeout=0, close_button=True)
            return
        self._notify(
            t(
                "files.upload_rejected",
                max_files=MAX_UPLOAD_FILES,
                max_size=get_max_file_size() // (1024 * 1024),
                max_total=MAX_TOTAL_UPLOAD_SIZE // (1024 * 1024 * 1024),
            ),
            type="negative",
        )

    def handle_create_file(self):
        """处理新建文件"""
        file_create_dialog(
            on_create=self._do_create_file,
        )

    # ==================== 外部知识库一次性导入 ====================

    def handle_import_folder(self):
        """导入本机目录（Obsidian vault 等），规则与 CLI file import 相同。"""
        folder_import_dialog(on_preview=self._preview_folder, on_import=self._import_folder)

    async def _preview_folder(self, options: dict) -> dict:
        candidates, skipped, excluded = await run_sync(
            import_candidates, [options["path"]], options["recursive"],
            exclude=options["exclude"], skip_link_notes=options["skip_link_notes"],
        )
        return {"candidates": candidates, "skipped": skipped, "excluded": excluded}

    async def _import_folder(self, options: dict):
        preview = await self._preview_folder(options)
        collection_ids = list(self.state.get("active_collection_ids") or [])
        accepted = duplicates = failed = 0
        for path in preview["candidates"]:
            try:
                result = await run_sync(file_service.import_file, path, None, collection_ids)
            except (ValueError, OSError):
                failed += 1
                continue
            if result["duplicate"]:
                duplicates += 1
            else:
                accepted += 1
        self._notify(t("import.folder_done", accepted=accepted, duplicates=duplicates,
                    failed=failed, excluded=len(preview["excluded"]) + len(preview["skipped"])),
                  type="positive" if accepted else "warning")
        await self.load_files()

    def handle_import_zotero(self):
        """从本机运行中的 Zotero 导入带 PDF 的条目。"""
        zotero_import_dialog(on_preview=self._preview_zotero, on_import=self._import_zotero)

    async def _preview_zotero(self, collection_keys, mode):
        return await run_sync(zotero_service.preview_import, collection_keys, mode)

    async def _import_zotero(self, collection_keys, mode):
        try:
            result = await run_sync(zotero_service.import_library, collection_keys, mode)
        except ValueError as exc:
            self._notify(str(exc), type="negative")
            return
        self._notify(t("import.zotero_done", accepted=result["accepted_count"], duplicates=result["duplicate_count"],
                    failed=result["failed_count"], skipped=result["skipped_count"]),
                  type="positive" if result["accepted_count"] else "warning")
        await self.load_collections()
        await self.load_files()

    async def _do_create_file(self, filename: str):
        """执行创建文件"""
        if not filename or not filename.strip():
            self._notify(t("files.create_name_empty"), type="warning")
            return

        try:
            result = await run_sync(file_service.create_empty_file, filename)
            self._notify(t("files.created", filename=result["filename"]), type="positive")

            # 与上传一致：集合视图下新建的文件直接归入该集合
            await self._assign_active_collections(result["file_id"])

            # 刷新文件列表
            await self.load_files()

            # 自动选中新创建的文件
            await self.load_chunks(result["file_id"])

        except ValueError as e:
            self._notify(str(e), type="negative")
        except Exception as e:
            self._notify(t("files.create_failed", error=str(e)), type="negative")

    # ==================== 文件选择与批量操作 ====================

    async def select_file(self, file_id: int, *, additive=False, extend=False, row_key=None, collection_id=None):
        """行身份包含所在分支；阅读与批量操作始终按文档本体 ID 去重。"""
        previous_key = self.state.get("focused_tree_key")
        self.state["focused_file_id"] = file_id
        if row_key is not None:
            self.state["focused_tree_key"] = row_key
            if is_tree_view(self.state):
                self.state["active_collection_ids"] = [collection_id] if collection_id is not None else []
                if self.ui_refs.get("collection_filter"):
                    self.ui_refs["collection_filter"].refresh()
        if additive or extend:
            if not self.state["batch_mode"]:
                self.state["batch_mode"] = True
                current = self.state.get("selected_file_id")
                self.state["batch_selected_ids"] = {current} if current is not None else set()
                self.state["selection_anchor"] = current
                self.state["selection_anchor_key"] = previous_key
                self._refresh_batch_layout(file_id)
            self.toggle_file_selection(file_id, extend=extend, row_key=row_key)
        elif self.state["batch_mode"]:
            self.toggle_file_selection(file_id, row_key=row_key)
        else:
            self.state["selection_anchor"] = file_id
            self.state["selection_anchor_key"] = row_key
            if file_id != self.state.get("selected_file_id"):
                await self.load_chunks(file_id)
            self.refresh_file_selection()

    def _refresh_batch_layout(self, focus_file_id=None):
        if self.state.get("collection_edit"):
            return
        if self.ui_refs.get("toolbar_buttons"):
            self.ui_refs["toolbar_buttons"].refresh()
        if self.ui_refs.get("file_list_container"):
            self.ui_refs["file_list_container"].refresh()
        if focus_file_id is not None and not self.state.get("focused_tree_key"):
            self.state["focused_tree_key"] = next((key for key, fid in self.state["visible_file_rows"] if fid == focus_file_id), None)
        self.refresh_file_selection()
        if self.state.get("focused_tree_key"):
            self.focus_tree_row(self.state["focused_tree_key"])

    def refresh_file_selection(self):
        """原位更新所有别名行；焦点只有一个，批量选择不重复计算同一文档。"""
        batch = self.state["batch_mode"]
        selected = self.state["batch_selected_ids"] if batch else {self.state.get("selected_file_id")}
        rows = self.ui_refs.get("catalog_rows", {})
        file_rows = self.ui_refs.get("file_rows", {})
        focused = self.state.get("focused_tree_key")
        if rows and focused not in rows:
            file_id = self.state.get("focused_file_id") or self.state.get("selected_file_id")
            focused = next((key for key, row in file_rows.items() if int(row._props["data-document-id"]) == file_id), next(iter(rows)))
            self.state["focused_tree_key"] = focused
        for key, row in rows.items():
            if row.is_deleted:
                continue
            file_id = int(row._props["data-document-id"]) if "data-document-id" in row._props else None
            active = (file_id in selected and (batch or key == focused)) if file_id is not None else key == focused
            row.classes(remove="theme-selected theme-hover", add="theme-selected" if active else "theme-hover")
            attribute = "aria-selected" if row._props.get("role") == "treeitem" else "aria-pressed"
            row.props(f'{attribute}={str(active).lower()} tabindex={0 if key == focused else -1}')
        for key, checkbox in self.ui_refs.get("file_checks", {}).items():
            if not checkbox.is_deleted:
                checkbox.set_value(int(key.rsplit(":", 1)[-1]) in selected)
        catalog = self.ui_refs.get("file_catalog")
        if catalog is not None and not catalog.is_deleted:
            catalog._props["data-selected-file-ids"] = json.dumps(sorted(self.state["batch_selected_ids"]))
            catalog.props(f'data-multiselect={str(batch).lower()}')
        if update_status := self.ui_refs.get("selection_status"):
            update_status()

    def enter_batch_mode(self):
        if self.state.get("collection_edit"):
            return
        self.state["batch_mode"] = True
        self.state["batch_selected_ids"] = set()
        self.state["selection_anchor"] = self.state.get("selected_file_id")
        self.state["selection_anchor_key"] = self.state.get("focused_tree_key")
        self._refresh_batch_layout()

    def exit_batch_mode(self):
        focused = self.state.get("focused_file_id")
        self.state["batch_mode"] = False
        self.state["batch_selected_ids"] = set()
        self._refresh_batch_layout(focused)

    def toggle_file_selection(self, file_id: int, *, extend=False, row_key=None):
        selected = self.state["batch_selected_ids"]
        visible = self.state["visible_file_rows"]
        keys = [key for key, _ in visible]
        anchor_key = self.state.get("selection_anchor_key")
        ids = [item["id"] for item in self.state["filtered_files"]]
        anchor = self.state.get("selection_anchor")
        if extend and anchor_key in keys and row_key in keys:
            start, end = sorted((keys.index(anchor_key), keys.index(row_key)))
            selected.update(fid for _, fid in visible[start:end + 1])
        elif extend and anchor in ids and file_id in ids:
            start, end = sorted((ids.index(anchor), ids.index(file_id)))
            selected.update(ids[start:end + 1])
        else:
            selected.symmetric_difference_update({file_id})
            self.state["selection_anchor"] = file_id
            self.state["selection_anchor_key"] = row_key
        self.state["focused_file_id"] = file_id
        self.refresh_file_selection()

    def select_all_visible(self):
        if self.state.get("collection_edit"):
            return
        self.state["batch_mode"] = True
        self.state["batch_selected_ids"].update(file["id"] for file in self.state["filtered_files"])
        self._refresh_batch_layout()

    def toggle_select_all(self, checked: bool | None = None):
        """只切换当前页，不抹掉其他页上明确选择的条目。"""
        page_ids = {f["id"] for f in self.state["filtered_files"]}
        selected = self.state["batch_selected_ids"]
        if checked is None:
            checked = not page_ids <= selected
        if checked:
            selected.update(page_ids)
        else:
            selected.difference_update(page_ids)
        self.refresh_file_selection()

    def is_all_selected(self) -> bool:
        page_ids = {f["id"] for f in self.state["filtered_files"]}
        return bool(page_ids) and page_ids <= self.state["batch_selected_ids"]

    async def confirm_batch_delete(self):
        """确认批量删除并固定本次选择，避免对话框打开后扩大范围。"""
        from indexing.services.maintenance_service import delete_files
        file_ids = list(self.state["batch_selected_ids"])
        if not file_ids:
            self._notify(t("files.batch_none_selected"), type="warning")
            return
        try:
            impact = await run_sync(delete_files, file_ids, dry_run=True)
        except ValueError as exc:
            self._notify(str(exc), type="negative")
            return

        confirm_dialog(
            title=t("files.batch_delete_confirm_title"),
            message=t("files.batch_delete_confirm_msg", count=len(file_ids)) + "\n\n" + t(
                "files.knowledge_delete_warning", count=impact["knowledge_evidence_count"]),
            on_confirm=lambda: self._do_batch_delete(file_ids),
            confirm_text=t("confirm_dialog.btn_delete"),
            danger=True,
        )

    async def _do_batch_delete(self, file_ids):
        """执行已确认的文件集合，避免阻塞界面。"""
        from indexing.services.maintenance_service import delete_files
        try:
            result = await run_sync(delete_files, file_ids, confirmed=True)
        except ValueError as exc:
            self._notify(str(exc), type="negative")
            return
        deleted_count = len(result["deleted_file_ids"])
        for item in result["failed"]:
            self._notify(item["error"], type="negative")

        # 失败项仍存在，不能把当前选中的失败文件清空。
        if self.state["selected_file_id"] in result["deleted_file_ids"]:
            self.state["selected_file_id"] = None
            self.state["chunks_data"] = []
            if self.ui_refs.get("chunk_inspector"):
                self.ui_refs["chunk_inspector"].refresh()

        # 退出批量模式并刷新
        self.exit_batch_mode()
        await self.load_files()

        self._notify(t("files.batch_deleted", count=deleted_count), type="positive")

    # ==================== 扫描并索引新文件 ====================

    async def refresh_and_scan(self):
        """刷新文件列表并扫描新文件

        扫描逻辑在服务层（云同步跑完也会调同一个函数），这里只负责把结果
        反馈到界面：新任务的进度由任务订阅自动带出来。
        """
        result = await run_sync(file_service.register_untracked_files)

        await self.load_files()

        for failure in result["failed"]:
            self._notify(
                t(
                    "files.scan_index_failed",
                    filename=failure["filename"],
                    error=failure["error"],
                ),
                type="negative",
            )
        if result["created"]:
            self._notify(
                t("files.scan_found_new", count=len(result["created"])),
                type="positive",
            )
