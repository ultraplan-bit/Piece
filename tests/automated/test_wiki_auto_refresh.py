"""Wiki 自动更新：外部写入 Markdown、内容哈希变化、分页及迟到请求。"""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from indexing.services import wiki_service as service


def apply(**parts):
    return service.apply({"request_key": str(uuid4()), "reason": "自动更新回归", **parts})


def remove(kind, ident, revision=None, content_hash=None):
    payload = {"kind": kind, "id": ident}
    if revision is not None:
        payload["expected_revision"] = revision
    if content_hash is not None:
        payload["expected_content_hash"] = content_hash
    preview = service.delete(payload)
    service.delete({**payload, "request_key": str(uuid4()), "impact_token": preview["impact_token"],
                    "dry_run": False, "confirmed": True})


def workbench():
    from app.ui.views.wiki_view import WikiWorkbench
    view = WikiWorkbench()
    view.refresh_list = Mock()
    view.refresh_detail = Mock()
    return view


def test_external_changes_update_list_detail_and_evidence_only_when_changed(knowledge_base):
    view = workbench()

    async def scenario():
        await view.poll()
        assert view.results["total"] == 0
        result = apply(pages=[{"ref": "a", "kind": "topic", "title": "外部页面", "body": "旧正文"}])
        oid = result["refs"]["a"]["id"]
        await view.poll()
        assert view.results["pages"][0]["id"] == oid
        await view.select("page", oid)
        record = service.get_record(kind="page", id=oid)["record"]
        view.refresh_list.reset_mock()
        view.refresh_detail.reset_mock()
        await view.poll()
        view.refresh_list.assert_not_called()
        view.refresh_detail.assert_not_called()

        area = SimpleNamespace(is_deleted=False, scroll_to=Mock())
        view._scroll_areas["detail"] = area
        view._scroll_positions["detail"] = 240
        apply(pages=[{"id": oid, "expected_revision": record["revision"],
                      "expected_content_hash": record["content_hash"], "body": "外部更新正文"}])
        await view.poll()
        assert view.detail["record"]["body"] == "外部更新正文"
        assert view.detail["record"]["revision"] == 2
        area.scroll_to.assert_called_with(pixels=240)

        updated = service.get_record(kind="page", id=oid)["record"]
        evidence = apply(pages=[{"id": oid, "expected_revision": updated["revision"],
                                 "expected_content_hash": updated["content_hash"]}],
                         evidence=[{"page": {"id": oid}, "source_kind": "user", "quote": "外部证据"}])
        await view.poll()
        assert view.detail["evidence"]["total"] == 1
        remove("evidence", evidence["evidence"][0]["id"],
               content_hash=service.get_record(kind="page", id=oid)["record"]["content_hash"])
        await view.poll()
        assert view.detail["evidence"]["total"] == 0
        current = service.get_record(kind="page", id=oid)["record"]
        remove("page", oid, current["revision"], current["content_hash"])
        await view.poll()
        assert view.detail is None and view.error
        assert all(item["id"] != oid for item in view.results["pages"])

    asyncio.run(scenario())


def test_source_changes_are_detected_without_a_page_revision(knowledge_base):
    from indexing.services import file_service, chunk_service
    fid = file_service.create_empty_file("证据源")["file_id"]
    chunk_service.create_chunk_add_task(fid, "来源", "原始引文")
    knowledge_base.drain()
    chunk = file_service.get_chunks_paginated(fid)["chunks"][0]
    library = service.list_pages()["library_id"]
    result = apply(pages=[{"ref": "a", "kind": "concept", "title": "引用页面", "body": "正文"}])
    oid = result["refs"]["a"]["id"]
    record = service.get_record(kind="page", id=oid)["record"]
    apply(pages=[{"id": oid, "expected_revision": record["revision"],
                  "expected_content_hash": record["content_hash"]}],
          evidence=[{"page": {"id": oid}, "source_kind": "piece", "source_library_id": library,
                     "source_file_id": fid, "source_chunk_id": chunk["id"], "quote": "原始引文"}])
    view = workbench()
    asyncio.run(view.select("page", oid))
    assert view.detail["evidence"]["items"][0]["location_status"] == "current"
    chunk_service.create_chunk_update_task(chunk["id"], "修改后的引文")
    knowledge_base.drain()
    before = view.detail["record"]["revision"]
    asyncio.run(view.poll())
    assert view.detail["record"]["revision"] == before
    assert view.detail["evidence"]["items"][0]["location_status"] == "changed"


