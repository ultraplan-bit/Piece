"""
MinerU 解析链路验证脚本

用 .env 中的 MINERU_TOKEN 走真实的 iter_mineru_pdf_pages 解析一份 PDF 的前若干页，
验证：Token 与连通性、逐页产出与页序、插图落盘、进度回调、耗时与产出质量。

配置直接注入进程内的 settings 缓存，不会写 config.json。

用法:
    uv run python tests/test_mineru_client.py <pdf路径> [页数] [model_version] [language] [force_ocr]

例:
    uv run python tests/test_mineru_client.py "data/files/originals/xxx.pdf" 4 vlm
    # 扫描件：强制 OCR，并指定文档语言
    uv run python tests/test_mineru_client.py "scan.pdf" 3 vlm japan 1
"""

import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pymupdf

import indexing.settings as settings_module
from indexing.settings import AppSettings, OcrSettings

OUTPUT_DIR = Path(__file__).parent / "output"

# 正文里的插图引用（HTML 形态，路径含空格与括号，解析结果已改写成工作目录相对路径）
_IMAGE_REF = re.compile(r'src="(?!https?://|/|data:)([^"]+)"')


def load_dotenv(path: Path) -> dict:
    """极简 .env 解析：KEY=VALUE，忽略空行和注释。"""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def inject_settings(token: str, model_version: str, language: str, is_ocr: bool) -> None:
    """把 MinerU 配置塞进 settings 的进程内缓存（不落盘）。"""
    settings_module._settings = AppSettings(
        ocr=OcrSettings(
            provider="mineru",
            mineru_token=token,
            mineru_model_version=model_version,
            mineru_language=language,
            mineru_is_ocr=is_ocr,
        )
    )


def extract_head_pages(src: Path, pages: int, dest: Path) -> int:
    """切出前 pages 页到独立 PDF，返回实际页数。"""
    with pymupdf.open(str(src)) as source:
        count = min(pages, len(source))
        part = pymupdf.open()
        try:
            part.insert_pdf(source, from_page=0, to_page=count - 1)
            part.save(str(dest))
        finally:
            part.close()
    return count


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    pdf_path = Path(sys.argv[1])
    pages = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    model_version = sys.argv[3] if len(sys.argv) > 3 else "vlm"
    language = sys.argv[4] if len(sys.argv) > 4 else "ch"
    is_ocr = len(sys.argv) > 5 and sys.argv[5].lower() in {"1", "true", "yes"}

    if not pdf_path.exists():
        print(f"PDF 不存在: {pdf_path}")
        return 1

    env = load_dotenv(Path(__file__).parent.parent / ".env")
    token = env.get("MINERU_TOKEN", "")
    if not token:
        print("请在 .env 中配置 MINERU_TOKEN（mineru.net 控制台申请）")
        return 1

    inject_settings(token, model_version, language, is_ocr)

    # 注入配置后再导入，确保客户端读到的是注入值
    from indexing.services.mineru_client import iter_mineru_pdf_pages, test_mineru_connection

    print(f"模型版本: {model_version}  语言: {language}  强制 OCR: {is_ocr}\n")

    print("[1/2] 测试连通性（提交一张探针图，消耗 1 页额度）...")
    started = time.monotonic()
    ok, message = test_mineru_connection(token, model_version)
    print(f"  [{'OK' if ok else 'FAIL'}] {message}  ({time.monotonic() - started:.1f}s)\n")
    if not ok:
        return 1

    print(f"[2/2] 解析 {pdf_path.name} 前 {pages} 页...")
    temp_dir = Path(tempfile.mkdtemp(prefix="piece-mineru-test-"))
    part_path = temp_dir / "head.pdf"
    actual = extract_head_pages(pdf_path, pages, part_path)

    progress = []
    results = []
    image_dir = OUTPUT_DIR / f"{pdf_path.stem}.mineru"
    started = time.monotonic()
    try:
        for page in iter_mineru_pdf_pages(
            part_path,
            image_dir,
            on_parse_progress=lambda done, total: progress.append((done, total)),
        ):
            elapsed = time.monotonic() - started
            results.append((page.page_number, page.page_text))
            print(
                f"  第 {page.page_number}/{page.total_pages} 页  "
                f"{len(page.page_text)} 字  累计 {elapsed:.1f}s"
            )
    finally:
        part_path.unlink(missing_ok=True)
        temp_dir.rmdir()

    total_elapsed = time.monotonic() - started
    print(f"\n完成: {len(results)}/{actual} 页, 总耗时 {total_elapsed:.1f}s, "
          f"平均 {total_elapsed / max(1, len(results)):.1f}s/页")

    # 页序必须严格递增，否则工作文件顺序和切片页号都会错
    page_numbers = [num for num, _ in results]
    assert page_numbers == sorted(page_numbers) == list(range(1, len(results) + 1)), \
        f"页序错误: {page_numbers}"
    assert len(results) == actual, f"页数不符: 期望 {actual}, 实际 {len(results)}"
    print(f"[OK] 页序严格递增且页数对齐；进度回调 {len(progress)} 次，末次 {progress[-1] if progress else None}")

    # 正文引用的插图必须真的落在工作目录里，否则检索取图会静默缺图
    markdown = "\n\n".join(text for _, text in results)
    refs = set(_IMAGE_REF.findall(markdown))
    missing = [ref for ref in refs if not (OUTPUT_DIR / ref).exists()]
    assert not missing, f"插图引用未落盘: {missing}"
    print(f"[OK] 插图引用 {len(refs)} 张，全部已落盘")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{pdf_path.stem}.mineru.md"
    out_path.write_text(markdown, encoding="utf-8")

    print(f"\n产出: {out_path}（含插图目录 {image_dir}）")
    print("--- 首 600 字 ---")
    print(markdown[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
