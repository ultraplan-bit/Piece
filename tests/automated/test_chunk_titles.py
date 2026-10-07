"""标题只影响工作副本，正文、向量和来源定位保持一致。"""

import pytest

from indexing import database
from indexing.repositories import ChunkRepository
from indexing.services import chunk_service as chunks, file_service as files, task_service as tasks
from indexing.services.chunking.utils import build_chunk
from indexing.utils import serialize_float32


def _import(knowledge_base, content):
    source = knowledge_base.path / "标题回归.md"
    source.write_text(content, encoding="utf-8")
    accepted = files.import_file(source)
    knowledge_base.drain()
    assert tasks.get_task(accepted["task_id"])["status"] == "completed"
    return accepted["file_id"], source


def _reindex(knowledge_base, file_id):
    accepted = files.reindex_file(file_id, source="working")
    knowledge_base.drain()
    assert tasks.get_task(accepted["task_id"])["status"] == "completed"
    return files.get_chunks_by_file_id(file_id)


def _embedding(chunk_id):
    with database.get_db_cursor() as cursor:
        return cursor.execute("SELECT embedding FROM chunks WHERE id=?", (chunk_id,)).fetchone()[0]


@pytest.mark.parametrize("mode", ["title_only", "title_and_body"])
@pytest.mark.parametrize("title", ["新标题", "自定义_新标题（v2）!"])
@pytest.mark.parametrize("content", [
    "## 旧标题\n\n正文\n\n### 内部子标题\n\n- 内容\n",
    "# 原文标题\n\n正文\n",
    "### 原文小节\n\n正文\n",
    "没有标题的正文\n\n第二段\n",
    "#标签不是标题\n\n正文\n",
])
def test_title_survives_export_and_working_reindex(knowledge_base, monkeypatch, mode, title, content):
    file_id, source = _import(knowledge_base, content)
    original = files.get_chunks_by_file_id(file_id)
    assert len(original) == 1
    chunk = original[0]
    old_vector = _embedding(chunk["id"])
    embedded = []

    async def embed(texts):
        embedded.extend(texts)
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(knowledge_base.model, "aembed_documents", embed)
    expected_body = chunk["chunk_text"]
    if mode == "title_only":
        chunks.update_chunk_title(chunk["id"], title)
    else:
        expected_body += "\n\n明确提交的新正文"
        task_id = chunks.create_chunk_update_task(chunk["id"], expected_body, title, request_key="rename")
        knowledge_base.drain()
        assert tasks.get_task(task_id)["status"] == "completed"
        assert chunks.create_chunk_update_task(chunk["id"], expected_body, title, request_key="rename") == task_id
    updated = chunks.get_chunk_by_id(chunk["id"])
    assert updated["doc_title"] == title and updated["chunk_text"] == expected_body
    assert updated["heading_path"] == chunk["heading_path"]
    assert updated["heading_level"] == chunk["heading_level"]
    if mode == "title_only":
        assert embedded == [] and _embedding(chunk["id"]) == old_vector
    else:
        assert embedded == [expected_body]
    working = files.export_file(file_id).read_text(encoding="utf-8")
    assert "新标题" in working
    if content.startswith("## 旧标题"):
        assert "旧标题" not in working
        assert "### 内部子标题\n\n- 内容" in working
        assert working.count("\n### 内部子标题") == 1
        assert updated["chunk_text"].startswith("## 旧标题")
    elif content.startswith("### "):
        assert working.startswith(f"### {title.split('_')[-1]}\n")
        assert expected_body.partition("\n")[2] in working
    else:
        assert expected_body in working
    assert source.read_text(encoding="utf-8") == content
    keys = ("doc_title", "chunk_text", "heading_path", "heading_level")
    before = [{key: updated[key] for key in keys}]
    for _ in range(2):
        rebuilt = _reindex(knowledge_base, file_id)
        assert [{key: row[key] for key in keys} for row in rebuilt] == before
        assert files.export_file(file_id).read_text(encoding="utf-8") == working
    assert embedded[-2:] == [expected_body] * 2


def test_rename_keeps_sibling_markdown_and_heading_delimiters(knowledge_base):
    content = "## 旧标题 ###\n\n正文\n\n### 内部标题\n内容\n\n## **未编辑标题**\n\n`代码` 与 [链接](https://example.invalid)\n"
    file_id, _ = _import(knowledge_base, content)
    first, sibling = files.get_chunks_by_file_id(file_id)
    chunks.update_chunk_title(first["id"], "新标题")
    working = files.export_file(file_id).read_text(encoding="utf-8")
    assert working.startswith("## 新标题 ###\n") and "旧标题" not in working
    assert sibling["chunk_text"] in working
    assert chunks.get_chunk_by_id(first["id"])["chunk_text"] == first["chunk_text"]
    rebuilt = _reindex(knowledge_base, file_id)
    assert len(rebuilt) == 2 and rebuilt[0]["doc_title"] == "新标题"
    assert rebuilt[0]["chunk_text"] == first["chunk_text"]
    assert rebuilt[1]["doc_title"] == sibling["doc_title"]
    assert rebuilt[1]["chunk_text"] == sibling["chunk_text"]


