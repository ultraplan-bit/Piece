"""文件业务：各入口共用导入、查重、归类、入队和物理文件保护规则。"""

import hashlib
import os
import re
import shutil
import stat
import tempfile
import uuid
from pathlib import Path

from ..database import get_db_cursor
from ..settings import get_parser_max_size, get_settings
from ..repositories import FileRepository, ChunkRepository
from . import task_service
from .task_service import serialized_mutation
from .errors import BusinessError
from .chunking import ChunkerFactory
from .metadata_service import encode_metadata

import logging
logger = logging.getLogger(__name__)
_file_repo = FileRepository()
_chunk_repo = ChunkRepository()
# 单文件大小上限（500MB）。上传与解析全程流式（>1MB 由 NiceGUI spool 到临时
# 文件，PDF 逐页解析），放宽上限不会抬高内存峰值；真正的处理闸门是
# converter.MAX_PDF_PAGES，500MB 大致对应 300dpi 彩色扫描的数百页。
# 解析后端另有更严的限制时以 get_max_file_size() 为准。
MAX_FILE_SIZE = 500 * 1024 * 1024


def get_max_file_size() -> int:
    """当前解析后端的单文件上限；后端没有额外限制时用全局上限。

    解析后端由 ocr.provider 单选，限制只跟着当前选中的那个走；
    切换后端只影响之后导入的文件，已入库的解析结果不受影响。
    """
    return get_parser_max_size() or MAX_FILE_SIZE


def _file_too_large_message(limit: int) -> str:
    return f"单文件不能超过 {limit // (1024 * 1024)} MiB"


def get_files_dir():
    return get_settings().get_files_path()


def get_originals_dir():
    return get_files_dir() / "originals"


def get_working_dir():
    return get_files_dir() / "working"


def ensure_files_dir():
    get_originals_dir().mkdir(parents=True, exist_ok=True)
    get_working_dir().mkdir(parents=True, exist_ok=True)
    return get_files_dir()


@serialized_mutation
def storage_owner():
    """本地文件区标识不参与云同步，用于区分本机暂存产物和下载回来的文件。"""
    root = get_files_dir()
    root.mkdir(parents=True, exist_ok=True)
    marker = root / ".instance"
    if marker.is_symlink():
        raise BusinessError("INVALID_PATH", "文件区标识不能是链接")
    if not marker.exists():
        with marker.open("x", encoding="ascii") as output:
            output.write(uuid.uuid4().hex)
    owner = marker.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", owner):
        raise BusinessError("INVALID_STORAGE", "文件区标识损坏，拒绝清理暂存产物")
    return owner


def calculate_file_hash(content):
    return hashlib.sha256(content).hexdigest()


def calculate_file_hash_from_path(file_path):
    digest = hashlib.sha256()
    with open(file_path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_filename(filename):
    filename = filename.strip()
    if (not filename or filename in {".", ".."} or len(filename) > 200
            or any(c in filename for c in '/\\:<>"|?*')
            or any(ord(c) < 32 for c in filename) or filename.endswith((".", " "))
            or Path(filename).stem.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}):
        raise BusinessError("INVALID_FILENAME", "请提供不含目录、控制字符或系统保留名称的文件名")
    return filename


def get_unique_filename(original_filename, check_in_working=True):
    name = validate_filename(original_filename)
    ensure_files_dir()
    directory = get_working_dir() if check_in_working else get_originals_dir()
    # 已发布的工作文件可能位于独立代目录，命名仍同时检查数据库。
    taken = {f["filename"].casefold() for f in get_files_list()} if check_in_working else set()
    path = Path(name)
    counter = 0
    while (directory / name).exists() or name.casefold() in taken:
        counter += 1
        name = f"{path.stem}_{counter}{path.suffix}"
    return name


def validate_import(filename, size):
    validate_filename(filename)
    extension = Path(filename).suffix.lower()
    if extension not in ChunkerFactory.get_supported_extensions():
        raise BusinessError("UNSUPPORTED_FORMAT", f"不支持的文档格式：{extension}")
    limit = get_max_file_size()
    if size > limit:
        # 先于快照复制拒绝，避免为一份注定失败的文件白拷一份哈希
        raise BusinessError("FILE_TOO_LARGE", _file_too_large_message(limit))
    from .office_convert import CONVERTER_ONLY_FORMATS, list_converters
    if extension in CONVERTER_ONLY_FORMATS and not list_converters():
        raise BusinessError("CONVERTER_UNAVAILABLE", f"解析 {extension} 需要 Microsoft Office 或 LibreOffice")


