"""
后台任务处理模块。

文件索引编排、HTTP 调用和数据库写入留在服务进程，原生解析由短命 helper 隔离。
"""

import asyncio
import logging
import os
import json
import shutil
import uuid
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

from app.platform import get_process_rss_mb as _get_process_rss_mb
from ..repositories import ChunkRepository
from ..database import get_db_cursor
from ..settings import (
    get_embedding_config,
    get_pdf_parser,
    get_performance_config,
)
from ..utils import run_sync, serialize_float32
from . import chunk_service, file_service, task_service
from .chunking import ChunkerFactory, PageChunker
from .chunking.utils import strip_obsidian_noise
from .converter import convert_to_markdown, iter_pdf_pages
from .embedding_client import get_embeddings_model
from .metadata_service import parse_frontmatter
from .mineru_client import iter_mineru_pdf_pages
from .ocr_client import iter_ocr_pdf_pages
from .office_convert import (
    CONVERTER_ONLY_FORMATS,
    PDF_ROUTED_FORMATS,
    convert_to_pdf,
    list_converters,
)
from .parser_helper import ParserStopped, run_parser
from .rate_limiter import estimate_tokens, get_rate_limiter
from .vlm_client import iter_vlm_pdf_pages

logger = logging.getLogger(__name__)

DATABASE_BATCH_SIZE = 50

# Worker 空闲时的任务轮询间隔
IDLE_POLL_SECONDS = 0.3

_chunk_repo = ChunkRepository()
_MEMORY_LOG_INTERVAL = 10
_tracemalloc_started = False

# 内存埋点仅用于排查问题：tracemalloc 会 hook 每次内存分配，
# 且每页产生多条日志，默认关闭，需要时用 PIECE_MEMORY_PROFILE=1 打开。
_MEMORY_PROFILE_ENABLED = os.getenv("PIECE_MEMORY_PROFILE", "").strip().lower() in (
    "1",
    "true",
    "yes",
)


def _start_memory_tracing() -> None:
    """启动服务进程的 Python 堆内存追踪。"""
    global _tracemalloc_started
    if not _MEMORY_PROFILE_ENABLED:
        return
    if not tracemalloc.is_tracing():
        tracemalloc.start()
    _tracemalloc_started = True


def _log_memory(stage: str, **details) -> None:
    """记录服务进程的 RSS 和 Python 堆；原生缓存随 helper 退出释放。"""
    if not _MEMORY_PROFILE_ENABLED:
        return

    rss_mb = _get_process_rss_mb()
    current_mb = peak_mb = None
    if _tracemalloc_started:
        current, peak = tracemalloc.get_traced_memory()
        current_mb = current / (1024 * 1024)
        peak_mb = peak / (1024 * 1024)

    logger.info(
        "[内存] stage=%s, rss_mb=%s, tracemalloc_current_mb=%s, "
        "tracemalloc_peak_mb=%s, details=%s",
        stage,
        f"{rss_mb:.1f}" if rss_mb is not None else "unavailable",
        f"{current_mb:.1f}" if current_mb is not None else "disabled",
        f"{peak_mb:.1f}" if peak_mb is not None else "disabled",
        details,
    )


class WorkerStopRequested(Exception):
    """Worker 收到停止请求。"""


class SourceFileGone(Exception):
    """任务处理期间关联文件被删除。"""


# 任务阶段：queued 排队 -> parsing 解析 -> embedding 向量化
STAGE_PARSING = "parsing"
STAGE_EMBEDDING = "embedding"

# 进度分配：起步 5%，解析和向量各占 45%，收尾置 100%
PROGRESS_START = 5
PROGRESS_PARSE_SHARE = 45
PROGRESS_EMBED_SHARE = 45


def _ensure_file_alive(file_id: int) -> None:
    """写库前确认文件未被删除。

    索引期间用户可能在 UI 上删掉该文件，此时 files 行已消失，
    继续写 chunks 会撞 FOREIGN KEY 约束并让 Worker 抛栈退出。
    """
    if not file_service.file_exists(file_id):
        raise SourceFileGone(f"文件已被删除（file_id={file_id}），任务终止")


def _raise_if_stopping(stop_event) -> None:
    if stop_event is not None and stop_event.is_set():
        raise WorkerStopRequested("Worker 已停止，任务未完成")


