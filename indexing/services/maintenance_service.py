"""可自动化维护用例；入口负责取得确认，服务负责计算影响和检查对象。"""

import re
from pathlib import Path
from dataclasses import asdict

from . import file_service, chunk_service, collection_service, metadata_service
from .task_service import serialized_mutation
from .errors import BusinessError

MAX_EXTRACT_MATCHES = 50


def file_info(file_id):
    file = file_service.get_file_by_id(file_id)
    if not file:
        raise BusinessError("NOT_FOUND", "文件不存在")
    return {**file, "chunks_count": chunk_service.get_chunks_count_by_file_id(file_id),
            "collections": collection_service.get_file_collections(file_id),
            "properties": metadata_service.decode_metadata(file.get("metadata"))}


def chunk_info(chunk_id):
    chunk = chunk_service.get_chunk_by_id(chunk_id)
    if not chunk:
        raise BusinessError("NOT_FOUND", "卡片不存在")
    file = file_service.get_file_by_id(chunk["file_id"])
    from .page_render import page_number_from_heading
    return {**chunk, "chunk_id": chunk["id"], "filename": file["filename"],
            "page_number": page_number_from_heading(chunk.get("heading_path"))}


def _parse_line_range(value, total):
    """解析 N 或 N-M 形式的行号；超出正文范围时给出正文总行数。"""
    match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d*)\s*)?", value or "")
    if not match:
        raise BusinessError("INVALID_INPUT", "lines 需为 N 或 N-M 形式，行号从 1 开始")
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    if start < 1 or end < start:
        raise BusinessError("INVALID_INPUT", "行号范围无效，需满足 1 <= N <= M")
    if start > total:
        raise BusinessError("NOT_FOUND", f"行号超出正文范围：正文共 {total} 行")
    return start, min(end, total)


def _grep_line_spans(lines, pattern, regex, max_matches, context):
    """按行匹配，返回 (start,end) 行号区间；定位宽松，引文仍取整行精确文本。"""
    if regex:
        try:
            search = re.compile(pattern).search
        except re.error as exc:
            raise BusinessError("INVALID_INPUT", f"正则表达式无效：{exc}") from exc
    else:
        search = lambda line: pattern in line  # noqa: E731 - 与正则分支统一调用形态
    spans, total = [], len(lines)
    for index, line in enumerate(lines):
        if search(line):
            spans.append((max(1, index + 1 - context), min(total, index + 1 + context)))
            if len(spans) >= max_matches:
                break
    return spans


def extract_chunk(chunk_id, lines=None, grep=None, context=0, max_matches=20, regex=False):
    """从卡片正文切出精确引文，直接产出可提交的证据字段。

    定位（行号或匹配）可以宽松，但返回的 quote 始终是正文的精确子串，仍受
    apply 的逐字校验约束；服务不替 Agent 判断引文是否支持断言。
    """
    from .knowledge_common import content_hash, library_id, normalize_newlines
    chunk = chunk_service.get_chunk_by_id(chunk_id)
    if not chunk:
        raise BusinessError("NOT_FOUND", "卡片不存在")
    file = file_service.get_file_by_id(chunk["file_id"])
    text = normalize_newlines(chunk["chunk_text"])
    all_lines = text.split("\n")
    total = len(all_lines)
    if lines is not None:
        spans, truncated = [_parse_line_range(lines, total)], False
    else:
        found = _grep_line_spans(all_lines, grep, regex, max_matches, context)
        truncated = len(found) >= max_matches
        # 仅合并重叠的上下文区间，避免重复引文；相邻但不相交的匹配保持独立。
        spans = []
        for start, end in found:
            if spans and start <= spans[-1][1]:
                spans[-1] = (spans[-1][0], max(spans[-1][1], end))
            else:
                spans.append((start, end))
    from .page_render import page_number_from_heading
    source = {"source_kind": "piece", "source_library_id": library_id(),
              "source_file_id": chunk["file_id"], "source_chunk_id": chunk_id,
              "expected_content_hash": content_hash(chunk["chunk_text"])}
    matches = [{"line_start": start, "line_end": end, "quote": "\n".join(all_lines[start - 1:end]),
                "evidence": {**source, "quote": "\n".join(all_lines[start - 1:end])}} for start, end in spans]
    return {"chunk_id": chunk_id, "file_id": chunk["file_id"], "filename": file["filename"],
            "doc_title": chunk["doc_title"], "heading_path": chunk["heading_path"],
            "page_number": page_number_from_heading(chunk.get("heading_path")),
            "total_lines": total, "matches": matches, "match_count": len(matches), "truncated": truncated,
            "hint": "quote 为正文精确子串，可直接放入 evidence；定位不等于断言，须自行确认引文是否支持结论。"}


