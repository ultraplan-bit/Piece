"""本机目录导入的候选展开：排除规则与纯链接笔记判定。

只依赖标准库，供 CLI（不加载业务层）与 GUI 共用，导入本身仍由服务层完成。
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path


# 目录导入默认跳过以点开头的隐藏目录和文件（.obsidian、.trash、.git 等），它们是
# 应用配置或回收站而非资料；用户可用 --exclude 追加规则，用 --include-hidden 放开
_LINK_NOTE_MIN_LINKS = 3
_LINK_NOTE_MAX_TEXT = 80
_WIKILINK = re.compile(r"!?\[\[[^\]]*\]\]")
_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\([^)]*\)")
_FRONTMATTER = re.compile(r"^\s*---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)
_LINE_DECORATION = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)]|#{1,6}|>)+[ \t]*", re.MULTILINE)


def is_link_note(path: Path) -> bool:
    """判断 Markdown 是否为几乎只有链接的索引笔记（Obsidian 的 MOC / 目录页）。

    判据：链接数达到下限，且去掉 frontmatter、链接、列表和标题记号后剩余文字极少。
    这类笔记导入后只会产出没有实质内容的卡片。读取失败时按普通文件处理。
    """
    if path.suffix.lower() != ".md":
        return False
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    content = _FRONTMATTER.sub("", content, count=1)
    links = len(_WIKILINK.findall(content)) + len(_MARKDOWN_LINK.findall(content))
    if links < _LINK_NOTE_MIN_LINKS:
        return False
    remainder = _MARKDOWN_LINK.sub("", _WIKILINK.sub("", content))
    remainder = _LINE_DECORATION.sub("", remainder)
    return len("".join(remainder.split())) < _LINK_NOTE_MAX_TEXT


def _exclusion_reason(relative: Path, patterns: list[str], include_hidden: bool) -> str | None:
    """按相对路径的每一段和整体匹配排除规则；命中返回原因。"""
    parts = relative.parts
    if not include_hidden and any(part.startswith(".") for part in parts):
        return "hidden"
    posix = relative.as_posix()
    for pattern in patterns:
        if fnmatch.fnmatch(posix, pattern) or any(fnmatch.fnmatch(part, pattern) for part in parts):
            return f"pattern: {pattern}"
    return None


def import_candidates(paths: list[Path], recursive: bool, *, exclude: list[str] | None = None,
                       include_hidden: bool = False, skip_link_notes: bool = False,
                       ) -> tuple[list[Path], list[dict[str, str]], list[dict[str, str]]]:
    """展开导入目标；符号链接在 resolve 之前判定，读取错误如实报告为 skipped。

    返回 (候选, skipped, excluded)：skipped 是无法处理的项，excluded 是按规则主动
    不导入的项——后者在用户意图之内，不影响命令的成功判定。
    """
    candidates: list[Path] = []
    skipped: list[dict[str, str]] = []
    excluded: list[dict[str, str]] = []
    seen: set[str] = set()
    patterns = list(exclude or [])

    def linked(path: Path) -> bool:
        # 包含祖先目录以及 Windows junction；必须在 resolve 之前检查。
        return any(p.is_symlink() or p.is_junction() for p in (path, *path.parents))

    def add_file(path: Path, relative: Path | None = None) -> None:
        if linked(path):
            skipped.append({"path": str(path), "reason": "linked_path"})
            return
        absolute = path.expanduser().resolve()
        key = os.path.normcase(str(absolute))
        if key in seen:
            skipped.append({"path": str(absolute), "reason": "duplicate"})
            return
        seen.add(key)
        if not absolute.is_file():
            skipped.append({"path": str(absolute), "reason": "not_file"})
            return
        if relative is not None:
            reason = _exclusion_reason(relative, patterns, include_hidden)
            if reason:
                excluded.append({"path": str(absolute), "reason": reason})
                return
        if skip_link_notes and is_link_note(absolute):
            excluded.append({"path": str(absolute), "reason": "link_note"})
            return
        candidates.append(absolute)

    def on_walk_error(error: OSError) -> None:
        skipped.append({"path": str(error.filename or ""), "reason": f"read_error: {error.strerror or error}"})

    for raw in paths:
        raw = raw.expanduser()
        if linked(raw):
            skipped.append({"path": str(raw), "reason": "linked_path"})
            continue
        absolute = raw.resolve()
        if absolute.is_dir():
            if not recursive:
                try:
                    children = sorted(absolute.iterdir(), key=lambda item: str(item).casefold())
                except OSError as exc:
                    on_walk_error(exc)
                    continue
                for child in children:
                    if linked(child):
                        skipped.append({"path": str(child), "reason": "symlink"})
                    elif child.is_dir():
                        skipped.append({"path": str(child), "reason": "subdirectory_not_recursive"})
                    else:
                        add_file(child, child.relative_to(absolute))
            else:
                for root, dirs, files in os.walk(absolute, topdown=True, followlinks=False, onerror=on_walk_error):
                    root_path = Path(root)
                    for directory in list(dirs):
                        directory_path = root_path / directory
                        if linked(directory_path):
                            skipped.append({"path": str(directory_path), "reason": "symlink_directory"})
                            dirs.remove(directory)
                            continue
                        # 整个目录命中排除规则时不再深入，只记一条
                        reason = _exclusion_reason(directory_path.relative_to(absolute), patterns, include_hidden)
                        if reason:
                            excluded.append({"path": str(directory_path), "reason": reason})
                            dirs.remove(directory)
                    for filename in sorted(files, key=str.casefold):
                        add_file(root_path / filename, (root_path / filename).relative_to(absolute))
        else:
            add_file(absolute)
    return candidates, skipped, excluded
