"""
PaddleOCR 文档解析客户端

通过 HTTP 直连 PaddleOCR 兼容服务（官方云 API 或本地自建服务），
将 PDF 按批次转换为带版面还原的 Markdown，并下载引用的图片。

接口协议参考 paddleocr 官方 SDK:
- 提交任务: POST {base_url}/api/v2/ocr/jobs (multipart, 字段 file/model/pageRanges/optionalPayload)
- 轮询状态: GET  {base_url}/api/v2/ocr/jobs/{jobId}
- 状态枚举: pending | running | done | failed
- 任务完成后 resultUrl.jsonUrl 指向 JSONL 结果，每行含
  result.layoutParsingResults[].markdown.{text, images}
- optionalPayload 为 JSON 字符串字段，透传模型解析参数（方向矫正、扭曲矫正、
  版面标签过滤等），字段表见 PaddleX serving schema paddleocr_vl.InferRequest
"""

import json
import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, Generator, List, Optional, Tuple

import httpx

from ..settings import get_ocr_config
from ..utils import wait_for_stop
from .parser_helper import run_parser

logger = logging.getLogger(__name__)

API_PATH = "/api/v2/ocr/jobs"
# 每批解析的页数：控制单批结果 JSONL 大小，同时让进度平滑推进
OCR_BATCH_PAGES = 40
# 轮询参数：与官方 SDK 默认值一致
POLL_INTERVAL_INITIAL = 3.0
POLL_INTERVAL_MAX = 15.0
POLL_INTERVAL_MULTIPLIER = 1.5


class OcrError(Exception):
    """OCR 服务调用失败。"""


class OcrPageData:
    """单页 OCR 解析结果。"""

    __slots__ = ("page_number", "total_pages", "page_text", "images")

    def __init__(
        self,
        page_number: int,
        total_pages: int,
        page_text: str,
        images: Dict[str, str],
    ):
        self.page_number = page_number
        self.total_pages = total_pages
        self.page_text = page_text
        # 相对路径 -> 临时下载 URL
        self.images = images


def _get_total_pages(file_path: Path) -> int:
    """只让 helper 打开 PDF，HTTP 与进度回调仍在服务进程。"""
    return run_parser("page_count", file_path)


def _format_page_ranges(start: int, end: int) -> str:
    """生成 pageRanges 参数格式，如 "41-80"（页码从 1 开始）。"""
    if start == end:
        return str(start)
    return f"{start}-{end}"


def _extract_page_range(file_path: Path, start: int, end: int, dest: Path) -> None:
    """把指定页范围交给 helper 导出，避免每批重传整份文件。"""
    run_parser("extract_pages", file_path, start, end, dest)


