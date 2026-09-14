"""隔离解析 helper 入口；支持按次操作和复用会话，不加载数据库或业务编排。"""

import json
import logging
import sys
import time
from pathlib import Path
from typing import cast


def _parse(operation: str, args: list):
    if operation == "office_com":
        from indexing.services.office_convert import _convert_with_com_local

        return _convert_with_com_local(Path(args[0]), Path(args[1]))
    if operation == "office_libreoffice":
        from indexing.services.office_convert import _convert_with_libreoffice_local

        return _convert_with_libreoffice_local(
            args[0], Path(args[1]), Path(args[2]), Path(args[3]),
        )

    import pymupdf

    if operation == "probe_image":
        with pymupdf.open() as document:
            page = document.new_page(width=320, height=120)
            page.insert_text((24, 70), "Piece OCR test", fontsize=28)
            return page.get_pixmap(dpi=96).tobytes("jpeg", jpg_quality=85)

    if operation not in {"page_count", "text_pages", "text_stats", "render_page", "extract_pages"}:
        raise ValueError(f"未知解析操作: {operation}")

    with pymupdf.open(args[0]) as document:
        total_pages = len(document)
        if operation == "page_count":
            return total_pages
        if operation == "text_stats":
            return {
                "pages": total_pages,
                "chars": sum(len(cast(str, page.get_text("text")).strip()) for page in document),
            }
        if operation == "text_pages":
            from indexing.services.converter import MAX_PDF_PAGES, _format_page_to_markdown

            if total_pages > MAX_PDF_PAGES:
                raise ValueError(f"PDF 页数过多（{total_pages} 页），最大支持 {MAX_PDF_PAGES} 页")
            pages = []
            flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
            for index in range(args[1], min(args[2], total_pages)):
                page_dict = cast(dict, document[index].get_text("dict", flags=flags))
                pages.append({
                    "page_number": index + 1,
                    "total_pages": total_pages,
                    "page_text": _format_page_to_markdown(page_dict, index),
                })
            return {"total_pages": total_pages, "pages": pages}
        if operation == "render_page":
            page_number, dpi, image_format = args[1:]
            if not 1 <= page_number <= total_pages:
                return None
            pixmap = document[page_number - 1].get_pixmap(dpi=dpi)
            if image_format == "jpeg":
                return pixmap.tobytes("jpeg", jpg_quality=85)
            return pixmap.tobytes("png")
        if operation == "extract_pages":
            start, end, dest = args[1:]
            if not 1 <= start <= end <= total_pages:
                raise ValueError(f"PDF 页码越界: {start}-{end}/{total_pages}")
            with pymupdf.open() as part:
                part.insert_pdf(document, from_page=start - 1, to_page=end - 1)
                part.save(dest)
            return None


def _parser_loop(root: Path) -> None:
    pending = root / "next.json"
    active = root / "active.json"
    # ponytail: 单进程按 10ms 文件信号轮询；若空闲唤醒成为瓶颈，再换阻塞 IPC。
    while root.is_dir():
        if pending.is_file():
            pending.replace(active)
            parser_main(str(active))
            active.unlink(missing_ok=True)
        else:
            time.sleep(0.01)


def parser_main(request_path: str) -> None:
    """文件协议也适用于 PyInstaller windowed 模式，不需要 stdout 可用。"""
    request_file = Path(request_path)
    root = request_file.parent
    try:
        request = json.loads(request_file.read_text(encoding="utf-8"))
        if request["operation"] == "parser_loop":
            _parser_loop(root)
            return
        result = _parse(request["operation"], request["args"])
        if isinstance(result, bytes):
            (root / "result.bin").write_bytes(result)
            response = {"binary": True}
        else:
            response = {"value": result}
    except Exception as exc:
        logging.exception("解析 helper 失败")
        response = {"error": {"type": type(exc).__name__, "message": str(exc)}}
    # 常驻调用方不会等进程退出；必须等 JSON 和二进制都写完后再发布结果。
    result_path = root / "result.tmp"
    result_path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
    result_path.replace(root / "result.json")


if __name__ == "__main__":
    parser_main(sys.argv[1])
