"""
MinerU 精准解析客户端

通过 mineru.net 的 API v4 解析文档，把结果还原成与 OCR 路径同形的逐页文本。

接口流程（官方文档 https://mineru.net/apiManage/docs）：
- 申请上传链接: POST {base}/file-urls/batch，body {"files": [{"name": ...}], ...}，
  返回 batch_id 与 file_urls（与请求 files 顺序一一对应）
- 上传文件:     PUT  <file_urls[i]>，不带 Authorization、不带 Content-Type。
  预签名链接对请求头敏感，多带任何一个都会签名校验失败
- 上传完成即自动提交解析，没有单独的提交接口
- 轮询结果:     GET  {base}/extract-results/batch/{batch_id}
  状态: waiting-file | pending | running | converting | done | failed
  注意 batch 级 code 恒为 0，"某个文件失败"只体现在该项自己的 state/err_msg 上，
  不能拿整体 code 判断成败
- 下载结果:     full_zip_url 指向 zip，内含 full.md、{名}_content_list.json、
  layout.json、images/

本项目按页切片，故只用 content_list.json：它按阅读顺序平铺，每块带 page_idx
（0 基）与类型，按 page_idx 分组即可还原逐页 Markdown。
"""

from __future__ import annotations

import io
import json
import logging
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Callable, Dict, Generator, List, Optional, Tuple

import httpx

from ..settings import MINERU_MAX_FILE_SIZE, get_ocr_config
from ..utils import wait_for_stop
from .ocr_client import OcrPageData, _extract_page_range, _get_total_pages
from .parser_helper import run_parser

logger = logging.getLogger(__name__)

API_BASE_URL = "https://mineru.net/api/v4"
UPLOAD_PATH = "/file-urls/batch"
RESULT_PATH = "/extract-results/batch/{batch_id}"

# 单个解析任务的页数上限：超出即本地切页分批提交。
# 官方两处文档对上限的说法不一致（200 页 / 600 页），按保守的 200 设计。
MAX_PAGES_PER_BATCH = 200

# 轮询参数：查询接口限 1000 次/分钟，起步比 PaddleOCR 保守，让出额度给其他任务
POLL_INTERVAL_INITIAL = 5.0
POLL_INTERVAL_MAX = 20.0
POLL_INTERVAL_MULTIPLIER = 1.5

# 服务端偶发在最后几页翻成 failed（err_msg "parsing failed, please try again later"），
# 同一文件重新提交通常就能成功，且相同文件命中服务端缓存后只需几秒。
# 上传/下载的网络抖动同理。认证、额度、取消这类确定性错误不重试。
MAX_BATCH_ATTEMPTS = 3
BATCH_RETRY_DELAY = 15.0

# 宽或高低于此值的图块是刊头、logo、作者头像之类的装饰，不进正文；
# 阈值比检索侧（chunk_images.MIN_IMAGE_EDGE）宽松一档，避免误删小尺寸的示意图
MIN_IMAGE_EDGE = 100

# 页眉页脚类版面块不进正文，与 PaddleOCR 侧忽略页眉页脚的默认意图一致
_PAGE_FURNITURE_TYPES = {"header", "footer", "page_number", "aside_text", "page_footnote"}

# 按正文处理的块类型。mineru.net 在线服务把参考文献每条输出为一个顶层 ref_text 块
# （开源 main 分支则合并成 list + sub_type=ref_text），漏掉它整份文档的参考文献都会消失
_TEXT_TYPES = {"text", "ref_text"}

# 已告警过的未知块类型：只在首次出现时告警，避免一种新类型刷出上百条日志
_UNKNOWN_TYPES_SEEN: set = set()

# 图注与子图标签：图 1 / 表 2 / Fig. 3 / Figure 4 / Table 5 / (a) …
# vlm 后端不把图注塞进 image 块的 image_caption，而是紧跟其后另起一个文本块；
# 组合图更是每个子图一个 image 块，中间夹着 "(a) …" 这类标签
_CAPTION_RE = re.compile(
    r"^\s*(?:图|表|Fig(?:ure)?\.?|Tab(?:le)?\.?)\s*\d|^\s*\(?[a-zA-Z]\)\s*\S", re.I
)
_CAPTION_MAX_LINES = 3
_CAPTION_MAX_LINE_CHARS = 60