def _assign_collections(cursor, file_id, collection_ids):
    for collection_id in dict.fromkeys(collection_ids or []):
        cursor.execute("SELECT id FROM collections WHERE id = ?", (collection_id,))
        if not cursor.fetchone():
            raise BusinessError("NOT_FOUND", f"集合不存在：{collection_id}")
        cursor.execute("INSERT OR IGNORE INTO file_collections VALUES (?, ?)", (file_id, collection_id))


@serialized_mutation
def import_file(source_path, filename=None, collection_ids=None, *, managed_original=False, metadata=None):
    """复制到受控目录后原子登记和入队；补偿仅删除本次创建的副本，绝不删用户原件。

    ``metadata`` 随登记一并写入 files.metadata：导入后文件立刻进入在途任务，
    此时不能再单独改属性；PDF 路径也不解析 frontmatter，外部来源（如 Zotero）
    的元数据只有这一条入口。命中重复文件时不覆盖已有属性。
    """
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise BusinessError("NOT_FOUND", f"导入文件不存在：{source}")
    name = validate_filename(filename or source.name)
    validate_import(name, source.stat().st_size)
    metadata_json = encode_metadata(metadata)
    ensure_files_dir()
    created = []
    # 先快照再哈希，避免源文件在查重和复制之间变化。
    with tempfile.TemporaryDirectory(prefix=".import-", dir=get_files_dir()) as directory:
        snapshot = Path(directory) / name
        # 源文件可能在快照开始后被替换，故这里按真实读到的字节再判一次上限
        limit = get_max_file_size()
        with source.open("rb") as reader, snapshot.open("xb") as writer:
            if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
                raise BusinessError("INVALID_INPUT", "仅允许导入普通文件")
            size = 0
            digest = hashlib.sha256()
            for block in iter(lambda: reader.read(1024 * 1024), b""):
                size += len(block)
                if size > limit:
                    raise BusinessError("FILE_TOO_LARGE", _file_too_large_message(limit))
                digest.update(block)
                writer.write(block)
        file_hash = digest.hexdigest()
        existing = _file_repo.find_by_hash(file_hash)
        if existing:
            with get_db_cursor(write=True) as cursor:
                _assign_collections(cursor, existing["id"], collection_ids)
            active_ids = [t["id"] for t in task_service.get_active_tasks() if t["file_id"] == existing["id"]]
            return {"file_id": existing["id"], "filename": existing["filename"], "duplicate": True,
                    "status": "duplicate", "task_ids": active_ids, "file": existing}
        original = source if managed_original else get_originals_dir() / get_unique_filename(name, False)
        if managed_original and not original.is_relative_to(get_originals_dir().resolve()):
            raise BusinessError("INVALID_PATH", "受控原件必须位于 originals 目录")
        working = get_working_dir() / get_unique_filename(f"{Path(name).stem}.md")
        try:
            if not managed_original:
                with original.open("xb") as target, snapshot.open("rb") as reader:
                    created.append(original)
                    shutil.copyfileobj(reader, target)
            with working.open("x", encoding="utf-8"):
                created.append(working)
            with get_db_cursor(write=True) as cursor:
                cursor.execute(
                    "INSERT INTO files (file_hash, filename, file_path, file_size, original_file_type, original_file_path, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (file_hash, working.name, str(working), size, Path(name).suffix.lower().lstrip("."), str(original), metadata_json),
                )
                file_id = cursor.lastrowid
                _assign_collections(cursor, file_id, collection_ids)
                task_id = task_service.create_task(name, file_id, input_data={"source": "original"}, cursor=cursor)
            return {"file_id": file_id, "filename": working.name, "task_id": task_id,
                    "task_ids": [task_id], "duplicate": False, "status": "accepted"}
        except BaseException:
            for path in created:
                path.unlink(missing_ok=True)
            raise


def check_file_hash_exists(file_hash):
    existing = _file_repo.find_by_hash(file_hash)
    return existing["id"] if existing else None


def insert_file_record(file_hash, filename, file_path, file_size, status="pending", original_file_type=None, original_file_path=None):
    return _file_repo.insert(file_hash, filename, file_path, file_size, status, original_file_type, original_file_path)


def update_file_status(file_id, status):
    return _file_repo.update_status(file_id, status)


def get_file_by_id(file_id):
    return _file_repo.find_by_id(file_id)


def file_exists(file_id):
    return _file_repo.exists(file_id)


def get_files_list(status=None):
    return _file_repo.find_by_status(status) if status else _file_repo.find_all_ordered(order_by="created_at", desc=True)


