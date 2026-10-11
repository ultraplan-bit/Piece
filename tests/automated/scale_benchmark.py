"""知识层性能基线：在种子数据上给关键入口计时，观察 OFFSET 分页随行数的退化。

手动运行（默认规模，几秒灌入 + 若干秒计时）：
    uv run python tests/automated/scale_benchmark.py
    uv run python tests/automated/scale_benchmark.py --objects 20000 --relations 40000 --wiki-pages 2000

只打印耗时与规模，不设死阈值（避免 CI 抖动误报）；对深 OFFSET 给一条宽松上限断言，
真正异常（数量级劣化）时才失败。也可作为 slow 用例：
    uv run pytest tests/automated/scale_benchmark.py --run-slow
"""

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from scale_fixtures import GRAPH_KEYWORD, WIKI_KEYWORD, seed_knowledge_base  # noqa: E402


def _prepare(data_path: Path):
    """初始化独立临时库，返回 settings 实例。"""
    from indexing import settings, database

    config = settings.AppSettings(data_path=str(data_path))
    config.embedding.vector_dim = 2
    config.mcp.api_key = "read-test-key"
    config.mcp.index_api_key = "write-test-key"
    settings._settings = config
    database.close_connection_pool()
    database.init_database()
    database.init_connection_pool()
    return config


def _timed(label, fn, *args, **kwargs):
    started = time.perf_counter()
    result = fn(*args, **kwargs)
    elapsed = time.perf_counter() - started
    return label, elapsed, result


def run_benchmark(scale_config, *, deep_offset=None):
    """灌种子并给入口计时，返回 (统计行列表, 句柄)。"""
    from indexing.services import knowledge_service as knowledge
    from indexing.services import wiki_service as wiki

    with tempfile.TemporaryDirectory(prefix="piece-scale-bench-") as tmp:
        data_path = Path(tmp)
        _prepare(data_path)
        try:
            seed_started = time.perf_counter()
            stats = seed_knowledge_base(data_path, **scale_config)
            seed_elapsed = time.perf_counter() - seed_started
            rows = [("seed(直插)", seed_elapsed, f"objects={scale_config['objects']} "
                     f"relations={scale_config['relations']} wiki_pages={scale_config['wiki_pages']}")]

            total_objects = knowledge.list_objects(limit=1)["total"]
            offset = deep_offset if deep_offset is not None else max(total_objects - 100, 0)

            calls = [
                ("list_objects 首页", lambda: knowledge.list_objects(limit=100)),
                ("list_objects 深OFFSET", lambda: knowledge.list_objects(limit=100, offset=offset)),
                ("search_objects 首页", lambda: knowledge.search_objects(query=GRAPH_KEYWORD, limit=100)),
                ("search_objects 深OFFSET", lambda: knowledge.search_objects(query=GRAPH_KEYWORD, limit=100, offset=offset)),
                ("references 首页", lambda: knowledge.references(
                    source_library_id=stats["library_id"], source_file_id=stats["source_file_id"], limit=100)),
                ("references 深OFFSET", lambda: knowledge.references(
                    source_library_id=stats["library_id"], source_file_id=stats["source_file_id"], limit=100,
                    offset=max(stats["reference_evidence"] - 100, 0))),
                ("get_record 证据页", lambda: knowledge.get_record(
                    kind="object", id=stats["hot_object"], limit=100, offset=max(scale_config["hot_object_evidence"] - 100, 0))),
                ("lint 首页", lambda: knowledge.lint(limit=100)),
                ("lint 深OFFSET", lambda: knowledge.lint(limit=100, offset=offset)),
                ("graph depth2", lambda: knowledge.graph(root_id=stats["object_ids"][0], depth=2)),
                ("history", lambda: knowledge.history(
                    kind="object", id=stats["history_object"], limit=100,
                    offset=max(scale_config["revisions"] - 100, 0))),
                ("wiki.list_pages 首页", lambda: wiki.list_pages(limit=100)),
                ("wiki.list_pages 深OFFSET", lambda: wiki.list_pages(
                    limit=100, offset=max(scale_config["wiki_pages"] - 100, 0))),
                ("wiki.search_pages 首页", lambda: wiki.search_pages(query=WIKI_KEYWORD, limit=100)),
                ("wiki.references 首页", lambda: wiki.references(
                    source_library_id=stats["library_id"], source_file_id=stats["source_file_id"], limit=100)),
                ("wiki.lint 首页", lambda: wiki.lint(limit=100)),
                ("wiki.get_record 证据页", lambda: wiki.get_record(
                    kind="page", id=stats["wiki_hot_page"], limit=100,
                    offset=max(scale_config["wiki_hot_page_evidence"] - 100, 0))),
                ("wiki.history", lambda: wiki.history(
                    id=stats["wiki_history_page"], limit=100, offset=max(scale_config["wiki_history"] - 100, 0))),
            ]
            results = [_timed(label, fn) for label, fn in calls]
            for label, elapsed, result in results:
                if isinstance(result, dict) and "total" in result:
                    detail = f"total={result['total']}"
                elif isinstance(result, dict) and "total_objects" in result:
                    detail = f"total_objects={result['total_objects']} issues={len(result['issues'])}"
                elif isinstance(result, dict) and "total_pages" in result:
                    detail = f"total_pages={result['total_pages']} issues={len(result['issues'])}"
                elif isinstance(result, dict) and "nodes" in result:
                    detail = f"nodes={len(result['nodes'])} edges={len(result['edges'])} truncated={result['truncated']}"
                else:
                    detail = ""
                rows.append((label, elapsed, detail))
            return rows, stats
        finally:
            from indexing import database
            database.close_connection_pool()


def print_report(rows):
    print("\n=== 知识层性能基线 ===")
    width = max(len(label) for label, _, _ in rows)
    for label, elapsed, detail in rows:
        print(f"{label:<{width}}  {elapsed * 1000:9.1f} ms   {detail}")
    print()


def _default_scale(objects, relations, wiki_pages):
    return dict(objects=objects, relations=relations, evidence=objects, hot_object_evidence=250,
                reference_evidence=250, revisions=250, wiki_pages=wiki_pages,
                wiki_evidence=max(wiki_pages // 2, 100), wiki_hot_page_evidence=150, wiki_history=250)


@pytest.mark.slow
def test_benchmark_runs_and_reports(capsys):
    """慢用例形式：默认规模跑一遍，打印耗时；仅对深 OFFSET 给宽松上限。"""
    rows, _ = run_benchmark(_default_scale(3000, 6000, 300))
    print_report(rows)
    captured = capsys.readouterr()
    assert "list_objects 首页" in captured.out
    # 宽松上限：单个入口超过 60s 视为数量级劣化（正常为毫秒到数秒）。
    for label, elapsed, _ in rows:
        assert elapsed < 60, f"{label} 耗时异常：{elapsed:.1f}s"


def main(argv=None):
    parser = argparse.ArgumentParser(description="知识层性能基线")
    parser.add_argument("--objects", type=int, default=5000)
    parser.add_argument("--relations", type=int, default=10000)
    parser.add_argument("--wiki-pages", type=int, default=800)
    parser.add_argument("--deep-offset", type=int, default=None, help="覆盖深分页的 offset")
    args = parser.parse_args(argv)

    rows, _ = run_benchmark(_default_scale(args.objects, args.relations, args.wiki_pages),
                            deep_offset=args.deep_offset)
    print_report(rows)


if __name__ == "__main__":
    main()