# 嵌入并发闸门：在事件循环内首次使用时按配置创建
_embedding_semaphore: Optional[asyncio.Semaphore] = None


def _get_embedding_semaphore() -> asyncio.Semaphore:
    """获取限制在途嵌入请求数的信号量。"""
    global _embedding_semaphore
    if _embedding_semaphore is None:
        concurrency = max(1, get_performance_config().embedding_concurrency)
        _embedding_semaphore = asyncio.Semaphore(concurrency)
    return _embedding_semaphore


def get_embedding_batch_size() -> int:
    """每个嵌入请求携带的切片数。"""
    return max(1, get_performance_config().embedding_batch_size)


async def _embed_batch(
    batch_chunks: List[Dict[str, str]],
    embeddings_model,
    semaphore: asyncio.Semaphore,
    stop_event=None,
) -> List[List[float]]:
    """发送单批嵌入请求，遇到限流错误时退避重试。"""
    texts = [chunk["chunk_text"] for chunk in batch_chunks]

    async with semaphore:
        for retry in range(3):
            _raise_if_stopping(stop_event)
            # 按预估 token 数申请额度：小请求不再被固定间隔拖慢
            await get_rate_limiter().acquire(estimate_tokens(texts))
            try:
                return await embeddings_model.aembed_documents(texts)
            except Exception as exc:
                error_message = str(exc).lower()
                is_rate_limit = (
                    "429" in error_message
                    or "403" in error_message
                    or "rate limit" in error_message
                )
                if is_rate_limit and retry < 2:
                    await asyncio.sleep(5 * (retry + 1))
                    continue
                raise

    raise RuntimeError("嵌入请求重试次数已用尽")


async def _generate_embeddings(
    chunks: List[Dict[str, str]], embeddings_model, stop_event=None
) -> List[List[float]]:
    """并发生成一组切片的向量，返回顺序与输入一致。"""
    _raise_if_stopping(stop_event)
    if not chunks:
        return []

    batch_size = get_embedding_batch_size()
    batches = [
        chunks[start : start + batch_size]
        for start in range(0, len(chunks), batch_size)
    ]

    _log_memory(
        "embedding_before",
        chunk_count=len(chunks),
        batch_count=len(batches),
        text_chars=sum(len(chunk["chunk_text"]) for chunk in chunks),
    )

    semaphore = _get_embedding_semaphore()
    # return_exceptions 保证所有批次都结算完再抛错，避免失败时留下游离任务
    batch_results = await asyncio.gather(
        *(
            _embed_batch(batch, embeddings_model, semaphore, stop_event)
            for batch in batches
        ),
        return_exceptions=True,
    )
    for result in batch_results:
        if isinstance(result, BaseException):
            raise result

    embeddings = [
        embedding for batch_result in batch_results for embedding in batch_result
    ]
    _log_memory(
        "embedding_after",
        chunk_count=len(chunks),
        embedding_count=len(embeddings),
    )

    _raise_if_stopping(stop_event)
    return embeddings


def insert_chunks_batch(file_id, chunks, embeddings, task_id=None) -> None:
    """解析结果先入暂存表，全部成功后才替换可检索索引。"""
    if len(chunks) != len(embeddings):
        raise ValueError("嵌入服务返回的向量数量与切片数量不一致")
    if task_id is None:
        raise ValueError("暂存切片必须关联持久化任务")
    with get_db_cursor(write=True) as cursor:
        cursor.execute("SELECT COALESCE(MAX(chunk_index), -1) + 1 FROM staged_chunks WHERE task_id = ?", (task_id,))
        next_index = cursor.fetchone()[0]
        cursor.executemany(
            "INSERT INTO staged_chunks (task_id, file_id, doc_title, chunk_text, chunk_index, heading_path, heading_level, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(task_id, file_id, c["doc_title"], c["chunk_text"], next_index + i, c.get("heading_path"),
              c.get("heading_level", 2), serialize_float32(v)) for i, (c, v) in enumerate(zip(chunks, embeddings))],
        )


