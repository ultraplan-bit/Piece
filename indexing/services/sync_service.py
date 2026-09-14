"""文件级 WebDAV 同步，不同步配置或 SQLite，不合并多端数据库。

首次同步保护两端已有文件，只补缺项；后续以本地为准，可能删除云端对应文件。
作业由核心服务持有，与 GUI 会话无关；目录枚举失败不能被当作空目录。
"""

import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

from webdav4.client import Client as WebDAV4Client
from indexing.settings import get_settings

logger = logging.getLogger(__name__)
_MAX_LOG_ENTRIES = 100


@dataclass
class SyncStatus:
    is_syncing: bool = False
    last_sync_time: Optional[datetime] = None
    last_error: Optional[str] = None
    uploaded_count: int = 0
    downloaded_count: int = 0
    skipped_count: int = 0


@dataclass
class SyncResult:
    success: bool
    uploaded: list = field(default_factory=list)
    downloaded: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    message: str = ""


def _internal_file(name):
    return name == ".piece-generation" or name.startswith((".rebuild-", ".sync-", ".piece-download-"))


def _relative_path(name):
    from .file_service import validate_filename
    if not isinstance(name, str) or not name or name.startswith("/") or "\\" in name:
        raise ValueError("同步路径必须是受控相对路径")
    path = PurePosixPath(name)
    if path.as_posix() != name or len(path.parts) > 64:
        raise ValueError("同步路径不规范或层级过深")
    for part in path.parts:
        validate_filename(part)
    return path


