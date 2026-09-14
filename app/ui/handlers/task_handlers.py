"""
任务订阅处理器

职责:
- 订阅任务表：按 updated_at 增量拉取有变化的任务
- 维护界面用的任务进度缓存
- 任务进入终态后提示并刷新受影响的界面

界面不再登记自己创建了哪些任务。任何来源（上传、重新索引、MCP 工具、
云同步后的扫描）写进任务表的行都会被同一份订阅看到，关窗重开、刷新页面
只是重新定一次订阅起点，进度不会丢。
"""

from indexing.utils import run_sync
from typing import Optional

from nicegui import ui

from indexing.services import task_service
from app.i18n import t


class TaskHandlers:
    """任务订阅处理器"""

    def __init__(self, state: dict, ui_refs: dict, file_handlers=None):
        """
        初始化任务处理器

        Args:
            state: 共享状态字典
            ui_refs: UI 组件引用字典
            file_handlers: 文件处理器实例（任务完成后刷新文件列表）
        """
        self.state = state
        self.ui_refs = ui_refs
        self.file_handlers = file_handlers
        self.chunk_handlers = None
        # 任务进度缓存 {task_id: {"file_id", "filename", "progress", "status", ...}}
        self.state["task_progress"] = {}
        self.state["task_progress_by_file_id"] = {}
        # 订阅起点：只拉 updated_at >= _marker 的行（活跃任务不受此限制，每轮都拉）。
        # 闭区间会把标记时刻的行重复带回来，合并逻辑是幂等的，重复无害
        self._marker: Optional[str] = None
        self._polling = False

    def set_file_handlers(self, file_handlers):
        """设置文件处理器引用（用于解决循环依赖）"""
        self.file_handlers = file_handlers

    def set_chunk_handlers(self, chunk_handlers):
        """设置切片处理器引用（用于解决循环依赖）"""
        self.chunk_handlers = chunk_handlers

    def _rebuild_task_progress_index(self):
        """按文件 ID 建立任务进度索引，避免渲染时重复遍历任务。"""
        self.state["task_progress_by_file_id"] = {
            progress_info["file_id"]: progress_info
            for progress_info in self.state["task_progress"].values()
            if progress_info.get("file_id") is not None
        }

    def _mark_task_progress_rendered(self):
        """记录最近一次已渲染的任务状态，支持按累计进度触发刷新。"""
        for progress_info in self.state["task_progress"].values():
            progress_info["rendered_progress"] = progress_info["progress"]
            progress_info["rendered_status"] = progress_info["status"]

    @staticmethod
    def _task_snapshot(task: dict) -> dict:
        """把任务行转成 UI 展示用的进度快照。"""
        progress = task.get("progress", 0)
        status = task.get("status", "pending")
        return {
            "file_id": task.get("file_id"),
            "filename": task.get("original_filename", ""),
            "progress": progress,
            "rendered_progress": progress,
            "current_page": task.get("current_page", 0),
            "total_pages": task.get("total_pages", 0),
            "processed_chunks": task.get("processed_chunks", 0),
            "stage": task.get("stage") or "queued",
            "status": status,
            "rendered_status": status,
        }

    async def init_active_tasks(self):
        """
        页面加载时装入进行中的任务，并把订阅起点定在现在

        先取标记再查活跃任务：两步之间发生的变化会被第一次轮询重新读到，
        进度缓存的写入是幂等的，重读没有副作用。
        """
        marker = task_service.now_marker()
        active_tasks = await run_sync(task_service.get_active_tasks)
        for task in active_tasks:
            self.state["task_progress"][task["id"]] = self._task_snapshot(task)

        self._marker = marker
        self._rebuild_task_progress_index()

        # 如果有活跃任务，刷新文件列表
        if active_tasks and self.ui_refs.get("file_list_container"):
            self.ui_refs["file_list_container"].refresh()

    async def poll(self):
        """
        拉取自上次轮询以来有变化的任务并更新界面（由页面定时器每秒调用）

        - 活跃任务：更新进度缓存，进度累计变化 5%、状态或阶段变化时刷新列表
        - 终态任务：从缓存移除，提示结果，刷新文件列表、统计和当前文件的切片
        - 任务关联的文件还不在列表里（MCP 新建、同步扫描出的）：刷新文件列表
        """
        if self._marker is None or self._polling:
            return

        self._polling = True
        try:
            marker = self._marker
            tasks = await run_sync(
                task_service.get_active_or_updated_since, marker
            )

            # 活跃任务每轮都会返回。缓存里有、这轮却没回来的任务，只可能是
            # 进入终态但时间戳早于标记（时钟回拨）或行已被删除：按 id 补查，
            # 别让进度条一直挂着
            returned_ids = {task["id"] for task in tasks}
            missing = [
                task_id for task_id in self.state["task_progress"]
                if task_id not in returned_ids
            ]
            dropped = False
            if missing:
                found = await run_sync(task_service.get_tasks_by_ids, missing)
                tasks = tasks + found
                found_ids = {task["id"] for task in found}
                for task_id in missing:
                    if task_id not in found_ids:
                        self.state["task_progress"].pop(task_id, None)
                        dropped = True

            list_changed, full_refresh_needed = self._absorb(tasks, marker)

            if full_refresh_needed or dropped:
                await self._refresh_all_async()
                self._mark_task_progress_rendered()
            elif list_changed and self.ui_refs.get("file_list_container"):
                self.ui_refs["file_list_container"].refresh()
                self._mark_task_progress_rendered()
        finally:
            self._polling = False

    def _absorb(self, tasks: list, marker: str) -> tuple:
        """把一批任务行合并进进度缓存，返回 (列表需要刷新, 需要全量刷新)。

        合并是幂等的：同一行被重复带回（闭区间查询、按 id 补查）不会产生
        重复提示。终态任务只在两种情况下提示——缓存里正跟踪着它，或它是
        标记之后才结束的（创建和完成都发生在两次轮询之间的快任务）。
        """
        list_changed = False
        full_refresh_needed = False
        known_file_ids = {f["id"] for f in self.state.get("files_data", [])}
        next_marker = marker

        for task in tasks:
            task_id = task["id"]
            updated_at = task.get("updated_at") or ""
            if updated_at > next_marker:
                next_marker = updated_at

            status = task.get("status", "pending")
            if status in task_service.ACTIVE_STATUSES:
                existing = self.state["task_progress"].get(task_id)
                snapshot = self._task_snapshot(task)
                if existing is None:
                    self.state["task_progress"][task_id] = snapshot
                    list_changed = True
                    # 关联文件还没出现在列表里，光有进度条没处挂
                    if task.get("file_id") not in known_file_ids:
                        full_refresh_needed = True
                    continue

                rendered_progress = existing.get("rendered_progress", existing["progress"])
                rendered_status = existing.get("rendered_status", existing["status"])
                snapshot["rendered_progress"] = rendered_progress
                snapshot["rendered_status"] = rendered_status
                self.state["task_progress"][task_id] = snapshot
                # 降低重绘频率：进度累计变化 5%、状态或阶段变化时才刷新列表
                list_changed = (
                    list_changed
                    or status != rendered_status
                    or snapshot["stage"] != existing.get("stage")
                    or snapshot["progress"] - rendered_progress >= 5
                )
                continue

            # 终态：completed / failed / cancelled
            tracked = self.state["task_progress"].pop(task_id, None) is not None
            if tracked or updated_at > marker:
                full_refresh_needed = True
                self._notify_finished(status, task)

        self._marker = next_marker
        self._rebuild_task_progress_index()
        return list_changed, full_refresh_needed

    @staticmethod
    def _notify_finished(status: str, task: dict) -> None:
        """按终态提示；文件被删除导致的取消静默收尾。"""
        if status == "completed":
            ui.notify(t("task.completed"), type="positive")
        elif status == "failed":
            error_msg = task.get("error_message") or ""
            if error_msg:
                ui.notify(f"{t('task.failed')}: {error_msg}", type="negative")
            else:
                ui.notify(t("task.failed"), type="negative")

    async def _refresh_all_async(self):
        """刷新文件列表、统计和当前文件的切片。"""
        if self.file_handlers:
            await self.file_handlers.load_files()
        elif self.ui_refs.get("file_list_container"):
            self.ui_refs["file_list_container"].refresh()

        # 切片走后端分页重载，避免把整份文档的切片一次性渲染出来
        if self.state.get("selected_file_id") and self.chunk_handlers:
            await self.chunk_handlers._reload_chunks()