# 服务端错误码里的可自解释项，补一句用户能直接照做的提示
_ERROR_HINTS = {
    "A0202": "Token 无效，请检查是否与 mineru.net 上的访问令牌一致",
    "A0211": "Token 已过期，请重新申请",
    "-60005": "文件超过 200MB 上限",
    "-60006": "文件超过 200 页上限",
    "-60018": "今日解析额度已用尽，请明日再试",
}

# 正文里的插图引用（HTML 形态，路径允许空格与括号）
_IMG_REF = re.compile(r'src="images/([^"]+)"')


class MineruError(Exception):
    """MinerU 服务调用失败。"""


class MineruTransientError(MineruError):
    """服务端瞬时故障：解析失败、上传或下载的网络错误。重新提交同一批次通常能成功。"""


def _error_hint(code: object) -> str:
    return _ERROR_HINTS.get(str(code), "")


class MineruClient:
    """MinerU API v4 客户端（阻塞调用，由上层放到线程池执行）。"""

    def __init__(self, token: str, timeout: float = 60.0):
        self._batch_url = f"{API_BASE_URL}{UPLOAD_PATH}"
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {token.strip()}"},
        )

    def close(self) -> None:
        self._client.close()

    def _unwrap(self, payload: dict) -> dict:
        """解包 API 响应的 data 字段，非零 code 视为错误。"""
        code = payload.get("code", 0)
        if code not in (0, None):
            message = payload.get("msg") or ""
            hint = _error_hint(code)
            raise MineruError(
                f"MinerU 返回错误 ({code}): {message}" + (f"；{hint}" if hint else "")
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MineruError("MinerU 响应缺少 data 字段")
        return data

    def request_upload(
        self,
        name: str,
        *,
        model_version: str,
        is_ocr: bool = False,
        language: str = "ch",
    ) -> tuple[str, str]:
        """申请一个文件的上传链接，返回 (batchId, 预签名上传地址)。

        model_version / language 是整批一致的顶层参数，is_ocr 属于单个文件。
        未暴露服务端的其余开关（enable_table / enable_formula / extra_formats 等）：
        前两者关闭后的输出形态官方没有说明，本项目也用不到导出格式。
        """
        body = {
            "files": [{"name": name, "is_ocr": is_ocr}],
            "model_version": model_version,
            "language": language,
        }
        try:
            resp = self._client.post(self._batch_url, json=body)
        except httpx.HTTPError as exc:
            raise MineruTransientError(f"申请 MinerU 上传链接失败: {exc}") from exc
        if resp.status_code in (401, 403):
            raise MineruError(f"MinerU 认证失败 ({resp.status_code})，请检查 Token")
        if not (200 <= resp.status_code < 300):
            raise MineruError(f"申请 MinerU 上传链接失败 ({resp.status_code}): {resp.text}")
        data = self._unwrap(resp.json())
        batch_id = data.get("batch_id")
        urls = data.get("file_urls")
        if not isinstance(batch_id, str) or not batch_id:
            raise MineruError("MinerU 响应缺少 batch_id")
        if not isinstance(urls, list) or not urls or not isinstance(urls[0], str) or not urls[0]:
            raise MineruError("MinerU 响应缺少上传地址")
        return batch_id, urls[0]

    def upload(self, url: str, file_path: Path) -> None:
        """上传待解析文件。

        预签名链接不接受额外请求头，因此这里用独立请求而不是复用客户端（客户端
        上挂着 Authorization）。传文件对象让 httpx 自行填 Content-Length，
        避免把整份文件读进内存，也避免退化成 chunked。
        """
        try:
            with file_path.open("rb") as source:
                resp = httpx.put(url, content=source, timeout=600.0)
        except httpx.HTTPError as exc:
            raise MineruTransientError(f"上传文件到 MinerU 失败: {exc}") from exc
        if resp.status_code >= 500:
            raise MineruTransientError(f"上传文件到 MinerU 失败 ({resp.status_code}): {resp.text}")
        if not (200 <= resp.status_code < 300):
            raise MineruError(f"上传文件到 MinerU 失败 ({resp.status_code}): {resp.text}")

    def _find_result(self, batch_id: str, file_name: str) -> dict:
        try:
            resp = self._client.get(f"{API_BASE_URL}{RESULT_PATH.format(batch_id=batch_id)}")
        except httpx.HTTPError as exc:
            # 单次网络抖动不终止整个任务，交给上层继续轮询到超时
            logger.warning("[MinerU] 轮询请求失败，稍后重试: %s", exc)
            return {}
        if not (200 <= resp.status_code < 300):
            raise MineruError(f"查询 MinerU 任务失败 ({resp.status_code}): {resp.text}")
        results = self._unwrap(resp.json()).get("extract_result")
        if not isinstance(results, list):
            return {}
        for item in results:
            if isinstance(item, dict) and item.get("file_name") == file_name:
                return item
        return {}

    def wait_batch(
        self,
        batch_id: str,
        file_name: str,
        poll_timeout: float = 7200.0,
        stop_check: Optional[Callable[[], bool]] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> str:
        """
        轮询任务直到完成，返回结果 zip 的下载地址。

        Args:
            stop_check: 返回 True 时中止轮询（用于响应 Worker 停止）
            progress_callback: (已解析页数, 该批总页数)，服务端上报解析进度时调用
        """
        interval = POLL_INTERVAL_INITIAL
        deadline = time.monotonic() + poll_timeout
        started = time.monotonic()
        while True:
            if stop_check is not None and stop_check():
                raise MineruError("任务已取消")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MineruError(f"MinerU 任务轮询超时（{poll_timeout:.0f} 秒），batchId={batch_id}")

            result = self._find_result(batch_id, file_name)
            if result:
                state = result.get("state")
                progress = result.get("extract_progress") or {}
                extracted = progress.get("extracted_pages")
                batch_total = progress.get("total_pages")
                elapsed = time.monotonic() - started
                # 无条件记录轮询状态：排队阶段不返回 extract_progress，
                # 把日志挂在进度字段下会让整个等待期静默，看起来像卡死
                if batch_total:
                    logger.info(
                        "[MinerU] batch=%s state=%s 解析=%s/%s 已等待=%.0fs",
                        batch_id, state, extracted, batch_total, elapsed,
                    )
                else:
                    logger.info("[MinerU] batch=%s state=%s 已等待=%.0fs", batch_id, state, elapsed)

                if progress_callback is not None and batch_total:
                    progress_callback(int(extracted or 0), int(batch_total))

                if state == "done":
                    zip_url = result.get("full_zip_url")
                    if not isinstance(zip_url, str) or not zip_url:
                        raise MineruError(f"MinerU 任务完成但缺少结果地址, batchId={batch_id}")
                    logger.info("[MinerU] batch=%s 解析完成，耗时 %.0fs", batch_id, elapsed)
                    return zip_url
                if state == "failed":
                    raise MineruTransientError(
                        f"MinerU 任务失败: {result.get('err_msg') or '未知错误'}, batchId={batch_id}"
                    )
                if state not in ("waiting-file", "pending", "running", "converting"):
                    raise MineruError(f"MinerU 任务状态异常: {state}, batchId={batch_id}")

            sleep_for = min(interval, remaining)
            if wait_for_stop(stop_check, sleep_for):
                raise MineruError("任务已取消")
            interval = min(interval * POLL_INTERVAL_MULTIPLIER, POLL_INTERVAL_MAX)

    def download_zip(self, url: str, dest: Path) -> None:
        """下载结果 zip（预签名链接，不能携带 API 认证头）。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest.with_suffix(dest.suffix + ".tmp")
        try:
            with httpx.stream("GET", url, timeout=600.0, follow_redirects=True) as resp:
                if not (200 <= resp.status_code < 300):
                    raise MineruTransientError(f"下载 MinerU 结果失败 ({resp.status_code})")
                with tmp_path.open("wb") as output:
                    for chunk in resp.iter_bytes():
                        output.write(chunk)
        except httpx.HTTPError as exc:
            raise MineruTransientError(f"下载 MinerU 结果失败: {exc}") from exc
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        tmp_path.replace(dest)


def _read_content_list(archive: zipfile.ZipFile) -> List[dict]:
    """从结果 zip 中读出 content_list.json。

    线上两种命名都出现过：{原文件名}_content_list.json 与 content_list.json。

    vlm 后端另有 *_content_list_v2.json（字段结构官方标注"开发中，可能调整"），
    本项目按稳定的 v1 还原页面；若某个模型版本只给了 v2，直接说清楚而不是
    抛一句"缺少 content_list"，否则用户只会看到一份解析失败的文档。
    """
    names = [name for name in archive.namelist() if name.endswith("content_list.json")]
    if not names:
        experimental = [name for name in archive.namelist() if name.endswith("content_list_v2.json")]
        if experimental:
            raise MineruError(
                "MinerU 只返回了实验性的 content_list_v2，本项目按 content_list v1 还原页面，"
                "请在设置中把解析模型改为 pipeline 后重试"
            )
        raise MineruError("MinerU 结果缺少 content_list.json")
    # 多个候选时优先取带文件名前缀的那个，否则取最浅的一层
    names.sort(key=lambda name: (name.count("/"), -len(name)))
    try:
        with archive.open(names[0]) as source:
            blocks = json.loads(source.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise MineruError(f"MinerU 结果 content_list.json 无法解析: {exc}") from exc
    if not isinstance(blocks, list):
        raise MineruError("MinerU 结果 content_list.json 结构异常")
    return [block for block in blocks if isinstance(block, dict)]


def _strip_html_wrapper(html: str) -> str:
    """去掉 table_body 外层的 <html><body>。

    切分侧的表格保护/归一化正则（chunking/utils.py）只认 <table>...</table>，
    留着包装会让首尾多出两个孤立标签，既占切片容量又干扰检索。
    """
    match = re.search(r"<table\b.*?</table>", html, re.IGNORECASE | re.DOTALL)
    return match.group(0) if match else html.strip()


def _caption(block: dict, key: str) -> str:
    items = block.get(key)
    if not isinstance(items, list):
        return ""
    return "\n".join(str(item).strip() for item in items if str(item).strip())


def _render_block(block: Dict[str, object]) -> str:
    """把一个 content_list 块还原成 Markdown 片段。"""
    block_type = block.get("type")
    if block_type in _PAGE_FURNITURE_TYPES:
        return ""

    if block_type in _TEXT_TYPES:
        text = str(block.get("text") or "").strip()
        level = block.get("text_level")
        if text and isinstance(level, int) and level > 0:
            return f"{'#' * min(level, 6)} {text}"
        return text

    if block_type == "equation":
        # text_format 为 latex，且已自带 $$ 定界符
        return str(block.get("text") or "").strip()

    if block_type == "code":
        body = str(block.get("code_body") or "").strip()
        return f"```\n{body}\n```" if body else ""

    if block_type in ("list", "index"):
        items = block.get("list_items")
        lines = []
        for item in items if isinstance(items, list) else []:
            text = str(item).strip()
            if not text:
                continue
            lines.append(text if text[:1] in "-*•" else f"- {text}")
        return "\n".join(lines)

    if block_type == "table":
        parts = [
            _caption(block, "table_caption"),
            _strip_html_wrapper(str(block.get("table_body") or "")),
            _caption(block, "table_footnote"),
        ]
        return "\n\n".join(part for part in parts if part)

    if block_type in ("image", "chart"):
        parts = []
        image_path = str(block.get("img_path") or "").strip()
        if image_path:
            # 用 HTML 形态而不是 ![](...)：引用会被改写成 "<文档名>/xxx.jpg"，
            # 而文档名可能带空格和括号（如 "报告 (1)"）。Markdown 的链接目标
            # 遇到空格即截断，界面侧的重写正则（components._RELATIVE_IMG_MD）
            # 也只接受不含空白的路径，结果是图片请求打到站点根目录而 404。
            # PaddleOCR 路径产出的同样是 <img src="...">，形态保持一致。
            parts.append(f'<img src="{image_path}">')
        parts.append(_caption(block, f"{block_type}_caption"))
        # 图注之外的脚注同样是正文：期刊末页的作者简介就挂在头像的 image_footnote 里
        parts.append(_caption(block, f"{block_type}_footnote"))
        if block_type == "chart":
            # 图表内容是模型对图表的文字描述，对检索有价值
            parts.append(str(block.get("content") or "").strip())
        # 图与图注之间只用单换行：空行是切分器的最高优先级切点，
        # 会把图和说明拆到两张卡片上
        return "\n".join(part for part in parts if part)

    # 未知类型带 text 就按正文保留：服务端新增块类型时宁可多留一段文字，
    # 也不能像 ref_text 那样静默丢掉整节内容
    text = str(block.get("text") or "").strip()
    if block_type not in _UNKNOWN_TYPES_SEEN:
        _UNKNOWN_TYPES_SEEN.add(block_type)
        logger.warning(
            "[MinerU] 未知版面块类型 %r，%s", block_type, "已按正文保留" if text else "无文本已跳过"
        )
    return text


def _is_image_caption(block: dict, rendered: str) -> bool:
    """紧跟插图的短文本块是否是图注或子图标签（图 1 / Fig. 2 / (a) …）。"""
    if block.get("type") != "text" or block.get("text_level"):
        return False
    lines = rendered.splitlines()
    if not 0 < len(lines) <= _CAPTION_MAX_LINES:
        return False
    if any(len(line) > _CAPTION_MAX_LINE_CHARS for line in lines):
        return False
    return _CAPTION_RE.match(rendered) is not None


def _join_page_blocks(blocks: List[dict]) -> str:
    """把一页的版面块拼成正文，插图和它的图注、子图标签合成一个段落。

    vlm 后端不把图注塞进 image 块，而是紧跟其后另起一个 text 块；组合图更是
    每个子图一个 image 块，中间夹着 "(a)" 之类的标签。段落之间用空行分隔，
    而空行正是切分器的首选切点，图和图注隔着空行就会被拆到不同切片，
    留下一张只有若干 <img> 的卡片。因此相邻的插图与图注只用单换行相连。
    """
    paragraphs: List[str] = []
    run: List[Tuple[bool, str]] = []  # (是否插图, 文本)

    def flush() -> None:
        if any(is_image for is_image, _ in run):
            paragraphs.append("\n".join(text for _, text in run))
        else:
            # 没挨着插图的"图 N"文字（例如指向表格的说明）保持独立段落
            paragraphs.extend(text for _, text in run)
        run.clear()

    for block in blocks:
        rendered = _render_block(block)
        if not rendered:
            continue
        if block.get("type") in ("image", "chart"):
            run.append((True, rendered))
        elif _is_image_caption(block, rendered):
            run.append((False, rendered))
        else:
            flush()
            paragraphs.append(rendered)
    flush()
    return "\n\n".join(paragraphs)


def _pages_from_blocks(blocks: List[dict], page_count: int) -> List[str]:
    """按 page_idx 把块分组还原成逐页文本。

    空白页在 content_list 里一块都没有，因此必须先铺满空页再填充：
    漏一页就会让后续所有页面整体前移，页码对不上，点卡片显示错误原页。
    """
    pages: List[List[dict]] = [[] for _ in range(page_count)]
    for block in blocks:
        page_idx = block.get("page_idx")
        if not isinstance(page_idx, int) or not 0 <= page_idx < page_count:
            logger.warning("[MinerU] 版面块页码越界，已跳过: page_idx=%s", page_idx)
            continue
        pages[page_idx].append(block)
    return [_join_page_blocks(page_blocks) for page_blocks in pages]


def _image_too_small(archive: zipfile.ZipFile, member: str) -> bool:
    """刊头、logo、作者头像之类的装饰图按尺寸滤掉；读不出尺寸时保守保留。"""
    try:
        from PIL import Image as PILImage

        with archive.open(member) as source:
            with PILImage.open(io.BytesIO(source.read())) as image:
                width, height = image.size
    except Exception:
        return False
    return width < MIN_IMAGE_EDGE or height < MIN_IMAGE_EDGE


def _store_images(page_text: str, archive: zipfile.ZipFile, image_dir: Path) -> str:
    """把正文引用到的插图从结果 zip 落盘，并把引用改写成工作目录相对路径。

    文件名里的下划线会被 Markdown 渲染器当作强调语法，统一换成连字符
    （与 PaddleOCR 路径的落盘规则一致，避免同一份文档两种写法）。
    装饰性小图不落盘，正文里的引用一并去掉。
    """
    members = set(archive.namelist())
    for name in set(_IMG_REF.findall(page_text)):
        member = f"images/{name}"
        if member not in members:
            logger.warning("[MinerU] 正文引用的插图不在结果包里: %s", member)
            continue
        if _image_too_small(archive, member):
            logger.debug("[MinerU] 跳过装饰性小图: %s", member)
            page_text = page_text.replace(f'<img src="{member}">', "")
            continue
        stored_name = Path(name).name.replace("_", "-")
        dest = image_dir / stored_name
        if not dest.exists():
            image_dir.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, dest.open("wb") as output:
                shutil.copyfileobj(source, output)
        page_text = page_text.replace(f"images/{name}", f"{image_dir.name}/{stored_name}")
    # 去掉小图后留下的多余空行
    return re.sub(r"\n{3,}", "\n\n", page_text).strip()


def iter_mineru_pdf_pages(
    file_path: Path,
    image_dir: Path,
    on_parse_progress: Optional[Callable[[int, int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Generator[OcrPageData, None, None]:
    """
    通过 MinerU 逐批解析 PDF 页面。

    超过 MAX_PAGES_PER_BATCH 页时先在本地切页再分批上传：服务端按 200 页封顶，
    而本地切页不依赖 page_ranges 的语义（vlm 模型版本是否支持未确认），
    也不会为了分批重复上传整份文件。

    图片按 content_list 的 img_path（images/xxx.jpg）从结果包中取出落到 image_dir，
    正文引用重写为 "<image_dir 名>/xxx.jpg"，与工作文件（working/<名>.md）和
    图片目录（working/<名>/）的布局对齐。

    Args:
        on_parse_progress: (已解析页数, 文档总页数)，页码为全局值

    Yields:
        OcrPageData: 单页文本
    """
    config = get_ocr_config()
    # 配置被 CLI 改成空值时回落到文档推荐的 vlm，避免发一个空模型版本过去
    model_version = config.mineru_model_version.strip() or "vlm"
    language = config.mineru_language.strip() or "ch"
    client = MineruClient(config.mineru_token)
    temp_dir = Path(tempfile.mkdtemp(prefix="piece-mineru-"))
    try:
        total_pages = _get_total_pages(file_path)
        if total_pages <= 0:
            raise MineruError("PDF 没有可解析的页面")
        # 导入侧已按当前后端拦截超限文件，这里兜住"换后端后重新索引"的情况，
        # 让用户看到可读原因而不是服务端的 -60005 / -60006
        size = file_path.stat().st_size
        if size > MINERU_MAX_FILE_SIZE:
            raise MineruError(
                f"文件 {size / 1024 / 1024:.0f}MB 超过 MinerU 的单文件上限 "
                f"{MINERU_MAX_FILE_SIZE // 1024 // 1024}MB"
            )

        batches = [
            (start, min(start + MAX_PAGES_PER_BATCH - 1, total_pages))
            for start in range(1, total_pages + 1, MAX_PAGES_PER_BATCH)
        ]
        # 单批时直接上传原文件，多批时才做本地切页
        split_pages = len(batches) > 1

        for batch_start, batch_end in batches:
            if stop_check is not None and stop_check():
                raise MineruError("任务已取消")
            part_path = None
            if split_pages:
                part_path = temp_dir / f"part-{batch_start}-{batch_end}.pdf"
                _extract_page_range(file_path, batch_start, batch_end, part_path)
            upload_path = part_path or file_path
            expected_pages = batch_end - batch_start + 1
            zip_path = temp_dir / f"result-{batch_start}-{batch_end}.zip"
            try:
                # 服务端上报的是该批次内的页号，加上批次起点换算成全局页码
                def _on_batch_progress(extracted: int, _batch_total: int) -> None:
                    if on_parse_progress is not None:
                        on_parse_progress(
                            min(batch_start - 1 + extracted, total_pages), total_pages
                        )

                for attempt in range(1, MAX_BATCH_ATTEMPTS + 1):
                    try:
                        batch_id, upload_url = client.request_upload(
                            upload_path.name,
                            model_version=model_version,
                            is_ocr=config.mineru_is_ocr,
                            language=language,
                        )
                        logger.info(
                            "[MinerU] 已申请上传链接 %s-%s/%s, batchId=%s",
                            batch_start, batch_end, total_pages, batch_id,
                        )
                        client.upload(upload_url, upload_path)
                        zip_url = client.wait_batch(
                            batch_id,
                            upload_path.name,
                            stop_check=stop_check,
                            progress_callback=_on_batch_progress,
                        )
                        if stop_check is not None and stop_check():
                            raise MineruError("任务已取消")
                        client.download_zip(zip_url, zip_path)
                        break
                    except MineruTransientError as exc:
                        # 服务端常在最后几页翻成 failed，重新提交同一文件通常就能过；
                        # 相同文件命中服务端缓存后几秒即完成，重试代价很低
                        if attempt >= MAX_BATCH_ATTEMPTS:
                            raise
                        logger.warning(
                            "[MinerU] 批次 %s-%s 第 %s 次尝试失败，%.0fs 后重试: %s",
                            batch_start, batch_end, attempt, BATCH_RETRY_DELAY, exc,
                        )
                        if wait_for_stop(stop_check, BATCH_RETRY_DELAY):
                            raise MineruError("任务已取消") from exc

                with zipfile.ZipFile(zip_path) as archive:
                    pages = _pages_from_blocks(_read_content_list(archive), expected_pages)
                    for offset, page_text in enumerate(pages):
                        if stop_check is not None and stop_check():
                            raise MineruError("任务已取消")
                        page_number = batch_start + offset
                        page_text = _store_images(page_text, archive, image_dir)
                        if on_parse_progress is not None:
                            on_parse_progress(page_number, total_pages)
                        yield OcrPageData(page_number, total_pages, page_text, {})
            finally:
                zip_path.unlink(missing_ok=True)
                if part_path is not None:
                    part_path.unlink(missing_ok=True)
    finally:
        client.close()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_mineru_connection(token: str, model_version: str = "vlm") -> tuple[bool, str]:
    """用一张带文字的小图走完整链路（申请链接 → 上传 → 解析），验证 Token 与网络。

    只发一个请求不足以证明 Token 有效：解析真的跑通才算可用。图片走同一套接口，
    代价是 1 页解析额度，比让用户等一份真实文档失败划算。
    """
    if not token.strip():
        return False, "请填写 Token"
    client = MineruClient(token, timeout=30.0)
    try:
        image = run_parser("probe_image")
        with tempfile.TemporaryDirectory(prefix="piece-mineru-probe-") as directory:
            probe = Path(directory) / "piece-probe.jpg"
            probe.write_bytes(image)
            batch_id, upload_url = client.request_upload(
                probe.name, model_version=model_version or "vlm"
            )
            client.upload(upload_url, probe)
            client.wait_batch(batch_id, probe.name, poll_timeout=180.0)
        return True, "连接成功"
    except MineruError as exc:
        return False, str(exc)
    except httpx.HTTPError as exc:
        return False, f"无法连接 MinerU: {exc}"
    finally:
        client.close()
