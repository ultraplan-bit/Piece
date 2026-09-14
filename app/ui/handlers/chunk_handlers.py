"""
切片操作处理器

职责:
- 切片编辑
- 切片删除
- 切片新增
- 在对比栏查看切片对应的 PDF 原页
"""

import asyncio
from indexing.utils import run_sync
from pathlib import Path
from typing import Optional

from nicegui import ui

from indexing.services import chunk_service, file_service
from indexing.services.page_render import (
    VIEW_DPI,
    page_number_from_heading,
    render_pdf_page,
    resolve_source_pdf,
)
from app.i18n import t
from app.ui.components import chunk_dialog, confirm_dialog


class ChunkHandlers:
    """切片操作处理器"""

    def __init__(self, state: dict, ui_refs: dict, on_refresh_files: callable):
        """
        初始化切片处理器

        切片增改产生的任务由 TaskHandlers 订阅任务表得知，这里不再登记。

        Args:
            state: 共享状态字典
            ui_refs: UI 组件引用字典
            on_refresh_files: 刷新文件列表的回调
        """
        self.state = state
        self.ui_refs = ui_refs
        self.on_refresh_files = on_refresh_files
        self._source_lock = asyncio.Lock()
        self._source_request: dict | None = None
        # 批量删除模式状态
        self.state["chunk_batch_mode"] = False
        self.state["chunk_batch_selected_ids"] = set()

    async def handle_view_source_page(self, page_number: int):
        """切换原页对比栏：点开某一页，再点同一页则收起。

        切片正文是 OCR / 文本层加工后的结果，校对时需要和原页对照着看，
        因此并排展开而不是弹窗——弹窗会把整页版面压得看不清。
        """
        current = self.state.get("source_page")
        if current and current["page"] == page_number:
            self.close_source_page()
            return

        self.state["source_pane_open"] = True
        # 手动点页立即响应，取消浏览器中尚未上报的自动跟随候选。
        revision = self.state.get("source_follow_revision", 0) + 1
        self.state["source_follow_revision"] = revision
        pane = self.ui_refs.get("source_pane")
        if pane is not None and not pane.is_deleted:
            pane.props(f"data-follow-revision={revision}")
        await self._show_source_page(page_number, notify_on_failure=True)

    async def handle_chunk_scroll(self, e):
        """浏览器确认阅读位置后跟随原页；丢弃旧文件或旧卡片的滚动事件。"""
        if not self.state.get("source_pane_open") or not isinstance(e.args, dict):
            return
        if e.args.get("file_id") != self.state.get("selected_file_id"):
            return
        chunk = next(
            (c for c in self.get_visible_chunks() if c["id"] == e.args.get("chunk_id")),
            None,
        )
        page_number = page_number_from_heading(chunk.get("heading_path")) if chunk else None
        if page_number is not None:
            # 即使回到了当前已显示页，也要替换正在等待的目标，避免旧请求稍后跳回来。
            await self._show_source_page(page_number)

    def invalidate_source_requests(self):
        """换文件、翻页或关闭时废弃旧目标；已开始的渲染只留缓存，不再更新界面。"""
        self._source_request = None

    async def sync_source_page(self):
        """切片列表换了内容后，把对比栏对齐到首个切片所在页。

        用户展开对比栏的意图跨文件保留，但换成没有原页的文件时先收起，
        免得右边一直挂着上一个文件的页面。
        """
        if not self.state.get("source_pane_open"):
            return

        chunks = self.get_visible_chunks()
        page_number = (
            page_number_from_heading(chunks[0].get("heading_path"))
            if chunks
            else None
        )
        if page_number is None:
            self.invalidate_source_requests()
            self.state["source_page"] = None
            self._refresh_source_column()
            return

        await self._show_source_page(page_number)

    async def _show_source_page(self, page_number: int, notify_on_failure: bool = False):
        """同页合并、待处理页取最新；正在渲染的旧页完成后只保留缓存。"""
        file_id = self.state.get("selected_file_id")
        file_info = next(
            (f for f in self.state.get("files_data", []) if f["id"] == file_id),
            None,
        )
        if not file_info:
            self.invalidate_source_requests()
            self.state["source_page"] = None
            self._refresh_source_column()
            return

        pending = self._source_request
        if pending and (pending["file_id"], pending["page"]) == (file_id, page_number):
            pending["notify"] |= notify_on_failure
            return
        request = {"file_id": file_id, "page": page_number, "notify": notify_on_failure}
        self._source_request = request
        try:
            current = self.state.get("source_page")
            if current and current["page"] == page_number:
                return
            async with self._source_lock:
                if self._source_request is not request:
                    return
                # Office 转换和原页渲染都在线程中等待，不阻塞 UI 事件循环。
                page_path = await run_sync(self._render_source_page, file_info, page_number)
                if (
                    self._source_request is not request
                    or self.state.get("selected_file_id") != file_id
                    or not self.state.get("source_pane_open")
                ):
                    return
                if page_path is None:
                    if request["notify"]:
                        ui.notify(t("chunks.source_page_failed"), type="negative")
                    self.state["source_page"] = None
                else:
                    self.state["source_page"] = {
                        "file_id": file_id,
                        "url": f"/pages/{page_path.name}",
                        "page": page_number,
                        "caption": t(
                            "chunks.source_page_caption",
                            filename=file_info["filename"],
                            page=page_number,
                        ),
                    }
                self._refresh_source_column()
        finally:
            if self._source_request is request:
                self._source_request = None

    @staticmethod
    def _render_source_page(file_info: dict, page_number: int) -> Optional[Path]:
        """在线程中完成原件解析和页面渲染"""
        pdf_path = resolve_source_pdf(
            file_info.get("original_file_path"),
            file_info.get("original_file_type"),
        )
        if pdf_path is None:
            return None
        return render_pdf_page(pdf_path, page_number, VIEW_DPI)

    def close_source_page(self):
        """收起原页对比栏，把版面还给切片列表"""
        self.invalidate_source_requests()
        self.state["source_pane_open"] = False
        self.state["source_page"] = None
        self._refresh_source_column()

    def _refresh_source_column(self):
        """同栏切页只换图片和标题，保留滚动容器；展开、关闭时才重建。"""
        source = self.state.get("source_page")
        image = self.ui_refs.get("source_image")
        if source and image is not None and not image.is_deleted:
            image.set_source(source["url"])
            self.ui_refs["source_caption"].set_text(source["caption"])
            self.ui_refs["source_tooltip"].set_text(source["caption"])
            self.ui_refs["source_pane"].props(
                f'data-file-id={source["file_id"]} data-page={source["page"]}'
            )
            return
        column = self.ui_refs.get("source_column")
        if column:
            try:
                column.refresh()
            except RuntimeError:
                # 慢转换期间，所在页面可能已经被销毁。
                pass

    async def handle_edit_chunk(self, chunk_id: int):
        """打开编辑切片对话框"""
        chunk = await run_sync(chunk_service.get_chunk_by_id, chunk_id)
        if not chunk:
            ui.notify(t("chunks.not_found"), type="negative")
            return

        chunk_dialog(
            owner_id=chunk_id,
            doc_title=chunk["doc_title"],
            chunk_text=chunk["chunk_text"],
            on_save=self._save_chunk_edit,
            on_close=lambda: None,
            is_edit=True,
        )

    async def _save_chunk_edit(self, chunk_id: int, new_title: str, new_text: str):
        """保存切片编辑"""
        chunk = await run_sync(chunk_service.get_chunk_by_id, chunk_id)
        if not chunk:
            ui.notify(t("chunks.not_found"), type="negative")
            return

        title_changed = new_title != chunk["doc_title"]
        text_changed = new_text != chunk["chunk_text"]

        if not title_changed and not text_changed:
            ui.notify(t("chunks.no_change"), type="info")
            return

        try:
            if text_changed:
                await run_sync(
                    chunk_service.create_chunk_update_task, chunk_id, new_text,
                    new_title if title_changed else None,
                )
                ui.notify(t("chunks.update_task_created"), type="positive")
            elif title_changed:
                await run_sync(chunk_service.update_chunk_title, chunk_id, new_title)
                ui.notify(t("chunks.title_updated"), type="positive")
        except ValueError as exc:
            ui.notify(str(exc), type="negative")
            return

        await self._reload_chunks()

    def handle_delete_chunk(self, chunk_id: int):
        """确认删除切片"""
        confirm_dialog(
            title=t("chunks.delete_confirm_title"),
            message=t("chunks.delete_confirm_msg"),
            on_confirm=lambda: self._do_delete_chunk(chunk_id),
            confirm_text=t("confirm_dialog.btn_delete"),
            danger=True,
        )

    async def _do_delete_chunk(self, chunk_id: int):
        """执行删除切片"""
        try:
            result = await run_sync(chunk_service.delete_chunk, chunk_id)
        except ValueError as exc:
            ui.notify(str(exc), type="negative")
            return
        if result["success"]:
            if result.get("file_deleted"):
                ui.notify(t("chunks.last_chunk_deleted"), type="positive")
                self.state["selected_file_id"] = None
                self.state["chunks_data"] = []
                await self.on_refresh_files()
                if self.ui_refs.get("chunk_inspector"):
                    self.ui_refs["chunk_inspector"].refresh()
            else:
                ui.notify(t("chunks.deleted"), type="positive")
                await self._reload_chunks()
        else:
            ui.notify(result.get("error", t("chunks.delete_failed")), type="negative")

    def handle_add_chunk(self):
        """打开新增切片对话框"""
        if self.state["selected_file_id"] is None:
            ui.notify(t("chunks.select_file_first"), type="warning")
            return

        chunk_dialog(
            owner_id=self.state["selected_file_id"],
            on_save=self._save_new_chunk,
            on_close=lambda: None,
        )

    async def _save_new_chunk(self, file_id: int, doc_title: str, chunk_text: str):
        """保存新增切片"""
        if not doc_title.strip() or not chunk_text.strip():
            ui.notify(t("chunks.title_content_required"), type="warning")
            return

        try:
            await run_sync(
                chunk_service.create_chunk_add_task,
                file_id=file_id,
                doc_title=doc_title.strip(),
                chunk_text=chunk_text.strip(),
            )
        except ValueError as exc:
            ui.notify(str(exc), type="negative")
            return
        ui.notify(t("chunks.add_task_created"), type="positive")
        await self.on_refresh_files()

    async def _reload_chunks(self):
        """重新加载当前文件的切片（异步 + 后端分页）"""
        if self.state["selected_file_id"]:
            # 异步加载第一页数据
            result = await run_sync(
                file_service.get_chunks_paginated,
                self.state["selected_file_id"],
                page=1,
                page_size=self.state["chunk_page_size"]
            )

            # 更新状态
            if result:
                self.state["chunks_data"] = result["chunks"]
                self.state["total_chunks"] = result["total"]
                self.state["total_chunk_pages"] = result["total_pages"]
            else:
                self.state["chunks_data"] = []
                self.state["total_chunks"] = 0
                self.state["total_chunk_pages"] = 1

            # 重置分页到第一页
            self.state["chunk_page"] = 1

            if self.ui_refs.get("chunk_inspector"):
                self.ui_refs["chunk_inspector"].refresh()

    # ==================== 分页功能 ====================

    async def go_to_chunk_page(self, page: int):
        """跳转到指定页（异步加载）"""
        total_pages = self.get_total_chunk_pages()
        if page < 1:
            page = 1
        elif page > total_pages:
            page = total_pages

        # 更新页码，同时废弃上一批卡片发起的原页请求。
        self.invalidate_source_requests()
        self.state["chunk_page"] = page

        # 清空数据，显示加载状态
        self.state["chunks_data"] = []
        if self.ui_refs.get("chunk_inspector"):
            self.ui_refs["chunk_inspector"].refresh()

        # 异步加载新页数据
        result = await run_sync(
            file_service.get_chunks_paginated,
            self.state["selected_file_id"],
            page=page,
            page_size=self.state["chunk_page_size"]
        )

        # 更新数据
        if result:
            self.state["chunks_data"] = result["chunks"]
        else:
            self.state["chunks_data"] = []

        # 刷新 UI
        if self.ui_refs.get("chunk_inspector"):
            self.ui_refs["chunk_inspector"].refresh()

        # 翻页后可见切片整批换过，对比栏跟到新的首个切片所在页
        await self.sync_source_page()

    async def prev_chunk_page(self):
        """上一页（异步加载）"""
        if self.state["chunk_page"] > 1:
            await self.go_to_chunk_page(self.state["chunk_page"] - 1)

    async def next_chunk_page(self):
        """下一页（异步加载）"""
        total_pages = self.get_total_chunk_pages()
        if self.state["chunk_page"] < total_pages:
            await self.go_to_chunk_page(self.state["chunk_page"] + 1)

    def get_total_chunk_pages(self) -> int:
        """获取总页数（从状态中读取）"""
        return self.state.get("total_chunk_pages", 1)

    def get_visible_chunks(self) -> list:
        """获取当前页可见的切片（后端分页，直接返回）"""
        return self.state["chunks_data"]

    # ==================== 批量删除功能 ====================

    def enter_chunk_batch_mode(self):
        """进入切片批量删除模式"""
        if self.state["selected_file_id"] is None:
            ui.notify(t("chunks.select_file_first"), type="warning")
            return
        self.state["chunk_batch_mode"] = True
        self.state["chunk_batch_selected_ids"] = set()
        if self.ui_refs.get("chunk_toolbar_buttons"):
            self.ui_refs["chunk_toolbar_buttons"].refresh()
        if self.ui_refs.get("chunk_inspector"):
            self.ui_refs["chunk_inspector"].refresh()

    def exit_chunk_batch_mode(self):
        """退出切片批量删除模式"""
        self.state["chunk_batch_mode"] = False
        self.state["chunk_batch_selected_ids"] = set()
        if self.ui_refs.get("chunk_toolbar_buttons"):
            self.ui_refs["chunk_toolbar_buttons"].refresh()
        if self.ui_refs.get("chunk_inspector"):
            self.ui_refs["chunk_inspector"].refresh()

    def toggle_chunk_selection(self, chunk_id: int):
        """切换单个切片的选中状态"""
        if chunk_id in self.state["chunk_batch_selected_ids"]:
            self.state["chunk_batch_selected_ids"].discard(chunk_id)
        else:
            self.state["chunk_batch_selected_ids"].add(chunk_id)
        # 只刷工具栏（全选态需要），不重建卡片列表：复选框自身的勾选态由
        # Quasar 在前端维护，而重建一次要把整页切片的 Markdown 全部重渲染。
        if self.ui_refs.get("chunk_toolbar_buttons"):
            self.ui_refs["chunk_toolbar_buttons"].refresh()

    def toggle_chunk_select_all(self):
        """全选/取消全选切片（仅当前页）"""
        visible_chunks = self.get_visible_chunks()
        visible_chunk_ids = {c["id"] for c in visible_chunks}
        if visible_chunk_ids.issubset(self.state["chunk_batch_selected_ids"]):
            # 当前页全部选中，取消选中
            self.state["chunk_batch_selected_ids"] -= visible_chunk_ids
        else:
            # 选中当前页所有切片
            self.state["chunk_batch_selected_ids"] |= visible_chunk_ids
        if self.ui_refs.get("chunk_toolbar_buttons"):
            self.ui_refs["chunk_toolbar_buttons"].refresh()
        if self.ui_refs.get("chunk_inspector"):
            self.ui_refs["chunk_inspector"].refresh()

    def is_all_chunks_selected(self) -> bool:
        """检查当前页是否全选"""
        visible_chunks = self.get_visible_chunks()
        if not visible_chunks:
            return False
        visible_chunk_ids = {c["id"] for c in visible_chunks}
        return visible_chunk_ids.issubset(self.state["chunk_batch_selected_ids"])

    async def confirm_chunk_batch_delete(self):
        """确认批量删除切片"""
        if not self.state["chunk_batch_selected_ids"]:
            ui.notify(t("chunks.batch_none_selected"), type="warning")
            return

        count = len(self.state["chunk_batch_selected_ids"])
        total_chunks = self.state.get("total_chunks", len(self.state["chunks_data"]))

        # 如果选中了所有切片，使用删除文件的警告
        if count >= total_chunks:
            file_info = await run_sync(
                file_service.get_file_by_id, self.state["selected_file_id"]
            )
            filename = file_info["filename"] if file_info else ""
            confirm_dialog(
                title=t("files.delete_confirm_title"),
                message=t("files.delete_confirm_msg", filename=filename),
                on_confirm=self._do_chunk_batch_delete,
                confirm_text=t("confirm_dialog.btn_delete"),
                danger=True,
            )
        else:
            confirm_dialog(
                title=t("chunks.batch_delete_confirm_title"),
                message=t("chunks.batch_delete_confirm_msg", count=count),
                on_confirm=self._do_chunk_batch_delete,
                confirm_text=t("confirm_dialog.btn_delete"),
                danger=True,
            )

    async def _do_chunk_batch_delete(self):
        """执行批量删除切片"""
        ids_to_delete = list(self.state["chunk_batch_selected_ids"])

        # 使用批量删除服务（一次性处理，避免循环阻塞）
        result = await run_sync(
            chunk_service.batch_delete_chunks, ids_to_delete
        )

        deleted_count = result["deleted_count"]
        file_deleted = len(result["deleted_files"]) > 0

        if file_deleted:
            self.state["selected_file_id"] = None
            self.state["chunks_data"] = []
            await self.on_refresh_files()

        # 退出批量模式
        self.state["chunk_batch_mode"] = False
        self.state["chunk_batch_selected_ids"] = set()

        if not file_deleted:
            await self._reload_chunks()

        if self.ui_refs.get("chunk_toolbar_buttons"):
            self.ui_refs["chunk_toolbar_buttons"].refresh()
        if self.ui_refs.get("chunk_inspector"):
            self.ui_refs["chunk_inspector"].refresh()

        if result["failed_count"]:
            ui.notify("；".join(result["errors"]), type="negative")
        if deleted_count:
            ui.notify(t("chunks.batch_deleted", count=deleted_count), type="positive")