@task_service.serialized_mutation
def _publish_file(task_id, file_id, working_path, metadata):
    """新工作代已落盘；索引、工作路径及任务产物在同一事务切换。"""
    with get_db_cursor(write=True) as cursor:
        cursor.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
        if cursor.fetchone()[0] != "processing":
            raise ValueError("任务已不在处理中，拒绝发布")
        cursor.execute("DELETE FROM vec_chunks WHERE chunk_id IN (SELECT id FROM chunks WHERE file_id = ?)", (file_id,))
        cursor.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
        staged = cursor.connection.execute("SELECT * FROM staged_chunks WHERE task_id = ? ORDER BY chunk_index", (task_id,))
        ids = []
        for chunk in staged:
            cursor.execute(
                "INSERT INTO chunks (file_id, doc_title, chunk_text, chunk_index, heading_path, heading_level, embedding) VALUES (?, ?, ?, ?, ?, ?, ?)",
                tuple(chunk[k] for k in ("file_id", "doc_title", "chunk_text", "chunk_index", "heading_path", "heading_level", "embedding")),
            )
            chunk_id = cursor.lastrowid
            ids.append(chunk_id)
            cursor.execute("INSERT INTO vec_chunks (chunk_id, embedding) VALUES (?, ?)", (chunk_id, chunk["embedding"]))
        staged.close()
        cursor.execute("UPDATE files SET file_path = ?, status = 'indexed', working_dirty = 0, updated_at = ? WHERE id = ?",
                       (str(working_path), task_service.now_marker(), file_id))
        if metadata:
            cursor.execute("UPDATE files SET metadata = ? WHERE id = ?", (json.dumps(metadata, ensure_ascii=False), file_id))
        cursor.execute("DELETE FROM staged_chunks WHERE task_id = ?", (task_id,))
        task_service.update_task_status(task_id, "completed", progress=100,
                                       result={"file_id": file_id, "chunk_ids": ids}, cursor=cursor)


def mark_file_failure(file_id):
    # 已有可用索引在重索引失败、中断时继续可检索。
    with get_db_cursor(write=True) as cursor:
        cursor.execute("UPDATE files SET status = 'error' WHERE id = ? AND NOT EXISTS (SELECT 1 FROM chunks WHERE file_id = ?)",
                       (file_id, file_id))


def _iter_pdf_source_pages(
    original_file_path: Path,
    working_file_path: Path,
    stop_event=None,
    on_parse_progress=None,
):
    """按配置选择 PDF 页面来源：PaddleOCR 服务、MinerU、自定义多模态模型或本地文本层。

    四种来源的页面对象均提供 page_number/total_pages/page_text 属性，
    并通过 on_parse_progress(已解析页, 总页数) 上报解析进度。
    """
    parser = get_pdf_parser()

    if parser == "paddle":
        # 图片下载到工作文件同名目录，Markdown 中的相对引用可直接生效
        image_dir = working_file_path.parent / working_file_path.stem
        return iter_ocr_pdf_pages(
            original_file_path,
            image_dir,
            on_parse_progress=on_parse_progress,
            stop_check=stop_event.is_set if stop_event is not None else None,
        )

    if parser == "mineru":
        # MinerU 同样产出插图，落盘规则与 PaddleOCR 路径一致
        image_dir = working_file_path.parent / working_file_path.stem
        return iter_mineru_pdf_pages(
            original_file_path,
            image_dir,
            on_parse_progress=on_parse_progress,
            stop_check=stop_event.is_set if stop_event is not None else None,
        )

    if parser == "vlm":
        # 多模态模型只产出文本，不落图片，因此不需要图片目录
        return iter_vlm_pdf_pages(
            original_file_path,
            on_parse_progress=on_parse_progress,
            stop_check=stop_event.is_set if stop_event is not None else None,
        )

    def _local_pages():
        # 本地文本层解析是逐页即时完成的，产出即视为已解析
        for page_data in iter_pdf_pages(
            original_file_path,
            stop_check=stop_event.is_set if stop_event is not None else None,
        ):
            page = SimpleNamespace(**page_data)
            if on_parse_progress is not None:
                on_parse_progress(page.page_number, page.total_pages)
            yield page

    return _local_pages()


# 没有 OCR 时才看本地文本层够不够兜底，每页平均字符数低于此值就放弃。
# 实测同一份 16 页数学 PPT：COM 导出 1848 字符（115/页），
# LibreOffice 只有 180 字符（11/页，公式被整块画成图片）
_TEXT_LAYER_MIN_CHARS_PER_PAGE = 50


