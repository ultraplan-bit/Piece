"""
文件操作处理器

职责:
- 文件列表加载和搜索
- 文件上传处理（支持批量）
- 文件删除
- 统计信息加载
"""

import asyncio

from nicegui import ui, events

from indexing.services import file_service, task_service
from indexing.utils import await_completion, run_sync
from indexing.services import chunk_service, collection_service, metadata_service
from app.i18n import t
from app.ui.components import (
    card_dialog,
    collection_manage_dialog,
    confirm_dialog,
    file_collections_dialog,
    file_create_dialog,
    file_properties_dialog,
)
from app.utils import (
    format_size,
    MAX_FILE_SIZE,
    MAX_TOTAL_UPLOAD_SIZE,
    MAX_UPLOAD_FILES,
)

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
        # 多选筛选，空列表表示"全部集合"
        self.state["active_collection_ids"] = []
        self.state["sort_key"] = "created_at"
        # 上传并发控制
        self._upload_semaphore = asyncio.Semaphore(_UPLOAD_CONCURRENCY)
        # 切片处理器在本类之后构造，由 set_chunk_handlers 注入
        self.chunk_handlers = None

    def set_chunk_handlers(self, chunk_handlers):
        """注入切片处理器，供切换文件后同步原页对比栏"""
        self.chunk_handlers = chunk_handlers

    async def load_files(self):
        """加载文件列表（异步）"""
        self.state["files_data"] = await run_sync(file_service.get_files_list)
        await self.load_collections(refresh=False)
        self.apply_filter()
        # 文件增删会改变集合内的文件数，筛选下拉需要跟着更新
        if self.ui_refs.get("collection_filter"):
            self.ui_refs["collection_filter"].refresh()
        await self.load_stats()

    async def load_collections(self, refresh: bool = True):
        """加载集合列表及文件归属关系"""
        self.state["collections"] = await run_sync(
            collection_service.list_collections
        )
        self.state["collections_by_file"] = await run_sync(
            collection_service.get_collections_by_file
        )

        # 集合被删除后，原本选中的筛选项要回到"全部"
        valid_ids = {item["id"] for item in self.state["collections"]}
        self.state["active_collection_ids"] = [
            cid for cid in self.state.get("active_collection_ids", []) if cid in valid_ids
        ]

        if refresh:
            self.apply_filter()
            if self.ui_refs.get("collection_filter"):
                self.ui_refs["collection_filter"].refresh()

    def apply_filter(self):
        """应用集合、搜索过滤和排序"""
        files = self.state["files_data"]

        collection_ids = self.state.get("active_collection_ids") or []
        if collection_ids:
            # 集合名在库中唯一，按名匹配即可复用 collections_by_file 这张映射
            names = {
                item["name"]
                for item in self.state["collections"]
                if item["id"] in set(collection_ids)
            }
            by_file = self.state.get("collections_by_file", {})
            # 多选取并集：命中任意一个集合即保留
            files = [f for f in files if names & set(by_file.get(f["id"], []))]

        keyword = self.state.get("search_keyword", "").strip().lower()
        if keyword:
            files = [f for f in files if keyword in f["filename"].lower()]

        sort_key = self.state.get("sort_key", "created_at")
        reverse = SORT_OPTIONS.get(sort_key, True)
        files = sorted(
            files, key=lambda f: (f.get(sort_key) or "").lower()
            if sort_key == "filename" else (f.get(sort_key) or ""),
            reverse=reverse,
        )

        self.state["filtered_files"] = files

        if self.ui_refs.get("file_list_container"):
            self.ui_refs["file_list_container"].refresh()

    def on_search_change(self, e):
        """搜索框内容变化时触发"""
        self.state["search_keyword"] = e.args
        self.apply_filter()

    def on_sort_change(self, sort_key: str):
        """切换排序方式"""
        self.state["sort_key"] = sort_key
        self.apply_filter()
        if self.ui_refs.get("list_toolbar"):
            self.ui_refs["list_toolbar"].refresh()

    # ==================== 集合 ====================

    def on_collection_change(self, collection_ids):
        """切换集合筛选（多选，空表示全部）"""
        self.state["active_collection_ids"] = list(collection_ids or [])
        self.apply_filter()

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
            on_delete=self._delete_collection,
        )

    async def _create_collection(self, name: str) -> list:
        """新建集合，返回刷新后的集合列表"""
        result = await run_sync(collection_service.create_collection, name)
        if not result["success"]:
            ui.notify(result["message"], type="warning")
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

    async def _delete_collection(self, collection_id: int) -> list:
        """删除集合（文件本身不受影响），返回刷新后的集合列表"""
        await run_sync(collection_service.delete_collection, collection_id)
        await self.load_collections()
        return self.state["collections"]

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

    def confirm_delete_file(self, file_id: int):
        """确认删除单个文件"""
        file_info = next(
            (f for f in self.state["files_data"] if f["id"] == file_id), None
        )
        if not file_info:
            return

        confirm_dialog(
            title=t("files.delete_confirm_title"),
            message=t("files.delete_confirm_msg", filename=file_info["filename"]),
            on_confirm=lambda: self._do_delete_file(file_id),
            confirm_text=t("confirm_dialog.btn_delete"),
            danger=True,
        )

    async def _do_delete_file(self, file_id: int):
        """执行删除单个文件"""
        try:
            success = await run_sync(file_service.delete_file, file_id)
        except ValueError as exc:
            ui.notify(str(exc), type="negative")
            return
        if not success:
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

        if self.ui_refs.get("stats_label"):
            self.ui_refs["stats_label"].set_text(
                t("stats.indexed", size=size_str, indexed=indexed, total=total)
            )
            self.ui_refs["stats_label"].update()

    async def load_chunks(self, file_id: int):
        """加载选中文件的切片（异步 + 后端分页）"""
        # 1. 立即更新选中状态，旧预览即使切走又切回也不能回写。
        if self.chunk_handlers:
            self.chunk_handlers.invalidate_source_requests()
        self.state["selected_file_id"] = file_id

        # 2. 清空旧数据，显示加载状态
        self.state["chunks_data"] = []
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

        # 4. 异步加载第一页数据
        result = await run_sync(
            file_service.get_chunks_paginated,
            file_id,
            page=1,
            page_size=self.state["chunk_page_size"]
        )

        # 5. 更新状态
        if result:
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

    def open_file_picker(self):
        """打开文件选择框。

        QUploader 上传成功后会把文件留在内部列表里（只有 reset/removeUploadedFiles
        能清），列表占满 max-files 后再选的文件会被静默拒绝。这里先清掉已完成的条目
        再弹选择框；用 removeUploadedFiles 而非 reset，避免中断正在上传的文件。
        """
        upload_input = self.ui_refs.get("upload_input")
        if not upload_input:
            return
        upload_input.run_method("removeUploadedFiles")
        upload_input.run_method("pickFiles")

    def on_begin_upload(self, _):
        """浏览器开始向服务端传输文件时触发，用于显示上传阶段提示。"""
        self.state["uploading_count"] = self.state.get("uploading_count", 0) + 1
        self._refresh_upload_banner()

    def _refresh_upload_banner(self):
        """刷新文件列表顶部的上传提示条。"""
        banner = self.ui_refs.get("upload_banner")
        if banner:
            banner.refresh()

    async def handle_upload(self, e: events.UploadEventArguments):
        """
        处理单个文件上传（带并发控制）

        使用信号量限制并发数，避免耗尽数据库连接池
        """
        # 获取信号量，限制并发数
        async with self._upload_semaphore:
            try:
                await self._process_single_upload(e)
            finally:
                # 传输阶段已结束（无论入库成功与否），撤下上传提示
                self.state["uploading_count"] = max(
                    0, self.state.get("uploading_count", 0) - 1
                )
                self._refresh_upload_banner()

    async def on_multi_upload_complete(self, _):
        """
        所有文件上传完成后的回调

        清理已完成的上传条目，避免占满 max-files 导致下次选不了文件。
        """
        upload_input = self.ui_refs.get("upload_input")
        if upload_input:
            upload_input.run_method("removeUploadedFiles")
        await self.load_files()

    async def _process_single_upload(self, e: events.UploadEventArguments):
        """GUI 只负责接收上传流和展示结果，导入规则与其他入口共用。"""
        import os
        import tempfile
        from pathlib import Path

        filename = e.file.name
        descriptor, temp_name = tempfile.mkstemp(suffix=Path(filename).suffix)
        os.close(descriptor)
        path = Path(temp_name)
        try:
            await await_completion(e.file.save(path))
            result = await run_sync(
                file_service.import_file, path, filename,
                list(self.state.get("active_collection_ids") or []),
            )
        except (ValueError, OSError) as exc:
            ui.notify(str(exc), type="negative")
            return
        finally:
            path.unlink(missing_ok=True)
        ui.notify(t("files.upload_exists") if result["duplicate"] else t("files.upload_processing"),
                  type="warning" if result["duplicate"] else "positive")
        await self.load_files()

    def on_upload_rejected(self, _):
        """处理被拒绝的文件。

        QUploader 的 rejected 事件不带具体原因，这里把四道边界都列出来，
        避免用户面对一句笼统提示猜是哪一条超了。
        """
        ui.notify(
            t(
                "files.upload_rejected",
                max_files=MAX_UPLOAD_FILES,
                max_size=MAX_FILE_SIZE // (1024 * 1024),
                max_total=MAX_TOTAL_UPLOAD_SIZE // (1024 * 1024 * 1024),
            ),
            type="negative",
        )

    def handle_create_file(self):
        """处理新建文件"""
        file_create_dialog(
            on_create=self._do_create_file,
        )

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

    def confirm_batch_delete(self):
        """确认批量删除"""
        if not self.state["batch_selected_ids"]:
            ui.notify(t("files.batch_none_selected"), type="warning")
            return

        count = len(self.state["batch_selected_ids"])
        confirm_dialog(
            title=t("files.batch_delete_confirm_title"),
            message=t("files.batch_delete_confirm_msg", count=count),
            on_confirm=self._do_batch_delete,
            confirm_text=t("confirm_dialog.btn_delete"),
            danger=True,
        )

    async def _do_batch_delete(self):
        """执行批量删除（异步优化，避免阻塞界面）"""
        from indexing.services.maintenance_service import delete_files
        try:
            result = await run_sync(delete_files, list(self.state["batch_selected_ids"]), confirmed=True)
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
