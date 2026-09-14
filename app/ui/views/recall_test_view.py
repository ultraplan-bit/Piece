"""
召回测试视图

职责:
- 渲染召回测试中栏（查询输入、过滤条件、三路召回明细）
- 渲染召回测试右栏（命中的 doc_title 列表与正文预览）

直接在进程内调用检索链路（resolve_database_keywords / get_docs），与检索 MCP
工具走同一份代码，只是不过 HTTP（同 logs_view 直读日志缓冲区的做法）。这样还能
拿到 debug_stats —— MCP 侧会把它剥掉只写进日志（retrieval/server.py），而三路
召回明细正是这个面板要展示的东西。
"""

from indexing.utils import run_sync
import time

from nicegui import ui

from app.i18n import t
from app.ui.components import chunk_markdown, collection_labels
from indexing.services.collection_service import list_collections
from retrieval.tools import get_docs, resolve_database_keywords

# 三路来源：debug_stats.fused_top_k 里的名次字段 -> 结果行上的短标签
_ROUTE_LABELS = {
    "exact_rank": "recall_test.route_exact",
    "bm25_rank": "recall_test.route_bm25",
    "vector_rank": "recall_test.route_vector",
}

# get-docs 单次最多取 3 篇，结果列表里把这几条标出来，
# 让用户知道 AI 实际大概率只会翻开最靠前的那几张卡片
_GET_DOCS_LIMIT = 3

# 三路全空或分词为空时，检索链路是以异常形式返回的，
# 这两种归到「没命中」而不是报错，否则最常见的情况看起来像崩了
_EMPTY_ERRORS = ("缺少检索结果", "未提取到有效关键词")


def render_recall_test_middle(recall_state: dict, ui_refs: dict):
    """
    渲染召回测试中栏

    Args:
        recall_state: 召回测试状态（查询、过滤条件、结果、统计）
        ui_refs: UI 组件引用字典
    """
    with ui.column().classes(
        "w-64 h-full flex flex-col overflow-hidden theme-panel gap-0"
    ).style("border-right: 1px solid var(--border-color)"):
        # 顶部标题栏
        with ui.row().classes(
            "w-full px-3 items-center justify-between"
        ).style("border-bottom: 1px solid var(--border-color); height: 49px"):
            ui.label(t("recall_test.title")).classes("text-sm font-medium theme-text")

        # 查询表单
        with ui.column().classes("w-full px-3 py-3 gap-3").style(
            "border-bottom: 1px solid var(--border-color)"
        ):
            ui.textarea(
                label=t("recall_test.query"),
                placeholder=t("recall_test.query_placeholder"),
                value=recall_state.get("query", ""),
                on_change=lambda e: recall_state.update(query=e.value),
            ).props("dense outlined autogrow input-class=text-sm").classes(
                "w-full theme-card theme-border-soft"
            )

            ui.input(
                label=t("recall_test.filenames"),
                placeholder=t("recall_test.filenames_placeholder"),
                value=recall_state.get("filenames", ""),
                on_change=lambda e: recall_state.update(filenames=e.value),
            ).props("dense outlined input-class=text-sm").classes(
                "w-full theme-card theme-border-soft"
            )

            # 集合按名字传给检索链路（工具侧就是模糊匹配名字，不是 id）
            ui.select(
                [item["name"] for item in list_collections()],
                value=list(recall_state.get("collection_names") or []),
                multiple=True,
                label=t("recall_test.collections"),
                on_change=lambda e: recall_state.update(collection_names=e.value),
            ).props("dense outlined options-dense use-chips").classes(
                "w-full text-sm theme-card theme-border-soft"
            )

            @ui.refreshable
            def run_button():
                button = ui.button(
                    t("recall_test.run"),
                    icon="search",
                    on_click=lambda: _run_search(recall_state, ui_refs),
                ).props("color=primary").classes("w-full")
                if recall_state.get("is_running"):
                    button.props("loading")

            ui_refs["recall_run_button"] = run_button
            run_button()

        # 检索明细（跑完才出现）
        with ui.scroll_area().classes("flex-1"):
            @ui.refreshable
            def recall_stats():
                stats = recall_state.get("stats")
                if not stats:
                    return
                with ui.column().classes("w-full px-3 py-3 gap-2"):
                    ui.label(t("recall_test.stats")).classes(
                        "text-xs font-medium theme-text-muted"
                    )
                    _stat_row(
                        t("recall_test.route_exact"), str(stats["exact"])
                    )
                    _stat_row(t("recall_test.route_bm25"), str(stats["bm25"]))
                    _stat_row(t("recall_test.route_vector"), str(stats["vector"]))
                    _stat_row(t("recall_test.fused"), str(stats["fused"]))
                    _stat_row(
                        t("recall_test.elapsed"), f"{stats['elapsed_ms']} ms"
                    )

                    file_ids = stats.get("file_ids_filter")
                    if file_ids:
                        _stat_row(
                            t("recall_test.file_filter"), str(len(file_ids))
                        )

                    tokens = stats.get("tokens") or []
                    if tokens:
                        ui.label(t("recall_test.tokens")).classes(
                            "text-xs theme-text-muted mt-1"
                        )
                        with ui.row().classes("w-full gap-1 flex-wrap"):
                            for token in tokens:
                                ui.badge(token).props("dense outline").classes(
                                    "text-xs"
                                )

            ui_refs["recall_stats"] = recall_stats
            recall_stats()