def _has_usable_text_layer(pdf_path: Path) -> bool:
    """判断 PDF 自带的文本层是否还值得一解析。"""
    try:
        stats = run_parser("text_stats", pdf_path)
    except ParserStopped:
        raise
    except Exception as exc:
        logger.warning("[Processor] 文本层探测失败: %s - %s", pdf_path.name, exc)
        return False

    return stats["pages"] > 0 and stats["chars"] / stats["pages"] >= _TEXT_LAYER_MIN_CHARS_PER_PAGE


def _office_pdf_route(original_file_path: Path) -> Optional[Path]:
    """把 docx/pptx 转成 PDF 供 PDF 管线解析，不适合走这条路时返回 None。

    markitdown 依赖的 python-pptx / mammoth 会跳过 mc:AlternateContent，
    公式连同周围正文整块丢失，所以优先转 PDF 交给 PDF 管线。

    转出来的 PDF 与原生 PDF 一视同仁，解析后端仍按 ocr.provider 决定。本地
    文本层虽然快，但只会按行读取，矩阵、表格这类二维版面会被拆成一行一个
    元素（实测 3x3 矩阵散成 9 行），而 OCR 能还原成 LaTeX；因此只在没有
    OCR 服务时才靠本地解析兜底，兜不住就回退 markitdown。

    Returns:
        转换后的 PDF 路径；无转换器、或没有 OCR 且文本层也不足时为 None
    """
    pdf_path = convert_to_pdf(original_file_path)
    if pdf_path is None:
        return None

    if get_pdf_parser() != "local" or _has_usable_text_layer(pdf_path):
        return pdf_path

    logger.info(
        "[Processor] %s 转出的 PDF 文本层不足且未配置 OCR，回退 markitdown",
        original_file_path.name,
    )
    return None


def _converter_only_failure(file_extension: str) -> str:
    """CONVERTER_ONLY_FORMATS 拿不到 PDF 时的失败原因。

    _office_pdf_route 返回 None 有两种成因，缺的东西完全不同：
    没有转换器（装 Office / LibreOffice），或转出的 PDF 文本层不足（配 OCR）。
    """
    if not list_converters():
        return (
            f"解析 {file_extension} 需要 Microsoft Office 或 LibreOffice，"
            "请安装其一或在设置中指定 LibreOffice 路径后重试"
        )
    return f"{file_extension} 转出的 PDF 没有可用文本层，请配置 OCR 服务后重试"


