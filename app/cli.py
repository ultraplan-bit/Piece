"""Piece 轻量命令行入口。

CLI 本身只依赖 Python 标准库。除 ``serve``、``start``、``open`` 和 ``autostart`` 等用户
明确要求的动作外，不会启动服务、打开数据库或创建数据目录；API 命令只通过
``app.client`` 访问已经运行的本地 HTTP 服务。
"""

from __future__ import annotations

import argparse
import errno
import importlib.util
import json
import multiprocessing
import os
import shlex
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, NoReturn

from app.client import (
    DEFAULT_PORT,
    ClientError,
    PieceClient,
    make_envelope,
    read_json_file,
)
from app.import_scan import import_candidates
from app.skills import get_version

VERSION = get_version()
_DEFAULT_TIMEOUT = 300
_JSON_REQUESTED = False


class CliParser(argparse.ArgumentParser):
    """在 ``--json`` 模式下把 argparse 错误也写成标准 JSON。"""

    def error(self, message: str) -> NoReturn:  # pragma: no cover - argparse 分支按输入覆盖
        if _JSON_REQUESTED:
            _write_json(make_envelope(False, message, None, {
                "code": "INVALID_ARGUMENT",
                "message": message,
            }))
        else:
            self.print_usage(sys.stderr)
            print(f"{self.prog}: error: {message}", file=sys.stderr)
        self.exit(2)