@serialized_mutation
def delete_files(file_ids, dry_run=False, confirmed=False):
    ids = list(dict.fromkeys(file_ids))
    files = [file_info(file_id) for file_id in ids]
    from .knowledge_service import file_reference_count as graph_reference_count
    from .wiki_service import file_reference_count as wiki_reference_count
    graph_count = sum(graph_reference_count(file_id) for file_id in ids)
    wiki_count = sum(wiki_reference_count(file_id) for file_id in ids)
    impact = {"file_ids": ids, "filenames": [f["filename"] for f in files],
              "chunks_count": sum(f["chunks_count"] for f in files), "deletes_original_copies": True,
              "knowledge_evidence_count": graph_count + wiki_count,
              "graph_evidence_count": graph_count, "wiki_evidence_count": wiki_count,
              "retains_knowledge_snapshots": True,
              "knowledge_warning": "Wiki 页面、图实体和各自引用快照将保留，来源将标记为 missing；彻底移除引用须分别显式删除相关证据和知识内容。"}
    if dry_run:
        return {**impact, "dry_run": True}
    if not confirmed:
        raise BusinessError("CONFIRMATION_REQUIRED", "删除文件及全部卡片、原件副本需要明确确认", data=impact)
    deleted, failed = [], []
    for file_id in ids:
        try:
            if file_service.delete_file(file_id):
                deleted.append(file_id)
        except Exception as exc:
            failed.append({"file_id": file_id, "error": str(exc)})
    return {**impact, "deleted_file_ids": deleted, "failed": failed, "failed_count": len(failed)}


@serialized_mutation
def delete_chunks(chunk_ids, dry_run=False, confirmed=False):
    ids = list(dict.fromkeys(chunk_ids))
    chunks = [chunk_info(chunk_id) for chunk_id in ids]
    files = {c["file_id"] for c in chunks}
    deleted_files = [fid for fid in files if sum(c["file_id"] == fid for c in chunks) == chunk_service.get_chunks_count_by_file_id(fid)]
    impact = {"chunk_ids": ids, "deletes_file_ids": deleted_files,
              "warning": "删除最后一张卡片会同时删除文件和原件副本"}
    if dry_run:
        return {**impact, "dry_run": True}
    if not confirmed:
        raise BusinessError("CONFIRMATION_REQUIRED", "删除卡片需要明确确认", data=impact)
    return {**impact, **chunk_service.batch_delete_chunks(ids)}


@serialized_mutation
def delete_collection(collection_id, dry_run=False, confirmed=False):
    impact = collection_service.collection_deletion_impact(collection_id)
    if dry_run:
        return {**impact, "dry_run": True}
    if not confirmed:
        raise BusinessError("CONFIRMATION_REQUIRED", "删除集合需要明确确认（不删除文件）", data=impact)
    collection_service.delete_collection(collection_id)
    return impact


def chunk_images(chunk_id):
    from retrieval.tools.chunk_images import collect_chunk_images
    chunk = chunk_info(chunk_id)
    file = file_service.get_file_by_id(chunk["file_id"])
    images, skipped = collect_chunk_images({chunk["doc_title"]: {**file, **chunk}}, max_images=20)
    return images, skipped


def source_page(file_id, page_number):
    from .page_render import resolve_source_pdf, render_pdf_page
    file = file_info(file_id)
    if not file.get("original_file_path"):
        raise BusinessError("NOT_FOUND", "没有原件可供查看")
    original = file_service.managed_path(file["original_file_path"])
    pdf = resolve_source_pdf(str(original), file["original_file_type"])
    path = render_pdf_page(pdf, page_number) if pdf else None
    if not path:
        raise BusinessError("PAGE_UNAVAILABLE", "无法取得原页，请检查页码和转换器")
    return path


def sync_status():
    from .sync_service import get_sync_service
    service = get_sync_service()
    return {"running": service.is_running(), "status": asdict(service.status),
            "result": asdict(service.last_result) if service.last_result else None,
            "finished_at": service.last_finished_at, "logs": service.get_logs()}


def run_sync_job(confirmed=False):
    from .sync_service import get_sync_service, start_sync
    if not confirmed:
        raise BusinessError("CONFIRMATION_REQUIRED", "同步会上传文档，日常同步还会删除云端对应文件，需要明确确认")
    if not get_sync_service().is_enabled():
        raise BusinessError("SYNC_DISABLED", "请先配置并启用 WebDAV")
    if not start_sync():
        raise BusinessError("SYNC_BUSY", "已有同步任务或服务正在退出")
    return {"status": "accepted", "message": "同步已受理，请通过 sync status 查询结果"}


def mcp_config(service="retrieval", include_secrets=False):
    from ..settings import get_settings, get_index_mcp_port
    settings = get_settings()
    port = settings.mcp.port if service == "retrieval" else get_index_mcp_port()
    name = "piece-kb" if service == "retrieval" else "piece-index"
    entry = {"url": f"http://127.0.0.1:{port}/mcp"}
    key = settings.mcp.get_api_key(service)
    if settings.mcp.auth_enabled and key:
        entry["headers"] = {"Authorization": f"Bearer {key}" if include_secrets else "Bearer <REDACTED>"}
    return {"mcpServers": {name: entry}, "contains_secrets": bool(include_secrets and key)}