def get_note_files():
    return _file_repo.find_notes()


def get_files_list_paginated(limit=20, offset=0, status=None, collection_ids=None):
    conditions, params = [], []
    if status:
        conditions.append("status = ?")
        params.append(status)
    if collection_ids is not None:
        conditions.append(f"id IN (SELECT file_id FROM file_collections WHERE collection_id IN ({','.join('?' for _ in collection_ids)}))" if collection_ids else "0")
        params.extend(collection_ids)
    where = " AND ".join(conditions) or "1"
    with get_db_cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM files WHERE {where}", params)
        total = cursor.fetchone()[0]
        cursor.execute(f"SELECT * FROM files WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset))
        files = [dict(row) for row in cursor.fetchall()]
    return {"files": files, "total": total, "limit": limit, "offset": offset}


def get_chunks_by_file_id(file_id):
    return _chunk_repo.find_by_file_id(file_id) if file_exists(file_id) else None


def get_chunks_paginated(file_id, page=1, page_size=50):
    if not file_exists(file_id):
        return None
    total = _chunk_repo.count_by_file_id(file_id)
    return {"chunks": _chunk_repo.find_by_file_id_paginated(file_id, page, page_size), "total": total,
            "page": page, "page_size": page_size, "total_pages": max(1, (total + page_size - 1) // page_size)}


def managed_path(path):
    resolved = Path(path).resolve()
    roots = (get_originals_dir().resolve(), get_working_dir().resolve())
    if not any(resolved.is_relative_to(root) and resolved != root for root in roots):
        raise BusinessError("INVALID_PATH", "文件路径不在知识库受控目录中")
    return resolved


def _unlink_quietly(path, label):
    try:
        managed_path(path).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("%s 清理失败：%s", label, exc)


@serialized_mutation
def delete_file(file_id):
    file_info = get_file_by_id(file_id)
    if not file_info:
        return False
    task_service.ensure_file_idle(file_id)
    working = managed_path(file_info["file_path"])
    original = managed_path(file_info["original_file_path"]) if file_info.get("original_file_path") else None
    _file_repo.delete_with_chunks(file_id)
    _unlink_quietly(working, "工作文件")
    images = working.parent / working.stem
    if images.is_dir():
        shutil.rmtree(managed_path(images), ignore_errors=True)
    if original:
        _unlink_quietly(original, "原件副本")
    return True


@serialized_mutation
def create_empty_file(filename, collection_ids=None):
    filename = validate_filename(filename)
    if not filename.lower().endswith(".md"):
        filename += ".md"
    path = get_working_dir() / get_unique_filename(filename)
    created = False
    try:
        with path.open("x", encoding="utf-8"):
            created = True
        with get_db_cursor(write=True) as cursor:
            cursor.execute("INSERT INTO files (file_hash, filename, file_path, file_size, status) VALUES (?, ?, ?, 0, 'empty')",
                           (f"note:{uuid.uuid4().hex}", path.name, str(path)))
            file_id = cursor.lastrowid
            _assign_collections(cursor, file_id, collection_ids)
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise
    return {"file_id": file_id, "filename": path.name, "file_path": str(path), "status": "empty"}


def _reindex_source(file_id, source):
    file_info = get_file_by_id(file_id)
    if not file_info:
        raise BusinessError("NOT_FOUND", "文件不存在")
    source = source or ("original" if file_info.get("original_file_path") else "working")
    if source not in {"original", "working"}:
        raise BusinessError("INVALID_INPUT", "重索引来源必须为 original 或 working")
    path = file_info.get("original_file_path" if source == "original" else "file_path")
    if not path or not managed_path(path).is_file():
        raise BusinessError("NOT_FOUND", "重索引来源文件不存在")
    return file_info, source


@serialized_mutation
def preview_reindex(file_id, source=None):
    file_info, source = _reindex_source(file_id, source)
    task_service.ensure_file_idle(file_id)
    return {"file_id": file_id, "filename": file_info["filename"], "source": source,
            "chunks_count": _chunk_repo.count_by_file_id(file_id), "overwrites_manual_edits": True,
            "chunk_ids_may_change": True}


@serialized_mutation
def reindex_file(file_id, source=None, request_key=None):
    file_info, source = _reindex_source(file_id, source)
    if not task_service.get_task_by_request_key(request_key):
        task_service.ensure_file_idle(file_id)
        if source == "working" and file_info.get("working_dirty"):
            from .chunk_service import rebuild_working_file
            rebuild_working_file(file_id)
    task_id = task_service.create_task(file_info["filename"], file_id, input_data={"source": source}, request_key=request_key)
    return {"file_id": file_id, "task_id": task_id, "task_ids": [task_id], "source": source,
            "request_key": request_key, "status": "accepted", "chunk_ids_may_change": True}


def export_file(file_id, format="markdown"):
    file_info = get_file_by_id(file_id)
    if not file_info:
        raise BusinessError("NOT_FOUND", "文件不存在")
    if format not in {"markdown", "original"}:
        raise BusinessError("INVALID_INPUT", "导出格式必须为 markdown 或 original")
    if format == "markdown" and file_info.get("working_dirty"):
        from .chunk_service import rebuild_working_file
        rebuild_working_file(file_id)
    source = file_info.get("original_file_path" if format == "original" else "file_path")
    if not source or not managed_path(source).is_file():
        raise BusinessError("NOT_FOUND", "没有可导出的文件")
    return managed_path(source)


@serialized_mutation
def export_snapshot(file_id, format="markdown", include_resources=False):
    """创建稳定导出快照；响应结束后由入口清理 temporary_dir。"""
    from zipfile import ZipFile, ZIP_DEFLATED
    from retrieval.tools.chunk_images import _iter_refs, _resolve

    if include_resources and format != "markdown":
        raise BusinessError("INVALID_INPUT", "仅 Markdown 导出支持配套资源")
    source = export_file(file_id, format)
    directory = Path(tempfile.mkdtemp(prefix="piece-export-"))
    try:
        if include_resources:
            assets = {}
            for ref in _iter_refs(source.read_text(encoding="utf-8")):
                asset = _resolve(source.parent, ref)
                if asset is None or ".." in Path(ref).parts:
                    raise BusinessError("RESOURCE_UNAVAILABLE", "引用资源缺失或越出文档目录，未导出不完整归档", data={"reference": ref})
                assets[asset.relative_to(source.parent).as_posix()] = asset
            output = directory / f"{source.stem}.zip"
            with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
                archive.write(source, source.name)
                for name, asset in assets.items():
                    if name != source.name:
                        archive.write(asset, name)
        else:
            output = directory / source.name
            shutil.copy2(source, output)
        return {"path": output, "filename": output.name, "temporary_dir": directory}
    except BaseException:
        shutil.rmtree(directory)
        raise


@serialized_mutation
def recover_file_storage():
    """启动时清理有任务记录且已结束的暂存/未引用工作代，不触碰未知目录。"""
    with get_db_cursor() as cursor:
        cursor.execute("SELECT id, status FROM tasks WHERE task_type='file_index'")
        states = {row[0]: row[1] for row in cursor.fetchall()}
        cursor.execute("SELECT file_path FROM files")
        referenced = {Path(row[0]).resolve().parent for row in cursor.fetchall()}
    owner = storage_owner()
    for root in (get_files_dir() / ".staging", get_working_dir() / ".generations"):
        if not root.is_dir() or root.is_symlink() or root.is_junction():
            continue
        for directory in root.iterdir():
            match = re.fullmatch(r"task-(\d+)(?:-[0-9a-f]{32})?", directory.name)
            if (not match or directory.is_symlink() or directory.is_junction() or not directory.is_dir()
                    or directory.resolve() in referenced):
                continue
            marker = directory / ".piece-generation"
            if (states.get(int(match[1])) in task_service.TERMINAL_STATUSES
                    and marker.is_file() and not marker.is_symlink()
                    and marker.read_text(encoding="ascii").strip() == owner):
                shutil.rmtree(directory)


def scan_untracked_files():
    if not get_originals_dir().is_dir():
        return []
    tracked = _file_repo.find_tracked_original_paths()
    supported = set(ChunkerFactory.get_supported_extensions())
    return [{"original_filename": p.name, "original_file_path": str(p)}
            for p in get_originals_dir().iterdir()
            if p.is_file() and not p.is_symlink() and p.suffix.lower() in supported and str(p) not in tracked]


@serialized_mutation
def register_untracked_files():
    result = {"created": [], "skipped": [], "failed": []}
    for item in scan_untracked_files():
        name = item["original_filename"]
        try:
            accepted = import_file(item["original_file_path"], managed_original=True)
            if accepted["duplicate"]:
                result["skipped"].append(name)
            else:
                result["created"].append(accepted)
        except Exception as exc:
            result["failed"].append({"filename": name, "error": str(exc)})
    return result


def get_storage_stats():
    return {**_file_repo.get_storage_stats(), "total_chunks": _chunk_repo.get_total_count()}
