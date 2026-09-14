"""
重新索引测试

process_task 本身就是幂等的（重读配置 → 清空旧切片 → 重新解析入库），
所以重新索引只是给已有 file_id 再建一个任务。这里验证入口的两条约束：
- 同一文件已有活跃任务时不再建新任务，否则两个任务会互相清空对方的切片
- 有原件和没原件的文件给出不同的确认文案（前者会覆盖手工编辑）
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.ui.handlers import file_handlers as file_handlers_module
from app.ui.handlers.file_handlers import FileHandlers


class _Notify(list):
    """记录 ui.notify 调用"""

    def __call__(self, message, **kwargs):
        self.append((message, kwargs.get("type")))


def _handlers(monkeypatch, files_data: list, active_tasks: list):
    """构造只保留重新索引所需依赖的处理器"""
    notify = _Notify()
    dialogs = []
    created = []

    monkeypatch.setattr(
        file_handlers_module, "ui", type("U", (), {"notify": staticmethod(notify)})()
    )
    monkeypatch.setattr(
        file_handlers_module,
        "confirm_dialog",
        lambda **kwargs: dialogs.append(kwargs),
    )
    monkeypatch.setattr(
        file_handlers_module.task_service, "get_active_tasks", lambda: active_tasks
    )
    monkeypatch.setattr(
        file_handlers_module.file_service,
        "reindex_file",
        lambda file_id: created.append(file_id) or {"task_id": 99},
    )

    handlers = FileHandlers.__new__(FileHandlers)
    handlers.state = {"files_data": files_data}
    handlers.ui_refs = {}
    handlers.load_files = lambda: asyncio.sleep(0)
    return handlers, notify, dialogs, created


_UPLOADED = {
    "id": 1,
    "filename": "数学练习.md",
    "original_file_path": "C:/data/originals/数学练习.pptx",
    "original_file_type": "pptx",
}
_NOTE = {"id": 2, "filename": "随手记.md", "original_file_path": None}


def test_blocked_while_indexing(monkeypatch):
    """已有活跃任务时不再建新任务，避免两个任务互相清空切片"""
    handlers, notify, dialogs, created = _handlers(
        monkeypatch, [_UPLOADED], [{"id": 7, "file_id": 1}]
    )

    asyncio.run(handlers.handle_reindex_file(1))

    assert created == []
    assert dialogs == []
    assert notify and notify[0][1] == "warning"


def test_other_files_task_does_not_block(monkeypatch):
    """别的文件在索引不影响本文件"""
    handlers, _, dialogs, _ = _handlers(
        monkeypatch, [_UPLOADED], [{"id": 7, "file_id": 42}]
    )

    asyncio.run(handlers.handle_reindex_file(1))

    assert len(dialogs) == 1


def test_uploaded_file_warns_about_edits(monkeypatch):
    """有原件的文件从原件重解析，要提示手工编辑会被覆盖"""
    handlers, _, dialogs, created = _handlers(monkeypatch, [_UPLOADED], [])

    asyncio.run(handlers.handle_reindex_file(1))

    assert "覆盖" in dialogs[0]["message"] or "overwritten" in dialogs[0]["message"]

    # 确认后才真正建任务
    asyncio.run(_call(dialogs[0]["on_confirm"]))
    assert created == [1]


def test_note_file_uses_milder_wording(monkeypatch):
    """应用内新建的文件解析的是工作文件本身，内容不会变"""
    handlers, _, dialogs, _ = _handlers(monkeypatch, [_NOTE], [])

    asyncio.run(handlers.handle_reindex_file(2))

    message = dialogs[0]["message"]
    assert "覆盖" not in message and "overwritten" not in message


def test_unknown_file_is_rejected(monkeypatch):
    """文件列表里没有的 id 不建任务"""
    handlers, notify, dialogs, created = _handlers(monkeypatch, [_UPLOADED], [])

    asyncio.run(handlers.handle_reindex_file(999))

    assert created == [] and dialogs == []
    assert notify and notify[0][1] == "warning"


async def _call(callback):
    """确认回调是 `lambda: self._do_xxx(...)`，返回协程需要 await"""
    result = callback()
    if asyncio.iscoroutine(result):
        await result
