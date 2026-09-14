"""
Office 文档转 PDF 模块

职责:
- 探测可用的转换后端（Windows COM / LibreOffice）
- 把 docx/pptx 转成 PDF 并缓存，供 PDF 管线复用

背景:
markitdown 依赖的 python-pptx 和 mammoth 都会跳过 OOXML 的 mc:AlternateContent，
而 Office 正是把公式连同周围正文一起装在这个元素里。实测一份 16 页的数学 PPT，
markitdown 只提取到 681 字符，其中第 7 页仅剩「法 1」3 个字。转成 PDF 后由 PDF
管线接手，同一份文件能拿到 1832 字符。

两种后端的产出差别很大，调用方需要按文本层质量决定后续怎么解析:
- COM（需安装 Microsoft Office）: 保留完整文本层，公式是 Unicode 数学符号，
  没有 OCR 时本地 PyMuPDF 也能兜底解析出正文
- LibreOffice: 渲染保真度同样很好，但会把公式区域整块画成图片，文本层几乎为空
  （实测同一份文件只剩 125 字符），内容提取必须走 OCR/VLM

即便是 COM 的产出，配了 OCR 也应优先走 OCR: 本地文本层按行读取，矩阵、表格
这类二维版面会被拆成一行一个元素，而 OCR 能还原成 LaTeX。

转换产物放在 data/cache/ 下而非 data/files/ 下: 它由原件随时可以重新生成，
不需要占用云同步的带宽。
"""

import hashlib
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional, Tuple

from app.platform import com_available as _com_available
from app.platform import find_libreoffice as _find_libreoffice
from ..settings import OfficeSettings, get_settings
from .parser_helper import ParserStopped, run_parser

logger = logging.getLogger(__name__)

# 走 PDF 管线的格式。xlsx 不在其中: markitdown 对表格的提取是结构化的，
# 转成 PDF 反而会把表格压平成版面文字，是明显的倒退
#
# 传统二进制格式（.doc/.ppt）和 ODF 格式（.odt/.odp）markitdown 根本读不了，
# 只能靠这条管线；能否受理取决于运行环境有没有 COM 或 LibreOffice。
_DOCUMENT_FORMATS = {".docx", ".doc", ".rtf", ".odt"}
_PRESENTATION_FORMATS = {".pptx", ".ppt", ".odp"}
PDF_ROUTED_FORMATS = _DOCUMENT_FORMATS | _PRESENTATION_FORMATS

# 只有转换器能处理的格式：没有 COM / LibreOffice 时直接判定为不受支持，
# 而不是交给 markitdown 再失败得莫名其妙
CONVERTER_ONLY_FORMATS = {".doc", ".rtf", ".odt", ".ppt", ".odp"}

# COM 的导出格式常量
_WD_FORMAT_PDF = 17  # Word: wdFormatPDF
_PP_SAVE_AS_PDF = 32  # PowerPoint: ppSaveAsPDF

# 首次运行要初始化用户配置（实测 18.5 秒），大文档还会更久，超时给宽一些
CONVERT_TIMEOUT = 300

# COM 串行闸门。Office 的自动化服务是进程级单例，两个任务同时转换时，先结束的
# 那个在 finally 里 Quit 会把另一个正在用的实例掐掉，报 Word.Application.Documents
# 或 <unknown>.Open。索引 worker 默认 2 个并发任务，实测能稳定复现。
_com_lock = threading.Lock()

# 传给 LibreOffice 的环境里要摘掉的变量：soffice 自带内嵌 Python，继承了宿主的
# PYTHONHOME/PYTHONPATH 会让它找不到自己的标准库，报
# "Could not find platform independent libraries <prefix>" 后转换静默失败
_PYTHON_ENV_VARS = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONEXECUTABLE",
    "PYTHONUSERBASE",
)