@pytest.mark.parametrize("mode", ["title_only", "title_and_body"])
def test_pdf_title_keeps_original_heading_and_source_page(knowledge_base, mode):
    file_id = files.create_empty_file("报告")["file_id"]
    with database.get_db_cursor(write=True) as cursor:
        cursor.execute("UPDATE files SET original_file_type='pdf' WHERE id=?", (file_id,))
    original = build_chunk(["报告", "第2页", "第1部分"], "## 原页真实标题\n\n原页正文\n\n### 内部标题\n内容")
    chunk_id = ChunkRepository().insert(file_id, embedding=serialize_float32([0.0, 0.0]), **original)
    expected_body = original["chunk_text"]
    if mode == "title_only":
        chunks.update_chunk_title(chunk_id, "新标题")
        assert _embedding(chunk_id) == serialize_float32([0.0, 0.0])
    else:
        expected_body += "\n\n用户补充"
        task_id = chunks.create_chunk_update_task(chunk_id, expected_body, "新标题")
        knowledge_base.drain()
        assert tasks.get_task(task_id)["status"] == "completed"
    current = chunks.get_chunk_by_id(chunk_id)
    assert current["heading_path"] == original["heading_path"]
    assert current["heading_level"] == original["heading_level"]
    assert current["chunk_text"] == expected_body
    working = files.export_file(file_id).read_text(encoding="utf-8")
    assert working.startswith("## 新标题\n\n") and expected_body in working
    rebuilt = _reindex(knowledge_base, file_id)
    assert len(rebuilt) == 1 and rebuilt[0]["doc_title"] == "新标题"
    assert rebuilt[0]["heading_path"] == original["heading_path"]
    assert rebuilt[0]["heading_level"] == original["heading_level"]
    assert rebuilt[0]["chunk_text"] == expected_body


def test_external_working_edit_is_parsed_instead_of_reusing_cards(knowledge_base):
    file_id, _ = _import(knowledge_base, "## 旧标题\n\n旧正文")
    chunk = files.get_chunks_by_file_id(file_id)[0]
    chunks.update_chunk_title(chunk["id"], "新标题")
    working = files.export_file(file_id)
    working.write_text(working.read_text(encoding="utf-8").replace("旧正文", "外部改稿正文"), encoding="utf-8")
    rebuilt = _reindex(knowledge_base, file_id)
    assert len(rebuilt) == 1 and "新标题" in rebuilt[0]["doc_title"]
    assert "外部改稿正文" in rebuilt[0]["chunk_text"] and "旧正文" not in rebuilt[0]["chunk_text"]
    working = files.export_file(file_id)
    working.write_text("## 外部标题\n\n外部正文\n\n## 外部第二节\n\n第二段", encoding="utf-8")
    rebuilt = _reindex(knowledge_base, file_id)
    assert len(rebuilt) == 2 and all("新标题" not in row["doc_title"] for row in rebuilt)
    assert "外部第二节" in rebuilt[1]["doc_title"]


def test_failed_working_write_recovers_edited_heading(knowledge_base, monkeypatch):
    file_id, _ = _import(knowledge_base, "## 旧标题\n\n正文")
    chunk = files.get_chunks_by_file_id(file_id)[0]
    old_vector = _embedding(chunk["id"])

    def fail(_):
        raise OSError("working unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(chunks, "rebuild_working_file", fail)
        with pytest.raises(OSError, match="working unavailable"):
            chunks.update_chunk_title(chunk["id"], "新标题")
    assert files.get_file_by_id(file_id)["working_dirty"] == 1
    current = chunks.get_chunk_by_id(chunk["id"])
    assert current["chunk_text"] == chunk["chunk_text"] and _embedding(chunk["id"]) == old_vector
    assert current["heading_path"] == chunk["heading_path"]
    chunks.recover_working_files()
    working = files.export_file(file_id).read_text(encoding="utf-8")
    assert "新标题" in working and "旧标题" not in working
    assert files.get_file_by_id(file_id)["working_dirty"] == 0
    rebuilt = _reindex(knowledge_base, file_id)
    assert rebuilt[0]["doc_title"] == "新标题" and rebuilt[0]["chunk_text"] == chunk["chunk_text"]
