"""正式离线回归：只使用临时知识库和确定性嵌入替身。"""

import asyncio
import importlib
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def isolated_entry_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PIECE_DATA_DIR", str(tmp_path / "uninitialized"))
    monkeypatch.setenv("NICEGUI_STORAGE_PATH", str(tmp_path / "gui-storage"))
    monkeypatch.delenv("PIECE_API_KEY", raising=False)


@pytest.fixture
def knowledge_base(tmp_path, monkeypatch):
    from indexing import settings, database
    from indexing.services import chunk_service, processor, embedding_client
    monkeypatch.setenv("PIECE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEFAULT_DATA_PATH", tmp_path)
    config = settings.AppSettings(data_path=str(tmp_path))
    config.embedding.vector_dim = 2
    config.embedding.api_key = "test-embedding-key"
    config.mcp.api_key = "read-test-key"
    config.mcp.index_api_key = "write-test-key"
    monkeypatch.setattr(settings, "_settings", config)
    assert settings.save_settings(config)
    database.close_connection_pool()
    database.init_database()
    database.init_connection_pool()

    class Embeddings:
        async def aembed_documents(self, texts):
            return [[1.0, 0.0] for _ in texts]

        def embed_query(self, text):
            return [1.0, 0.0]

        async def aembed_query(self, text):
            return [1.0, 0.0]

    model = Embeddings()
    for module in (chunk_service, processor, embedding_client,
                   importlib.import_module("retrieval.nodes.vector_search_node")):
        monkeypatch.setattr(module, "get_embeddings_model", lambda: model)
    monkeypatch.setattr(processor, "convert_to_markdown", lambda path, image_dir: path.read_text(encoding="utf-8"))

    def drain():
        from indexing.services import task_service
        async def process():
            while task := task_service.claim_next_pending_task():
                await processor.TaskProcessor()._dispatch_task(task, threading.Event())
        asyncio.run(process())

    yield SimpleNamespace(path=tmp_path, settings=config, model=model, drain=drain)
    database.close_connection_pool()


@pytest.fixture
def api(knowledge_base):
    from fastapi.testclient import TestClient
    from app.api import create_api, identity
    from app.runtime import BootstrapTokens
    opened = []
    runtime = SimpleNamespace(port=8689, ready=True, with_gui=False, with_mcp=False,
                              database_path=knowledge_base.settings.get_db_path().resolve(),
                              status=lambda: {"components": {"worker": "ready"}},
                              bootstrap_tokens=BootstrapTokens(ttl=60),
                              open_window=lambda url: opened.append(url))
    client = TestClient(create_api(runtime), base_url="http://127.0.0.1:8689")
    client.headers.update({"Authorization": f"Bearer {knowledge_base.settings.api.admin_key}",
                           "X-Piece-Target": identity(runtime)["target_id"]})
    with client:
        yield client