async def _process_pdf_file(
    task_id: int,
    file_id: int,
    original_file_path: Path,
    working_file_path: Path,
    base_name: str,
    embeddings_model,
    stop_event=None,
) -> None:
    """按页完成 PDF 解析、切片、向量生成和入库。

    解析后端由 ocr.provider 选择：PaddleOCR 服务、MinerU、自定义多模态模型，
    两者都未配置完整时使用本地 PyMuPDF 文本层。
    单页通常只切出 1 个切片，因此切片跨页累积到一定数量再统一发送，
    避免每页各发一次嵌入请求。

    解析与向量化是流水线重叠的（当前批结果在向量化时，下一批已提交给 OCR
    服务），所以进度由"已解析页"和"已入库页"两个单调计数分别计权，
    两个写入方各自只会让自己那一半前进，不会互相回退。
    """
    _start_memory_tracing()
    _log_memory("pdf_start", task_id=task_id)
    page_chunker = ChunkerFactory.get_chunker(".pdf")
    assert isinstance(page_chunker, PageChunker)

    parsed_pages = 0      # 解析侧：OCR 服务已解析 / 本地已产出的页数
    embedded_pages = 0    # 入库侧：切片已写库覆盖到的页数
    total_pages = 0
    consumed_pages = 0    # 实际消费到的页数（用于判断是否有有效内容）
    processed_chunks = 0
    pending_last_page = 0

    # 攒够一轮并发所需的切片数再发送，让并发闸门跑满
    performance = get_performance_config()
    flush_size = get_embedding_batch_size() * max(1, performance.embedding_concurrency)
    pending_chunks: List[Dict[str, str]] = []

    def _report_progress() -> None:
        """按已解析/已入库页数换算总进度写库（解析回调在工作线程中调用）。"""
        if total_pages <= 0:
            return
        parsing = parsed_pages < total_pages
        # 向量段再细分：页面切分入缓冲算一半，实际写库算另一半。
        # 否则切片总数不足 flush_size 的文档（一次性 flush）会从 50% 直接跳到 95%。
        embed_units = embedded_pages + consumed_pages
        progress = (
            PROGRESS_START
            + int(PROGRESS_PARSE_SHARE * parsed_pages / total_pages)
            + int(PROGRESS_EMBED_SHARE * embed_units / (2 * total_pages))
        )
        task_service.update_page_progress(
            task_id,
            parsed_pages if parsing else consumed_pages,
            total_pages,
            processed_chunks,
            min(progress, 95),
            STAGE_PARSING if parsing else STAGE_EMBEDDING,
        )

    def _on_parse_progress(parsed: int, total: int) -> None:
        """解析侧进度回调，在生成器所在的工作线程中执行。"""
        nonlocal parsed_pages, total_pages
        total_pages = total
        parsed_pages = max(parsed_pages, parsed)
        _report_progress()

    async def flush_pending() -> None:
        """把累积的切片并发生成向量后批量写库。"""
        nonlocal processed_chunks, embedded_pages
        if not pending_chunks:
            return
        batch = list(pending_chunks)
        covered_page = pending_last_page
        pending_chunks.clear()
        embeddings = await _generate_embeddings(batch, embeddings_model, stop_event)
        # 向量生成期间用户可能已把文件删掉，写库前复查，避免撞外键约束
        await run_sync(_ensure_file_alive, file_id)
        await run_sync(insert_chunks_batch, file_id, batch, embeddings, task_id)
        processed_chunks += len(batch)
        embedded_pages = max(embedded_pages, covered_page)
        await run_sync(_report_progress)

    working_file_path.parent.mkdir(parents=True, exist_ok=True)

    # 先把阶段标成"解析中"：OCR 提交和服务端排队可能持续很久，
    # 在拿到第一次轮询响应之前至少让 UI 知道任务已经动起来了
    await run_sync(
        task_service.update_page_progress,
        task_id, 0, 0, 0, PROGRESS_START, STAGE_PARSING,
    )

    pages = _iter_pdf_source_pages(
        original_file_path, working_file_path, stop_event, _on_parse_progress
    )
    try:
        with open(working_file_path, "w", encoding="utf-8") as working_file:
            while True:
                # 生成器内部是阻塞的（OCR 轮询最长可达数十分钟），逐页放到
                # 线程里推进，避免占住 Worker 的事件循环
                page_data = await run_sync(next, pages, None)
                if page_data is None:
                    break
                _raise_if_stopping(stop_event)

                page_number = page_data.page_number
                total_pages = page_data.total_pages
                page_text = page_data.page_text
                consumed_pages = page_number

                working_file.write(page_text)
                working_file.write("\n\n")
                working_file.flush()

                page_chunks = page_chunker.chunk_page(
                    page_text,
                    base_name,
                    page_number,
                )
                pending_chunks.extend(page_chunks)
                pending_last_page = page_number
                if len(pending_chunks) >= flush_size:
                    await flush_pending()
                else:
                    # 未触发 flush 也让进度前进一格，避免整段向量化只有一次跳变
                    await run_sync(_report_progress)

                if (
                    page_number % _MEMORY_LOG_INTERVAL == 0
                    or page_number == total_pages
                ):
                    _log_memory(
                        "pdf_page",
                        task_id=task_id,
                        page=page_number,
                        total_pages=total_pages,
                        page_text_chars=len(page_text),
                        chunk_count=len(page_chunks),
                        pending_chunks=len(pending_chunks),
                    )

                del page_text, page_chunks, page_data

        # 收尾：处理最后一批不足 flush_size 的切片
        await flush_pending()
    finally:
        # 提前退出（停止/删除/异常）时关闭生成器，触发其内部的
        # OCR 客户端关闭和临时目录清理
        await run_sync(pages.close)

    if consumed_pages == 0 or processed_chunks == 0:
        raise ValueError("PDF 无有效文本切片（扫描件请配置 OCR 服务后重试）")