class OcrClient:
    """PaddleOCR 兼容服务的异步客户端（阻塞调用，由上层放到线程池执行）。"""

    def __init__(self, base_url: str, token: str, model: str, timeout: float = 60.0):
        base_url = base_url.strip().rstrip("/")
        self._jobs_url = f"{base_url}{API_PATH}"
        self._model = model
        self._timeout = timeout
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
        )

    def close(self) -> None:
        self._client.close()

    def _unwrap(self, payload: dict) -> dict:
        """解包 API 响应的 data 字段，非零 code 视为错误。"""
        code = payload.get("code", 0)
        if code not in (0, None):
            msg = payload.get("msg") or payload.get("errorMsg") or payload.get("message") or ""
            raise OcrError(f"OCR 服务返回错误 (code={code}): {msg}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise OcrError("OCR 服务响应缺少 data 字段")
        return data

    def submit_job(
        self, file_path: Path, page_ranges: Optional[str] = None, optional_payload: Optional[dict] = None
    ) -> str:
        """提交文档解析任务，返回 jobId。

        multipart 上传时 optionalPayload 必须是 JSON 字符串字段（与官方 SDK 一致）。
        """
        data = {"model": self._model}
        if page_ranges is not None:
            data["pageRanges"] = page_ranges
        if optional_payload:
            data["optionalPayload"] = json.dumps(optional_payload)
        try:
            with open(file_path, "rb") as f:
                resp = self._client.post(
                    self._jobs_url,
                    data=data,
                    files={"file": f},
                )
        except httpx.HTTPError as exc:
            raise OcrError(f"提交 OCR 任务失败: {exc}") from exc
        if resp.status_code in (401, 403):
            raise OcrError(f"OCR 认证失败 ({resp.status_code})，请检查 api_key: {resp.text}")
        if resp.status_code == 429:
            raise OcrError(f"OCR 服务限流 (429): {resp.text}")
        if not (200 <= resp.status_code < 300):
            raise OcrError(f"提交 OCR 任务失败 ({resp.status_code}): {resp.text}")
        job_id = self._unwrap(resp.json()).get("jobId")
        if not isinstance(job_id, str) or not job_id:
            raise OcrError("OCR 服务响应缺少 jobId")
        return job_id

    def poll_until_done(
        self,
        job_id: str,
        poll_timeout: float = 7200.0,
        stop_check: Optional[Callable[[], bool]] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> str:
        """
        轮询任务直到完成，返回结果 JSONL 的 URL。

        Args:
            job_id: 任务 ID
            poll_timeout: 总超时秒数
            stop_check: 返回 True 时中止轮询（用于响应 Worker 停止）
            progress_callback: (已解析页数, 该批总页数)，服务端上报解析进度时调用
        """
        interval = POLL_INTERVAL_INITIAL
        deadline = time.monotonic() + poll_timeout
        started = time.monotonic()
        while True:
            if stop_check is not None and stop_check():
                raise OcrError("任务已取消")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OcrError(f"OCR 任务轮询超时（{poll_timeout:.0f} 秒），jobId={job_id}")

            try:
                resp = self._client.get(f"{self._jobs_url}/{job_id}")
            except httpx.HTTPError as exc:
                # 单次网络抖动不终止整个任务，继续重试到超时为止
                logger.warning("[OCR] 轮询请求失败，稍后重试: %s", exc)
                resp = None
            if resp is not None:
                if not (200 <= resp.status_code < 300):
                    raise OcrError(f"查询 OCR 任务失败 ({resp.status_code}): {resp.text}")
                data = self._unwrap(resp.json())
                state = data.get("state")
                ep = data.get("extractProgress") or {}
                extracted = ep.get("extractedPages")
                batch_total = ep.get("totalPages")

                # 无条件记录轮询状态：服务端排队阶段不返回 extractProgress，
                # 之前把日志挂在该字段下导致整个等待期完全静默，看起来像卡死。
                elapsed = time.monotonic() - started
                if batch_total:
                    logger.info(
                        "[OCR] job=%s state=%s 解析=%s/%s 已等待=%.0fs",
                        job_id, state, extracted, batch_total, elapsed,
                    )
                else:
                    logger.info(
                        "[OCR] job=%s state=%s 已等待=%.0fs", job_id, state, elapsed
                    )

                if progress_callback is not None and batch_total:
                    progress_callback(int(extracted or 0), int(batch_total))

                if state == "done":
                    result_url = data.get("resultUrl") or {}
                    json_url = result_url.get("jsonUrl")
                    if not isinstance(json_url, str) or not json_url:
                        raise OcrError(f"OCR 任务完成但缺少结果地址, jobId={job_id}")
                    logger.info("[OCR] job=%s 解析完成，耗时 %.0fs", job_id, elapsed)
                    return json_url
                if state == "failed":
                    raise OcrError(
                        f"OCR 任务失败: {data.get('errorMsg') or '未知错误'}, jobId={job_id}"
                    )
                if state not in ("pending", "running"):
                    raise OcrError(f"OCR 任务状态异常: {state}, jobId={job_id}")

            sleep_for = min(interval, remaining)
            if wait_for_stop(stop_check, sleep_for):
                raise OcrError("任务已取消")
            interval = min(interval * POLL_INTERVAL_MULTIPLIER, POLL_INTERVAL_MAX)

    def fetch_result_jsonl(self, json_url: str) -> List[dict]:
        """下载 JSONL 结果并解析为页面列表。

        结果 URL 是预签名对象存储链接，不能携带 API 认证头。
        """
        try:
            resp = httpx.get(json_url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise OcrError(f"下载 OCR 结果失败: {exc}") from exc
        if not (200 <= resp.status_code < 300):
            raise OcrError(f"下载 OCR 结果失败 ({resp.status_code})")
        pages = []
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pages.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise OcrError(f"OCR 结果 JSONL 格式错误: {exc}") from exc
        return pages

    @staticmethod
    def parse_pages(jsonl_lines: List[dict]) -> List[tuple]:
        """从 JSONL 行中提取 (markdown_text, markdown_images) 列表。"""
        extracted = []
        for line_obj in jsonl_lines:
            result = line_obj.get("result") or {}
            for item in result.get("layoutParsingResults") or []:
                markdown = item.get("markdown") or {}
                text = markdown.get("text", "")
                images = markdown.get("images") or {}
                if isinstance(text, str):
                    extracted.append((text, images if isinstance(images, dict) else {}))
        return extracted

    def download_image(self, url: str, dest: Path) -> None:
        """下载单个图片资源到本地（预签名链接，不带认证头）。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            resp = httpx.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise OcrError(f"下载图片失败 ({url}): {exc}") from exc
        if not (200 <= resp.status_code < 300):
            raise OcrError(f"下载图片失败 ({resp.status_code}): {url}")
        tmp_path = dest.with_suffix(dest.suffix + ".tmp")
        tmp_path.write_bytes(resp.content)
        tmp_path.replace(dest)


def iter_ocr_pdf_pages(
    file_path: Path,
    image_dir: Path,
    on_parse_progress: Optional[Callable[[int, int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Generator[OcrPageData, None, None]:
    """
    通过 OCR 服务逐批解析 PDF 页面。

    多批次时先在本地切出该批页面再上传，避免每批重传整份 PDF；
    并在消费当前批之前就提交下一批，让服务端解析与调用方的向量生成重叠。

    图片按 markdownImages 的相对路径（imgs/xxx.jpg）下载到 image_dir，
    文件名中的下划线替换为连字符（避免 Markdown 强调语法破坏渲染路径），
    页面文本中的引用重写为 "<image_dir 名>/xxx.jpg"，与图片落盘位置对齐。

    Args:
        on_parse_progress: (已解析页数, 文档总页数)。服务端解析进度，页码为全局值，
            调用方据此在等待期间推进任务进度（否则整批解析完之前进度条完全不动）。

    Yields:
        OcrPageData: 单页文本和图片映射
    """
    config = get_ocr_config()
    client = OcrClient(config.base_url, config.api_key, config.model)
    # 只在有显式配置时携带解析参数；全默认（payload 为空）时保持旧行为完全不传
    optional_payload = config.payload.to_payload()
    temp_dir = Path(tempfile.mkdtemp(prefix="piece-ocr-"))
    try:
        total_pages = _get_total_pages(file_path)
        batches = [
            (start, min(start + OCR_BATCH_PAGES - 1, total_pages))
            for start in range(1, total_pages + 1, OCR_BATCH_PAGES)
        ]
        # 单批时直接上传原文件（保持既有行为），多批时才做本地切页
        split_pages = len(batches) > 1

        def submit(index: int) -> Optional[Tuple[str, int, int, Optional[Path]]]:
            """提交第 index 批，返回 (jobId, 起始页, 结束页, 临时分片路径)。"""
            if stop_check is not None and stop_check():
                raise OcrError("任务已取消")
            if index >= len(batches):
                return None
            start, end = batches[index]
            if not split_pages:
                job_id = client.submit_job(
                    file_path,
                    page_ranges=_format_page_ranges(start, end),
                    optional_payload=optional_payload,
                )
                part_path = None
            else:
                part_path = temp_dir / f"part-{start}-{end}.pdf"
                _extract_page_range(file_path, start, end, part_path)
                job_id = client.submit_job(part_path, optional_payload=optional_payload)
            logger.info(
                "[OCR] 已提交批次 %s-%s/%s, jobId=%s",
                start,
                end,
                total_pages,
                job_id,
            )
            return job_id, start, end, part_path

        def prefetch(index: int) -> Optional[Tuple[str, int, int, Optional[Path]]]:
            """预取第 index 批。服务端不接受并发任务时返回 None，改为稍后串行提交。"""
            try:
                return submit(index)
            except OcrError as exc:
                logger.warning("[OCR] 预取下一批失败，回退为串行提交: %s", exc)
                return None

        page_number = 0
        batch_index = 0
        pending = submit(batch_index)
        while pending is not None:
            job_id, batch_start, batch_end, part_path = pending

            # 当前批在服务端排队/解析时先提交下一批，形成流水线
            batch_index += 1
            next_pending = prefetch(batch_index)

            try:
                # 服务端上报的是该批次内的页号，加上批次起点换算成全局页码
                def _on_batch_progress(extracted: int, _batch_total: int) -> None:
                    if on_parse_progress is not None:
                        on_parse_progress(
                            min(batch_start - 1 + extracted, total_pages), total_pages
                        )

                json_url = client.poll_until_done(
                    job_id,
                    stop_check=stop_check,
                    progress_callback=_on_batch_progress,
                )
                if stop_check is not None and stop_check():
                    raise OcrError("任务已取消")
                batch_texts = client.parse_pages(client.fetch_result_jsonl(json_url))
            finally:
                if part_path is not None:
                    part_path.unlink(missing_ok=True)

            expected_pages = batch_end - batch_start + 1
            if len(batch_texts) != expected_pages:
                raise OcrError(
                    f"OCR 批次 {batch_start}-{batch_end} 返回页数不符: "
                    f"期望 {expected_pages}, 实际 {len(batch_texts)}"
                )

            for page_text, page_images in batch_texts:
                if stop_check is not None and stop_check():
                    raise OcrError("任务已取消")
                page_number += 1
                downloaded: Dict[str, str] = {}
                for rel_path, url in page_images.items():
                    if stop_check is not None and stop_check():
                        raise OcrError("任务已取消")
                    # 文件名中的下划线会被 Markdown 渲染器解析为强调语法，
                    # 导致图片请求路径损坏，统一替换为连字符存储
                    stored_name = Path(rel_path).name.replace("_", "-")
                    dest = image_dir / stored_name
                    if not dest.exists():
                        client.download_image(url, dest)
                    downloaded[stored_name] = url
                    # 重写 Markdown 中的图片引用，指向 image_dir 相对路径，
                    # 与工作文件（working/<名>.md）和图片目录（working/<名>/）的布局对齐
                    rel_ref = f"{image_dir.name}/{stored_name}"
                    page_text = page_text.replace(rel_path, rel_ref)

                yield OcrPageData(page_number, total_pages, page_text, downloaded)

            # 预取失败时在这里补上串行提交，保证剩余批次不被丢掉
            if next_pending is None and batch_index < len(batches):
                next_pending = submit(batch_index)

            pending = next_pending
    finally:
        client.close()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_ocr_connection(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    """
    测试 OCR 服务连通性和认证。

    Returns:
        (是否成功, 描述信息)
    """
    client = OcrClient(base_url, api_key, model, timeout=15.0)
    try:
        headers = dict(client._client.headers)
        resp = client._client.post(
            client._jobs_url,
            data={"model": model},
            files={"file": ("probe.txt", b"", "text/plain")},
            headers=headers,
        )
        if resp.status_code == 200:
            return True, "连接成功"
        if resp.status_code in (401, 403):
            return False, f"认证失败 ({resp.status_code})，请检查 api_key"
        if resp.status_code == 400:
            # 无效文件返回 400 说明服务和认证均正常
            return True, "连接成功"
        return False, f"服务返回异常状态 ({resp.status_code})"
    except httpx.HTTPError as exc:
        return False, f"无法连接服务: {exc}"
    finally:
        client.close()
