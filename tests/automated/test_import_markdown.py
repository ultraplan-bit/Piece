"""只传正文的导入入口：属性并入原件 frontmatter，与本地文件导入共用查重和任务规则。"""

from pathlib import Path

import pytest

from indexing.services import file_service as files, task_service as tasks, metadata_service
from indexing.services.errors import BusinessError


def test_import_markdown_keeps_properties_in_original(knowledge_base):
    content = "---\ntitle: 原标题\ntags: [a]\n---\n\n# 正文\n\n第一段"
    properties = {"source_url": "https://example.com/a", "title": "显式标题"}

    accepted = files.import_markdown("网页文章", content, metadata=properties)

    assert accepted["filename"] == "网页文章.md" and accepted["status"] == "accepted"
    file_id = accepted["file_id"]
    # 显式属性优先，frontmatter 里其余属性保留，登记时即可见
    assert metadata_service.get_file_metadata(file_id) == {
        "title": "显式标题", "tags": ["a"], "source_url": "https://example.com/a",
    }
    original = Path(files.get_file_by_id(file_id)["original_file_path"]).read_text(encoding="utf-8")
    assert original.startswith("---\n") and "source_url: https://example.com/a" in original
    assert original.rstrip().endswith("第一段")

    knowledge_base.drain()
    assert tasks.get_task(accepted["task_id"])["status"] == "completed"
    assert metadata_service.get_file_metadata(file_id)["source_url"] == "https://example.com/a"
    chunk_texts = [chunk["chunk_text"] for chunk in files.get_chunks_by_file_id(file_id)]
    assert chunk_texts and all("source_url" not in text for text in chunk_texts)

    again = files.import_markdown("网页文章", content, metadata=properties)
    assert again["duplicate"] and again["file_id"] == file_id


def test_import_markdown_rejects_bad_input_and_cleans_up(knowledge_base):
    with pytest.raises(BusinessError, match="正文不能为空"):
        files.import_markdown("空", "   ")
    with pytest.raises(BusinessError, match="不能超过"):
        files.import_markdown("超长", "字" * (files.MAX_MARKDOWN_CHARS + 1))
    with pytest.raises(BusinessError, match="JSON 对象"):
        files.import_markdown("超限", "正文", metadata={"x": "y" * 30000})
    # 登记阶段失败时，临时正文、原件副本和工作文件都不能残留
    with pytest.raises(BusinessError, match="集合不存在"):
        files.import_markdown("归类失败", "正文", collection_ids=[999])
    assert not list(files.get_files_dir().glob(".content-*"))
    assert not list(files.get_originals_dir().glob("归类失败*")) and not list(files.get_working_dir().glob("归类失败*"))
    assert files.get_files_list() == []