def get_office_pdf_cache_dir() -> Path:
    """获取 Office 转 PDF 的缓存目录"""
    cache_dir = get_settings().get_data_path() / "cache" / "office_pdf"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _libreoffice_profile_dir() -> Path:
    """获取本线程专用的 LibreOffice 配置目录。

    LibreOffice 对用户配置目录加锁，多个索引任务共用会互相卡死；而每次都换新
    目录又要重新初始化配置（实测 18.5 秒 vs 复用后的 6.3 秒）。按线程划分既能
    让同一个 worker 线程复用配置，又能让并发任务互不干扰。
    """
    profile = (
        get_settings().get_data_path()
        / "cache"
        / "lo_profile"
        / f"{os.getpid()}_{threading.get_ident()}"
    )
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def find_libreoffice(office: Optional[OfficeSettings] = None) -> Optional[str]:
    """返回 soffice 可执行文件路径，找不到时返回 None。"""
    return _find_libreoffice((office or get_settings().office).libreoffice_path)


def list_converters(office: Optional[OfficeSettings] = None) -> list[Tuple[str, str]]:
    """按优先级列出可用的转换后端。

    Args:
        office: 指定配置，留空则读全局设置。设置界面用它探测尚未保存的表单值

    Returns:
        [("com", ""), ("libreoffice", soffice 路径)] 形式的列表，越靠前越优先
    """
    office = office or get_settings().office
    mode = office.converter
    if mode == "off":
        return []

    candidates: list[Tuple[str, str]] = []
    if mode in ("auto", "com") and _com_available():
        candidates.append(("com", ""))

    if mode in ("auto", "libreoffice"):
        soffice = find_libreoffice(office)
        if soffice:
            candidates.append(("libreoffice", soffice))

    return candidates


def detect_converter() -> Optional[Tuple[str, str]]:
    """返回优先级最高的可用后端，供设置界面显示；都不可用时为 None。"""
    candidates = list_converters()
    return candidates[0] if candidates else None


def _cache_path(source: Path, converter_kind: str) -> Optional[Path]:
    """按原件的路径、修改时间和转换后端生成缓存路径。

    后端也纳入键：换了转换后端（如 COM 改成 LibreOffice）就该重新转，
    否则命中旧缓存会让新后端完全不生效。
    """
    try:
        stat = source.stat()
    except OSError:
        return None

    key = f"{source}|{stat.st_mtime_ns}|{stat.st_size}|{converter_kind}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return get_office_pdf_cache_dir() / f"office-{digest}.pdf"


def _temp_target(target: Path) -> Path:
    """同目录下的中间产物路径，转完再原子改名到 target。

    多个进程可能同时转同一份文件（索引 worker 和界面的原页对比各调一次），
    直接写 target 会撞上共享冲突或读到写了一半的 PDF。
    """
    return target.with_name(
        f"{target.stem}.part-{os.getpid()}-{threading.get_ident()}.pdf"
    )


def _convert_with_com(source: Path, target: Path) -> bool:
    """服务进程串行调度 COM；闸门覆盖 helper 的整个生命周期，包括 Quit。"""
    with _com_lock:
        # 等锁期间，同一原件的另一个请求可能已经完成转换。
        if target.is_file():
            return True
        with tempfile.TemporaryDirectory(dir=target.parent, prefix="office-com-") as directory:
            converted = Path(directory) / "converted.pdf"
            if run_parser("office_com", source, converted, timeout=CONVERT_TIMEOUT) and converted.is_file():
                converted.replace(target)
                return True
            return False


def _convert_with_com_local(source: Path, target: Path) -> bool:
    """仅在解析 helper 内用 Microsoft Office 导出 PDF。"""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    app = None
    document = None
    temp_target = _temp_target(target)
    # DispatchEx 而非 Dispatch: 不接管用户已经打开的 Office 文档。
    try:
        if source.suffix.lower() in _PRESENTATION_FORMATS:
            app = win32com.client.DispatchEx("PowerPoint.Application")
            document = app.Presentations.Open(
                str(source), ReadOnly=True, WithWindow=False
            )
            document.SaveAs(str(temp_target), _PP_SAVE_AS_PDF)
        else:
            app = win32com.client.DispatchEx("Word.Application")
            app.Visible = False
            document = app.Documents.Open(str(source), ReadOnly=True)
            document.SaveAs(str(temp_target), _WD_FORMAT_PDF)
        if not temp_target.is_file():
            return False
        os.replace(temp_target, target)
        return True
    finally:
        # 不关闭会把 WINWORD.EXE / POWERPNT.EXE 留成孤儿进程
        try:
            if document is not None:
                document.Close()
        except Exception:
            pass
        try:
            if app is not None:
                app.Quit()
        except Exception:
            pass
        pythoncom.CoUninitialize()
        temp_target.unlink(missing_ok=True)


