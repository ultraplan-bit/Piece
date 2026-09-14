"""服务进程内的索引编排生命周期；原生解析另交短命 helper。"""

import asyncio
import logging
import threading
from typing import Optional

from indexing.services import task_service
from indexing.services.parser_helper import start_parser_helpers, stop_parser_helpers
from indexing.services.processor import TaskProcessor, mark_file_failure
from indexing.settings import get_performance_config
from indexing.utils import run_sync

logger = logging.getLogger(__name__)


class WorkerManager:
    """在应用事件循环中管理任务编排，不再启动业务 Worker 进程。"""

    def __init__(self, shutdown_timeout: float = 15.0):
        self.shutdown_timeout = shutdown_timeout
        self._task: Optional[asyncio.Task] = None
        self._stop_event = threading.Event()
        self._lock = asyncio.Lock()

    def _mark_processing_failed(self, error_message: str) -> int:
        """恢复遗留任务；切片编辑失败不改变已有文件的索引状态。"""
        processing_tasks = task_service.get_processing_tasks()
        failed_count = task_service.mark_processing_tasks_failed(error_message)
        for task in processing_tasks:
            file_id = task.get("file_id")
            if file_id and task["task_type"] == "file_index":
                mark_file_failure(file_id)
        return failed_count

    async def start(self) -> None:
        async with self._lock:
            if self.is_alive():
                return
            failed_count = await run_sync(
                self._mark_processing_failed, "应用重启，原索引任务已中断",
            )
            if failed_count:
                logger.warning("[Worker] 已恢复 %s 个遗留任务", failed_count)
            from indexing.services.chunk_service import recover_working_files
            from indexing.services.file_service import recover_file_storage
            await run_sync(recover_file_storage)
            await run_sync(recover_working_files)
            start_parser_helpers(get_performance_config().worker_concurrency)
            self._stop_event = threading.Event()
            self._task = asyncio.create_task(
                TaskProcessor().run(self._stop_event), name="PieceWorker",
            )
            self._task.add_done_callback(self._on_done)
            logger.info("[Worker] 服务内任务编排已启动")

    def _on_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("[Worker] 任务编排异常退出", exc_info=task.exception())

    async def stop(self) -> None:
        """停止领任务；取消等待并排空同步写入后，调用方才可关闭数据库。"""
        async with self._lock:
            if self._task is None:
                await run_sync(stop_parser_helpers)
                return
            self._stop_event.set()
            helpers = asyncio.create_task(run_sync(stop_parser_helpers))
            done, _ = await asyncio.wait({self._task}, timeout=self.shutdown_timeout)
            if not done:
                logger.warning("[Worker] 正在取消在途任务并等待同步操作收尾")
                self._task.cancel()
            # ponytail: OCR/VLM 沿用同步客户端，退出须等当前 HTTP 请求结束；
            # 如需秒级退出，再改 AsyncClient，不能遗留仍在使用资源的线程。
            results = await asyncio.gather(self._task, helpers, return_exceptions=True)
            # 编排任务取消是预期行为；helper 回收失败则不能向 Runtime 宣称已停止。
            if isinstance(results[1], BaseException):
                raise results[1]
            await run_sync(self._mark_processing_failed, "应用关闭，索引任务已中断")
            self._task = None
            logger.info("[Worker] Worker 已停止")

    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done()


worker_manager = WorkerManager()
