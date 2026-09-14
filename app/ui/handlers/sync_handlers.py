"""
云同步操作处理器

职责:
- 触发后台同步（服务进程里的线程作业，与界面会话无关）
- 轮询同步服务的状态和日志，刷新界面

同步本身不在这里跑：关窗、刷新页面都不会中断它，重新打开后
仍能看到进度、日志和上次结果。
"""

from datetime import datetime
from typing import Optional

from nicegui import ui

from indexing.services.sync_service import get_sync_service, start_sync
from app.i18n import t


class SyncHandlers:
    """云同步操作处理器"""

    def __init__(self, sync_state: dict, ui_refs: dict):
        """
        初始化同步处理器

        Args:
            sync_state: 同步状态字典 {"is_syncing", "last_sync", "logs"}
            ui_refs: UI 组件引用字典
        """
        self.sync_state = sync_state
        self.ui_refs = ui_refs
        self.sync_service = get_sync_service()
        # 上次渲染时的日志版本与运行状态，只在变化时重绘
        self._rendered_log_version: Optional[int] = None
        self._rendered_running: Optional[bool] = None
        # 已经提示过结果的那次同步（按结束时间区分）：本次会话之前结束的不再提示
        finished_at = self.sync_service.last_finished_at
        self._notified_finish: Optional[datetime] = finished_at
        if finished_at is not None:
            # 本轮服务运行期间同步过，以它为准（配置里只记成功的那次）
            self.sync_state["last_sync"] = finished_at.strftime("%Y-%m-%d %H:%M")
        self.refresh_from_service()

    def is_enabled(self) -> bool:
        """检查云同步是否启用"""
        return self.sync_service.is_enabled()

    def _refresh(self, *keys: str) -> None:
        """刷新指定的可刷新组件（未渲染或已销毁时跳过）"""
        for key in keys:
            component = self.ui_refs.get(key)
            if not component:
                continue
            try:
                component.refresh()
            except RuntimeError:
                pass

    def refresh_from_service(self) -> bool:
        """把服务侧的状态同步到界面状态，返回是否有变化"""
        running = self.sync_service.is_running()
        log_version = self.sync_service.get_log_version()

        changed = False
        if running != self._rendered_running:
            self.sync_state["is_syncing"] = running
            self._rendered_running = running
            changed = True
        if log_version != self._rendered_log_version:
            self.sync_state["logs"] = self.sync_service.get_logs()
            self._rendered_log_version = log_version
            changed = True

        finished_at = self.sync_service.last_finished_at
        if finished_at is not None and finished_at != self._notified_finish:
            self._notified_finish = finished_at
            self.sync_state["last_sync"] = finished_at.strftime("%Y-%m-%d %H:%M")
            changed = True
        return changed

    def do_sync(self):
        """启动一次后台同步"""
        if not self.is_enabled():
            ui.notify(t("cloud_sync.not_configured"), type="warning")
            return

        if not start_sync(start_message=t("cloud_sync.sync_started")):
            ui.notify(t("cloud_sync.already_syncing"), type="warning")
            return

        self.poll()

    def poll(self):
        """定时器回调：服务状态有变化时刷新界面"""
        result = self.sync_service.last_result
        finished_at = self.sync_service.last_finished_at
        notify_finish = (
            finished_at is not None and finished_at != self._notified_finish
        )

        if not self.refresh_from_service():
            return

        self._refresh("sync_buttons", "last_sync_info", "sync_logs")

        if notify_finish and result is not None:
            ui.notify(
                result.message, type="positive" if result.success else "negative"
            )

    def clear_logs(self):
        """清空同步日志"""
        self.sync_service.clear_logs()
        self.poll()
