"""
自定义多模态模型（VLM）PDF 解析客户端

把 PDF 逐页渲染成图片，通过 OpenAI 兼容的 chat/completions 接口请求多模态模型
输出 Markdown。与 PaddleOCR 路（ocr_client.py）并列，由 ocr.provider 选择。

与 PaddleOCR 路的差异：
- 1 次请求 = 1 页，页数天然对齐，无需批次页数校验
- 不产出插图：图表由模型以一句话文字描述代替，不写图片文件

并发模型：HTTP 请求在线程池中重叠，同一文档的渲染复用独立 helper，服务内不持有
MuPDF Document。滑动窗口按页序返回，在途图片与 HTTP 请求数都有界。
停止时先取消未开始的页，再等待在途请求结束，最后才关闭 HTTP 客户端。
"""

import base64
import logging
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Deque, Generator, Optional

import httpx

from ..settings import get_ocr_config
from ..utils import wait_for_stop
from .converter import MAX_PDF_PAGES
from .parser_helper import ParserSession, run_parser

logger = logging.getLogger(__name__)

API_PATH = "/chat/completions"

# 部分服务商（如 GMI）会按 User-Agent 拦截爬虫，httpx 的默认 UA
# (python-httpx/x.y) 会被直接限流或在 TLS 层断连，这里声明自己的应用标识。
USER_AGENT = "Piece/0.1"

# 单页请求参数：一页 A4 的 Markdown 通常远小于 8192 token，留足余量避免截断
MAX_OUTPUT_TOKENS = 8192
REQUEST_TIMEOUT = 300.0
# 重试次数按"长文档必须整体成功"来定：聚合网关的单次失败率实测可达 20-30%
# （随机路由到过载上游），3 次重试下百页文档几乎必然有一页彻底失败并让任务失败。
MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 5.0

PAGE_PROMPT = (
    "将图片中的这一页文档完整转录为 Markdown。要求：\n"
    "1. 保留标题层级、段落、列表；表格用 Markdown 表格语法还原；\n"
    "2. 公式用 LaTeX 表示（行内 $...$，独立成行 $$...$$）；\n"
    "3. 插图、照片、图表用一句话概括内容，写成 `[图：概括]`；\n"
    "4. 忽略页眉、页脚、页码、水印；\n"
    "5. 只输出该页的 Markdown 正文，不要任何解释说明，不要用代码块包裹整页。\n"
    "6. 页面没有任何可识别内容时，只输出空字符串。"
)


class VlmError(Exception):
    """VLM 服务调用失败。

    retriable=False 用于凭据、模型名等重试也不会好转的错误，调用方据此放弃重试。
    """

    def __init__(self, message: str, retriable: bool = True):
        super().__init__(message)
        self.retriable = retriable


def _parts_text(parts: list) -> str:
    """拼接分段内容数组里的文本。"""
    return "".join(part.get("text", "") for part in parts if isinstance(part, dict))


def _extract_content(data: dict) -> tuple[Optional[str], Optional[str]]:
    """从响应中取出 (文本, 结束原因)，无法识别响应结构时文本为 None。

    兼容两种形态：标准 OpenAI chat/completions 的 choices[0].message.content，
    以及聚合网关把请求路由到 Anthropic 风格上游时直接透传的顶层 content 数组。
    上游确实没生成内容（content 为 null）时返回空串，交由重试逻辑处理。
    """
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message")
        finish = choice.get("finish_reason")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content, finish
            if isinstance(content, list):
                return _parts_text(content), finish
            if content is None:
                return "", finish
        return None, finish

    if data.get("type") == "message":
        stop_reason = data.get("stop_reason")
        finish = "length" if stop_reason == "max_tokens" else stop_reason
        content = data.get("content")
        if isinstance(content, str):
            return content, finish
        if isinstance(content, list):
            return _parts_text(content), finish
        if content is None:
            return "", finish

    return None, None