async def _process_regular_file(
    task_id: int,
    file_id: int,
    original_file_path: Optional[Path],
    working_file_path: Path,
    file_extension: str,
    base_name: str,
    embeddings_model,
    stop_event=None,
) -> None:
    """处理非 PDF 文件。"""
    _raise_if_stopping(stop_event)

    # 转换阶段没有页的概念，只给出阶段和起止进度
    await run_sync(
        task_service.update_page_progress,
        task_id, 0, 0, 0, PROGRESS_START, STAGE_PARSING,
    )

    if original_file_path and original_file_path.exists():
        # 内嵌图片落到工作文件同名目录，与 OCR 解析产出的布局一致，
        # 正文中的相对引用可直接被切片渲染和 MCP 插图返回复用
        content = await run_sync(
            convert_to_markdown,
            original_file_path,
            working_file_path.parent / working_file_path.stem,
        )
    else:
        content = await run_sync(
            working_file_path.read_text,
            encoding="utf-8",
        )

    # Markdown 开头的 YAML frontmatter 存为文件属性，并从正文剥离，
    # 否则这段元数据会混进第一个切片污染检索
    metadata, content = await run_sync(parse_frontmatter, content)
    # 属性随成功发布的索引一起切换，失败不能提前覆盖现有属性。
    if file_extension == ".md":
        # Obsidian 笔记的 %% 注释和 dataview 查询块不是正文，不进切片
        content = strip_obsidian_noise(content)

    await run_sync(
        working_file_path.write_text,
        content,
        encoding="utf-8",
    )

    chunker = ChunkerFactory.get_chunker(file_extension)
    chunks = await run_sync(chunker.chunk, content, base_name)
    del content
    if not chunks:
        raise ValueError("无有效分块")

    processed_chunks = 0
    total_chunks = len(chunks)
    # 转换完成，进入向量化阶段
    embed_base = PROGRESS_START + PROGRESS_PARSE_SHARE
    await run_sync(
        task_service.update_page_progress,
        task_id, 0, total_chunks, 0, embed_base, STAGE_EMBEDDING,
    )
    # 每轮交给 _generate_embeddings 的切片数：并发闸门跑满的同时保持进度可见
    performance = get_performance_config()
    flush_size = get_embedding_batch_size() * max(1, performance.embedding_concurrency)
    for start in range(0, total_chunks, flush_size):
        _raise_if_stopping(stop_event)
        batch_chunks = chunks[start : start + flush_size]
        batch_embeddings = await _generate_embeddings(
            batch_chunks,
            embeddings_model,
            stop_event,
        )
        # 向量生成期间用户可能已把文件删掉，写库前复查，避免撞外键约束
        await run_sync(_ensure_file_alive, file_id)
        await run_sync(
            insert_chunks_batch,
            file_id,
            batch_chunks,
            batch_embeddings,
            task_id,
        )

        processed_chunks += len(batch_chunks)
        progress = embed_base + int(PROGRESS_EMBED_SHARE * processed_chunks / total_chunks)
        await run_sync(
            task_service.update_page_progress,
            task_id,
            processed_chunks,
            total_chunks,
            processed_chunks,
            progress,
            STAGE_EMBEDDING,
        )
        del batch_chunks, batch_embeddings
    return metadata


def _clean_staging(task_id, file_id, staging, destination):
    current = file_service.get_file_by_id(file_id)
    if destination.exists() and (not current or Path(current["file_path"]).parent != destination):
        file_service.remove_tree(destination, ignore_errors=True)
    file_service.remove_tree(staging, ignore_errors=True)
    with get_db_cursor(write=True) as cursor:
        cursor.execute("DELETE FROM staged_chunks WHERE task_id = ?", (task_id,))


def _mark_generation(staging):
    """暂存目录带本机文件区标识，重启清理只回收自己产生的目录。"""
    staging.mkdir(parents=True, exist_ok=False)
    (staging / ".piece-generation").write_text(file_service.storage_owner(), encoding="ascii")


