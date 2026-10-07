"""原始资料删除分别预览 Wiki 与图谱引用，不跨功能清理内容。"""
from indexing.database import get_db_cursor
from indexing.services import chunk_service, file_service, maintenance_service
from indexing.services import knowledge_service as graph
from indexing.services import wiki_service as wiki


def test_source_deletion_counts_and_preserves_both_features(knowledge_base):
    source = file_service.create_empty_file("共同来源.md")
    file_id = source["file_id"]
    chunk_service.create_chunk_add_task(file_id, "来源", "共同引用原文", request_key="source")
    knowledge_base.drain()
    with get_db_cursor() as cursor:
        chunk_id = cursor.execute("SELECT id FROM chunks WHERE file_id=?", (file_id,)).fetchone()[0]
    evidence = {"source_kind": "piece", "source_library_id": graph.library_id(),
                "source_file_id": file_id, "source_chunk_id": chunk_id, "quote": "共同引用原文"}
    page = wiki.apply({"request_key": "wiki", "reason": "创建页面", "pages": [
        {"ref": "item", "kind": "topic", "title": "共同名称", "body": "页面正文"}],
        "evidence": [{**evidence, "page": {"ref": "item"}}]})
    entity = graph.apply({"request_key": "graph", "reason": "创建实体", "objects": [
        {"ref": "item", "kind": "entity", "title": "共同名称"}],
        "evidence": [{**evidence, "object": {"ref": "item"}}]})
    preview = maintenance_service.delete_files([file_id], dry_run=True)
    assert preview["wiki_evidence_count"] == preview["graph_evidence_count"] == 1
    assert preview["knowledge_evidence_count"] == 2
    assert preview["retains_knowledge_snapshots"] is True
    maintenance_service.delete_files([file_id], confirmed=True)
    page_record = wiki.get_record(kind="page", id=page["pages"][0]["id"])
    entity_record = graph.get_record(kind="object", id=entity["objects"][0]["id"])
    assert page_record["record"]["body"] == "页面正文"
    for record in (page_record, entity_record):
        assert record["evidence"]["items"][0]["location_status"] == "missing"
