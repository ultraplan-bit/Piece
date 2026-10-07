"""Wiki 展示逻辑与共享工作台基础设施回归（不依赖后端服务，可独立运行）。"""
from uuid import uuid4

import pytest

from app.ui.views.knowledge_common import PendingWrite, local_source, safe_external_url
from app.ui.views.wiki_presenter import KINDS, safe_destination, safe_wiki_html


def test_wiki_kinds_are_page_types():
    assert KINDS == ("concept", "entity", "topic", "synthesis", "source_summary")


def test_wiki_internal_links_use_piece_wiki_uuid():
    ident = uuid4()
    assert safe_destination("piece://wiki/" + str(ident)) == ("wiki", str(ident))
    assert safe_destination("piece://wiki/" + str(ident).upper()) == ("wiki", str(ident))
    assert safe_destination("https://example.com/?a=1")[0] == "external"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///C:/secret", "//example.com", "/working/private",
                                  "https://user:secret@example.com", "https://example.com:bad", "https://x/\\evil",
                                  "https://x/\n", "piece://wiki/../../secret", "piece://wiki/not-a-uuid",
                                  # 旧的知识对象内链不再是 Wiki 页面链接。
                                  "piece://knowledge/" + str(uuid4())])
def test_unsafe_or_legacy_destinations_rejected(url):
    assert safe_destination(url) is None


def test_safe_wiki_html_keeps_only_internal_and_external_links():
    ident = uuid4()
    source = ('<script>alert(1)</script><img src=x onerror=alert(2)>'
              '<a href="javascript:alert(3)" onclick="evil()">bad</a>'
              f'<a href="piece://wiki/{ident}">内链</a>'
              '<a href="https://example.com/?a=1&amp;b=2">外链</a>')
    safe = safe_wiki_html(source)
    assert "<script" not in safe and "<img" not in safe and "onclick" not in safe and "onerror" not in safe
    assert "javascript:" not in safe and 'href="piece:' not in safe
    assert f'data-wiki-id="{ident}"' in safe
    assert 'rel="noopener noreferrer"' in safe


def test_external_url_only_accepts_clean_http():
    assert safe_external_url("https://example.com/a") == "https://example.com/a"
    assert safe_external_url("https://user:secret@example.com") is None
    assert safe_external_url("javascript:alert(1)") is None
    assert safe_external_url("https://x/\\evil") is None


def test_cross_library_and_missing_sources_never_open_local_integer_ids():
    library = str(uuid4())
    evidence = {"source_kind": "piece", "source_library_id": library, "source_file_id": 1,
                "source_chunk_id": 2, "location_status": "current"}
    assert local_source(evidence, library) == (1, 2)
    assert local_source(evidence, str(uuid4())) is None
    for status in ("missing", "unresolved", "unverified"):
        assert local_source({**evidence, "location_status": status}, library) is None
    assert local_source({**evidence, "source_file_id": True}, library) is None


def test_pending_request_retains_original_payload_and_key():
    draft = {"reason": "人工修订", "pages": [{"id": str(uuid4()), "expected_revision": 1,
                                              "expected_content_hash": "a" * 64, "body": "草稿"}]}
    request = PendingWrite(draft)
    key = request.key
    draft["pages"][0]["body"] = "后续修改"
    request.uncertain = True
    assert request.key == key and request.payload["pages"][0]["body"] == "草稿"
    assert request.payload["pages"][0]["expected_content_hash"] == "a" * 64