async def process_task(task_id: int, stop_event=None) -> None:
    """处理一个文件索引任务。"""
    # 与 UI 共用配置缓存，不再从另一个进程重读并覆盖刚保存的设置。

    task = await run_sync(task_service.get_task, task_id)
    if not task:
        return

    file_id = task.get("file_id")
    if not file_id:
        await run_sync(
            task_service.update_task_status,
            task_id,
            "failed",
            error_message="任务未关联文件",
        )
        return

    file_info = await run_sync(file_service.get_file_by_id, file_id)
    if not file_info:
        await run_sync(
            task_service.update_task_status,
            task_id,
            "failed",
            error_message="关联文件不存在",
        )
        return

    old_working = file_service.managed_path(file_info["file_path"])
    source = task["input"].get("source", "original" if file_info.get("original_file_path") else "working")
    original_file_path = (
        file_service.managed_path(file_info["original_file_path"])
        if source == "original" and file_info.get("original_file_path") else None
    )
    file_extension = f".{file_info['original_file_type']}" if original_file_path else ".md"
    base_name = Path(file_info["filename"]).stem
    staging = file_service.get_files_dir() / ".staging" / f"task-{task_id}"
    # 代目录名计入插图路径长度（见 file_service.MAX_STORED_STEM），8 位随机串足以区分同一任务的重跑
    destination = file_service.get_working_dir() / ".generations" / f"task-{task_id}-{uuid.uuid4().hex[:8]}"
    working_file_path = staging / file_info["filename"]
    metadata = None

    try:
        await run_sync(_mark_generation, staging)
        if source == "working":
            await run_sync(shutil.copy2, old_working, working_file_path)
            old_images = old_working.parent / old_working.stem
            if old_images.is_dir():
                await run_sync(shutil.copytree, old_images, staging / old_working.stem)
        elif original_file_path is None or not original_file_path.is_file():
            raise FileNotFoundError("原始文件不存在")
        config = get_embedding_config()
        if not config["api_key"]:
            raise ValueError("未配置 EMBEDDING_API_KEY")

        await run_sync(
            task_service.update_task_status,
            task_id,
            "processing",
            progress=1,
        )

        embeddings_model = await run_sync(get_embeddings_model)

        # Word/PPT 系列先转 PDF 再走 PDF 管线，转不了或不划算时回退 markitdown。
        # xlsx 不在此列：markitdown 对表格的提取是结构化的，转 PDF 会把表格
        # 压平成版面文字，是明显的倒退
        office_pdf = None
        if (
            file_extension in PDF_ROUTED_FORMATS
            and original_file_path
            and original_file_path.exists()
        ):
            # 转换要几秒到几十秒，先把阶段标成解析中，别让进度条干停在 1%
            await run_sync(
                task_service.update_page_progress,
                task_id, 0, 0, 0, PROGRESS_START, STAGE_PARSING,
            )
            office_pdf = await run_sync(_office_pdf_route, original_file_path)
            _raise_if_stopping(stop_event)

            # 传统二进制格式和 ODF markitdown 读不了，转不出 PDF 就没有退路，
            # 与其让它在 markitdown 里报一句看不懂的错，不如直说缺什么
            if office_pdf is None and file_extension in CONVERTER_ONLY_FORMATS:
                raise ValueError(_converter_only_failure(file_extension))

        if file_extension == ".pdf":
            if not original_file_path or not original_file_path.exists():
                raise FileNotFoundError("PDF 原始文件不存在")
            await _process_pdf_file(
                task_id,
                file_id,
                original_file_path,
                working_file_path,
                base_name,
                embeddings_model,
                stop_event,
            )
        elif office_pdf is not None:
            await _process_pdf_file(
                task_id,
                file_id,
                office_pdf,
                working_file_path,
                base_name,
                embeddings_model,
                stop_event,
            )
        else:
            metadata = await _process_regular_file(
                task_id,
                file_id,
                original_file_path,
                working_file_path,
                file_extension,
                base_name,
                embeddings_model,
                stop_event,
            )

        _raise_if_stopping(stop_event)
        destination.parent.mkdir(parents=True, exist_ok=True)
        await run_sync(staging.rename, destination)
        await run_sync(_publish_file, task_id, file_id, destination / file_info["filename"], metadata)
        # 新一代已经发布；旧代不再被数据库引用，尽力清理即可。
        if old_working.parent.parent.name == ".generations":
            await run_sync(file_service.remove_tree, old_working.parent, True)
        else:
            await run_sync(file_service._unlink_quietly, old_working, "旧工作文件")
        _log_memory("task_end", task_id=task_id, file_id=file_id)
        logger.info("[Worker] 文件处理完成: task_id=%s, file_id=%s", task_id, file_id)
    except SourceFileGone as exc:
        # 文件已被用户删除：files 行不存在，不能再写 file_status，
        # 任务本身标记为取消，避免在 UI 上留下一条失败记录
        logger.info("[Worker] %s，task_id=%s", exc, task_id)
        await run_sync(
            task_service.update_task_status,
            task_id,
            "cancelled",
            error_message=str(exc),
        )
    except (WorkerStopRequested, ParserStopped) as exc:
        await run_sync(mark_file_failure, file_id)
        await run_sync(
            task_service.update_task_status,
            task_id,
            "failed",
            error_message=str(exc),
        )
    except Exception as exc:
        logger.exception("[Worker] 文件处理失败: task_id=%s", task_id)
        await run_sync(mark_file_failure, file_id)
        await run_sync(
            task_service.update_task_status,
            task_id,
            "failed",
            error_message=str(exc),
            error_code="PROCESSING_FAILED",
        )
    finally:
        await run_sync(_clean_staging, task_id, file_id, staging, destination)


