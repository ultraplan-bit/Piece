"""浏览器本地的工作区偏好；按资料库隔离，不保存正文、草稿或凭据。"""
import hashlib
import math
import os
from pathlib import Path


RESULTS_WIDTHS = {
    "files": 320, "wiki": 260, "graph": 260, "recall_test": 300,
    "cloud_sync": 240, "mcp_config": 240, "skills": 240, "logs": 240, "settings": 240,
}
MIN_NAVIGATION_WIDTH = 160
MIN_RESULTS_WIDTH = 220


def storage_key(data_path) -> str:
    identity = os.path.normcase(str(Path(data_path).resolve()))
    return "piece.workspace.v1." + hashlib.sha256(identity.encode()).hexdigest()[:16]


def _number(value, default, minimum, maximum):
    if type(value) not in (int, float) or (isinstance(value, float) and not math.isfinite(value)):
        return default
    return max(minimum, min(maximum, int(value)))


def _ids(values, *, strings=False):
    if not isinstance(values, list):
        return []
    valid = []
    for value in values[:1000]:
        if strings and isinstance(value, str) and value.isdecimal() and len(value) < 19:
            value = int(value)
        if type(value) is int and 0 < value < 2**63:
            valid.append(str(value) if strings else value)
    return list(dict.fromkeys(valid))


def restore_workspace(data, state: dict, widths: dict) -> None:
    """只接收白名单字段；损坏或旧版偏好不能把目录收起或污染查询参数。"""
    if not isinstance(data, dict):
        return
    saved_widths = data.get("widths")
    if isinstance(saved_widths, dict):
        for view, default in RESULTS_WIDTHS.items():
            widths[view] = _number(saved_widths.get(view), default, MIN_RESULTS_WIDTH, 800)
    state["navigation_width"] = _number(data.get("navigation_width"), state.get("navigation_width", 160), MIN_NAVIGATION_WIDTH, 380)
    state["results_width"] = widths["files"]
    saved = data.get("files")
    if not isinstance(saved, dict):
        return
    for key in ("uncategorized", "include_descendants", "source_follow"):
        if type(saved.get(key)) is bool:
            state[key] = saved[key]
    state["active_collection_ids"] = _ids(saved.get("active_collection_ids"))[:1]
    state["expanded_collection_ids"] = _ids(saved.get("expanded_collection_ids"), strings=True)
    for key in ("file_page", "chunk_page"):
        state[key] = _number(saved.get(key), 1, 1, 1_000_000)
    for key in ("file_scroll", "tree_scroll", "chunk_scroll"):
        state[key] = _number(saved.get(key), 0, 0, 10_000_000)
    selected = saved.get("selected_file_id")
    if type(selected) is int and 0 < selected < 2**63:
        state["selected_file_id"] = selected
        state["chunk_scroll_by_file"][selected] = state["chunk_scroll"]
        state["chunk_page_by_file"][selected] = state["chunk_page"]
    if isinstance(saved.get("search_keyword"), str):
        state["search_keyword"] = saved["search_keyword"][:1000]
    if saved.get("sort_key") in ("created_at", "updated_at", "filename"):
        state["sort_key"] = saved["sort_key"]
    if saved.get("file_reading_mode") in ("reading", "cards"):
        state["file_reading_mode"] = saved["file_reading_mode"]
    state["library_mode"] = saved.get("library_mode") if saved.get("library_mode") in ("tree", "all", "uncategorized") else "tree"
    state["tree_page_numbers"] = {}
    pages = saved.get("tree_page_numbers")
    if isinstance(pages, dict):
        for key, value in list(pages.items())[:1000]:
            if key == "root" or (isinstance(key, str) and key.isdecimal() and len(key) < 19):
                state["tree_page_numbers"][key] = _number(value, 1, 1, 1_000_000)
    focused = saved.get("focused_tree_key")
    if isinstance(focused, str) and len(focused) < 80:
        parts = focused.split(":")
        if (len(parts) == 2 and parts[0] == "c" and parts[1].isdecimal()) or (
            len(parts) == 3 and parts[0] == "f" and parts[1] in ("root", "flat") and parts[2].isdecimal()
        ) or (len(parts) == 3 and parts[0] == "f" and all(part.isdecimal() for part in parts[1:])):
            state["focused_tree_key"] = focused
    if "library_mode" not in saved:
        # 旧布局展开的是纯资料集树；首次迁移只展开当前路径，避免一次加载所有旧分支的文档。
        state["expanded_collection_ids"] = [str(cid) for cid in state["active_collection_ids"]]
    state["source_zoom"] = _number(saved.get("source_zoom"), 100, 50, 300)
    state["library_view"] = "compare" if saved.get("library_view") == "compare" else "reading"
    state["source_pane_open"] = state["library_view"] == "compare"


def workspace_snapshot(state: dict, widths: dict) -> dict:
    return {
        "navigation_width": state["navigation_width"],
        "widths": dict(widths),
        "files": {key: state.get(key) for key in (
            "selected_file_id", "search_keyword", "sort_key", "file_page", "file_scroll", "tree_scroll",
            "active_collection_ids", "expanded_collection_ids", "uncategorized", "include_descendants",
            "library_mode", "tree_page_numbers", "focused_tree_key",
            "chunk_page", "chunk_scroll", "file_reading_mode", "library_view", "source_zoom", "source_follow",
        )},
    }
