"""
VLM PDF 解析链路验证脚本

用 .env 中的 OpenAI 兼容配置，走真实的 iter_vlm_pdf_pages 解析一份 PDF 的前若干页，
验证：连通性、图片输入支持、按页序产出、并发窗口、耗时与产出质量。

配置直接注入进程内的 settings 缓存，不会写 config.json。

用法:
    uv run python tests/test_vlm_client.py <pdf路径> [页数] [并发数] [DPI]

例:
    uv run python tests/test_vlm_client.py "data/files/originals/xxx.pdf" 4 4 150
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pymupdf

import indexing.settings as settings_module
from indexing.settings import AppSettings, OcrSettings

OUTPUT_DIR = Path(__file__).parent / "output"


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


def inject_settings(base_url: str, api_key: str, model: str, dpi: int, concurrency: int) -> None:
    """把 VLM 配置塞进 settings 的进程内缓存（不落盘）。"""
    settings = AppSettings(
        ocr=OcrSettings(
            provider="vlm",
            vlm_base_url=base_url,
            vlm_api_key=api_key,
            vlm_model=model,
            vlm_dpi=dpi,
            vlm_concurrency=concurrency,
        )
    )
    settings_module._settings = settings


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
    concurrency = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    dpi = int(sys.argv[4]) if len(sys.argv) > 4 else 150

    if not pdf_path.exists():
        print(f"PDF 不存在: {pdf_path}")
        return 1

    env = load_dotenv(Path(__file__).parent.parent / ".env")
    base_url = env.get("OPENAI_BASE_URL", "")
    api_key = env.get("OPENAI_API_KEY", "")
    model = env.get("OPENAI_MODEL", "")
    if not (base_url and api_key and model):
        print("请在 .env 中配置 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL")
        return 1

    inject_settings(base_url, api_key, model, dpi, concurrency)

    # 注入配置后再导入，确保客户端读到的是注入值
    from indexing.services.vlm_client import iter_vlm_pdf_pages, test_vlm_connection

    print(f"服务: {base_url}\n模型: {model}\nDPI={dpi} 并发={concurrency}\n")

    print("[1/2] 测试连通性和图片输入支持...")
    started = time.monotonic()
    ok, message = test_vlm_connection(base_url, api_key, model)
    print(f"  [{'OK' if ok else 'FAIL'}] {message}  ({time.monotonic() - started:.1f}s)\n")
    if not ok:
        return 1

    print(f"[2/2] 解析 {pdf_path.name} 前 {pages} 页...")
    temp_dir = Path(tempfile.mkdtemp(prefix="piece-vlm-test-"))
    part_path = temp_dir / "head.pdf"
    actual = extract_head_pages(pdf_path, pages, part_path)

    progress = []
    results = []
    started = time.monotonic()
    try:
        for page in iter_vlm_pdf_pages(
            part_path,
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

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{pdf_path.stem}.vlm.md"
    out_path.write_text(
        "\n\n".join(text for _, text in results), encoding="utf-8"
    )
    print(f"\n产出已保存: {out_path}")
    print(f"\n--- 第 1 页前 800 字 ---\n{results[0][1][:800] if results else '(空)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
