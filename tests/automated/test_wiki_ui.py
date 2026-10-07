"""Wiki GUI 展示、控制器与真实 NiceGUI 构建回归。"""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.i18n import t


def test_wiki_workbench_has_no_graph_relation_management():
    from app.ui.views.wiki_view import WikiWorkbench
    workbench = WikiWorkbench()
    # Wiki 不出现图谱关系管理：没有建边、图探索入口。
    for name in ("edit_edge", "edit_relation", "render_graph", "load_graph", "graph_click",
                 "view_graph", "edit_object"):
        assert not hasattr(workbench, name), name
    assert hasattr(workbench, "edit_page")


def test_wiki_controller_search_read_and_inline_links(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench
    from indexing.services import wiki_service as service

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    made = service.apply({"request_key": str(uuid4()), "reason": "test", "pages": [
        {"ref": "a", "kind": "topic", "title": "页面一", "body": "正文"},
        {"ref": "b", "kind": "concept", "title": "页面二", "body": ""}]})
    a, b = made["refs"]["a"]["id"], made["refs"]["b"]["id"]
    assert made["committed"] and made["index_status"] in ("current", "stale")
    workbench = WikiWorkbench()

    async def scenario():
        assert workbench.results is None
        workbench.kind = "topic"
        await workbench.search()
        assert [item["id"] for item in workbench.results["pages"]] == [a]
        # 页面列表是摘要，不含正文。
        assert "body" not in workbench.results["pages"][0]
        await workbench.select("page", a)
        assert workbench.detail["record"]["body"] == "正文"
        assert workbench.detail["record"]["content_hash"]
        # 正文内链 piece://wiki/<UUID> 点击后在本功能内跳转，不进入图谱。
        await workbench.internal_link(SimpleNamespace(args=b))
        assert workbench.detail["record"]["id"] == b
        await workbench.select("page", str(uuid4()))
        assert workbench.error and workbench.detail["record"]["id"] == b
    asyncio.run(scenario())


def test_real_nicegui_wiki_pages_forms_and_review_construct(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench
    from indexing.services import wiki_service as service

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    made = service.apply({"request_key": str(uuid4()), "reason": "test", "pages": [
        {"ref": "a", "kind": "synthesis", "title": "知识页",
         "body": "|a|b|\n|-|-|\n|1|2|\n\n$x^2$"}]})
    oid = made["refs"]["a"]["id"]
    workbench = WikiWorkbench()
    asyncio.run(workbench.select("page", oid))
    asyncio.run(workbench.search())
    with ui.column() as container:
        workbench.render_middle()
        workbench.render_right()
        workbench.edit_page(deepcopy(workbench.detail["record"]))
        workbench.edit_evidence(oid)
        workbench.mode = "review"
        workbench.checks = service.lint()
        workbench.render_review()
        assert container.descendants() is not None
    container.delete()


def test_rebuild_index_reports_no_file_rewrite(knowledge_base):
    from indexing.services import wiki_service as service
    service.apply({"request_key": str(uuid4()), "reason": "test",
                   "pages": [{"ref": "a", "kind": "concept", "title": "重建页", "body": "正文"}]})
    result = service.rebuild_index()
    assert result["committed"] and result["rewrote_files"] is False
    assert result["index_status"] in ("current", "stale")


def test_page_links_and_backlinks_render_as_internal_navigation(knowledge_base, monkeypatch):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    target = str(uuid4())
    broken = str(uuid4())
    workbench = WikiWorkbench()
    workbench.detail = {"library_id": str(uuid4()), "kind": "page",
                        "record": {"id": str(uuid4()), "title": "页面", "kind": "concept", "status": "active",
                                   "revision": 1, "content_hash": "a" * 64, "body": "", "aliases": []},
                        "evidence": {"items": [], "total": 0, "limit": 25, "offset": 0}}
    # 服务返回导航对象：出链取 target_id，反向链接取 source_id，exists=False 表示断链。
    workbench.links = {"items": [{"source_id": workbench.detail["record"]["id"], "target_id": target,
                                  "title": "目标页", "exists": True},
                                 {"source_id": workbench.detail["record"]["id"], "target_id": broken,
                                  "title": None, "exists": False}],
                       "total": 2, "limit": 25, "offset": 0}
    workbench.backlinks = {"items": [], "total": 0, "limit": 25, "offset": 0}
    with ui.column() as container:
        workbench.render_page_links()
        labels = [getattr(e, "text", "") for e in container.descendants()]
        assert "目标页" in labels
        assert any(t("wiki.link_missing") in label for label in labels if isinstance(label, str))
    container.delete()


@pytest.mark.parametrize("navigation", ["links", "backlinks"])
def test_link_navigation_can_page_without_evidence(knowledge_base, navigation):
    from nicegui import ui
    from app.ui.views.wiki_view import WikiWorkbench
    from indexing.services import wiki_service as service

    root = service.apply({"request_key": str(uuid4()), "reason": "链接分页回归", "pages": [
        {"ref": "root", "kind": "topic", "title": "目标页", "body": ""}]})["pages"][0]["id"]
    linked = []
    for start, stop in ((0, 20), (20, 30)):
        made = service.apply({"request_key": str(uuid4()), "reason": "链接分页回归", "pages": [
            {"ref": str(i), "kind": "topic", "title": f"链接页{i}",
             "body": f"[目标](piece://wiki/{root})" if navigation == "backlinks" else ""}
            for i in range(start, stop)]})
        linked.extend(item["id"] for item in made["pages"])
    if navigation == "links":
        record = service.get_record(kind="page", id=root)["record"]
        service.apply({"request_key": str(uuid4()), "reason": "出链分页回归", "pages": [
            {"id": root, "expected_revision": record["revision"], "expected_content_hash": record["content_hash"],
             "body": "\n".join(f"[链接](piece://wiki/{ident})" for ident in linked)}]})
    view = WikiWorkbench()
    container = ui.column()

    async def scenario():
        await view.select("page", root)
        assert view.detail["evidence"]["total"] == 0
        nav = getattr(view, navigation)
        assert len(nav["items"]) == 25 and nav["total"] == 30
        key = "target_id" if navigation == "links" else "source_id"
        first = {item[key] for item in nav["items"]}
        pagers = []
        view.pager = lambda offset, limit, total, callback: pagers.append((offset, limit, total, callback))
        with container:
            view.render_page_links()
        container.delete()
        _, _, _, advance = next(pager for pager in pagers if pager[2] == 30)
        await advance(1)
        nav = getattr(view, navigation)
        assert nav["offset"] == 25 and len(nav["items"]) == 5
        assert first | {item[key] for item in nav["items"]} == set(linked)
        await view.poll()
        assert getattr(view, navigation)["offset"] == 25

    asyncio.run(scenario())
