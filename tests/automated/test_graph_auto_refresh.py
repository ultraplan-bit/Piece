"""图谱自动更新：外部写入、只读差异刷新、分页及迟到请求。"""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from indexing.services import knowledge_service as service


def apply(**parts):
    return service.apply({"request_key": str(uuid4()), "reason": "自动更新回归", **parts})


def remove(kind, ident, revision=None):
    payload = {"kind": kind, "id": ident}
    if revision is not None:
        payload["expected_revision"] = revision
    preview = service.delete(payload)
    service.delete({**payload, "request_key": str(uuid4()), "impact_token": preview["impact_token"],
                    "dry_run": False, "confirmed": True})


def workbench():
    from app.ui.views.graph_view import GraphWorkbench
    view = GraphWorkbench()
    view.refresh_list = Mock()
    view.refresh_detail = Mock()
    return view


def test_external_changes_update_list_detail_relations_and_evidence_only_when_changed(knowledge_base):
    view = workbench()

    async def scenario():
        await view.poll()
        assert view.results["total"] == 0
        result = apply(objects=[{"ref": "a", "kind": "concept", "title": "外部节点", "summary": "旧摘要"}])
        oid = result["refs"]["a"]["id"]
        await view.poll()
        assert view.results["objects"][0]["id"] == oid
        await view.select("object", oid)
        view.refresh_list.reset_mock()
        view.refresh_detail.reset_mock()
        await view.poll()
        view.refresh_list.assert_not_called()
        view.refresh_detail.assert_not_called()

        area = SimpleNamespace(is_deleted=False, scroll_to=Mock())
        view._scroll_areas["detail"] = area
        view._scroll_positions["detail"] = 240
        apply(objects=[{"id": oid, "expected_revision": 1, "summary": "外部更新摘要"}])
        await view.poll()
        assert view.detail["record"]["summary"] == "外部更新摘要"
        assert view.detail["record"]["revision"] == 2
        area.scroll_to.assert_called_with(pixels=240)

        # 单独增加引用不会提高对象 revision，仍必须刷新证据。
        evidence = apply(evidence=[{"object": {"id": oid}, "source_kind": "user", "quote": "外部证据"}])
        await view.poll()
        assert view.detail["record"]["revision"] == 2
        assert view.detail["evidence"]["total"] == 1
        linked = apply(objects=[{"ref": "b", "kind": "entity", "title": "关联节点"}],
                       relations=[{"ref": "r", "source": {"id": oid}, "target": {"ref": "b"},
                                   "predicate": "related_to", "description": "外部关系", "basis": "inference"}])
        await view.poll()
        assert view.local_relations["edges"][0]["id"] == linked["relations"][0]["id"]
        remove("evidence", evidence["evidence"][0]["id"])
        await view.poll()
        assert view.detail["evidence"]["total"] == 0
        remove("object", oid, 2)
        await view.poll()
        assert view.detail is None and view.selected_object is None and view.error
        assert all(item["id"] != oid for item in view.results["objects"])

    asyncio.run(scenario())


def test_source_changes_are_detected_without_a_knowledge_revision(knowledge_base):
    from indexing.services import file_service, chunk_service
    fid = file_service.create_empty_file("证据源")["file_id"]
    chunk_service.create_chunk_add_task(fid, "来源", "原始引文")
    knowledge_base.drain()
    chunk = file_service.get_chunks_paginated(fid)["chunks"][0]
    library = service.list_objects()["library_id"]
    result = apply(objects=[{"ref": "a", "kind": "concept", "title": "引用节点"}], evidence=[{
        "object": {"ref": "a"}, "source_kind": "piece", "source_library_id": library,
        "source_file_id": fid, "source_chunk_id": chunk["id"], "quote": "原始引文"}])
    view = workbench()
    asyncio.run(view.select("object", result["refs"]["a"]["id"]))
    assert view.detail["evidence"]["items"][0]["location_status"] == "current"
    chunk_service.create_chunk_update_task(chunk["id"], "修改后的引文")
    knowledge_base.drain()
    asyncio.run(view.poll())
    assert view.detail["record"]["revision"] == 1
    assert view.detail["evidence"]["items"][0]["location_status"] == "changed"


def test_poll_preserves_applied_filters_and_moves_off_deleted_last_page(knowledge_base):
    for start, stop in ((0, 20), (20, 26)):
        apply(objects=[{"ref": str(i), "kind": "concept", "title": f"保留范围 {i}"} for i in range(start, stop)])
    apply(objects=[{"ref": "x", "kind": "entity", "title": "保留范围外实体"}])
    view = workbench()

    async def scenario():
        view.kind, view.query = "concept", "保留范围"
        await view.search()
        await view.list_page(1)
        last = view.results["objects"][0]
        assert view.offset == 25 and view.results["total"] == 26
        # 输入框的草稿不应被后台轮询自动提交。
        view.query = "还没按搜索"
        await view.poll()
        assert view.offset == 25 and view.results["total"] == 26
        remove("object", last["id"], last["revision"])
        await view.poll()
        assert view.offset == 0 and view.results["total"] == 25
        assert len(view.results["objects"]) == 25
        assert view.kind == "concept" and view.query == "还没按搜索"

    asyncio.run(scenario())


def test_late_poll_cannot_overwrite_a_new_search_or_overlap(knowledge_base, monkeypatch):
    from app.ui.views import graph_view
    apply(objects=[{"ref": "a", "kind": "entity", "title": "新查询"}])
    view = workbench()

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def io_bound(function, **kwargs):
            calls.append(function)
            if function is service.list_objects:
                entered.set()
                await release.wait()
            return function(**kwargs)

        monkeypatch.setattr(graph_view.run, "io_bound", io_bound)
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
    from app.ui.views import graph_view
    result = apply(objects=[{"ref": ref, "kind": "concept", "title": ref} for ref in ("a", "b")])
    a, b = (result["refs"][ref]["id"] for ref in ("a", "b"))
    view = workbench()

    async def scenario():
        await view.select("object", a)
        entered, release = asyncio.Event(), asyncio.Event()

        async def io_bound(function, **kwargs):
            if function is service.get_record and kwargs["id"] == a:
                entered.set()
                await release.wait()
            return function(**kwargs)

        monkeypatch.setattr(graph_view.run, "io_bound", io_bound)
        old = asyncio.create_task(view.poll())
        await entered.wait()
        await view.select("object", b)
        release.set()
        await old
        assert view.detail["record"]["id"] == b
        assert view.selected_object == b

    asyncio.run(scenario())


def test_failed_poll_keeps_data_and_stopped_view_does_not_query(knowledge_base, monkeypatch):
    from app.ui.views import graph_view
    view = workbench()
    asyncio.run(view.poll())
    previous = view.results

    async def fail(*args, **kwargs):
        raise RuntimeError("temporary read failure")

    original = graph_view.run.io_bound
    monkeypatch.setattr(graph_view.run, "io_bound", fail)
    asyncio.run(view.poll())
    assert view.results is previous and not view._polling
    monkeypatch.setattr(graph_view.run, "io_bound", original)
    apply(objects=[{"ref": "new", "kind": "concept", "title": "恢复后可见"}])
    view._poll_timer = SimpleNamespace(is_deleted=True)
    asyncio.run(view.poll())
    assert view.results is previous
    view._poll_timer = None
    asyncio.run(view.poll())
    assert view.results["total"] == 1