def _strip_code_fence(text: str) -> str:
    """剥掉模型习惯性给整页套的 ```markdown 围栏。

    只在首尾成对出现时剥离，避免破坏页面内真实的代码块。
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 2 or not lines[-1].strip().startswith("```"):
        return stripped
    # 首行是 ``` 或 ```markdown 之类的语言标记
    if lines[0].strip("` ").lower() not in ("", "markdown", "md"):
        return stripped
    return "\n".join(lines[1:-1]).strip()


def _render_page_jpeg(parser: ParserSession, file_path: Path, page_index: int, dpi: int) -> bytes:
    """子进程只回传压缩图片；HTTP 请求不跨进程。"""
    image = parser.run("render_page", file_path, page_index + 1, dpi, "jpeg")
    if image is None:
        raise VlmError(f"PDF 页码越界: {page_index + 1}", retriable=False)
    return image


class VlmClient:
    """OpenAI 兼容多模态模型的阻塞客户端（httpx.Client 可跨线程共享）。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = REQUEST_TIMEOUT):
        base_url = base_url.strip().rstrip("/")
        self._url = f"{base_url}{API_PATH}"
        self._model = model
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": USER_AGENT,
            },
        )

    def close(self) -> None:
        self._client.close()

    def _build_payload(self, image: bytes) -> dict:
        image_b64 = base64.b64encode(image).decode("ascii")
        return {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                        },
                        {"type": "text", "text": PAGE_PROMPT},
                    ],
                }
            ],
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 0.0,
        }

    def _request_once(self, image: bytes, page_number: int) -> str:
        """发一次请求并取出 Markdown 文本。

        Raises:
            VlmError: 调用失败，retriable 标明是否值得重试
        """
        try:
            resp = self._client.post(self._url, json=self._build_payload(image))
        except httpx.HTTPError as exc:
            raise VlmError(f"VLM 请求失败（第 {page_number} 页）: {exc}") from exc

        if resp.status_code in (401, 403):
            raise VlmError(
                f"VLM 认证失败 ({resp.status_code})，请检查 api_key: {resp.text[:200]}",
                retriable=False,
            )
        if resp.status_code == 429:
            # 聚合网关的 429 多为上游池过载而非本方超配额（实测串行时失败率
            # 反而更高），所以按普通可重试错误处理，不做额外的加倍退避
            raise VlmError(f"VLM 服务限流 (429)（第 {page_number} 页）: {resp.text[:200]}")
        if resp.status_code == 400:
            # 多数是模型不支持图片输入或请求体被上游拒绝，重试无意义
            raise VlmError(
                f"VLM 拒绝请求 (400)，请确认模型支持图片输入: {resp.text[:200]}",
                retriable=False,
            )
        if not (200 <= resp.status_code < 300):
            raise VlmError(
                f"VLM 请求失败 ({resp.status_code})（第 {page_number} 页）: {resp.text[:200]}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise VlmError(f"VLM 响应不是合法 JSON（第 {page_number} 页）: {exc}") from exc

        content, finish_reason = _extract_content(data)
        if content is None:
            error = data.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else error
            raise VlmError(
                f"VLM 响应无法解析（第 {page_number} 页）: {message or str(data)[:200]}"
            )

        if finish_reason == "length":
            # 截断的页面保留已有内容：丢半页仍好过整页缺失，同时留日志便于调高精度或换模型
            logger.warning(
                "[VLM] 第 %s 页输出被 max_tokens 截断，已保留前 %s 字",
                page_number,
                len(content),
            )
        return _strip_code_fence(content)

    def parse_page(self, image: bytes, page_number: int, stop_check=None) -> str:
        """解析单页，可恢复错误按退避重试；停止后不再发起重试。"""
        for attempt in range(MAX_RETRIES):
            if stop_check is not None and stop_check():
                raise VlmError("任务已取消", retriable=False)
            last_attempt = attempt == MAX_RETRIES - 1
            try:
                text = self._request_once(image, page_number)
            except VlmError as exc:
                if not exc.retriable or last_attempt:
                    raise
                backoff = RETRY_BACKOFF_SECONDS * (attempt + 1)
                logger.warning(
                    "[VLM] 第 %s 页解析失败（第 %s/%s 次尝试），%.0fs 后重试: %s",
                    page_number, attempt + 1, MAX_RETRIES, backoff, exc,
                )
                if wait_for_stop(stop_check, backoff):
                    raise VlmError("任务已取消", retriable=False)
                continue

            if text or last_attempt:
                if not text:
                    logger.warning("[VLM] 第 %s 页多次返回空内容，按空白页处理", page_number)
                return text

            logger.warning(
                "[VLM] 第 %s 页返回空内容（第 %s/%s 次尝试），重试",
                page_number, attempt + 1, MAX_RETRIES,
            )
            if wait_for_stop(stop_check, RETRY_BACKOFF_SECONDS * (attempt + 1)):
                raise VlmError("任务已取消", retriable=False)

        return ""


def iter_vlm_pdf_pages(
    file_path: Path,
    on_parse_progress: Optional[Callable[[int, int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Generator[SimpleNamespace, None, None]:
    """
    通过多模态模型逐页解析 PDF，按页码顺序产出。

    Args:
        file_path: PDF 路径
        on_parse_progress: (已解析页数, 总页数)，供上层推进进度条
        stop_check: 返回 True 时中止解析（响应 Worker 停止）

    Yields:
        带 page_number / total_pages / page_text 属性的对象，与其他 PDF 解析路一致
    """
    config = get_ocr_config()
    concurrency = max(1, config.vlm_concurrency)
    dpi = max(72, config.vlm_dpi)

    cancelled = threading.Event()

    def stopping() -> bool:
        return cancelled.is_set() or (stop_check is not None and stop_check())

    with ParserSession(stop_check=stopping) as parser:
        total_pages = parser.run("page_count", file_path)
        if total_pages > MAX_PDF_PAGES:
            raise ValueError(f"PDF 页数过多（{total_pages} 页），最大支持 {MAX_PDF_PAGES} 页")
        logger.info(
            "[VLM] 开始解析: %s, 总页数=%s, model=%s, dpi=%s, 并发=%s",
            file_path.name, total_pages, config.vlm_model, dpi, concurrency,
        )

        with closing(VlmClient(config.vlm_base_url, config.vlm_api_key, config.vlm_model)) as client:
            pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="vlm-page")
            inflight: Deque[Future] = deque()
            started = time.monotonic()
            try:
                def submit(page_index: int) -> Future:
                    if stopping():
                        raise VlmError("任务已取消", retriable=False)
                    image = _render_page_jpeg(parser, file_path, page_index, dpi)
                    return pool.submit(client.parse_page, image, page_index + 1, stopping)

                next_index = 0
                while next_index < min(concurrency, total_pages):
                    inflight.append(submit(next_index))
                    next_index += 1

                page_number = 0
                while inflight:
                    future = inflight.popleft()
                    while True:
                        if stopping():
                            raise VlmError("任务已取消", retriable=False)
                        try:
                            page_text = future.result(timeout=0.1)
                            break
                        except TimeoutError:
                            if future.done():
                                # 区分等待超时和任务本身抛出的 TimeoutError，后者不能无限重试。
                                page_text = future.result()
                                break
                    page_number += 1

                    if next_index < total_pages:
                        inflight.append(submit(next_index))
                        next_index += 1

                    if on_parse_progress is not None:
                        on_parse_progress(page_number, total_pages)

                    if page_number % 10 == 0 or page_number == total_pages:
                        logger.info(
                            "[VLM] 已解析 %s/%s 页, 耗时=%.0fs",
                            page_number, total_pages, time.monotonic() - started,
                        )

                    yield SimpleNamespace(
                        page_number=page_number,
                        total_pages=total_pages,
                        page_text=page_text,
                    )
                    del page_text
            finally:
                cancelled.set()
                # 先释放文档 helper，再等在途 HTTP 退出，最后由 closing 关闭客户端。
                try:
                    parser.close()
                finally:
                    pool.shutdown(wait=True, cancel_futures=True)


def _make_probe_image() -> bytes:
    """生成一张带文字的小图作为探针。

    带文字（而非纯白）才能区分"链路通但模型读不到图"和"模型正常"：
    读得到图的模型会把文字转录出来，纯文本模型则通常报错或返回空。
    """
    return run_parser("probe_image")


def test_vlm_connection(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    """
    测试 VLM 服务连通性、认证和图片输入支持。

    Returns:
        (是否成功, 描述信息)
    """
    client = VlmClient(base_url, api_key, model, timeout=60.0)
    try:
        text = client.parse_page(_make_probe_image(), page_number=0)
        if not text.strip():
            return False, "服务可达但模型未返回内容，请确认该模型支持图片输入"
        return True, "连接成功"
    except VlmError as exc:
        return False, str(exc)
    except Exception as exc:  # 渲染探针或响应解析的意外错误
        return False, f"测试失败: {exc}"
    finally:
        client.close()