def _libreoffice_env() -> dict:
    """给 soffice 用的干净环境（摘掉宿主 Python 的变量，见 _PYTHON_ENV_VARS）"""
    env = os.environ.copy()
    for name in _PYTHON_ENV_VARS:
        env.pop(name, None)
    return env


def _convert_with_libreoffice(soffice: str, source: Path, target: Path) -> bool:
    # profile 按服务线程复用，不按短命 helper 的 PID 生成，避免每次冷启动。
    with tempfile.TemporaryDirectory(dir=target.parent, prefix="office-lo-") as directory:
        converted = Path(directory) / "converted.pdf"
        ok = run_parser(
            "office_libreoffice", soffice, source, converted, _libreoffice_profile_dir(),
            timeout=CONVERT_TIMEOUT + 10,
        )
        if ok and converted.is_file():
            converted.replace(target)
            return True
        return False


def _convert_with_libreoffice_local(
    soffice: str, source: Path, target: Path, profile: Path,
) -> bool:
    """仅在解析 helper 内用 LibreOffice headless 导出 PDF。"""
    # 每次调用独立的产物目录，同一份原件并发转换也不会读到半个 PDF。
    outdir = _temp_target(target).with_suffix("")
    outdir.mkdir(parents=True, exist_ok=True)
    command = [
        soffice,
        "--headless",
        "--norestore",
        f"-env:UserInstallation={profile.resolve().as_uri()}",
        "--convert-to",
        "pdf",
        "--outdir",
        str(outdir),
        str(source),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=CONVERT_TIMEOUT,
            check=False,
            env=_libreoffice_env(),
        )
        produced = outdir / f"{source.stem}.pdf"
        if not produced.is_file():
            logger.warning(
                "[Office转换] LibreOffice 未产出 PDF (code=%s): %s",
                result.returncode,
                result.stderr.decode("utf-8", "replace")[:200],
            )
            return False

        produced.replace(target)
        return True
    finally:
        try:
            outdir.rmdir()
        except OSError:
            pass


def convert_to_pdf(source: Path) -> Optional[Path]:
    """把 Office 文档转成 PDF，返回缓存路径；不可用或失败时返回 None。

    auto 模式下前一个后端失败会顺位试下一个：装了 Office 但版本不兼容、
    或文档被占用时，LibreOffice 往往还能转出来。

    缓存按「实际使用的后端」分开存，按优先级逐个「命中就用、没有就转」。这样
    一次瞬时的 COM 失败只会让本次回退到 LibreOffice，不会把保真度更差的产物
    钉在首选后端的缓存位上——下次调用仍会重试 COM，成功后它的产物自动胜出。

    Args:
        source: 原始 Office 文档路径

    Returns:
        PDF 路径，转换器不可用或全部失败时为 None
    """
    # COM 的工作目录不是当前进程的 cwd，相对路径会直接报「找不到路径」
    source = source.resolve()
    if not source.is_file():
        return None

    candidates = list_converters()
    if not candidates:
        logger.info("[Office转换] 无可用转换器，回退 markitdown: %s", source.name)
        return None

    for kind, executable in candidates:
        target = _cache_path(source, kind)
        if target is None:
            return None
        if target.is_file():
            return target

        try:
            if kind == "com":
                ok = _convert_with_com(source, target)
            else:
                ok = _convert_with_libreoffice(executable, source, target)
        except ParserStopped:
            raise
        except subprocess.TimeoutExpired:
            logger.warning("[Office转换] 超时 (%s): %s", kind, source.name)
            continue
        except Exception as exc:
            logger.warning("[Office转换] 失败 (%s): %s - %s", kind, source.name, exc)
            continue

        if ok:
            logger.info("[Office转换] %s -> %s (%s)", source.name, target.name, kind)
            return target

    return None