class TaskProcessor:
    """服务进程内的任务处理循环，与 UI、MCP 共用事件循环和连接池。"""

    async def run(self, stop_event) -> None:
        global _embedding_semaphore
        _embedding_semaphore = None
        _start_memory_tracing()
        _log_memory("worker_start")
        max_concurrency = max(1, get_performance_config().worker_concurrency)
        logger.info("[Worker] 任务处理循环已启动，并发任务数=%s", max_concurrency)

        running: set[asyncio.Task] = set()
        try:
            while not stop_event.is_set():
                while len(running) < max_concurrency and not stop_event.is_set():
                    try:
                        task = await run_sync(task_service.claim_next_pending_task)
                    except Exception:
                        logger.exception("[Worker] 领取任务失败，稍后重试")
                        break
                    if not task:
                        break
                    running.add(asyncio.create_task(
                        self._dispatch_task(task, stop_event), name=f"index-task-{task['id']}",
                    ))

                if not running:
                    await asyncio.sleep(IDLE_POLL_SECONDS)
                    continue
                done, running = await asyncio.wait(
                    running, timeout=IDLE_POLL_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                self._log_failures(done)

            if running:
                await asyncio.gather(*running, return_exceptions=True)
        finally:
            stop_event.set()
            # 编排协程被取消也必须等待全部子任务，不把写线程遗留给已关闭的连接池。
            for task in running:
                if not task.done():
                    task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
                self._log_failures(running)
            _log_memory("worker_stop")
            logger.info("[Worker] 任务处理循环已停止")

    @staticmethod
    def _log_failures(done: set) -> None:
        for finished in done:
            if not finished.cancelled() and finished.exception() is not None:
                logger.error("[Worker] 任务协程异常退出", exc_info=finished.exception())

    @staticmethod
    def _fail_task(task_id: int, message: str) -> None:
        task = task_service.get_task(task_id)
        if not task or task["status"] != "processing":
            return
        task_service.update_task_status(task_id, "failed", error_message=message)
        if task.get("file_id") and task["task_type"] == "file_index":
            mark_file_failure(task["file_id"])

    async def _dispatch_task(self, task: dict, stop_event) -> None:
        task_id = task["id"]
        task_type = task["task_type"]
        try:
            _raise_if_stopping(stop_event)
            if task_type == "chunk_update":
                await chunk_service.process_chunk_update_task(task_id)
            elif task_type == "chunk_add":
                await chunk_service.process_chunk_add_task(task_id)
            elif task_type == "file_index":
                await process_task(task_id, stop_event)
            else:
                raise ValueError(f"未知任务类型：{task_type}")
        except asyncio.CancelledError:
            await run_sync(self._fail_task, task_id, "应用关闭，索引任务已中断")
            raise
        except (WorkerStopRequested, ParserStopped) as exc:
            await run_sync(self._fail_task, task_id, str(exc))
        except Exception as exc:
            logger.exception("[Worker] 任务调度失败: task_id=%s", task_id)
            await run_sync(self._fail_task, task_id, str(exc))


processor = TaskProcessor()
