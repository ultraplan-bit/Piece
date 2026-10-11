"""浏览器验收入口：真实 Piece UI，Zotero 与嵌入仅使用进程内测试替身。"""

import importlib
import os
import sys
from pathlib import Path

import httpx
import pymupdf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from test_zotero_collaboration import FakeZotero, _attachment, _collection, _item
from indexing.services import chunk_service, embedding_client, processor, zotero_service
vector_search_node = importlib.import_module("retrieval.nodes.vector_search_node")


class Embeddings:
    async def aembed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0]

    async def aembed_query(self, text):
        return [1.0, 0.0]


model = Embeddings()
for module in (chunk_service, embedding_client, processor, vector_search_node):
    module.get_embeddings_model = lambda: model

path = Path(os.environ["PIECE_DATA_DIR"]) / "zotero-paper.pdf"
with pymupdf.open() as document:
    document.new_page().insert_text((72, 72), "Zotero browser acceptance. This is a local research document for indexing verification.")
    document.save(path)

fake = FakeZotero()
fake.item_fields["journalArticle"].extend(["abstractNote", "publicationTitle", "language"])
fake.collections["CLLAAAAA"] = _collection("CLLAAAAA", "研究资料")
fake.add_item(_item("ITEMAAAA", title="Zotero 浏览验收", date="2024", creators=[{"name": "Researcher", "creatorType": "author"}],
                    abstractNote="书目摘要不是论文全文", collections=["CLLAAAAA"]))
fake.add_item(_item("ITEMBBBB", title="没有 PDF 的文献", date="2023"))
fake.add_item(_attachment("ATTAAAAA", "ITEMAAAA", path, title="主文档 PDF"))
fake.add_item(_item("NTEAAAAA", item_type="note", parentItem="ITEMAAAA",
                    note="<p>用户的阅读笔记</p><script>window.zoteroUnsafe = true</script>"))
fake.add_item(_item("ANNAAAAA", item_type="annotation", parentItem="ATTAAAAA", annotationType="highlight",
                    annotationText="选定高亮文本", annotationComment="用户的待核查问题", annotationPageLabel="1"))
fake.order = ["ITEMAAAA", "ITEMBBBB"]
fake.children = {"ITEMAAAA": ["ATTAAAAA", "NTEAAAAA"], "ATTAAAAA": ["ANNAAAAA"]}
fake.fulltext["ATTAAAAA"] = {"content": "可用的索引片段", "coverage": {"indexedPages": 1, "totalPages": 3}}

original_client = zotero_service.ZoteroClient


def local_test_client(*args, **kwargs):
    return original_client(*args, transport=httpx.MockTransport(fake.handler), **kwargs)


zotero_service.ZoteroClient = local_test_client

if __name__ == "__main__":
    from app.cli import main
    raise SystemExit(main())