def test_poll_preserves_applied_filters_and_moves_off_deleted_last_page(knowledge_base):
    for start, stop in ((0, 20), (20, 26)):
        apply(pages=[{"ref": str(i), "kind": "concept", "title": f"保留范围 {i}", "body": ""}
                     for i in range(start, stop)])
    apply(pages=[{"ref": "x", "kind": "entity", "title": "保留范围外实体", "body": ""}])
    view = workbench()

    async def scenario():
        view.kind, view.query = "concept", "保留范围"
        await view.search()
        await view.list_page(1)
        last = view.results["pages"][0]
        assert view.offset == 25 and view.results["total"] == 26
        view.query = "还没按搜索"
        await view.poll()
        assert view.offset == 25 and view.results["total"] == 26
        current = service.get_record(kind="page", id=last["id"])["record"]
        remove("page", last["id"], current["revision"], current["content_hash"])
        await view.poll()
        assert view.offset == 0 and view.results["total"] == 25
        assert len(view.results["pages"]) == 25
        assert view.kind == "concept" and view.query == "还没按搜索"

    asyncio.run(scenario())


def test_late_poll_cannot_overwrite_a_new_search_or_overlap(knowledge_base, monkeypatch):
    from app.ui.views import wiki_view
    apply(pages=[{"ref": "a", "kind": "entity", "title": "新查询", "body": ""}])
    view = workbench()

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def io_bound(function, **kwargs):
            calls.append(function)
            if function is service.list_pages:
                entered.set()
                await release.wait()
            return function(**kwargs)

        monkeypatch.setattr(wiki_view.run, "io_bound", io_bound)
        old = asyncio.create_task(view.poll())
        await entered.wait()
        await view.poll()
        assert len(calls) == 1
        view.query = "新查询"
        await view.search()
        expected = deepcopy(view.results)
        release.set()
        await old
        assert view.results == expected
        assert view._search_params["query"] == "新查询"

    asyncio.run(scenario())


def test_late_detail_cannot_overwrite_selection(knowledge_base, monkeypatch):
    from app.ui.views import wiki_view
    result = apply(pages=[{"ref": ref, "kind": "topic", "title": ref, "body": ""} for ref in ("a", "b")])
    a, b = (result["refs"][ref]["id"] for ref in ("a", "b"))
    view = workbench()

    async def scenario():
        await view.select("page", a)
        entered, release = asyncio.Event(), asyncio.Event()

        async def io_bound(function, **kwargs):
            if function is service.get_record and kwargs["id"] == a:
                entered.set()
                await release.wait()
            return function(**kwargs)

        monkeypatch.setattr(wiki_view.run, "io_bound", io_bound)
        old = asyncio.create_task(view.poll())
        await entered.wait()
        await view.select("page", b)
        release.set()
        await old
        assert view.detail["record"]["id"] == b

    asyncio.run(scenario())


def test_failed_poll_keeps_data_and_stopped_view_does_not_query(knowledge_base, monkeypatch):
    from app.ui.views import wiki_view
    view = workbench()
    asyncio.run(view.poll())
    previous = view.results

    async def fail(*args, **kwargs):
        raise RuntimeError("temporary read failure")

    original = wiki_view.run.io_bound
    monkeypatch.setattr(wiki_view.run, "io_bound", fail)
    asyncio.run(view.poll())
    assert view.results is previous and not view._polling
    monkeypatch.setattr(wiki_view.run, "io_bound", original)
    apply(pages=[{"ref": "new", "kind": "concept", "title": "恢复后可见", "body": ""}])
    view._poll_timer = SimpleNamespace(is_deleted=True)
    asyncio.run(view.poll())
    assert view.results is previous
    view._poll_timer = None
    asyncio.run(view.poll())
    assert view.results["total"] == 1


@pytest.mark.parametrize("damage", ["missing", "invalid"])
@pytest.mark.parametrize("refresh", ["poll", "select"])
def test_unreadable_selected_file_drops_stale_body(knowledge_base, monkeypatch, damage, refresh):
    from nicegui import ui
    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    made = apply(pages=[{"ref": "a", "kind": "topic", "title": "文件失效", "body": "不应继续展示的旧正文"}])
    ident = made["pages"][0]["id"]
    view = workbench()

    async def scenario():
        await view.select("page", ident)
        path = service.wiki_path() / view.detail["record"]["path"]
        original = path.read_bytes()
        if damage == "missing":
            path.unlink()
        else:
            path.write_text("---\nnot valid JSON\n---\n损坏文件", encoding="utf-8")
        if refresh == "poll":
            await view.poll()
        else:
            await view.select("page", ident)
        assert view.detail is None and view.links is None and view.backlinks is None
        assert view.error
        path.write_bytes(original)
        await view.select("page", ident)
        assert view.detail["record"]["body"] == "不应继续展示的旧正文" and view.error is None

    asyncio.run(scenario())
