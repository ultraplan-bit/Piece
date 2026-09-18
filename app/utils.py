"""
公共工具函数

职责:
- 提供可复用的工具函数
- 定义公共常量
"""


# 单文件大小上限的真值在导入侧，且随当前生效的解析后端变化，见
# indexing.services.file_service.get_max_file_size()。界面上的两道上限只
# 约束浏览器一次能选多少文件，服务端仍会按同一规则复核。

# 单次批量上传的总大小限制（2GB）
# 必须与单文件限制分开：否则批量传多个各自合规的文件会被 QUploader 整批拒绝。
MAX_TOTAL_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024

# 单次批量上传的文件数量限制
MAX_UPLOAD_FILES = 30


async def open_external(url: str) -> None:
    """在新标签页打开外部链接。

    界面跑在真正的浏览器里，target=_blank 由浏览器自己处理。
    """
    from nicegui import ui

    ui.navigate.to(url, new_tab=True)


def format_size(size_bytes: int) -> str:
    """
    格式化文件大小为人类可读的字符串

    Args:
        size_bytes: 文件大小（字节）

    Returns:
        格式化后的字符串，如 "1.2 MB"
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
