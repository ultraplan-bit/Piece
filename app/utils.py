"""
公共工具函数

职责:
- 提供可复用的工具函数
- 定义公共常量
"""


# 单文件大小限制（500MB）
# 上传与解析全程流式（>1MB 由 NiceGUI spool 到临时文件，PDF 逐页解析），
# 放宽上限不会抬高内存峰值。真正的处理闸门是 converter.MAX_PDF_PAGES；
# 500MB 大致对应 300dpi 彩色扫描的数百页，与页数上限量级相当。
MAX_FILE_SIZE = 500 * 1024 * 1024

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