def _write_json(value: Any) -> None:
    """stdout 只写 UTF-8 JSON，不写日志或诊断信息。"""
    json.dump(value, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    try:
        sys.stdout.flush()
    except OSError as exc:
        # 无控制台子系统的 exe 在 cmd 直接执行时 flush 抛 EINVAL；
        # 数据已交给流，不让诊断噪声盖住 JSON 输出
        if exc.errno != errno.EINVAL:
            raise


def _write_result(result: dict[str, Any], json_mode: bool) -> None:
    if json_mode:
        _write_json(result)
        return
    if result.get("success"):
        print(result.get("message", "操作成功"))
        data = result.get("data")
        if data is not None:
            print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(result.get("message", "操作失败"), file=sys.stderr)
        if result.get("data") is not None:
            print(json.dumps(result["data"], ensure_ascii=False, indent=2), file=sys.stderr)


def _exit_code(result: dict[str, Any]) -> int:
    if result.get("success"):
        return 0
    code = (result.get("error") or {}).get("code")
    if code in {"WAIT_TIMEOUT", "START_TIMEOUT"}:
        return 4
    if code in {"SERVICE_UNAVAILABLE", "NOT_READY"}:
        return 3
    if code in {"INVALID_ARGUMENT", "INVALID_INPUT", "INVALID_JSON"}:
        return 2
    return 1


def _port(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("端口必须是整数") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须在 1-65535 之间")
    return port


def _positive(value: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("必须是正整数") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def _nonnegative(value: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("必须是非负整数") from None
    if number < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return number


def _is_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    """每个叶子命令都接受命令后的机器输出、端口和数据目录选项。"""
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="输出标准 JSON envelope")
    parser.add_argument("--port", type=_port, default=argparse.SUPPRESS,
                        help=f"管理 API 端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS,
                        help="配置目录（覆盖 PIECE_DATA_DIR 和平台默认目录）")


def _add_wait_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--wait", action="store_true", help="等待受理的任务完成")
    parser.add_argument("--timeout", type=_positive, default=_DEFAULT_TIMEOUT,
                        help=f"--wait 超时秒数（默认 {_DEFAULT_TIMEOUT}）")


def _add_confirmation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--yes", action="store_true", help="确认破坏性操作或允许覆盖")
    parser.add_argument("--dry-run", action="store_true", help="只检查将要执行的操作")


def _build_parser() -> CliParser:
    parser = CliParser(prog="piece", description="Piece 本地知识库命令行工具")
    parser.add_argument("--version", action="store_true", help="显示版本并退出")
    # 全局形式也支持，命令后的同名选项由叶子 parser 接收。
    parser.add_argument("--json", action="store_true", default=False,
                        help="输出标准 JSON envelope")
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT,
                        help=f"管理 API 端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="配置目录（覆盖 PIECE_DATA_DIR 和平台默认目录）")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="前台运行服务，Ctrl+C 安全退出")
    _add_runtime_options(serve)
    serve.add_argument("--open", action="store_true", help="服务就绪后打开管理界面")
    serve.add_argument("--no-gui", action="store_true", help="不启动 GUI")
    serve.add_argument("--no-mcp", action="store_true", help="不启动 MCP")
    serve.add_argument("--no-tray", action="store_true", help="不启动托盘")

    start = commands.add_parser("start", help="后台启动服务并等待就绪（默认无 GUI、MCP 和托盘）")
    _add_runtime_options(start)
    start.add_argument("--gui", action=argparse.BooleanOptionalAction, default=None, help="启用管理界面")
    start.add_argument("--mcp", action=argparse.BooleanOptionalAction, default=False, help="启用 MCP 服务")
    start.add_argument("--open", action="store_true", help="启用并打开管理界面")
    start.add_argument("--timeout", type=_positive, default=60, help="等待就绪的总超时秒数（默认 60）")

    open_ui = commands.add_parser("open", help="打开已运行服务的管理界面，不启动第二个服务")
    _add_runtime_options(open_ui)

    autostart = commands.add_parser("autostart", help="安装或卸载当前用户登录自启")
    actions = autostart.add_subparsers(dest="action", required=True)
    install = actions.add_parser("install", help="配置登录自启，不启动当前服务")
    uninstall = actions.add_parser("uninstall", help="移除登录自启，不停止当前服务")
    for action in (install, uninstall):
        _add_runtime_options(action)
    install.add_argument("--no-tray", action="store_true", help=argparse.SUPPRESS)

    status = commands.add_parser("status", help="查询服务状态")
    _add_runtime_options(status)

    stats = commands.add_parser("stats", help="查看入库文件数、已索引文件数、切片数及原件大小统计")
    _add_runtime_options(stats)

    stop = commands.add_parser("stop", help="安全停止目标知识库的服务（等待在途工作收尾）")
    _add_runtime_options(stop)

    doctor = commands.add_parser("doctor", help="纯本地诊断依赖、路径、Python 和 SQLite")
    _add_runtime_options(doctor)
    doctor.add_argument("--server", action="store_true", help="额外探测本地服务握手")

    file_parser = commands.add_parser("file", help="文件操作")
    file_commands = file_parser.add_subparsers(dest="file_command", required=True)

    file_list = file_commands.add_parser("list", help="分页列出文件")
    _add_runtime_options(file_list)
    file_list.add_argument("--limit", type=_positive, default=20)
    file_list.add_argument("--offset", type=_nonnegative, default=0)
    file_list.add_argument("--status")
    file_list.add_argument("--name", help="按文件名包含的文字筛选（不是通配符）")
    file_list.add_argument("--collection", dest="collections", action="append", help="完整集合名，可重复；默认包含后代")
    file_list.add_argument("--direct-only", action="store_true", help="集合范围只读直接归属，不包含后代")
    file_list.add_argument("--uncategorized", action="store_true", help="只列出没有任何直接集合关联的文件")
    file_list.add_argument("--sort-by", choices=("created_at", "updated_at", "filename", "id"), default="created_at")
    file_list.add_argument("--ascending", action="store_true", help="升序排列（默认降序）")

    file_scan = file_commands.add_parser("scan", help="扫描原件目录，登记未入库文件并创建索引任务")
    _add_runtime_options(file_scan)
    file_scan.add_argument("--dry-run", action="store_true", help="只列出待登记文件，不创建索引任务")
    _add_wait_options(file_scan)

    file_get = file_commands.add_parser("get", help="获取文件详情")
    _add_runtime_options(file_get)
    file_get.add_argument("file_id", type=_positive)

    file_create = file_commands.add_parser("create", help="创建空白 Markdown 文件")
    _add_runtime_options(file_create)
    file_create.add_argument("filename")
    file_create.add_argument("--collection", dest="collections", action="append")

    file_import = file_commands.add_parser("import", help="导入一个或多个文件")
    _add_runtime_options(file_import)
    file_import.add_argument("paths", nargs="+", type=Path)
    file_import.add_argument("--recursive", action="store_true", help="递归导入目录")
    file_import.add_argument("--collection", dest="collections", action="append",
                             help="已存在的完整集合名，可重复")
    file_import.add_argument("--properties", help="附加到每个导入文件的属性：JSON 对象文件路径，或 - 读 stdin")
    file_import.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                             help="目录导入时排除的相对路径或名称通配（如 templates、attachments/*），可重复")
    file_import.add_argument("--include-hidden", action="store_true",
                             help="目录导入时不跳过以点开头的隐藏目录和文件（默认跳过 .obsidian、.trash 等）")
    file_import.add_argument("--skip-link-notes", action="store_true",
                             help="跳过几乎只有链接的 Markdown 索引笔记（Obsidian 的 MOC / 目录页）")
    _add_wait_options(file_import)

    import_markdown = file_commands.add_parser(
        "import-markdown", help="把已取得的 Markdown 正文整篇导入（网页、文章、字幕等），自动切片并保留原件")
    _add_runtime_options(import_markdown)
    import_markdown.add_argument("filename", help="文件名，自动补 .md 后缀，重名时自动加序号")
    import_markdown.add_argument("--input", required=True, help="正文 UTF-8 文件路径，或 - 读 stdin；可自带 YAML frontmatter")
    import_markdown.add_argument("--collection", dest="collections", action="append",
                                 help="已存在的完整集合名，可重复")
    import_markdown.add_argument("--properties", help="文件属性：JSON 对象文件路径，或 - 读 stdin；与 frontmatter 合并且优先")
    _add_wait_options(import_markdown)

    file_reindex = file_commands.add_parser("reindex", help="重新索引文件")
    _add_runtime_options(file_reindex)
    file_reindex.add_argument("file_id", type=_positive)
    file_reindex.add_argument("--source", choices=("original", "working"), help="默认有原件则用原件，否则用工作文件")
    file_reindex.add_argument("--request-id")
    _add_wait_options(file_reindex)
    _add_confirmation_options(file_reindex)

    file_delete = file_commands.add_parser("delete", help="删除文件")
    _add_runtime_options(file_delete)
    file_delete.add_argument("file_ids", nargs="+", type=_positive)
    _add_confirmation_options(file_delete)

    file_properties = file_commands.add_parser("properties", help="设置文件属性")
    _add_runtime_options(file_properties)
    file_properties.add_argument("file_id", type=_positive)
    file_properties.add_argument("--input", required=True, help="JSON 对象文件路径，或 - 读 stdin")

    export = file_commands.add_parser("export", help="导出原件或 Markdown")
    _add_runtime_options(export)
    export.add_argument("file_id", type=_positive)
    export.add_argument("--format", choices=("markdown", "original"), default="markdown")
    export.add_argument("--output", type=Path, required=True,
                        help="本地输出文件；若为已有目录则使用服务端文件名")
    export.add_argument("--yes", action="store_true", help="允许覆盖输出文件")
    export.add_argument("--with-resources", action="store_true", help="把 Markdown 和引用资源导出为 ZIP")

    page = file_commands.add_parser("page", help="下载文件第 n 页渲染图")
    _add_runtime_options(page)
    page.add_argument("file_id", type=_positive)
    page.add_argument("page_number", type=_positive)
    page.add_argument("--output", type=Path, required=True, help="本地输出文件")
    page.add_argument("--yes", action="store_true", help="允许覆盖输出文件")

    chunk_parser = commands.add_parser("chunk", help="切片操作")
    chunk_commands = chunk_parser.add_subparsers(dest="chunk_command", required=True)

    chunk_list = chunk_commands.add_parser("list", help="分页列出文件切片")
    _add_runtime_options(chunk_list)
    chunk_list.add_argument("file_id", type=_positive)
    chunk_list.add_argument("--page", type=_positive, default=1)
    chunk_list.add_argument("--page-size", type=_positive, default=50)

    chunk_get = chunk_commands.add_parser("get", help="获取切片详情")
    _add_runtime_options(chunk_get)
    chunk_get.add_argument("chunk_id", type=_positive)

    chunk_extract = chunk_commands.add_parser(
        "extract", help="从卡片正文切出精确引文，直接产出可提交的知识证据",
        description="定位可宽松，返回的 quote 始终是正文精确子串，仍受知识证据逐字校验约束。")
    _add_runtime_options(chunk_extract)
    chunk_extract.add_argument("chunk_id", type=_positive)
    locate = chunk_extract.add_mutually_exclusive_group(required=True)
    locate.add_argument("--lines", help="行号或范围，如 12 或 12-14（1 基）")
    locate.add_argument("--grep", help="按行匹配定位；默认子串匹配")
    chunk_extract.add_argument("--regex", action="store_true", help="--grep 按正则表达式匹配")
    chunk_extract.add_argument("--context", type=_nonnegative, default=0, help="--grep 时前后各取的行数")
    chunk_extract.add_argument("--max-matches", type=_positive, default=20, help="--grep 最多返回的匹配数（最大 50）")
    chunk_extract.add_argument("--out", type=Path, help="将唯一且未截断的匹配保存为证据 JSON；不覆盖已有文件")

    chunk_add = chunk_commands.add_parser("add", help="新增切片")
    _add_runtime_options(chunk_add)
    chunk_add.add_argument("file_id", type=_positive)
    chunk_add.add_argument("--title", required=True, help="切片标题")
    chunk_add.add_argument("--input", required=True, help="正文 UTF-8 文件路径，或 - 读 stdin")
    chunk_add.add_argument("--request-id")
    _add_wait_options(chunk_add)

    chunk_batch = chunk_commands.add_parser("batch-add", help="批量新增切片")
    _add_runtime_options(chunk_batch)
    chunk_batch.add_argument("file_id", type=_positive)
    chunk_batch.add_argument("--input", required=True, help="JSON 数组 UTF-8 文件路径，或 - 读 stdin")
    chunk_batch.add_argument("--request-id")
    _add_wait_options(chunk_batch)

    chunk_update = chunk_commands.add_parser("update", help="修改切片")
    _add_runtime_options(chunk_update)
    chunk_update.add_argument("chunk_id", type=_positive)
    chunk_update.add_argument("--title", help="新的切片标题")
    chunk_update.add_argument("--input", help="正文 UTF-8 文件路径，或 - 读 stdin")
    chunk_update.add_argument("--request-id")
    _add_wait_options(chunk_update)

    chunk_delete = chunk_commands.add_parser("delete", help="删除切片")
    _add_runtime_options(chunk_delete)
    chunk_delete.add_argument("chunk_ids", nargs="+", type=_positive)
    _add_confirmation_options(chunk_delete)

    chunk_images = chunk_commands.add_parser("images", help="列出并可下载切片图片")
    _add_runtime_options(chunk_images)
    chunk_images.add_argument("chunk_id", type=_positive)
    chunk_images.add_argument("--output-dir", type=Path,
                              help="提供时下载全部图片到已有目录")
    chunk_images.add_argument("--yes", action="store_true", help="允许覆盖图片")

    chunk_image = chunk_commands.add_parser("image", help="下载切片的一张图片")
    _add_runtime_options(chunk_image)
    chunk_image.add_argument("chunk_id", type=_positive)
    chunk_image.add_argument("image_index", type=_nonnegative)
    chunk_image.add_argument("--output", type=Path, required=True)
    chunk_image.add_argument("--yes", action="store_true", help="允许覆盖输出文件")

    collection_parser = commands.add_parser("collection", help="层级集合操作（逻辑分类，不移动原件）",
                                            description="集合是逻辑分类：读取父范围默认包含后代，归类只写所选集合，不自动加入祖先。名称中的 / 是普通字符，不是层级路径。")
    collection_commands = collection_parser.add_subparsers(dest="collection_command", required=True)

    collection_list = collection_commands.add_parser("list", help="分页列出集合")
    _add_runtime_options(collection_list)
    collection_list.add_argument("--limit", type=_positive, default=20)
    collection_list.add_argument("--offset", type=_nonnegative, default=0)

    collection_tree = collection_commands.add_parser("tree", help="读取集合树及直接/子树文件计数，不附带文件正文")
    _add_runtime_options(collection_tree)

    collection_create = collection_commands.add_parser("create", help="创建根集合或子集合")
    _add_runtime_options(collection_create)
    collection_create.add_argument("name")
    collection_create.add_argument("--description")
    collection_create.add_argument("--parent-id", type=_positive, help="父集合 ID；省略则创建根集合")

    collection_move = collection_commands.add_parser("move", help="移动集合，不改动文件直接归属和原件")
    _add_runtime_options(collection_move)
    collection_move.add_argument("collection_id", type=_positive)
    parent = collection_move.add_mutually_exclusive_group(required=True)
    parent.add_argument("--parent-id", type=_positive, help="目标父集合 ID")
    parent.add_argument("--root", action="store_true", help="移到根层")

    collection_rename = collection_commands.add_parser("rename", help="重命名集合")
    _add_runtime_options(collection_rename)
    collection_rename.add_argument("collection_id", type=_positive)
    collection_rename.add_argument("name")

    collection_delete = collection_commands.add_parser("delete", help="删除叶子集合，仅解除直接归类、不删除文件；有子集合时拒绝")
    _add_runtime_options(collection_delete)
    collection_delete.add_argument("collection_id", type=_positive)
    _add_confirmation_options(collection_delete)

    collection_set = collection_commands.add_parser("set", help="覆盖文件的直接集合归属，不自动加入祖先或后代")
    _add_runtime_options(collection_set)
    collection_set.add_argument("file_ids", nargs="+", type=_positive)
    collection_set.add_argument("--collection", dest="collection_names", action="append")

    task_parser = commands.add_parser("task", help="任务操作")
    task_commands = task_parser.add_subparsers(dest="task_command", required=True)

    task_list = task_commands.add_parser("list", help="分页列出任务")
    _add_runtime_options(task_list)
    task_list.add_argument("--limit", type=_positive, default=20)
    task_list.add_argument("--offset", type=_nonnegative, default=0)
    task_list.add_argument("--status")
    task_list.add_argument("--request-id", help="按受理请求键查找任务（含批量子任务）")

    task_get = task_commands.add_parser("get", help="查询一个任务")
    _add_runtime_options(task_get)
    task_get.add_argument("task_id", type=_positive)

    task_query = task_commands.add_parser("query", help="批量查询任务")
    _add_runtime_options(task_query)
    task_query.add_argument("task_ids", nargs="+", type=_positive)

    task_cancel = task_commands.add_parser("cancel", help="取消任务")
    _add_runtime_options(task_cancel)
    task_cancel.add_argument("task_id", type=_positive)

    task_retry = task_commands.add_parser("retry", help="重试任务")
    _add_runtime_options(task_retry)
    task_retry.add_argument("task_id", type=_positive)
    _add_wait_options(task_retry)

    task_wait = task_commands.add_parser("wait", help="等待任务到达终态")
    _add_runtime_options(task_wait)
    task_wait.add_argument("task_ids", nargs="+", type=_positive)
    task_wait.add_argument("--timeout", type=_positive, default=_DEFAULT_TIMEOUT,
                           help=f"超时秒数（默认 {_DEFAULT_TIMEOUT}）")

    search = commands.add_parser("search", help="搜索知识库")
    _add_runtime_options(search)
    search.add_argument("query")
    search.add_argument("--file-id", dest="file_ids", action="append", type=_positive)
    search.add_argument("--collection", dest="collections", action="append", help="按集合名范围检索，默认包含后代")
    search.add_argument("--direct-only", action="store_true", help="集合范围仅检索直接归属文件")
    search.add_argument("--limit", type=_positive, default=20)
    search.add_argument("--diagnostics", action="store_true")

    _add_knowledge_commands(commands, "wiki")
    _add_knowledge_commands(commands, "graph")

    config_parser = commands.add_parser("config", help="配置操作")
    config_commands = config_parser.add_subparsers(dest="config_command", required=True)
    config_init = config_commands.add_parser("init", help="离线初始化配置")
    _add_runtime_options(config_init)
    config_init.add_argument("--offline", action="store_true", help=argparse.SUPPRESS)
    config_init.add_argument("--interactive", action="store_true", help="交互配置嵌入模型，确认后保存（不自动联网）")

    config_show = config_commands.add_parser("show", help="显示配置")
    _add_runtime_options(config_show)
    config_show.add_argument("--offline", action="store_true", help="只读本地配置，不连接服务")

    config_update = config_commands.add_parser("update", help="更新配置")
    _add_runtime_options(config_update)
    config_update.add_argument("--input", required=True, help="JSON patch 文件路径，或 - 读 stdin")
    config_update.add_argument("--offline", action="store_true", help="离线原子更新本地配置")

    config_test = config_commands.add_parser("test", help="测试一个配置组件")
    _add_runtime_options(config_test)
    config_test.add_argument("component", choices=("embedding", "ocr", "office", "webdav"))

    sync_parser = commands.add_parser("sync", help="同步操作")
    sync_commands = sync_parser.add_subparsers(dest="sync_command", required=True)
    sync_status = sync_commands.add_parser("status", help="查询同步状态")
    _add_runtime_options(sync_status)
    sync_run = sync_commands.add_parser("run", help="运行一次同步")
    _add_runtime_options(sync_run)
    sync_run.add_argument("--yes", action="store_true", help="确认上传文档及可能的云端删除")

    logs = commands.add_parser("logs", help="读取服务日志")
    _add_runtime_options(logs)
    logs.add_argument("--level")
    logs.add_argument("--limit", type=_positive, default=100)

    zotero_parser = commands.add_parser("zotero", help="从本机运行中的 Zotero 一次性导入带 PDF 的条目")
    zotero_commands = zotero_parser.add_subparsers(dest="zotero_command", required=True)
    zotero_preview = zotero_commands.add_parser("preview", help="列出 Zotero 集合与将要导入的条目，不写入")
    zotero_import = zotero_commands.add_parser("import", help="导入条目：PDF 复制入库，元数据写入文件属性")
    for sub in (zotero_preview, zotero_import):
        _add_runtime_options(sub)
        sub.add_argument("--zotero-collection", dest="collection_keys", action="append", metavar="KEY",
                         help="只导入该 Zotero 集合（含子集合）的条目，可重复；不指定则整库")
        sub.add_argument("--collection-mode", choices=("path", "top", "none"), default="path",
                         help="Zotero 集合映射：path=按「父/子」路径建集合（默认），top=只取顶层，none=不映射")
        sub.add_argument("--base-url", help="Zotero Local API 地址（默认 http://127.0.0.1:23119/api）")
    zotero_import.add_argument("--collection", dest="collections", action="append",
                               help="额外归入的已存在 Piece 集合名，可重复")
    _add_wait_options(zotero_import)

    mcp_config = commands.add_parser("mcp-config", help="取得 MCP 客户端配置")
    _add_runtime_options(mcp_config)
    mcp_config.add_argument("--service", choices=("retrieval", "index"), required=True)
    mcp_config.add_argument("--include-secrets", action="store_true",
                            help="明确要求返回密钥（默认脱敏）")

    # 纯本地资源命令：不连接服务，也是 uv 安装层（无 GUI）导出 skill 的入口
    skill_parser = commands.add_parser("skill", help="列出或导出内置 Skill（纯本地）")
    skill_commands = skill_parser.add_subparsers(dest="skill_command", required=True)
    skill_list = skill_commands.add_parser("list", help="列出内置 Skill")
    _add_runtime_options(skill_list)
    skill_export = skill_commands.add_parser("export", help="导出内置 Skill 到目录")
    _add_runtime_options(skill_export)
    skill_export.add_argument("--dir", type=Path, required=True,
                              help="导出目录（不存在时创建）")
    skill_export.add_argument("--overwrite", action="store_true",
                              help="覆盖目标目录中已存在的 Skill")
    skill_export.add_argument("ids", nargs="*", metavar="ID",
                              help="要导出的 Skill ID；缺省导出全部")

    return parser


def _set_data_dir_environment(args: argparse.Namespace) -> None:
    value = getattr(args, "data_dir", None)
    if value is not None:
        os.environ["PIECE_DATA_DIR"] = str(Path(value).expanduser().resolve())


def _get_data_dir(args: argparse.Namespace) -> Path:
    value = getattr(args, "data_dir", None)
    if value is not None:
        return Path(value).expanduser().resolve()
    # 仅导入不读配置、不建目录；get_default_data_dir 本身只计算路径。
    from app.platform import get_default_data_dir

    return Path(get_default_data_dir()).expanduser().resolve()


class ConfigInfo:
    __slots__ = ("data_dir", "config_path", "db_path", "config", "missing")

    def __init__(self, data_dir: Path, config_path: Path, db_path: Path,
                 config: dict[str, Any] | None, missing: bool):
        self.data_dir = data_dir
        self.config_path = config_path
        self.db_path = db_path
        self.config = config
        self.missing = missing


def _load_config_info(args: argparse.Namespace) -> ConfigInfo:
    """只读配置，严格拒绝坏配置；绝不调用 settings.load_settings。"""
    data_dir = _get_data_dir(args)
    config_path = data_dir / "config.json"
    if not config_path.exists():
        return ConfigInfo(data_dir, config_path, data_dir / "kb.db", None, True)
    try:
        with config_path.open("r", encoding="utf-8", newline="") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return ConfigInfo(data_dir, config_path, data_dir / "kb.db", None, True)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientError(f"配置文件无效：{config_path}: {exc}", code="INVALID_CONFIG") from exc
    if not isinstance(value, dict):
        raise ClientError(f"配置文件必须是 JSON 对象：{config_path}", code="INVALID_CONFIG")

    # 新契约的 admin_key 位于 api.admin_key；即使 PIECE_API_KEY 覆盖它，
    # 结构损坏的配置也不得通过猜默认值继续运行。
    api = value.get("api")
    if (
        not isinstance(api, dict)
        or not isinstance(api.get("admin_key"), str)
        or not api["admin_key"].strip()
    ):
        raise ClientError("配置 api.admin_key 无效", code="INVALID_CONFIG")
    data_value = value.get("data_path")
    if data_value is not None and (
        not isinstance(data_value, str) or not data_value.strip()
    ):
        raise ClientError("配置 data_path 必须是非空字符串", code="INVALID_CONFIG")
    if data_value is not None:
        db_dir = Path(data_value).expanduser()
        if not db_dir.is_absolute():
            db_dir = data_dir / db_dir
        db_path = db_dir.resolve() / "kb.db"
    else:
        db_path = (data_dir / "kb.db").resolve()
    return ConfigInfo(data_dir, config_path.resolve(), db_path, value, False)


def _client_for(args: argparse.Namespace) -> PieceClient:
    info = _load_config_info(args)
    if info.missing:
        raise ClientError(
            f"配置不存在：{info.config_path}；请先显式执行 piece serve 启动此目标服务",
            code="SERVICE_UNAVAILABLE",
        )
    env_key = os.environ.get("PIECE_API_KEY")
    config_key = info.config["api"]["admin_key"]  # _load_config_info 已严格校验
    key = env_key if env_key is not None else config_key
    return PieceClient(
        port=getattr(args, "port", DEFAULT_PORT),
        config_dir=info.data_dir,
        db_path=info.db_path,
        api_key=key,
        timeout=10,
    )


def _read_text(path_value: str) -> str:
    if path_value == "-":
        return sys.stdin.read()
    path = Path(path_value)
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise ClientError(f"读取 UTF-8 文件失败：{path}: {exc}", code="FILE_ERROR") from exc


def _read_input_json(path_value: str) -> Any:
    if path_value == "-":
        try:
            return json.loads(sys.stdin.read())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClientError(f"stdin 不是有效 JSON：{exc}", code="INVALID_JSON") from exc
    return read_json_file(Path(path_value))


def _object_input(path_value: str) -> dict[str, Any]:
    value = _read_input_json(path_value)
    if not isinstance(value, dict):
        raise ClientError("输入 JSON 必须是对象", code="INVALID_JSON")
    return value


def _write_new_json(path: Path, value: Any) -> Path:
    """完整写入后发布；证据和请求文件都不覆盖已有内容。"""
    path = path.expanduser().resolve()
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".piece-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
    except FileExistsError as exc:
        raise ClientError(f"文件已存在，未覆盖：{path}", code="ALREADY_EXISTS") from exc
    except OSError as exc:
        raise ClientError(f"保存 JSON 文件失败：{path}: {exc}", code="FILE_ERROR") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def _add_knowledge_commands(commands, namespace):
    is_wiki = namespace == "wiki"
    group = commands.add_parser(namespace, help="Markdown Wiki 页面与引用" if is_wiki else "独立图谱实体、关系与证据")
    operations = group.add_subparsers(dest="knowledge_command", required=True)
    descriptions = {
        "list": "分页列出页面" if is_wiki else "分页列出图实体",
        "search": "只搜索 Wiki 页面" if is_wiki else "只搜索图实体",
        "get": "读取记录与证据", "references": "按库 UUID 和来源文件反查证据",
        "lint": "只读检查，不自动修复", "history": "读取修订历史",
        "request": "按请求键读取原提交结果",
        "apply": "保存 MD；批次可能部分成功" if is_wiki else "同步事务提交图谱批次",
        "delete": "默认预览；正式删除需预览 token、请求键和确认",
    }
    descriptions["rebuild-index"] = "从 MD 重建派生索引，不重写页面" if is_wiki else "重建图谱查询索引，不修改实体、关系或证据"
    if not is_wiki:
        descriptions["graph"] = "读取有界局部语义图"
    for name, help_text in descriptions.items():
        sub = operations.add_parser(name, help=help_text, description=help_text)
        _add_runtime_options(sub)
        if name in {"list", "search", "get", "references", "lint", "history"}:
            sub.add_argument("--limit", type=_positive, default=50, help="分页大小，最大 100")
            sub.add_argument("--offset", type=_nonnegative, default=0)
        if name in {"list", "search"}:
            sub.add_argument("--kind")
            sub.add_argument("--status", choices=("active", "disputed", "outdated"))
        if name == "search":
            sub.add_argument("query")
        if name in {"get", "history", "delete"}:
            kinds = ("page",) if is_wiki else ("object", "relation")
            sub.add_argument("kind", choices=kinds if name == "history" else (*kinds, "evidence"))
            sub.add_argument("id", help="记录 UUID，不是文件/卡片整数 ID")
        if name == "graph":
            sub.add_argument("root_id", help="根图实体 UUID")
            sub.add_argument("--depth", type=int, choices=(1, 2), default=1)
            sub.add_argument("--predicate", dest="predicates", action="append")
            sub.add_argument("--status", dest="statuses", action="append")
            sub.add_argument("--max-nodes", type=_positive, default=100)
            sub.add_argument("--max-edges", type=_positive, default=300)
        if name == "references":
            sub.add_argument("source_library_id", help="来源库 UUID（list 返回 library_id）")
            sub.add_argument("source_file_id", type=_positive)
        if name == "lint":
            sub.add_argument("--page-id" if is_wiki else "--object-id", action="append",
                             dest="page_ids" if is_wiki else "object_ids")
        if name == "request":
            source = sub.add_mutually_exclusive_group(required=True)
            source.add_argument("request_key", nargs="?")
            source.add_argument("--input", help="从原请求文件读取请求键，只查询，不提交")
        if name in {"apply", "request"}:
            sub.add_argument("--read-back", action="store_true", help="读回最多 20 条受影响记录")
        if name == "apply":
            sub.add_argument("--input", required=True, help="批次 JSON UTF-8 文件，或 - 读 stdin；最大 512 KiB")
            sub.add_argument("--request-id", help="正式提交必填（或在 JSON 中提供 request_key）")
            sub.add_argument("--dry-run", action="store_true", help="只校验，不保存知识")
        if name == "delete":
            sub.add_argument("--expected-revision", type=_positive)
            if is_wiki:
                sub.add_argument("--expected-content-hash", help="读回页面的 content_hash，防止覆盖外部编辑")
            sub.add_argument("--impact-token", help="预览返回的 token")
            sub.add_argument("--request-id", help=f"正式删除必填；超时后用 {namespace} request 查询")
            _add_confirmation_options(sub)
    kinds = (("page", "Wiki 页面"),) if is_wiki else (("object", "图实体"), ("relation", "语义关系"))
    for kind, label in kinds:
        group = operations.add_parser(kind, help=f"按意图写入{label}并读回")
        actions = group.add_subparsers(dest="knowledge_action", required=True)
        for action in ("add", "update"):
            sub = actions.add_parser(action, help=f"{'新增' if action == 'add' else '增量修改'}{label}")
            _add_runtime_options(sub)
            adding = action == "add"
            if not adding:
                sub.add_argument("id", help="已有记录 UUID；不按标题匹配")
                sub.add_argument("--expected-revision", type=_positive, required=True)
                if is_wiki:
                    sub.add_argument("--expected-content-hash", required=True)
            if kind in {"object", "page"}:
                sub.add_argument("--kind", required=adding)
                sub.add_argument("--title", required=adding)
                sub.add_argument("--summary")
                if is_wiki:
                    sub.add_argument("--body-file", help="正文 UTF-8 文件，或 - 读 stdin")
                sub.add_argument("--alias", dest="aliases", action="append", help="别名，可重复；更新时替换别名列表")
            else:
                sub.add_argument("--source", required=adding, help="来源图实体 UUID")
                sub.add_argument("--predicate", required=adding,
                                 choices=("is_a", "part_of", "depends_on", "applies_to", "supports", "contradicts", "related_to"))
                sub.add_argument("--target", required=adding, help="目标图实体 UUID")
                sub.add_argument("--description", required=adding)
                sub.add_argument("--basis", required=adding, choices=("explicit", "synthesis", "inference", "user_statement"))
                sub.add_argument("--qualifier")
            sub.add_argument("--status", choices=("active", "disputed", "outdated"))
            sub.add_argument("--evidence-file", action="append", default=[], help="chunk extract --out 保存的证据 JSON，可重复")
            sub.add_argument("--stance", choices=("supports", "contradicts", "context"), default="supports")
            sub.add_argument("--request-file", type=Path, required=True, help="保存请求及自动生成的键；仅允许原样重试")
            sub.add_argument("--reason", help="修改原因；省略时记录 CLI 操作名称")
            sub.add_argument("--dry-run", action="store_true", help="保存请求文件并预检，不写入知识库")


def _wiki_input(client: PieceClient, value: dict[str, Any]) -> dict[str, Any]:
    if "request" not in value and "target_id" not in value:
        return value
    if set(value) != {"target_id", "request"} or not isinstance(value["request"], dict):
        raise ClientError("请求文件必须包含 target_id 和 request 对象", code="INVALID_JSON")
    if value["target_id"] != client.expected_target_id:
        raise ClientError("请求文件属于另一目标实例，未提交", code="TARGET_MISMATCH")
    payload = dict(value["request"])
    if not payload.get("request_key") or "dry_run" in payload:
        raise ClientError("保存的请求须有 request_key；预检请使用 --dry-run，不修改文件", code="INVALID_JSON")
    return payload


def _wiki_intent(args: argparse.Namespace) -> dict[str, Any]:
    if args.evidence_file.count("-") + (getattr(args, "body_file", None) == "-") > 1:
        raise ClientError("stdin 只能供一个输入使用；其余正文或证据请保存为 UTF-8 文件", code="INVALID_ARGUMENT")
    kind, adding = args.knowledge_command, args.knowledge_action == "add"
    fields = (("kind", "title", "summary", "aliases", "status") if kind in {"object", "page"} else
              ("source", "predicate", "target", "description", "qualifier", "basis", "status"))
    values = {key: getattr(args, key) for key in fields if getattr(args, key) is not None}
    if kind == "page" and args.body_file is not None:
        values["body"] = _read_text(args.body_file)
    if not adding and not values:
        raise ClientError(f"update 至少需要一个待修改字段；只添加证据请用 {args.command} apply", code="INVALID_ARGUMENT")
    for field in ("source", "target"):
        if field in values:
            values[field] = {"id": values[field]}
    if adding:
        owner = {"ref": "item"}
        values.update(owner)
    else:
        owner = {"id": args.id}
        values.update(owner, expected_revision=args.expected_revision)
        if kind == "page":
            values["expected_content_hash"] = args.expected_content_hash
    evidence = []
    required = {"source_kind", "source_library_id", "source_file_id", "source_chunk_id", "expected_content_hash", "quote"}
    for filename in args.evidence_file:
        item = _object_input(filename)
        if set(item) != required or item.get("source_kind") != "piece" or not item.get("expected_content_hash"):
            raise ClientError(f"不是 chunk extract --out 生成的完整证据文件：{filename}", code="INVALID_ARGUMENT")
        evidence.append({**item, kind: owner, "stance": args.stance})
    return {"reason": args.reason if args.reason is not None else f"CLI {args.command} {kind} {args.knowledge_action}",
            {"page": "pages", "object": "objects", "relation": "relations"}[kind]: [values], "evidence": evidence}


def _wiki_save_request(client: PieceClient, path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path = path.expanduser().resolve()
    candidate = {**payload, "request_key": str(uuid.uuid4())}
    try:
        _write_new_json(path, {"target_id": client.expected_target_id, "request": candidate})
        return candidate
    except ClientError as exc:
        if exc.code != "ALREADY_EXISTS":
            raise
    saved = _object_input(str(path))
    if "request" not in saved:
        raise ClientError("已存在的文件不是绑定目标的请求文件，未覆盖", code="INVALID_JSON")
    previous = _wiki_input(client, saved)
    if {key: value for key, value in previous.items() if key != "request_key"} != payload:
        raise ClientError("请求文件已保存不同内容，未覆盖；新操作请使用新文件，原请求仅允许原样重试",
                          code="REQUEST_CONFLICT", data={"request_file": str(path)})
    return previous


def _request_key(args: argparse.Namespace) -> str:
    value = getattr(args, "request_id", None)
    return value or str(uuid.uuid4())


def _clean_strings(values: Iterable[str] | None, field: str) -> list[str]:
    cleaned = []
    for value in values or []:
        if not isinstance(value, str) or not value.strip():
            raise ClientError(f"{field} 不能包含空名称", code="INVALID_ARGUMENT")
        cleaned.append(value.strip())
    return cleaned


def _optional_names(values: Iterable[str] | None) -> list[str] | None:
    """未指定过滤时省略字段；空列表在服务端表示空范围，不能误发。"""
    return _clean_strings(values, "collection") or None


def _task_ids_from_data(data: Any) -> list[int]:
    if not isinstance(data, dict):
        return []
    values: list[Any] = []
    if isinstance(data.get("task_id"), int):
        values.append(data["task_id"])
    if isinstance(data.get("task_ids"), list):
        values.extend(data["task_ids"])
    items = data.get("items")
    if isinstance(items, list):
        values.extend(item.get("task_id") for item in items if isinstance(item, dict))
    result = []
    for value in values:
        if type(value) is int and value > 0 and value not in result:
            result.append(value)
    return result


def _confirm(args: argparse.Namespace, operation: str) -> bool:
    if getattr(args, "dry_run", False):
        return False
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        raise ClientError(
            f"{operation} 需要显式 --yes 或交互确认；当前 stdin 不是终端",
            code="CONFIRMATION_REQUIRED",
        )
    try:
        # 提示写 stderr，交互模式下 stdout 仍保持纯 JSON。
        sys.stderr.write(f"确认{operation}？输入 yes 继续：")
        sys.stderr.flush()
        answer = sys.stdin.readline()
        if not answer:
            raise EOFError
    except EOFError as exc:
        raise ClientError(f"{operation} 未确认", code="CONFIRMATION_REQUIRED") from exc
    if answer.strip().lower() not in {"y", "yes"}:
        raise ClientError(f"{operation} 未确认", code="CONFIRMATION_REQUIRED")
    return True


def _query_tasks(client: PieceClient, task_ids: list[int]) -> dict[str, Any]:
    """每批最多 100 个，失败立即停止；保留此前查询结果和全部受理 ID。"""
    task_ids = list(dict.fromkeys(task_ids))
    results = []
    for start in range(0, len(task_ids), 100):
        try:
            result = client.post("/api/v1/task/query", {"task_ids": task_ids[start:start + 100]})
        except (ClientError, KeyboardInterrupt) as exc:
            error = exc if isinstance(exc, ClientError) else ClientError("等待已中断，任务未取消", code="WAIT_INTERRUPTED")
            error.data = {"task_ids": task_ids, "partial_results": results, "query_error": error.data}
            raise error from None
        results.append(result)
        if not result.get("success"):
            return make_envelope(False, result["message"],
                                 {"task_ids": task_ids, "results": results}, result["error"])
        data = result.get("data")
        if not isinstance(data, dict) or any(type(data.get(key)) is not bool for key in ("all_done", "all_succeeded")):
            raise ClientError("任务查询响应无效", code="PROTOCOL_ERROR",
                              data={"task_ids": task_ids, "partial_results": results})
    if len(results) == 1:
        return results[0]
    data = {
        "task_ids": task_ids, "results": results,
        "tasks": [task for result in results for task in result["data"].get("tasks", [])],
        "all_done": all(result["data"]["all_done"] for result in results),
        "all_succeeded": all(result["data"]["all_succeeded"] for result in results),
        "poll_after_ms": max(result["data"].get("poll_after_ms", 500) for result in results),
    }
    return make_envelope(True, "任务查询成功", data)


def _wait_for_tasks(client: PieceClient, task_ids: list[int], timeout: int) -> tuple[bool, bool, dict[str, Any]]:
    """返回 (all_succeeded, timed_out, last_result)，不重发写入或取消任务。"""
    deadline = time.monotonic() + timeout
    last_result: dict[str, Any] = {}
    with client.time_budget(timeout):
        while time.monotonic() < deadline:
            try:
                last_result = _query_tasks(client, task_ids)
            except ClientError as exc:
                if exc.code == "WAIT_TIMEOUT":
                    return False, True, make_envelope(False, exc.message,
                        {"last_result": last_result, "partial_query": exc.data}, {"code": exc.code, "message": exc.message})
                raise
            data = last_result.get("data") or {}
            if not last_result.get("success"):
                return False, False, last_result
            if data.get("all_done"):
                return data["all_succeeded"], False, last_result
            poll_ms = max(100, data.get("poll_after_ms", 500))
            time.sleep(min(poll_ms / 1000, max(0, deadline - time.monotonic())))
    return False, True, last_result


def _submit(client: PieceClient, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """新增响应丢失时保留请求键和对象 ID；绝不猜测未受理或自动重发。"""
    reference = {key: payload[key] for key in ("file_id", "chunk_id", "task_id", "request_key") if key in payload}
    if payload.get("request_key"):
        namespace = "graph" if path.startswith("/api/v1/knowledge/") else "wiki" if path.startswith("/api/v1/wiki/") else None
        reference["recovery"] = {"command": f"piece {namespace} request" if namespace else "piece task list --request-id",
                                 "request_id": payload["request_key"], "same_target_required": True,
                                 "do_not_resubmit": namespace is None}
        if namespace:
            reference["recovery"]["retry_identical_only"] = True
    try:
        result = client.post(path, payload)
    except (ClientError, KeyboardInterrupt) as exc:
        error = exc if isinstance(exc, ClientError) else ClientError("提交被中断，受理结果未知", code="SUBMISSION_INTERRUPTED")
        detail = error.data if isinstance(error.data, dict) else {}
        error.data = {**reference, **detail, "outcome_unknown": detail.get("request_sent") is not False}
        raise error from None
    if reference:
        result["data"] = {**reference, **(result.get("data") or {})}
    return result


def _with_wait(client: PieceClient, initial: dict[str, Any], args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    task_ids = _task_ids_from_data(initial.get("data"))
    if not getattr(args, "wait", False):
        if task_ids and isinstance(initial.get("data"), dict):
            initial["data"].setdefault("task_ids", task_ids)
        return _simple(initial)
    if not task_ids:
        return _simple(initial)
    try:
        succeeded, timed_out, last_result = _wait_for_tasks(client, task_ids, args.timeout)
    except KeyboardInterrupt:
        raise ClientError(
            "等待被中断；任务仍在服务端继续运行，请用 piece task wait 继续查询，不要重发",
            code="WAIT_INTERRUPTED",
            data={**dict(initial.get("data") or {}), "task_ids": task_ids},
        ) from None
    except ClientError as exc:
        exc.data = {**dict(initial.get("data") or {}), "task_ids": task_ids, "query_error": exc.data}
        raise
    data = dict(initial.get("data") or {})
    data["task_ids"] = task_ids
    data["last_result"] = last_result
    if timed_out:
        return make_envelope(
            False,
            f"等待任务完成超时（{args.timeout} 秒）；任务仍会继续运行，请勿盲目重发",
            data,
            {"code": "WAIT_TIMEOUT", "message": "任务等待超时"},
        ), 4
    if not succeeded:
        error = last_result.get("error") or {"code": "TASK_FAILED", "message": "任务失败或取消"}
        return _simple(make_envelope(False, error.get("message", "任务未全部成功完成"), data, error))
    # 部分受理的初始 response=false 即使已受理项成功，整体仍保持失败退出码。
    if not initial.get("success"):
        return make_envelope(
            False,
            "部分请求未受理；已受理任务已完成，但整体操作失败",
            data,
            initial.get("error") or {"code": "PARTIAL_FAILURE", "message": "部分项目未受理"},
        ), 1
    return make_envelope(True, "任务已全部成功完成", data), 0


def _file_import(client: PieceClient, args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    collections = _clean_strings(args.collections, "collection")
    properties = _object_input(args.properties) if getattr(args, "properties", None) else None
    candidates, skipped, excluded = import_candidates(
        args.paths, args.recursive, exclude=args.exclude, include_hidden=args.include_hidden,
        skip_link_notes=args.skip_link_notes,
    )
    accepted: list[int] = []
    failures: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for index, path in enumerate(candidates):
        try:
            result = client.post("/api/v1/file/import", {
                "path": str(path),
                "collections": collections or None,
                "properties": properties,
            })
        except (ClientError, KeyboardInterrupt) as exc:
            error = exc if isinstance(exc, ClientError) else ClientError("导入被中断，当前受理结果未知", code="SUBMISSION_INTERRUPTED")
            detail = error.data if isinstance(error.data, dict) else {}
            unknown = detail.get("request_sent") is not False
            items.append({"path": str(path), "status": "unknown" if unknown else "not_submitted",
                          "error": {"code": error.code, "message": error.message}})
            error.data = {
                "accepted_task_ids": accepted, "task_ids": accepted, "items": items,
                "skipped": skipped, "failures": failures,
                "unknown": [str(path)] if unknown else [],
                "not_submitted": [str(p) for p in candidates[index + (1 if unknown else 0):]],
                "guidance": "先查询文件和任务，勿整批重发；当前未知项可能已受理，后续项确定未提交",
            }
            raise error from None
        item = {"path": str(path), "response": result}
        task_ids = _task_ids_from_data(result.get("data"))
        if task_ids:
            accepted.extend(task_id for task_id in task_ids if task_id not in accepted)
        if not result.get("success"):
            failures.append({"path": str(path), "message": result.get("message", "导入失败")})
        items.append(item)
    data = {
        "accepted_task_ids": accepted,
        "task_ids": accepted,
        "items": items,
        "skipped": skipped,
        "excluded": excluded,
        "failures": failures,
        "accepted_count": len(accepted),
        "duplicate_count": sum(bool((item["response"].get("data") or {}).get("duplicate")) for item in items),
        "skipped_count": len(skipped),
        "excluded_count": len(excluded),
        "failed_count": len(failures),
    }
    initial_success = bool(candidates) and not failures and not skipped
    result = make_envelope(
        initial_success,
        f"已处理 {len(candidates)} 个文件，受理 {len(accepted)} 个任务",
        data,
        None if initial_success else {
            "code": "IMPORT_PARTIAL" if accepted else "IMPORT_FAILED",
            "message": "部分文件未导入" if accepted else "没有文件成功受理",
        },
    )
    result, wait_code = _with_wait(client, result, args)
    if wait_code == 0 and not initial_success:
        wait_code = 1
    return result, wait_code


def _download_images(client: PieceClient, args: argparse.Namespace, result: dict[str, Any]) -> dict[str, Any]:
    output_dir = args.output_dir
    if output_dir is None:
        return result
    output_dir = Path(output_dir).expanduser()
    if not output_dir.is_dir():
        raise ClientError(f"目标目录不存在：{output_dir}", code="INVALID_PATH")
    data = result.get("data")
    image_items = data if isinstance(data, list) else (
        data.get("images", []) if isinstance(data, dict) else []
    )
    saved = []
    failures = []
    if not isinstance(image_items, list):
        raise ClientError("chunk/images 返回的图片列表无效", code="PROTOCOL_ERROR")
    for position, item in enumerate(image_items):
        if isinstance(item, dict):
            index = item.get("index", item.get("image_index", position))
        else:
            index = position
        try:
            index = int(index)
        except (TypeError, ValueError):
            index = position
        destination = output_dir / f"chunk-{args.chunk_id}-{index}.png"
        try:
            saved_result = client.download(
                f"/api/v1/chunk/{args.chunk_id}/image/{index}",
                destination,
                overwrite=args.yes,
            )
            if saved_result.get("success"):
                saved.append(saved_result.get("data"))
            else:
                failures.append({"index": index, "result": saved_result})
        except ClientError as exc:
            failures.append({"index": index, "message": exc.message, "code": exc.code})
    combined = dict(result)
    combined["data"] = {"images": image_items, "saved": saved, "failures": failures}
    if failures:
        combined["success"] = False
        combined["error"] = {"code": "IMAGE_PARTIAL", "message": "部分图片保存失败"}
    return combined


def _knowledge_path(args: argparse.Namespace, operation: str) -> str:
    namespace = "wiki" if args.command == "wiki" else "knowledge"
    return f"/api/v1/{namespace}/{operation}"


def _wiki_next(args: argparse.Namespace, words: list[str], hint: str) -> dict[str, Any]:
    from app.skills import cli_argv
    argv = [*cli_argv(_get_data_dir(args), args.port), *words, "--json"]
    return {"next_argv": argv, "next_command": shlex.join(argv), "next_command_shell": "bash",
            "recovery_hint": hint + "；只恢复一次，仍失败就停止并报告。"}


def _wiki_recovery(args: argparse.Namespace, result: dict[str, Any], payload=None) -> dict[str, Any]:
    if result.get("success"):
        return result
    data = dict(result.get("data") or {})
    code = (result.get("error") or {}).get("code")
    detail = data
    if args.command == "wiki" and data.get("errors") and isinstance(data["errors"][0], dict):
        detail = data["errors"][0]
        code = detail.get("code", code)
    payload = payload or {}
    words, hint = None, ""
    if code in {"VERSION_CONFLICT", "CONTENT_CONFLICT"}:
        kind = {"objects": "object", "relations": "relation", "pages": "page"}.get(detail.get("section", ""))
        record_id = detail.get("id") or detail.get("page_id")
        if kind and isinstance(record_id, str):
            words = [args.command, "get", kind, record_id]
            hint = "读回当前版本与内容哈希并比较；不要仅替换并发条件后覆盖"
    elif code in {"QUOTE_MISMATCH", "SOURCE_CHANGED", "INVALID_SOURCE"}:
        index = detail.get("index")
        evidence = payload.get("evidence", [])
        if (detail.get("section") == "evidence" and isinstance(evidence, list)
                and type(index) is int and 0 <= index < len(evidence)):
            item = evidence[index]
            chunk_id = item.get("source_chunk_id") if isinstance(item, dict) else None
            if type(chunk_id) is int and chunk_id > 0:
                words = ["chunk", "get", str(chunk_id)]
                hint = "重新读取来源，再用 chunk extract 选择引文；不要手改 quote 或哈希来绕过校验"
    if words is None and args.command == "wiki" and code == "INDEX_FAILED" and data.get("committed"):
        words = ["wiki", "rebuild-index"]
        hint = "文件已有提交；只重建派生索引，再核查每页回执，不重写正文"
    key = data.get("request_key") or payload.get("request_key") or getattr(args, "request_key", None)
    if words is None and isinstance(key, str) and key and (data.get("outcome_unknown") or code == "REQUEST_CONFLICT"):
        request_file = getattr(args, "request_file", None)
        words = ([args.command, "request", "--input", str(request_file.expanduser().resolve()), "--read-back"]
                 if request_file is not None else [args.command, "request", key, "--read-back"])
        hint = "先查询原请求；结果未知时不要换键重提，只能在核对后原样重试同一请求文件"
    if words is not None:
        data.update(_wiki_next(args, words, hint))
    result["data"] = data
    return result


def _wiki_read_back(client: PieceClient, args: argparse.Namespace, result: dict[str, Any]) -> dict[str, Any]:
    data = dict(result.get("data") or {})
    if not result.get("success") or data.get("committed") is not True or data.get("dry_run") is True:
        return result
    targets = {}
    issues = []
    for section, kind in (("pages", "page"), ("objects", "object"), ("relations", "relation"), ("evidence", "evidence")):
        entries = data.get(section, [])
        if not isinstance(entries, list):
            issues.append({"code": "PROTOCOL_ERROR", "message": "提交回执的记录列表无效"})
            break
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                issues.append({"code": "PROTOCOL_ERROR", "message": "提交回执缺少记录 ID"})
                break
            key = kind, entry["id"]
            if key in targets and "submitted_fields" in entry:
                entry = {**entry, "submitted_fields": sorted(set(entry["submitted_fields"])
                         | set(targets[key].get("submitted_fields", [])))}
            targets[key] = entry
    if (not targets and data.get("kind") in {"page", "object", "relation", "evidence"}
            and isinstance(data.get("id"), str) and data.get("impact_token")):
        targets[data["kind"], data["id"]] = {"deleted": True}
    # ponytail: 大批次只自动读回前 20 条，剩余记录通过 wiki get 按需读取。
    limit = 20
    items = list(targets.items())
    receipt = {"complete": False, "records": [], "total": len(items), "limit": limit,
               "truncated": len(items) > limit, "semantic_review_required": True, "issues": issues}
    next_record = None
    for (kind, record_id), submitted in items[:limit]:
        try:
            response = client.post(_knowledge_path(args, "get"), {"kind": kind, "id": record_id})
        except (ClientError, KeyboardInterrupt) as exc:
            issues.append({"kind": kind, "id": record_id,
                           "code": exc.code if isinstance(exc, ClientError) else "INTERRUPTED",
                           "message": str(exc) or "读回已中断"})
            next_record = next_record or (kind, record_id)
            break
        if submitted.get("deleted") or submitted.get("action") == "deleted":
            if not response.get("success") and (response.get("error") or {}).get("code") == "NOT_FOUND":
                receipt["records"].append({"kind": kind, "id": record_id, "deleted": True})
                continue
            if response.get("success"):
                issues.append({"kind": kind, "id": record_id, "code": "RECORD_REAPPEARED",
                               "message": "原删除已提交，但当前记录存在，需核查恢复或并发变化"})
                next_record = next_record or (kind, record_id)
                break
        current = response.get("data") or {}
        record = current.get("record") if isinstance(current, dict) else None
        if not response.get("success") or not isinstance(record, dict) or record.get("id") != record_id:
            issues.append({"kind": kind, "id": record_id, "code": (response.get("error") or {}).get("code", "PROTOCOL_ERROR"),
                           "message": response.get("message", "读回记录无效")})
            next_record = next_record or (kind, record_id)
            break
        if data.get("library_id") and current.get("library_id") != data["library_id"]:
            issues.append({"kind": kind, "id": record_id, "code": "TARGET_MISMATCH", "message": "读回时库身份已变化"})
            next_record = next_record or (kind, record_id)
            break
        item = {"kind": kind, "record": record}
        if "submitted_fields" in submitted:
            item["submitted_fields"] = submitted["submitted_fields"]
        if kind in {"page", "object", "relation"}:
            item.update(submitted_revision=submitted.get("revision"),
                        revision_matches=type(submitted.get("revision")) is int and record.get("revision") == submitted["revision"],
                        has_evidence=current.get("has_evidence"),
                        evidence_total=(current.get("evidence") or {}).get("total"))
            if kind == "page":
                item["content_hash_matches"] = bool(submitted.get("content_hash")) and record.get("content_hash") == submitted["content_hash"]
            if not item["revision_matches"] or (kind == "page" and not item["content_hash_matches"]):
                issues.append({"kind": kind, "id": record_id, "code": "REVISION_CHANGED",
                               "message": "当前版本不同于本次提交回执，需比较后再操作"})
                next_record = next_record or (kind, record_id)
        elif (kind == "evidence" and record.get("source_kind") == "piece"
              and record.get("source_library_id") == data.get("library_id")
              and record.get("location_status") != "current"):
            issues.append({"kind": kind, "id": record_id, "code": "SOURCE_CHANGED",
                           "message": "提交后来源已变化或缺失，不能视为当前有效证据"})
            next_record = next_record or (kind, record_id)
        receipt["records"].append(item)
    receipt["complete"] = bool(items) and not issues and not receipt["truncated"]
    data["read_back"] = receipt
    if receipt["complete"]:
        return {**result, "data": data}
    if next_record is None and len(items) > limit:
        next_record = items[limit][0]
    data["recovery"] = {**data.get("recovery", {}), "do_not_resubmit": True}
    if next_record is not None:
        data.update(_wiki_next(args, [args.command, "get", *next_record], "写入已提交，只补充读回和比较，不重新提交"))
    return make_envelope(False, "写入已提交，但读回不完整或状态已变化；不要重新写入", data,
                         {"code": "READ_BACK_INCOMPLETE", "message": "提交已成功，需继续只读核查"})


def _wiki_submit(client: PieceClient, args: argparse.Namespace, payload: dict[str, Any], *, read_back=False) -> dict[str, Any]:
    try:
        result = _submit(client, _knowledge_path(args, "apply"), payload)
    except ClientError as exc:
        recovery = _wiki_recovery(args, make_envelope(False, exc.message, exc.data, {"code": exc.code}), payload)
        exc.data = recovery["data"]
        raise
    result = _wiki_recovery(args, result, payload)
    return _wiki_read_back(client, args, result) if read_back else result


def _api_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    command = args.command
    if command == "doctor":
        return _doctor(args)
    if command == "serve":
        raise AssertionError("serve is handled separately")
    if command == "start":
        if args.open and args.gui is False:
            raise ClientError("--open 不能与 --no-gui 同时使用", code="INVALID_ARGUMENT")
        info = _load_config_info(args)
        if info.missing:
            if _is_listening(args.port):
                raise ClientError("目标配置不存在且端口已被占用，未启动服务；请核对 --data-dir 和 --port",
                                  code="PORT_IN_USE")
            _offline_config_init(args)
        from app.background import start_service
        return _simple(start_service(_client_for(args), timeout=args.timeout,
                                     with_gui=bool(args.gui), with_mcp=args.mcp, open_ui=args.open))
    if command == "open":
        client = _client_for(args)
        result = client.post("/api/v1/window/open", {})
        return result, _exit_code(result)
    if command == "stop":
        client = _client_for(args)
        return _simple(client.post("/api/v1/shutdown", {}))
    if command == "status":
        client = _client_for(args)
        result = client.get("/api/v1/status")
        if result.get("success") and isinstance(result.get("data"), dict):
            result["data"]["cli_version"] = VERSION
            components = result["data"].get("components") or {}
            degraded = [name for name, item in components.items()
                        if isinstance(item, dict) and item.get("status") in {"degraded", "failed"}]
            if degraded:
                result = make_envelope(False, f"核心可用，但可选入口降级：{', '.join(degraded)}", result["data"],
                                       {"code": "DEGRADED", "message": "部分可选入口未启动"})
        return result, _exit_code(result)

    # 离线配置命令必须在构造 HTTP client 之前处理：它们可在没有配置文件、
    # 服务和可选运行时依赖的情况下分别执行（config init 除外只显式导入服务）。
    if command == "config":
        if args.config_command == "init":
            return _offline_config_init(args)
        if args.config_command == "show" and args.offline:
            return _offline_config_show(args)
        if args.config_command == "update" and args.offline:
            return _offline_config_update(args, _object_input(args.input))

    client = _client_for(args)

    if command == "stats":
        return _simple(client.post("/api/v1/storage/stats", {}))

    if command == "file":
        operation = args.file_command
        if operation == "list":
            payload = {"limit": args.limit, "offset": args.offset, "status": args.status,
                       "collections": _optional_names(args.collections),
                       "include_descendants": not args.direct_only, "uncategorized": args.uncategorized,
                       "sort_by": args.sort_by, "descending": not args.ascending}
            if args.name is not None:
                if not args.name.strip():
                    raise ClientError("--name 不能为空", code="INVALID_ARGUMENT")
                payload["name"] = args.name
            return _simple(client.post("/api/v1/file/list", payload))
        if operation == "scan":
            if args.dry_run and args.wait:
                raise ClientError("--dry-run 不能与 --wait 同时使用", code="INVALID_ARGUMENT")
            return _with_wait(client, _submit(client, "/api/v1/file/scan", {
                "dry_run": args.dry_run,
            }), args)
        if operation == "get":
            return _simple(client.post("/api/v1/file/get", {"file_id": args.file_id}))
        if operation == "create":
            return _simple(client.post("/api/v1/file/create", {
                "filename": args.filename, "collections": _optional_names(args.collections),
            }))
        if operation == "import":
            return _file_import(client, args)
        if operation == "import-markdown":
            if args.input == "-" and args.properties == "-":
                raise ClientError("--input 和 --properties 不能同时读 stdin", code="INVALID_ARGUMENT")
            properties = _object_input(args.properties) if args.properties else None
            # 同一正文按哈希查重，响应丢失时重发只会返回已有文件，无需请求键
            return _with_wait(client, client.post("/api/v1/file/import-markdown", {
                "filename": args.filename, "content": _read_text(args.input),
                "collections": _optional_names(args.collections), "properties": properties,
            }), args)
        if operation == "reindex":
            if args.dry_run:
                return _simple(client.post("/api/v1/file/reindex", {
                    "file_id": args.file_id, "source": args.source, "dry_run": True,
                }))
            confirmed = _confirm(args, f"重新索引文件 ID {args.file_id}（覆盖手工修改，卡片 ID 可能改变）")
            result = _submit(client, "/api/v1/file/reindex", {
                "file_id": args.file_id, "source": args.source,
                "confirmed": confirmed, "request_key": _request_key(args),
            })
            return _with_wait(client, result, args)
        if operation == "delete":
            confirmed = _confirm(args, "删除文件（知识对象与引用快照保留，来源将标记缺失）")
            return _simple(client.post("/api/v1/file/delete", {
                "file_ids": args.file_ids, "dry_run": bool(args.dry_run), "confirmed": confirmed,
            }))
        if operation == "properties":
            properties = _object_input(args.input)
            return _simple(client.post("/api/v1/file/properties", {
                "file_id": args.file_id, "properties": properties,
            }))
        if operation == "export":
            if args.with_resources and args.format != "markdown":
                raise ClientError("--with-resources 仅支持 Markdown", code="INVALID_ARGUMENT")
            suffix = "&include_resources=true" if args.with_resources else ""
            return _simple(client.download(
                f"/api/v1/file/{args.file_id}/content?format={args.format}{suffix}",
                args.output, overwrite=args.yes,
            ))
        if operation == "page":
            return _simple(client.download(
                f"/api/v1/file/{args.file_id}/page/{args.page_number}",
                args.output, overwrite=args.yes,
            ))

    if command == "chunk":
        operation = args.chunk_command
        if operation == "list":
            return _simple(client.post("/api/v1/chunk/list", {
                "file_id": args.file_id, "page": args.page, "page_size": args.page_size,
            }))
        if operation == "get":
            return _simple(client.post("/api/v1/chunk/get", {"chunk_id": args.chunk_id}))
        if operation == "extract":
            payload: dict[str, Any] = {"chunk_id": args.chunk_id, "max_matches": args.max_matches}
            if args.lines is not None:
                payload["lines"] = args.lines
            else:
                payload.update({"grep": args.grep, "regex": bool(args.regex), "context": args.context})
            result = client.post("/api/v1/chunk/extract", payload)
            if args.out is not None and result.get("success"):
                data = dict(result.get("data") or {})
                matches = data.get("matches") or []
                if len(matches) != 1 or data.get("truncated"):
                    data.update(_wiki_next(args, ["chunk", "get", str(args.chunk_id)],
                                           "先选择单段行号，再用 chunk extract --lines N-M --out 新文件保存；不自动取首条"))
                    return _simple(make_envelope(False, "导出证据需要唯一且未截断的匹配，未写文件", data,
                                                 {"code": "EVIDENCE_SELECTION_REQUIRED", "message": "请先选择唯一引文范围"}))
                saved = _write_new_json(args.out, matches[0]["evidence"])
                result["data"] = {**data, "evidence_file": str(saved)}
            return _simple(result)
        if operation == "add":
            text = _read_text(args.input)
            payload = {
                "file_id": args.file_id, "doc_title": args.title, "chunk_text": text,
                "request_key": _request_key(args),
            }
            return _with_wait(client, _submit(client, "/api/v1/chunk/add", payload), args)
        if operation == "batch-add":
            value = _read_input_json(args.input)
            if not isinstance(value, list):
                raise ClientError("batch-add 输入必须是 JSON 数组", code="INVALID_JSON")
            chunks = []
            for index, item in enumerate(value):
                if not isinstance(item, dict) or not isinstance(item.get("doc_title"), str) \
                        or not isinstance(item.get("chunk_text"), str):
                    raise ClientError(f"第 {index} 项必须含 doc_title 和 chunk_text 字符串",
                                      code="INVALID_JSON")
                chunks.append({"doc_title": item["doc_title"], "chunk_text": item["chunk_text"]})
            payload = {"file_id": args.file_id, "chunks": chunks, "request_key": _request_key(args)}
            return _with_wait(client, _submit(client, "/api/v1/chunk/batch-add", payload), args)
        if operation == "update":
            if args.input is None and args.title is None:
                raise ClientError("chunk update 至少需要 --input 或 --title", code="INVALID_ARGUMENT")
            payload: dict[str, Any] = {"chunk_id": args.chunk_id, "request_key": _request_key(args)}
            if args.input is not None:
                payload["chunk_text"] = _read_text(args.input)
            if args.title is not None:
                payload["doc_title"] = args.title
            return _with_wait(client, _submit(client, "/api/v1/chunk/update", payload), args)
        if operation == "delete":
            confirmed = _confirm(args, "删除切片")
            return _simple(client.post("/api/v1/chunk/delete", {
                "chunk_ids": args.chunk_ids, "dry_run": bool(args.dry_run), "confirmed": confirmed,
            }))
        if operation == "images":
            result = client.post("/api/v1/chunk/images", {"chunk_id": args.chunk_id})
            if not result.get("success"):
                return result, 1
            return _simple(_download_images(client, args, result))
        if operation == "image":
            return _simple(client.download(
                f"/api/v1/chunk/{args.chunk_id}/image/{args.image_index}",
                args.output, overwrite=args.yes,
            ))

    if command == "collection":
        operation = args.collection_command
        if operation == "list":
            return _simple(client.post("/api/v1/collection/list", {
                "limit": args.limit, "offset": args.offset,
            }))
        if operation == "tree":
            return _simple(client.post("/api/v1/collection/tree", {}))
        if operation == "create":
            payload = {"name": args.name}
            if args.parent_id is not None:
                payload["parent_id"] = args.parent_id
            if args.description is not None:
                payload["description"] = args.description
            return _simple(client.post("/api/v1/collection/create", payload))
        if operation == "move":
            return _simple(client.post("/api/v1/collection/move", {
                "collection_id": args.collection_id, "parent_id": None if args.root else args.parent_id,
            }))
        if operation == "rename":
            return _simple(client.post("/api/v1/collection/rename", {
                "collection_id": args.collection_id, "name": args.name,
            }))
        if operation == "delete":
            confirmed = _confirm(args, "删除集合")
            return _simple(client.post("/api/v1/collection/delete", {
                "collection_id": args.collection_id, "dry_run": bool(args.dry_run),
                "confirmed": confirmed,
            }))
        if operation == "set":
            return _simple(client.post("/api/v1/collection/set", {
                "file_ids": args.file_ids,
                "collection_names": _clean_strings(args.collection_names, "collection"),
            }))

    if command == "task":
        operation = args.task_command
        if operation == "list":
            return _simple(client.post("/api/v1/task/list", {
                "limit": args.limit, "offset": args.offset, "status": args.status, "request_key": args.request_id,
            }))
        if operation == "get":
            return _simple(client.post("/api/v1/task/get", {"task_id": args.task_id}))
        if operation == "query":
            return _simple(_query_tasks(client, args.task_ids))
        if operation == "cancel":
            return _simple(client.post("/api/v1/task/cancel", {"task_id": args.task_id}))
        if operation == "retry":
            return _with_wait(client, _submit(client, "/api/v1/task/retry", {"task_id": args.task_id}), args)
        if operation == "wait":
            args.wait = True
            initial = make_envelope(True, "开始等待任务", {"task_ids": list(dict.fromkeys(args.task_ids))})
            return _with_wait(client, initial, args)

    if command in {"wiki", "graph"}:
        operation = args.knowledge_command
        path = _knowledge_path(args, operation)
        if operation in {"page", "object", "relation"}:
            payload = _wiki_save_request(client, args.request_file, _wiki_intent(args))
            if args.dry_run:
                payload["dry_run"] = True
            try:
                result = _wiki_submit(client, args, payload, read_back=True)
            except ClientError as exc:
                exc.data = {**(exc.data or {}), "request_file": str(args.request_file.expanduser().resolve())}
                raise
            result["data"] = {**(result.get("data") or {}), "request_file": str(args.request_file.expanduser().resolve())}
            return _simple(result)
        if operation == "apply":
            payload = _wiki_input(client, _object_input(args.input))
            if args.request_id is not None:
                if "request_key" in payload and payload["request_key"] != args.request_id:
                    raise ClientError("--request-id 与输入 request_key 不一致", code="INVALID_ARGUMENT")
                payload["request_key"] = args.request_id
            if args.dry_run:
                payload["dry_run"] = True
            if payload.get("dry_run") is not True and not payload.get("request_key"):
                raise ClientError("正式提交必须提供 --request-id 或 request_key", code="INVALID_ARGUMENT")
            return _simple(_wiki_submit(client, args, payload, read_back=args.read_back))
        if operation == "delete":
            payload = {"kind": args.kind, "id": args.id}
            if args.expected_revision is not None:
                payload["expected_revision"] = args.expected_revision
            if getattr(args, "expected_content_hash", None) is not None:
                payload["expected_content_hash"] = args.expected_content_hash
            if args.dry_run or not args.impact_token:
                if args.yes and not args.dry_run:
                    raise ClientError("请先预览，再携带 --impact-token 和 --request-id 确认删除", code="INVALID_ARGUMENT")
                return _simple(client.post(path, {**payload, "dry_run": True}))
            if not args.request_id:
                raise ClientError("正式删除必须提供 --request-id", code="INVALID_ARGUMENT")
            return _simple(_submit(client, path, {
                **payload, "dry_run": False, "impact_token": args.impact_token,
                "request_key": args.request_id, "confirmed": _confirm(args, "删除知识记录（不删除来源文件）"),
            }))
        if operation == "request" and args.input is not None:
            saved = _wiki_input(client, _object_input(args.input))
            args.request_key = saved.get("request_key")
            if not isinstance(args.request_key, str) or not args.request_key.strip():
                raise ClientError("请求文件缺少有效 request_key", code="INVALID_ARGUMENT")
        fields = {
            "list": ("kind", "status", "limit", "offset"),
            "search": ("query", "kind", "status", "limit", "offset"),
            "get": ("kind", "id", "limit", "offset"),
            "graph": ("root_id", "depth", "predicates", "statuses", "max_nodes", "max_edges"),
            "references": ("source_library_id", "source_file_id", "limit", "offset"),
            "lint": ("page_ids" if command == "wiki" else "object_ids", "limit", "offset"),
            "history": ("kind", "id", "limit", "offset"),
            "request": ("request_key",),
            "rebuild-index": (),
        }[operation]
        result = client.post(path, {key: getattr(args, key) for key in fields if getattr(args, key) is not None})
        if operation == "request" and args.read_back:
            if result.get("success"):
                result["data"] = {"request_key": args.request_key, **(result.get("data") or {})}
            result = _wiki_read_back(client, args, result)
        return _simple(_wiki_recovery(args, result))

    if command == "search":
        return _simple(client.post("/api/v1/search", {
            "query": args.query, "file_ids": args.file_ids,
            "collections": _optional_names(args.collections),
            "include_descendants": not args.direct_only,
            "limit": args.limit, "diagnostics": bool(args.diagnostics),
        }))

    if command == "config":
        operation = args.config_command
        if operation == "update":
            patch = _object_input(args.input)
            return _simple(client.post("/api/v1/config/update", {"patch": patch}))
        if operation == "show":
            return _simple(client.post("/api/v1/config/show", {}))
        if operation == "test":
            return _simple(client.post("/api/v1/config/test", {"component": args.component}))

    if command == "sync":
        if args.sync_command == "status":
            return _simple(client.post("/api/v1/sync/status", {}))
        confirmed = _confirm(args, "运行同步")
        return _simple(client.post("/api/v1/sync/run", {"confirmed": confirmed}))

    if command == "logs":
        return _simple(client.post("/api/v1/logs", {"level": args.level, "limit": args.limit}))

    if command == "zotero":
        payload = {
            "collection_keys": _clean_strings(args.collection_keys, "zotero-collection") or None,
            "collection_mode": args.collection_mode, "base_url": args.base_url,
        }
        if args.zotero_command == "preview":
            return _simple(client.post("/api/v1/zotero/preview", payload))
        result = client.post("/api/v1/zotero/import", {**payload, "collections": _optional_names(args.collections)})
        return _with_wait(client, result, args)

    if command == "mcp-config":
        return _simple(client.post("/api/v1/mcp-config", {
            "service": args.service, "include_secrets": bool(args.include_secrets),
        }))

    raise ClientError("未知命令", code="INVALID_ARGUMENT")


def _simple(result: dict[str, Any]) -> tuple[dict[str, Any], int]:
    return result, _exit_code(result)


def _offline_config_init(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    try:
        if getattr(args, "interactive", False):
            from app.setup_wizard import initialize_interactively
            value = initialize_interactively()
        else:
            from indexing.services.config_service import initialize_config
            value = initialize_config()
    except ImportError as exc:
        raise ClientError(f"配置服务不可用：{exc}", code="MISSING_DEPENDENCY") from exc
    if not isinstance(value, dict):
        raise ClientError("config_service.initialize_config 返回无效结果", code="PROTOCOL_ERROR")
    message = "配置初始化成功"
    config_path = value.get("config_path")
    if isinstance(config_path, str) and "config.json" in config_path:
        # 只指引路径不打印密钥，避免进入终端历史或日志。
        message = f"配置初始化成功；管理界面登录密码位于 {config_path} 的 api.admin_key"
    return make_envelope(True, message, value), 0


def _offline_config_show(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    try:
        from indexing.services.config_service import show_config
        value = show_config(offline=True)
    except ImportError as exc:
        raise ClientError(f"配置服务不可用：{exc}", code="MISSING_DEPENDENCY") from exc
    if not isinstance(value, dict):
        raise ClientError("config_service.show_config 返回无效结果", code="PROTOCOL_ERROR")
    return make_envelope(True, "配置读取成功", value), 0


def _offline_config_update(args: argparse.Namespace, patch: dict[str, Any]) -> tuple[dict[str, Any], int]:
    try:
        from indexing.services.config_service import update_config
        value = update_config(patch, offline=True)
    except ImportError as exc:
        raise ClientError(f"配置服务不可用：{exc}", code="MISSING_DEPENDENCY") from exc
    if not isinstance(value, dict):
        raise ClientError("config_service.update_config 返回无效结果", code="PROTOCOL_ERROR")
    return make_envelope(True, "配置更新成功", value), 0


def _doctor(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    dependencies = {
        name: importlib.util.find_spec(name) is not None
        for name in ("fastapi", "uvicorn", "pydantic", "sqlite_vec", "httpx", "yaml")
    }
    optional_dependencies = {
        name: importlib.util.find_spec(name) is not None
        for name in ("nicegui", "fastmcp", "pystray", "webview")
    }
    data_dir = _get_data_dir(args)
    try:
        extension_supported = hasattr(sqlite3.Connection, "enable_load_extension")
        sqlite_detail = {"version": sqlite3.sqlite_version, "load_extension": extension_supported}
    except Exception as exc:  # pragma: no cover - 极少见的解释器损坏
        sqlite_detail = {"error": str(exc)}
        extension_supported = False
    data = {
        "python": {"version": sys.version, "executable": sys.executable},
        "dependencies": dependencies,
        "optional_dependencies": optional_dependencies,
        "paths": {
            "data_dir": str(data_dir),
            "exists": data_dir.exists(),
            "is_dir": data_dir.is_dir(),
            "config_path": str(data_dir / "config.json"),
        },
        "sqlite": sqlite_detail,
        "executables": {"python": shutil.which("python"), "soffice": shutil.which("soffice")},
    }
    if args.server:
        # doctor --server 只做无认证握手，不读取配置、不发送密钥。
        client = PieceClient(
            port=getattr(args, "port", DEFAULT_PORT),
            config_dir=data_dir,
            db_path=data_dir / "kb.db",
            api_key=None,
            timeout=2,
        )
        try:
            handshake = client.probe_handshake()
            if not handshake["data"]["ready"]:
                handshake = make_envelope(False, "Piece 尚未就绪", handshake["data"],
                                          {"code": "NOT_READY", "message": "核心尚未就绪"})
            data["server"] = handshake
        except ClientError as exc:
            data["server"] = make_envelope(False, exc.message, None, {
                "code": exc.code, "message": exc.message,
            })
    healthy = all(dependencies.values()) and extension_supported
    if args.server:
        healthy = healthy and data["server"].get("success") is True
    return make_envelope(healthy, "诊断通过" if healthy else "诊断发现问题", data,
                         None if healthy else {"code": "DOCTOR_FAILED", "message": "请检查诊断 data"}), 0 if healthy else 1


def _skill_command(args: argparse.Namespace) -> int:
    """纯本地 Skill 资源命令：不连接服务、不创建数据目录、不初始化数据库。

    前缀用 CLI 自身的数据目录与端口参数生成；目标 config.json 缺失时
    只提示（stderr / data.warnings），不阻断导出。
    """
    from app import skills as skill_resources

    json_mode = bool(getattr(args, "json", False))
    warnings: list[str] = []
    data_dir = _get_data_dir(args)
    if not (data_dir / "config.json").exists():
        warnings.append(
            f"未找到该目标的配置：{data_dir / 'config.json'}；前缀按当前参数生成"
        )
        if not json_mode:
            for warning in warnings:
                print(warning, file=sys.stderr)

    if args.skill_command == "list":
        items = skill_resources.list_skills()
        result = make_envelope(
            True, f"共 {len(items)} 个内置 Skill", {"skills": items, "warnings": warnings}
        )
        _write_result(result, json_mode)
        return 0

    ids = args.ids or [item["id"] for item in skill_resources.list_skills()]
    prefix = skill_resources.cli_prefix(data_dir, getattr(args, "port", DEFAULT_PORT))
    data: dict[str, Any] = {
        "dir": str(args.dir), "prefix": prefix, "warnings": warnings,
        "exported": [], "exists": [], "missing": [],
    }
    try:
        outcome = skill_resources.export_skills(
            ids, args.dir, prefix=prefix, version=skill_resources.get_version(),
            overwrite=args.overwrite,
        )
    except OSError as exc:
        raise ClientError(f"导出失败：{exc}", code="FILE_ERROR") from exc
    data.update(outcome)
    exported, exists, missing = outcome["exported"], outcome["exists"], outcome["missing"]
    if exported:
        result = make_envelope(
            True, f"已导出 {len(exported)} 个 Skill 到「{args.dir}」", data
        )
        _write_result(result, json_mode)
        return 0

    problems = []
    if missing:
        problems.append(f"未找到 Skill：{'、'.join(missing)}")
    if exists:
        problems.append(f"目标已存在：{'、'.join(exists)}（加 --overwrite 覆盖）")
    message = "；".join(problems) or "没有可导出的 Skill"
    code = "SKILL_NOT_FOUND" if missing else ("TARGET_EXISTS" if exists else "NO_SKILLS")
    result = make_envelope(False, message, data, {"code": code, "message": message})
    _write_result(result, json_mode)
    # 不存在的 ID 属参数错误（2）；已存在未覆盖等其余失败归 1
    return 2 if missing else 1


def _run_legacy_or_service(args: argparse.Namespace) -> int:
    if args.command == "autostart":
        from app.platform import configure_autostart
        try:
            path = configure_autostart(args.action, port=getattr(args, "port", DEFAULT_PORT))
        except Exception as exc:
            raise ClientError(f"登录自启操作失败：{exc}", code="AUTOSTART_FAILED") from exc
        result = make_envelope(True, (
            "已配置登录自启，下次登录生效（不启动当前服务）" if args.action == "install"
            else "登录自启已移除（不停止正在运行的服务）"
        ), {"path": str(path)})
        _write_result(result, bool(getattr(args, "json", False)))
        return 0

    if args.command == "skill":
        return _skill_command(args)

    if args.command == "serve":
        port = getattr(args, "port", DEFAULT_PORT)
        if _is_listening(port):
            result = make_envelope(False,
                f"管理端口 {port} 已被占用；若 Piece 已运行，请使用 piece open --port {port}。",
                None, {"code": "PORT_IN_USE", "message": "管理端口已被占用"})
            _write_result(result, bool(getattr(args, "json", False)))
            return 1
        try:
            from app.server import main as serve_main
            serve_main(
                port=port,
                open_ui=args.open,
                with_gui=not args.no_gui,
                with_mcp=not args.no_mcp,
                with_tray=not args.no_tray,
            )
        except KeyboardInterrupt:
            return 0
        except SystemExit as exc:
            result = make_envelope(False, "Piece 服务未能启动或未能安全停止，请查看日志", None,
                                   {"code": "SERVICE_START_FAILED", "message": f"exit {exc.code}"})
            _write_result(result, bool(getattr(args, "json", False)))
            return 1
        except (OSError, RuntimeError, TypeError) as exc:
            result = make_envelope(False, f"Piece 启动失败：{exc}", None,
                                   {"code": getattr(exc, "code", "SERVICE_START_FAILED"), "message": str(exc)})
            _write_result(result, bool(getattr(args, "json", False)))
            return 1
        return 0
    return -1


def main(argv: list[str] | None = None) -> int:
    global _JSON_REQUESTED
    multiprocessing.freeze_support()
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    _JSON_REQUESTED = "--json" in raw_argv
    parser = _build_parser()

    # --version 与 --help 都必须能脱离子命令工作；版本检查放在 parse_args
    # 前面，避免 required subparsers 先把它拒绝，也不触碰配置或服务。
    if "--version" in raw_argv:
        result = make_envelope(True, f"Piece {VERSION}", {"version": VERSION})
        _write_result(result, _JSON_REQUESTED)
        return 0

    # PyInstaller 内部入口必须在任何服务依赖、日志和数据库导入之前处理。
    if raw_argv and raw_argv[0] == "--parse-helper":
        if len(raw_argv) != 2:
            parser.error("--parse-helper 需要一个请求文件路径")
        from indexing.worker_process import parser_main
        parser_main(raw_argv[1])
        return 0
    if raw_argv and raw_argv[0] == "--window":
        if not 2 <= len(raw_argv) <= 3:
            parser.error("--window 需要 URL 和可选图标路径")
        from app.window_host import run
        run(raw_argv[1], raw_argv[2] if len(raw_argv) == 3 else None)
        return 0

    # 无参数启动保留双击 Piece.exe / 原源码入口的桌面行为。
    args = parser.parse_args(raw_argv or ["serve", "--open"])
    _set_data_dir_environment(args)

    try:
        legacy_result = _run_legacy_or_service(args)
        if legacy_result >= 0:
            return legacy_result
        result, code = _api_command(args)
        _write_result(result, bool(getattr(args, "json", False)))
        return code
    except ClientError as exc:
        result = make_envelope(False, exc.message, exc.data,
                               {"code": exc.code, "message": exc.message})
        _write_result(result, bool(getattr(args, "json", False)))
        return _exit_code(result)
    except (OSError, ValueError, RuntimeError) as exc:
        result = make_envelope(False, str(exc), getattr(exc, "data", None),
                               {"code": getattr(exc, "code", "CLI_ERROR"), "message": str(exc)})
        _write_result(result, bool(getattr(args, "json", False)))
        return _exit_code(result)
    except KeyboardInterrupt:
        result = make_envelope(False, "命令已中断；不会取消服务端已受理任务", None,
                               {"code": "INTERRUPTED", "message": "客户端已中断"})
        _write_result(result, bool(getattr(args, "json", False)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