def render_recall_test_right(recall_state: dict, ui_refs: dict):
    """
    渲染召回测试右栏（召回结果列表）

    Args:
        recall_state: 召回测试状态
        ui_refs: UI 组件引用字典
    """
    with ui.column().classes("flex-1 h-full flex flex-col theme-content min-w-0 gap-0"):
        # 顶部信息区
        with ui.row().classes(
            "w-full px-5 items-center justify-between theme-sidebar"
        ).style("border-bottom: 1px solid var(--border-color); height: 49px"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("manage_search", size="xs").classes("theme-text-accent")
                ui.label(t("recall_test.results")).classes(
                    "text-sm font-medium theme-text"
                )

            @ui.refreshable
            def recall_count():
                results = recall_state.get("results") or []
                if results:
                    ui.label(str(len(results))).classes("text-xs theme-text-muted")

            ui_refs["recall_count"] = recall_count
            recall_count()

        # 结果区
        with ui.scroll_area().classes("flex-1 min-w-0"):
            @ui.refreshable
            def recall_results():
                error = recall_state.get("error")
                results = recall_state.get("results") or []

                if error:
                    _placeholder("error_outline", t("recall_test.failed"), error)
                elif not recall_state.get("has_run"):
                    _placeholder(
                        "manage_search",
                        t("recall_test.empty"),
                        t("recall_test.empty_hint"),
                    )
                elif not results:
                    _placeholder(
                        "search_off",
                        t("recall_test.no_results"),
                        t("recall_test.no_results_hint"),
                    )
                else:
                    with ui.column().classes("w-full gap-1 px-4 py-3"):
                        ui.label(
                            t("recall_test.top_hint", limit=_GET_DOCS_LIMIT)
                        ).classes("text-xs theme-text-muted pb-1")
                        for index, item in enumerate(results, start=1):
                            _render_result(index, item)

            ui_refs["recall_results"] = recall_results
            recall_results()


def _stat_row(label: str, value: str):
    """一行「名称 —— 数值」的统计项"""
    with ui.row().classes("w-full items-center justify-between"):
        ui.label(label).classes("text-xs theme-text-muted")
        ui.label(value).classes("text-xs theme-text")


def _placeholder(icon: str, title: str, hint: str):
    """结果区的空状态/错误态占位"""
    with ui.column().classes(
        "w-full h-full items-center justify-center gap-1 px-8 py-16"
    ):
        ui.icon(icon, size="lg").classes("theme-text-muted")
        ui.label(title).classes("text-sm theme-text-muted")
        ui.label(hint).classes("text-xs theme-text-muted text-center")


def _render_result(index: int, item: dict):
    """
    渲染一条召回结果

    Args:
        index: 排名（从 1 开始）
        item: {"doc_title", "score", "routes", "doc"}
    """
    doc = item.get("doc") or {}

    with ui.expansion().props("dense dense-toggle expand-separator").classes(
        "w-full rounded-md theme-card"
    ) as expansion:
        with expansion.add_slot("header"):
            with ui.row().classes("w-full items-center gap-2 min-w-0"):
                # 前 3 条高亮：AI 单次 get-docs 最多翻开这么多张
                rank_classes = "text-xs w-5 text-center shrink-0 "
                if index <= _GET_DOCS_LIMIT:
                    rank_classes += "theme-text-accent font-semibold"
                else:
                    rank_classes += "theme-text-muted"
                ui.label(str(index)).classes(rank_classes)

                ui.label(item["doc_title"]).classes(
                    "text-sm theme-text flex-1 min-w-0 truncate"
                )

                for route in item.get("routes") or []:
                    ui.badge(t(_ROUTE_LABELS[route])).props("dense outline").classes(
                        "text-xs shrink-0"
                    )

                ui.label(f"{item.get('score', 0):.3f}").classes(
                    "text-xs theme-text-muted shrink-0 w-10 text-right"
                )

        with ui.column().classes("w-full gap-1 px-3 pb-2 min-w-0"):
            if not doc:
                ui.label(t("recall_test.doc_missing")).classes(
                    "text-xs theme-text-muted"
                )
                return

            meta = [doc.get("filename", "")]
            if doc.get("heading_path"):
                meta.append(doc["heading_path"])
            meta.append(
                t(
                    "recall_test.chunk_position",
                    index=doc.get("chunk_index_in_file", 0),
                    total=doc.get("total_chunks_in_file", 0),
                )
            )
            with ui.row().classes("w-full items-center gap-2 min-w-0"):
                ui.label(" · ".join(part for part in meta if part)).classes(
                    "text-xs theme-text-muted flex-1 min-w-0 truncate"
                )
                toggle_button = ui.button(icon="code").props(
                    "flat dense round size=xs"
                ).classes("theme-text-muted shrink-0").tooltip(
                    t("recall_test.toggle_view")
                )

            if doc.get("collections"):
                collection_labels(doc["collections"])

            # 默认按知识卡片的样子渲染 Markdown：表格、公式、图片在纯文本下没法看。
            # 但原文视图要留着 —— 排查召回问题时得能确认 AI 实际收到的那份纯文本，
            # 两份都渲染出来靠显隐切换，省得展开态被 refresh 冲掉
            chunk_text = doc.get("chunk_text", "")
            rendered = chunk_markdown(chunk_text, chunk_id=doc.get("chunk_id"), file_path=doc.get("file_path"))
            raw = ui.label(chunk_text).classes(
                "text-xs theme-text whitespace-pre-wrap"
            ).style("word-break: break-word")
            raw.set_visibility(False)

            view = {"raw": False}

            def toggle_view():
                view["raw"] = not view["raw"]
                rendered.set_visibility(not view["raw"])
                raw.set_visibility(view["raw"])
                toggle_button.props(f"icon={'article' if view['raw'] else 'code'}")

            toggle_button.on_click(toggle_view)


def _split_filenames(raw: str) -> list:
    """把「限定文件名」输入拆成列表（中英文逗号都认）"""
    return [part.strip() for part in raw.replace("，", ",").split(",") if part.strip()]


async def _run_search(recall_state: dict, ui_refs: dict):
    """跑一次召回：resolve-keywords 拿标题，再用 get-docs 把正文补齐"""
    query = (recall_state.get("query") or "").strip()
    if not query:
        ui.notify(t("recall_test.empty_query"), type="warning")
        return
    if recall_state.get("is_running"):
        return

    recall_state["is_running"] = True
    _refresh(ui_refs, "recall_run_button")

    try:
        filenames = _split_filenames(recall_state.get("filenames") or "")
        collections = list(recall_state.get("collection_names") or [])

        started = time.perf_counter()
        result = await resolve_database_keywords(
            query,
            filenames=filenames or None,
            collections=collections or None,
        )
        keywords = result.get("keywords", [])
        # 一次把 top-k 的正文全取回来：条数上限 3 是 MCP 工具层的约束，
        # get_docs 本身不限，预取后展开卡片就不用再跑异步查询
        docs = (
            await run_sync(get_docs, keywords) if keywords else {}
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        debug_stats = result.get("debug_stats", {})
        confidence_scores = result.get("confidence_scores", {})
        routes_by_title = {
            entry["doc_title"]: [
                field for field in _ROUTE_LABELS if entry.get(field) is not None
            ]
            for entry in debug_stats.get("fused_top_k", [])
        }

        recall_state["results"] = [
            {
                "doc_title": title,
                "score": confidence_scores.get(title, 0.0),
                "routes": routes_by_title.get(title, []),
                "doc": docs.get(title),
            }
            for title in keywords
        ]
        recall_state["stats"] = {
            "exact": debug_stats.get("exact_recall_count", 0),
            "bm25": debug_stats.get("bm25_recall_count", 0),
            "vector": debug_stats.get("vector_recall_count", 0),
            "fused": result.get("stats", {}).get("total_fused_results", 0),
            "tokens": debug_stats.get("tokens") or [],
            "file_ids_filter": debug_stats.get("file_ids_filter"),
            "elapsed_ms": elapsed_ms,
        }
        recall_state["error"] = None

    except Exception as e:
        message = str(e)
        recall_state["results"] = []
        recall_state["stats"] = None
        if message in _EMPTY_ERRORS:
            recall_state["error"] = None
        else:
            recall_state["error"] = message
            ui.notify(f"{t('recall_test.failed')}: {message}", type="negative")

    finally:
        recall_state["is_running"] = False
        recall_state["has_run"] = True
        _refresh(
            ui_refs,
            "recall_run_button",
            "recall_stats",
            "recall_count",
            "recall_results",
        )


def _refresh(ui_refs: dict, *keys: str):
    """刷新指定的可刷新组件（未渲染时跳过）"""
    for key in keys:
        component = ui_refs.get(key)
        if component:
            component.refresh()
