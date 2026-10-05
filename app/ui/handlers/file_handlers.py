"""
文件操作处理器

职责:
- 文件列表加载和搜索
- 文件上传处理（支持批量）
- 文件删除
- 统计信息加载
"""

import asyncio
import logging

from nicegui import ui, events

from indexing.services import file_service, task_service, zotero_service
from indexing.utils import await_completion, run_sync
from indexing.services import chunk_service, collection_service, metadata_service, maintenance_service
from app.i18n import t
from app.import_scan import import_candidates
from app.ui.components import (
    card_dialog,
    collection_manage_dialog,
    confirm_dialog,
    file_collections_dialog,
    file_create_dialog,
    file_properties_dialog,
)
from app.ui.import_dialogs import folder_import_dialog, zotero_import_dialog
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
        # 批量删除模式状态
        self.state["batch_mode"] = False
        self.state["batch_selected_ids"] = set()
        # 正在向服务端传输的文件数（用于顶部上传提示）
        self.state["uploading_count"] = 0
        # 集合状态：collections 为全部集合，collections_by_file 供列表渲染标签
        self.state["collections"] = []
        self.state["collections_by_file"] = {}
        # 界面一次只持有当前文件页和正在阅读的文件。
        self.state["active_collection_ids"] = []
        self.state["uncategorized"] = False
        self.state["include_descendants"] = True
        self.state["expanded_collection_ids"] = []
        self.state["file_page"] = 1
        self.state["file_page_size"] = 50
        self.state["file_total"] = 0
        self.state["file_scroll"] = 0
        self.state["tree_scroll"] = 0
        self.state["sort_key"] = "created_at"
        self._list_revision = 0
        # 上传并发控制
        self._upload_semaphore = asyncio.Semaphore(_UPLOAD_CONCURRENCY)
        self._upload_cancelled = False
        self._upload_received = False
        self._upload_collection_ids = []
        self.state["latest_file_tasks"] = {}
        # 切片处理器在本类之后构造，由 set_chunk_handlers 注入
        self.chunk_handlers = None

    def set_chunk_handlers(self, chunk_handlers):
        """注入切片处理器，供切换文件后同步原页对比栏"""
        self.chunk_handlers = chunk_handlers

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
            ui.notify(t("collections.selection_deleted"), type="info")
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

    async def load_file_page(self):
        """与 API/MCP 共用分页和范围展开；迟到的查询不得覆盖新的筛选结果。"""
        self._list_revision += 1
        revision = self._list_revision
        size = self.state["file_page_size"]
        page = self.state["file_page"]
        sort_key = self.state["sort_key"]
        options = dict(
            limit=size, offset=(page - 1) * size,
            collection_ids=list(self.state["active_collection_ids"]) or None,
            include_descendants=self.state["include_descendants"],
            uncategorized=self.state["uncategorized"],
            name=self.state.get("search_keyword") or None,
            sort_by=sort_key, descending=SORT_OPTIONS[sort_key],
        )
        result = await run_sync(file_service.get_files_list_paginated, **options)
        if revision != self._list_revision:
            return
        last_page = max(1, (result["total"] + size - 1) // size)
        if page > last_page:
            self.state["file_page"] = last_page
            await self.load_file_page()
            return

        files = list(result["files"])
        selected_id = self.state.get("selected_file_id")
        selected_file = None
        if selected_id is not None and not any(f["id"] == selected_id for f in files):
            selected_file = await run_sync(file_service.get_file_by_id, selected_id)
            if selected_file:
                files.append(selected_file)
        by_file = await run_sync(collection_service.get_collections_by_file, [f["id"] for f in files])
        latest_tasks = await run_sync(task_service.get_latest_file_tasks, [f["id"] for f in files])
        if revision != self._list_revision:
            return
        if self.state.get("selected_file_id") != selected_id:
            await self.load_file_page()
            return
        if selected_id is not None and not any(f["id"] == selected_id for f in files):
            self.state["selected_file_id"] = None
            self.state["chunks_data"] = []
            self.state["source_page"] = None
            for key in ("chunk_inspector", "source_column"):
                if self.ui_refs.get(key):
                    self.ui_refs[key].refresh()
        # 阅读区身份独立于筛选/分页，切换范围不会卸掉正在核验的原页。
        self.state["files_data"] = files
        self.state["filtered_files"] = result["files"]
        self.state["collections_by_file"] = by_file
        self.state["latest_file_tasks"] = latest_tasks
        self.state["file_total"] = result["total"]
        for key in ("file_list_container", "file_pagination", "file_info_panel", "chunk_toolbar_buttons"):
            if self.ui_refs.get(key):
                self.ui_refs[key].refresh()

    def _reset_file_page(self):
        self.state["file_page"] = 1
        self.state["file_scroll"] = 0
        if self.ui_refs.get("file_scroll"):
            self.ui_refs["file_scroll"].scroll_to(pixels=0)

    async def on_search_change(self, e):
        self.state["search_keyword"] = e.args
        self._reset_file_page()
        await self.load_file_page()

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

    async def on_collection_change(self, collection_ids, *, uncategorized=False):
        self.state["active_collection_ids"] = list(collection_ids or [])
        self.state["uncategorized"] = uncategorized
        self._reset_file_page()
        tree = self.ui_refs.get("tree_element")
        if tree:
            tree.select("uncategorized" if uncategorized else
                        str(collection_ids[0]) if collection_ids else "all")
        if self.ui_refs.get("collection_filter"):
            self.ui_refs["collection_filter"].refresh()
        await self.load_file_page()

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
            ui.notify(
                t("collections.auto_assigned", name=" · ".join(names)), type="info"
            )

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
                ui.notify(result["message"], type="warning")
        except ValueError as exc:
            ui.notify(str(exc), type="warning")
        await self.load_collections()
        return self.state["collections"]

    async def _rename_collection(self, collection_id: int, name: str) -> list:
        """重命名集合，返回刷新后的集合列表"""
        result = await run_sync(
            collection_service.rename_collection, collection_id, name
        )
        if not result["success"]:
            ui.notify(result["message"], type="warning")
        await self.load_collections()
        return self.state["collections"]

    async def _move_collection(self, collection_id: int, parent_id: int | None) -> list:
        try:
            result = await run_sync(collection_service.move_collection, collection_id, parent_id)
            if not result["success"]:
                ui.notify(result["message"], type="warning")
        except ValueError as exc:
            ui.notify(str(exc), type="warning")
        await self.load_collections()
        return self.state["collections"]

    async def _preview_delete_collection(self, collection_id: int):
        try:
            return await run_sync(maintenance_service.delete_collection, collection_id, dry_run=True)
        except ValueError as exc:
            ui.notify(str(exc), type="warning")
            return None

    async def _delete_collection(self, collection_id: int) -> list:
        """确认后重新校验，不使用过期预览作为删除依据。"""
        try:
            await run_sync(maintenance_service.delete_collection, collection_id, confirmed=True)
            ui.notify(t("collections.deleted"), type="positive")
        except ValueError as exc:
            ui.notify(str(exc), type="warning")
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
        ui.notify(t("collections.assigned"), type="positive")

    def handle_batch_collections(self):
        """批量归类：把选中的文件一起放进同一批集合"""
        selected = self.state["batch_selected_ids"]
        if not selected:
            ui.notify(t("files.batch_none_selected"), type="warning")
            return

        file_collections_dialog(
            filename=t("collections.batch_target", count=len(selected)),
            collections=self.state["collections"],
            selected_ids=set(),
            on_save=self._save_batch_collections,
            on_create=self._create_collection,
        )

    async def _save_batch_collections(self, collection_ids: list):
        """覆盖式设置所有选中文件的集合"""
        file_ids = list(self.state["batch_selected_ids"])
        for file_id in file_ids:
            await run_sync(
                collection_service.set_file_collections, file_id, collection_ids
            )

        self.exit_batch_mode()
        await self.load_collections()
        ui.notify(t("collections.batch_assigned", count=len(file_ids)), type="positive")

    # ==================== 单个文件删除 ====================

    async def confirm_delete_file(self, file_id: int):
        """确认删除单个文件并展示知识引用保留范围。"""
        from indexing.services.maintenance_service import delete_files
        try:
            impact = await run_sync(delete_files, [file_id], dry_run=True)
        except ValueError as exc:
            ui.notify(str(exc), type="negative")
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
            ui.notify(str(exc), type="negative")
            return
        if file_id not in result["deleted_file_ids"]:
            ui.notify(t("files.delete_failed"), type="negative")
            return

        if self.state["selected_file_id"] == file_id:
            self.state["selected_file_id"] = None
            self.state["chunks_data"] = []
            if self.ui_refs.get("chunk_inspector"):
                self.ui_refs["chunk_inspector"].refresh()
            if self.ui_refs.get("file_info_panel"):
                self.ui_refs["file_info_panel"].refresh()

        await self.load_files()
        ui.notify(t("files.deleted"), type="positive")

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
        ui.notify(t("properties.saved"), type="positive")

    async def handle_export_original(self, file_id: int):
        """另存原始文件。

        原始文件上传后一直只作为解析输入保留在 originals/ 下，
        本地原件删除后无法取回，这里提供一个导出入口。
        """
        try:
            await run_sync(file_service.export_file, file_id, "original")
        except ValueError as exc:
            ui.notify(str(exc), type="warning")
            return
        ui.download.from_url(f"/gui/file/{file_id}/content?format=original")

    async def handle_export_working(self, file_id: int):
        """导出当前 Markdown 快照，包含已完成的卡片增删改。"""
        try:
            await run_sync(file_service.export_file, file_id, "markdown")
        except ValueError as exc:
            ui.notify(str(exc), type="warning")
            return
        ui.download.from_url(f"/gui/file/{file_id}/content?format=markdown")

    async def handle_reindex_file(self, file_id: int):
        """重新索引文件。

        换了 PDF 解析后端、Office 转换后端或分块参数之后，已入库的切片仍是
        旧配置的产物，需要重跑一遍才能生效。
        """
        file_info = next(
            (f for f in self.state["files_data"] if f["id"] == file_id), None
        )
        if not file_info:
            ui.notify(t("files.original_missing"), type="warning")
            return

        # 同一文件两个任务并发会互相清空对方写入的切片
        active = await run_sync(task_service.get_active_tasks)
        if any(task.get("file_id") == file_id for task in active):
            ui.notify(t("files.reindex_busy"), type="warning")
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
            ui.notify(str(exc), type="negative")
            return
        ui.notify(t("files.reindex_started"), type="positive")
        await self.load_files()

    async def handle_cancel_task(self, task_id: int):
        try:
            task = await run_sync(task_service.cancel_task, task_id)
        except Exception as exc:
            logger.error("取消任务失败 (%s)", type(exc).__name__)
            ui.notify(t("task.action_failed"), type="negative")
            return
        key = "task.cancelling" if task["status"] in task_service.ACTIVE_STATUSES else (
            "task.cancelled" if task["status"] == "cancelled" else "task.already_finished")
        ui.notify(t(key), type="info")
        await self.load_files()

    async def handle_retry_task(self, task_id: int):
        try:
            await run_sync(task_service.retry_task, task_id)
        except ValueError as exc:
            ui.notify(str(exc), type="warning")
            return
        except Exception as exc:
            logger.error("重试任务失败 (%s)", type(exc).__name__)
            ui.notify(t("task.action_failed"), type="negative")
            return
        ui.notify(t("files.reindex_started"), type="positive")
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
            ui.notify(t("chunks.title_content_required"), type="warning")
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
            ui.notify(str(exc), type="negative")
            return

        ui.notify(t("cards.created"), type="positive")
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
        # 1. 立即更新选中状态，旧预览即使切走又切回也不能回写。
        if self.chunk_handlers:
            self.chunk_handlers.invalidate_source_requests()
        self.state["selected_file_id"] = file_id
        # 新建文件或切换分页后，阅读对象可能不在当前结果页。
        if not any(f["id"] == file_id for f in self.state["files_data"]):
            file_info = await run_sync(file_service.get_file_by_id, file_id)
            collections = await run_sync(collection_service.get_file_collections, file_id)
            if self.state["selected_file_id"] != file_id:
                return
            if not file_info:
                self.state["selected_file_id"] = None
                return
            self.state["files_data"] = list(self.state["filtered_files"]) + [file_info]
            self.state["collections_by_file"][file_id] = [c["name"] for c in collections]

        # 2. 清空旧数据，显示加载状态
        self.state["chunks_data"] = []
        self.state["chunk_scroll"] = 0
        if self.ui_refs.get("chunk_scroll"):
            self.ui_refs["chunk_scroll"].scroll_to(pixels=0)
        self.state["chunk_page"] = 1
        self.state["total_chunks"] = 0
        self.state["total_chunk_pages"] = 1
        # 对比栏里的原页属于上一个文件，先清空，切片加载完再按新文件对齐
        self.state["source_page"] = None

        # 3. 立即刷新 UI（显示"加载中"）
        if self.ui_refs.get("file_list_container"):
            self.ui_refs["file_list_container"].refresh()
        if self.ui_refs.get("file_info_panel"):
            self.ui_refs["file_info_panel"].refresh()
        # 顶栏的另存原件/重新索引按钮绑定的是当前文件，换文件必须跟着重建
        if self.ui_refs.get("chunk_toolbar_buttons"):
            self.ui_refs["chunk_toolbar_buttons"].refresh()
        if self.ui_refs.get("chunk_inspector"):
            self.ui_refs["chunk_inspector"].refresh()
        if self.ui_refs.get("source_column"):
            self.ui_refs["source_column"].refresh()

        # 4. 异步加载首页，或引用卡片所在页；不按标题重定位。
        result = await run_sync(
            file_service.get_chunks_paginated,
            file_id,
            page=1,
            page_size=self.state["chunk_page_size"],
            **({"chunk_id": chunk_id} if chunk_id is not None else {}),
        )

        # 5. 更新状态，忽略上一个阅读对象迟到的查询。
        if self.state["selected_file_id"] != file_id:
            return
        if result:
            self.state["chunk_page"] = result["page"]
            self.state["chunks_data"] = result["chunks"]
            self.state["total_chunks"] = result["total"]
            self.state["total_chunk_pages"] = result["total_pages"]
        else:
            self.state["chunks_data"] = []
            self.state["total_chunks"] = 0
            self.state["total_chunk_pages"] = 1

        # 6. 刷新 UI（显示数据）
        if self.ui_refs.get("chunk_inspector"):
            self.ui_refs["chunk_inspector"].refresh()

        # 7. 展开着的对比栏跟到新文件（不是 PDF 则由该方法收起）
        if self.chunk_handlers:
            await self.chunk_handlers.sync_source_page()

    async def open_file_picker(self):
        """一次接收一批；更换组件身份，取消后迟到的回调不能污染下一批。"""
        if self.state.get("uploading_count"):
            ui.notify(t("files.upload_busy"), type="warning")
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
        if not self._upload_stopped(e.sender) and not self._upload_received:
            self.state["uploading_count"] += e.args
            self._refresh_upload_banner()

    def _refresh_upload_banner(self):
        banner = self.ui_refs.get("upload_banner")
        if banner:
            banner.refresh()

    def cancel_upload(self):
        """中止传输和未受理文件；已受理的任务在文件列表中单独取消。"""
        self._upload_cancelled = True
        upload_input = self.ui_refs.get("upload_input")
        if upload_input:
            upload_input.run_method("abort")
            upload_input.reset()
        self.state["uploading_count"] = 0
        self._refresh_upload_banner()
        ui.notify(t("files.upload_cancelled"), type="info")

    def on_upload_failed(self, e):
        # QUploader 的 abort 同样触发 failed，不把主动取消误报成网络错误。
        if self._upload_stopped(e.sender):
            return
        self._upload_cancelled = True
        e.sender.reset()
        self.state["uploading_count"] = 0
        self._refresh_upload_banner()
        ui.notify(t("files.upload_failed"), type="negative", timeout=0, close_button=True)

    async def handle_multi_upload(self, e: events.MultiUploadEventArguments):
        """NiceGUI 的批次回调不等待逐文件回调，由这里统一等待落盘和受理。"""
        if self._upload_stopped(e.sender):
            return
        self._upload_received = True
        collection_ids = self._upload_collection_ids[:]
        self.state["uploading_count"] = len(e.files)
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
                        self._refresh_upload_banner()

        await asyncio.gather(*(process(file) for file in e.files))
        if e.sender is self.ui_refs.get("upload_input"):
            e.sender.run_method("removeUploadedFiles")
        try:
            await self.load_files()
        except Exception as exc:
            logger.error("上传后刷新失败 (%s)", type(exc).__name__)
            ui.notify(t("files.upload_refresh_failed"), type="warning")

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
            ui.notify(t("files.upload_file_failed", filename=file.name, error=reason),
                      type="negative", timeout=0, close_button=True)
        else:
            ui.notify(t("files.upload_exists") if result["duplicate"] else t("files.upload_processing"),
                      type="warning" if result["duplicate"] else "positive")
        finally:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("上传临时文件清理失败 (%s)", type(exc).__name__)

    def on_upload_rejected(self, _):
        """处理被拒绝的文件。

        QUploader 的 rejected 事件不带具体原因，这里把四道边界都列出来，
        避免用户面对一句笼统提示猜是哪一条超了。
        """
        ui.notify(
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
        ui.notify(t("import.folder_done", accepted=accepted, duplicates=duplicates,
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
            ui.notify(str(exc), type="negative")
            return
        ui.notify(t("import.zotero_done", accepted=result["accepted_count"], duplicates=result["duplicate_count"],
                    failed=result["failed_count"], skipped=result["skipped_count"]),
                  type="positive" if result["accepted_count"] else "warning")
        await self.load_collections()
        await self.load_files()

    async def _do_create_file(self, filename: str):
        """执行创建文件"""
        if not filename or not filename.strip():
            ui.notify(t("files.create_name_empty"), type="warning")
            return

        try:
            result = await run_sync(file_service.create_empty_file, filename)
            ui.notify(t("files.created", filename=result["filename"]), type="positive")

            # 与上传一致：集合视图下新建的文件直接归入该集合
            await self._assign_active_collections(result["file_id"])

            # 刷新文件列表
            await self.load_files()

            # 自动选中新创建的文件
            await self.load_chunks(result["file_id"])

        except ValueError as e:
            ui.notify(str(e), type="negative")
        except Exception as e:
            ui.notify(t("files.create_failed", error=str(e)), type="negative")

    # ==================== 批量删除功能 ====================

    def enter_batch_mode(self):
        """进入批量删除模式"""
        self.state["batch_mode"] = True
        self.state["batch_selected_ids"] = set()
        if self.ui_refs.get("toolbar_buttons"):
            self.ui_refs["toolbar_buttons"].refresh()
        if self.ui_refs.get("file_list_container"):
            self.ui_refs["file_list_container"].refresh()

    def exit_batch_mode(self):
        """退出批量删除模式"""
        self.state["batch_mode"] = False
        self.state["batch_selected_ids"] = set()
        if self.ui_refs.get("toolbar_buttons"):
            self.ui_refs["toolbar_buttons"].refresh()
        if self.ui_refs.get("file_list_container"):
            self.ui_refs["file_list_container"].refresh()

    def toggle_file_selection(self, file_id: int):
        """切换单个文件的选中状态"""
        if file_id in self.state["batch_selected_ids"]:
            self.state["batch_selected_ids"].discard(file_id)
        else:
            self.state["batch_selected_ids"].add(file_id)
        if self.ui_refs.get("toolbar_buttons"):
            self.ui_refs["toolbar_buttons"].refresh()
        if self.ui_refs.get("file_list_container"):
            self.ui_refs["file_list_container"].refresh()

    def toggle_select_all(self):
        """全选/取消全选"""
        filtered_ids = {f["id"] for f in self.state["filtered_files"]}
        if self.state["batch_selected_ids"] == filtered_ids:
            # 已全选，取消全选
            self.state["batch_selected_ids"] = set()
        else:
            # 未全选，执行全选
            self.state["batch_selected_ids"] = filtered_ids
        if self.ui_refs.get("toolbar_buttons"):
            self.ui_refs["toolbar_buttons"].refresh()
        if self.ui_refs.get("file_list_container"):
            self.ui_refs["file_list_container"].refresh()

    def is_all_selected(self) -> bool:
        """检查是否全选"""
        if not self.state["filtered_files"]:
            return False
        filtered_ids = {f["id"] for f in self.state["filtered_files"]}
        return self.state["batch_selected_ids"] == filtered_ids

    async def confirm_batch_delete(self):
        """确认批量删除并固定本次选择，避免对话框打开后扩大范围。"""
        from indexing.services.maintenance_service import delete_files
        file_ids = list(self.state["batch_selected_ids"])
        if not file_ids:
            ui.notify(t("files.batch_none_selected"), type="warning")
            return
        try:
            impact = await run_sync(delete_files, file_ids, dry_run=True)
        except ValueError as exc:
            ui.notify(str(exc), type="negative")
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
            ui.notify(str(exc), type="negative")
            return
        deleted_count = len(result["deleted_file_ids"])
        for item in result["failed"]:
            ui.notify(item["error"], type="negative")

        # 失败项仍存在，不能把当前选中的失败文件清空。
        if self.state["selected_file_id"] in result["deleted_file_ids"]:
            self.state["selected_file_id"] = None
            self.state["chunks_data"] = []
            if self.ui_refs.get("chunk_inspector"):
                self.ui_refs["chunk_inspector"].refresh()

        # 退出批量模式并刷新
        self.exit_batch_mode()
        await self.load_files()

        ui.notify(t("files.batch_deleted", count=deleted_count), type="positive")

    # ==================== 扫描并索引新文件 ====================

    async def refresh_and_scan(self):
        """刷新文件列表并扫描新文件

        扫描逻辑在服务层（云同步跑完也会调同一个函数），这里只负责把结果
        反馈到界面：新任务的进度由任务订阅自动带出来。
        """
        result = await run_sync(file_service.register_untracked_files)

        await self.load_files()

        for failure in result["failed"]:
            ui.notify(
                t(
                    "files.scan_index_failed",
                    filename=failure["filename"],
                    error=failure["error"],
                ),
                type="negative",
            )
        if result["created"]:
            ui.notify(
                t("files.scan_found_new", count=len(result["created"])),
                type="positive",
            )