class SyncService:
    def __init__(self):
        self.status = SyncStatus()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._logs: list[dict[str, str]] = []
        self._log_version = 0
        self.last_result: Optional[SyncResult] = None
        self.last_finished_at: Optional[datetime] = None

    def _get_client(self):
        webdav = get_settings().webdav
        if not self.is_enabled() or not webdav.username or not webdav.password:
            return None
        return WebDAV4Client(webdav.hostname, auth=(webdav.username, webdav.password), timeout=60.0)

    def _get_local_paths(self):
        root = get_settings().get_files_path()
        return {"originals": root / "originals", "working": root / "working"}

    def is_enabled(self):
        webdav = get_settings().webdav
        return webdav.enabled and bool(webdav.hostname)

    def is_first_sync(self):
        return get_settings().webdav.last_sync_time is None

    def is_running(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def add_log(self, log_type, message):
        with self._lock:
            self._logs.append({"type": log_type, "message": message,
                               "timestamp": datetime.now().strftime("%H:%M:%S")})
            self._logs = self._logs[-_MAX_LOG_ENTRIES:]
            self._log_version += 1

    def get_logs(self):
        with self._lock:
            return list(self._logs)

    def get_log_version(self):
        with self._lock:
            return self._log_version

    def clear_logs(self):
        with self._lock:
            self._logs.clear()
            self._log_version += 1

    def start(self):
        with self._lock:
            if self.is_running():
                raise RuntimeError("上一轮同步尚未停止")
            self._stop_event.clear()

    def stop(self):
        """等待当前传输及登记回调结束，不遗留会在关库后继续登记的线程。"""
        with self._lock:
            self._stop_event.set()
            thread = self._thread
        if thread is not None:
            thread.join()

    def _check_stopping(self):
        if self._stop_event.is_set():
            raise RuntimeError("应用关闭，同步已停止")

    def start_background_sync(self, on_finished: Optional[Callable[[SyncResult], Any]] = None,
                              start_message="同步已开始..."):
        with self._lock:
            if self._stop_event.is_set() or self.is_running():
                return False
            self.add_log("info", start_message)
            self._thread = threading.Thread(target=self._run_background, args=(on_finished,), daemon=True, name="CloudSync")
            self._thread.start()
        return True

    def _run_background(self, on_finished):
        try:
            result = self.sync(lambda current, total, name: self.add_log("info", f"[{current}/{total}] {name}"))
        except Exception as exc:
            result = SyncResult(False, errors=[type(exc).__name__], message="同步失败，请检查服务日志和连接配置")
        if on_finished is not None:
            try:
                on_finished(result)
            except Exception as exc:
                result.success = False
                result.errors.append(f"登记下载文件失败：{type(exc).__name__}")
        if result.errors:
            result.success = False
            result.message = f"同步部分失败：↑{len(result.uploaded)} ↓{len(result.downloaded)}，错误 {len(result.errors)} 项"
        for name in result.uploaded:
            self.add_log("upload", f"↑ {name}")
        for name in result.downloaded:
            self.add_log("download", f"↓ {name}")
        for error in result.errors:
            self.add_log("error", error)
        self.add_log("success" if result.success else "error", result.message)
        with self._lock:
            self.last_result = result
            self.last_finished_at = datetime.now()
            self.status.last_error = None if result.success else result.message

    def _update_last_sync_time(self):
        from .config_service import update_config
        update_config({"webdav": {"last_sync_time": datetime.now().isoformat()}})

    def _ensure_remote_dir(self, client, path):
        if not path:
            return
        parts = _relative_path(path).parts
        for count in range(1, len(parts) + 1):
            self._check_stopping()
            current = "/".join(parts[:count])
            if not client.exists(current):
                # 权限/网络错误不得吞掉，否则后面会在未知目录状态下继续写。
                client.mkdir(current)

    def _get_local_files(self, local_dir):
        files = {}
        if not local_dir.exists():
            return files
        if local_dir.is_symlink() or local_dir.is_junction():
            raise ValueError("同步根目录不能是链接")
        def on_error(error):
            raise error
        for root, dirs, names in os.walk(local_dir, followlinks=False, onerror=on_error):
            self._check_stopping()
            for name in dirs:
                directory = Path(root) / name
                if directory.is_symlink() or directory.is_junction():
                    raise ValueError("同步目录中存在链接，拒绝按不完整列表执行同步")
            for name in names:
                if _internal_file(name):
                    continue
                path = Path(root) / name
                if path.is_symlink() or not path.is_file():
                    raise ValueError("同步仅支持普通文件")
                rel = path.relative_to(local_dir).as_posix()
                _relative_path(rel)
                files[rel] = path.stat().st_size
        return files

    def _get_remote_files(self, client, remote_dir):
        """webdav4 的 name 相对 base_url；递归返回完整相对路径，不用 display_name。"""
        files = {}
        if not client.exists(remote_dir):
            return files
        pending, visited = [remote_dir], set()
        while pending:
            self._check_stopping()
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            for item in client.ls(current, detail=True):
                if not isinstance(item, dict):
                    raise ValueError("云端目录响应无效")
                raw_name = item.get("name", "")
                name = raw_name.rstrip("/")
                path = _relative_path(name)
                if name == current:
                    continue
                if path.parent.as_posix() != current or not name.startswith(remote_dir + "/"):
                    raise ValueError("云端返回了越界目录项")
                if item.get("type") == "directory" or raw_name.endswith("/"):
                    pending.append(name)
                    continue
                if _internal_file(path.name):
                    continue
                size = item.get("content_length")
                if size is None or int(size) < 0:
                    raise ValueError("云端未返回有效文件大小")
                files[name[len(remote_dir) + 1:]] = int(size)
        return files

    def _download_atomic(self, client, remote_file, local_file, expected_size):
        local_file.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".sync-", dir=local_file.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            client.download_file(remote_file, temporary)
            self._check_stopping()
            if temporary.stat().st_size != expected_size:
                raise OSError("云端文件传输不完整或在传输中发生变化")
            with temporary.open("ab") as output:
                output.flush()
                os.fsync(output.fileno())
            # 只在完整下载后可见；另一写者抢先创建时不覆盖本地文件。
            try:
                os.link(temporary, local_file)
            except FileExistsError:
                return False
            return True
        finally:
            temporary.unlink(missing_ok=True)

    def sync(self, progress_callback=None):
        result = SyncResult(True)
        with self._lock:
            if self.status.is_syncing:
                return SyncResult(False, message="同步正在进行中")
            self.status.is_syncing = True
            self.status.last_error = None
        client = None
        try:
            self._check_stopping()
            client = self._get_client()
            if client is None:
                result.success = False
                result.message = "云同步未启用或连接配置不完整"
                return result
            paths = self._get_local_paths()
            first = self.is_first_sync()
            # 先完整取得两棵目录树。任何枚举异常都在上传/删除之前终止。
            snapshots = []
            for remote, local in paths.items():
                local.mkdir(parents=True, exist_ok=True)
                snapshots.append((remote, local, self._get_local_files(local), self._get_remote_files(client, remote)))
            for remote, local, local_files, remote_files in snapshots:
                self._sync_directory(client, local, remote, result, progress_callback,
                                     snapshot=(local_files, remote_files), first=first)
            self._check_stopping()
            result.success = not result.errors
            if result.success:
                self._update_last_sync_time()
                self.status.last_sync_time = datetime.now()
            result.message = f"同步{'完成' if result.success else '部分失败'}: ↑{len(result.uploaded)} ↓{len(result.downloaded)}，错误 {len(result.errors)} 项"
        except Exception as exc:
            result.success = False
            result.errors.append(f"同步失败：{type(exc).__name__}")
            result.message = "同步失败，请检查目录权限、连接和服务状态；已完成项保留，不要盲目重发"
            logger.error("[Sync] 同步失败 (%s)", type(exc).__name__)
        finally:
            with self._lock:
                self.status.is_syncing = False
                self.status.last_error = None if result.success else result.message
                self.status.uploaded_count = len(result.uploaded)
                self.status.downloaded_count = len(result.downloaded)
                self.status.skipped_count = len(result.skipped)
            if client is not None:
                try:
                    client.http.close()
                except Exception as exc:
                    result.success = False
                    result.errors.append(f"同步连接关闭失败：{type(exc).__name__}")
                    result.message = "同步收尾失败，已完成项保留"
                    self.status.last_error = result.message
        return result

    def _sync_directory(self, client, local_dir, remote_dir, result, progress_callback=None, *, snapshot=None, first=None):
        local_files, remote_files = snapshot if snapshot is not None else (
            self._get_local_files(local_dir), self._get_remote_files(client, remote_dir))
        first = self.is_first_sync() if first is None else first
        names = sorted(local_files.keys() | remote_files.keys())
        for index, rel in enumerate(names, 1):
            self._check_stopping()
            if progress_callback:
                progress_callback(index, len(names), rel)
            local_file = local_dir / rel
            remote_file = f"{remote_dir}/{rel}"
            label = f"{remote_dir}/{rel}"
            try:
                if rel not in local_files:
                    if first:
                        if not local_file.resolve().is_relative_to(local_dir.resolve()):
                            raise ValueError("下载路径越出本地目录")
                        if self._download_atomic(client, remote_file, local_file, remote_files[rel]):
                            result.downloaded.append(label)
                        else:
                            result.skipped.append(label)
                    else:
                        client.remove(remote_file)
                        result.uploaded.append(f"[删除] {label}")
                elif rel not in remote_files or (not first and local_files[rel] != remote_files[rel]):
                    self._ensure_remote_dir(client, str(PurePosixPath(remote_file).parent))
                    # 新项上传不允许覆盖扫描后才出现的远端对象。
                    headers = {"If-None-Match": "*"} if rel not in remote_files else None
                    client.upload_file(local_file, remote_file, overwrite=not first, headers=headers)
                    result.uploaded.append(label)
                else:
                    result.skipped.append(label)
            except Exception as exc:
                result.errors.append(f"{label}: {type(exc).__name__}")
                result.success = False


_sync_service: Optional[SyncService] = None


def get_sync_service():
    global _sync_service
    if _sync_service is None:
        _sync_service = SyncService()
    return _sync_service


def _register_downloads(result):
    from . import file_service
    service = get_sync_service()
    scan = file_service.register_untracked_files()
    if scan["created"]:
        service.add_log("info", f"已将 {len(scan['created'])} 个新文件加入索引队列")
    for failure in scan["failed"]:
        message = f"{failure['filename']}: {failure['error']}"
        service.add_log("error", message)
        result.errors.append(message)
        result.success = False


def start_sync(start_message="同步已开始..."):
    return get_sync_service().start_background_sync(on_finished=_register_downloads, start_message=start_message)
