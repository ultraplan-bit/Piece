"""新增 wiki/chunk CLI 契约回归：请求文件、读回与失败恢复。

复用 ``test_cli.py`` 的短命 HTTP 替身；不调用模型，也不触碰真实用户数据目录。
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

from app.client import make_envelope, target_id
from test_cli import endpoint, failure, invoke  # noqa: F401  复用 fixture 与调用助手

OBJECT_UUID = "00000000-0000-0000-0000-0000000000a1"
RELATION_UUID = "00000000-0000-0000-0000-0000000000b2"
SOURCE_UUID = "00000000-0000-0000-0000-0000000000c3"
TARGET_UUID = "00000000-0000-0000-0000-0000000000d4"
LIBRARY_UUID = "11111111-1111-1111-1111-111111111111"

# 覆盖 LaTeX 反斜杠、HTML 与多行引文：写盘必须逐字无损。
EVIDENCE = {
    "source_kind": "piece",
    "source_library_id": LIBRARY_UUID,
    "source_file_id": 7,
    "source_chunk_id": 9,
    "expected_content_hash": "a" * 64,
    "quote": "第一行 \\frac{1}{2} 与 <td>表格</td>\n第二行 $ V_{DD} $",
}


# ---------------------------------------------------------------------------
# 共享助手
# ---------------------------------------------------------------------------

def _apply_calls(endpoint):
    return [call for call in endpoint.calls if call["path"] == "/api/v1/knowledge/apply"]


def _get_calls(endpoint):
    return [call for call in endpoint.calls if call["path"] == "/api/v1/knowledge/get"]


def _write_json(path, value):
    path = Path(path)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _evidence_file(tmp_path, value=None, name="evidence.json"):
    return _write_json(tmp_path / name, EVIDENCE if value is None else value)


def _option_value(argv, name):
    return argv[argv.index(name) + 1]


def _fake_apply(handler, payload):
    """回显一次成功提交，形状与真实 knowledge/apply 结果一致。"""
    refs, objects, relations, evidence = {}, [], [], []
    for item in payload.get("objects", []):
        updating = "id" in item
        entry = {"id": item.get("id") or str(uuid.uuid4()),
                 "action": "updated" if updating else "created",
                 "revision": item["expected_revision"] + 1 if updating else 1}
        objects.append(entry)
        if "ref" in item:
            refs[item["ref"]] = {"id": entry["id"], "kind": "object", "revision": entry["revision"]}
    for item in payload.get("relations", []):
        updating = "id" in item
        entry = {"id": item.get("id") or str(uuid.uuid4()),
                 "action": "updated" if updating else "created",
                 "revision": item["expected_revision"] + 1 if updating else 1}
        relations.append(entry)
        if "ref" in item:
            refs[item["ref"]] = {"id": entry["id"], "kind": "relation", "revision": entry["revision"]}
    for _ in payload.get("evidence", []):
        evidence.append({"id": str(uuid.uuid4()), "action": "created"})
    return make_envelope(True, "已提交", {
        "committed": True, "dry_run": bool(payload.get("dry_run")), "refs": refs,
        "objects": objects, "relations": relations, "links": [], "evidence": evidence,
        "library_id": LIBRARY_UUID,
        "counts": {"created": len(objects) + len(relations) + len(evidence), "updated": 0, "reused": 0}})


def _get_route(revision=1):
    def route(handler, payload):
        kind = payload["kind"]
        if kind == "object":
            extra = {"evidence": {"items": [], "total": 1, "limit": 50, "offset": 0}, "has_evidence": True}
            record = {"id": payload["id"], "kind": "concept", "title": "概念", "revision": revision}
        elif kind == "relation":
            extra = {"evidence": {"items": [], "total": 0, "limit": 50, "offset": 0}, "has_evidence": False}
            record = {"id": payload["id"], "revision": revision}
        elif kind == "evidence":
            extra = {}
            record = {"id": payload["id"], "quote": EVIDENCE["quote"], "location_status": "current"}
        else:
            extra = {}
            record = {"id": payload["id"]}
        # 真实 knowledge/get 返回 library_id，读回用它核对库身份。
        return make_envelope(True, "", {"library_id": LIBRARY_UUID, "kind": kind, "record": record, **extra})
    return route


def _extract_route(matches, truncated=False):
    def route(handler, payload):
        return make_envelope(True, "", {"chunk_id": payload["chunk_id"], "file_id": 7,
                                        "matches": matches, "match_count": len(matches),
                                        "truncated": truncated, "total_lines": 10})
    return route


# ---------------------------------------------------------------------------
# 1. chunk extract ID --lines N-M --out path
# ---------------------------------------------------------------------------

def _one_match(evidence=None):
    evidence = EVIDENCE if evidence is None else evidence
    return [{"line_start": 2, "line_end": 3, "quote": evidence["quote"], "evidence": evidence}]


def test_extract_out_saves_verbatim_evidence(endpoint, capsys, tmp_path):
    endpoint.routes["/api/v1/chunk/extract"] = _extract_route(_one_match())
    out = tmp_path / "证据.json"
    code, result, _ = invoke(endpoint, capsys, "chunk", "extract", "9", "--lines", "2-3", "--out", str(out))
    assert code == 0 and result["success"]
    assert endpoint.calls[-1]["path"] == "/api/v1/chunk/extract"
    assert endpoint.calls[-1]["payload"] == {"chunk_id": 9, "lines": "2-3", "max_matches": 20}
    assert out.exists(), "恰好一个匹配时必须落盘"
    assert json.loads(out.read_text(encoding="utf-8")) == EVIDENCE
    # LaTeX 反斜杠、HTML 与多行引文必须无损。
    raw = out.read_text(encoding="utf-8")
    assert "\\\\frac" in raw and "<td>表格</td>" in raw and "\\n" in raw
    # 提取只读，不写库。
    assert _apply_calls(endpoint) == []


@pytest.mark.parametrize("matches,truncated,reason", [
    ([], False, "零匹配"),
    (_one_match() + _one_match(), False, "多匹配"),
    (_one_match(), True, "截断"),
])
def test_extract_out_refuses_non_single_match(endpoint, capsys, tmp_path, matches, truncated, reason):
    endpoint.routes["/api/v1/chunk/extract"] = _extract_route(matches, truncated=truncated)
    out = tmp_path / "证据.json"
    code, result, _ = invoke(endpoint, capsys, "chunk", "extract", "9", "--grep", "关键句", "--out", str(out))
    assert code != 0 and not result["success"], reason
    assert not out.exists(), f"{reason}时不得写文件"
    assert _apply_calls(endpoint) == []


def test_extract_out_refuses_to_overwrite_existing_file(endpoint, capsys, tmp_path):
    endpoint.routes["/api/v1/chunk/extract"] = _extract_route(_one_match())
    out = tmp_path / "证据.json"
    out.write_text("保留我", encoding="utf-8")
    code, result, _ = invoke(endpoint, capsys, "chunk", "extract", "9", "--lines", "2-3", "--out", str(out))
    assert code != 0 and not result["success"]
    assert out.read_text(encoding="utf-8") == "保留我"


def test_extract_out_lossless_round_trip_multiline_quote(endpoint, capsys, tmp_path):
    evidence = {**EVIDENCE, "quote": "甲 <td>a</td>\n乙 \\frac{3}{4}\n丙 & 丁"}
    endpoint.routes["/api/v1/chunk/extract"] = _extract_route(_one_match(evidence))
    out = tmp_path / "证据.json"
    invoke(endpoint, capsys, "chunk", "extract", "9", "--lines", "2-4", "--out", str(out))
    assert json.loads(out.read_text(encoding="utf-8"))["quote"] == evidence["quote"]


# ---------------------------------------------------------------------------
# 2. wiki object/relation add/update
# ---------------------------------------------------------------------------

def test_object_add_submits_object_and_evidence_atomically(endpoint, capsys, tmp_path):
    body = tmp_path / "正文.md"
    # 用字节写 LF，避免平台换行翻译；CLI 逐字读取须无损。
    body.write_bytes("正文第一行\n正文第二行".encode("utf-8"))
    ev1 = _evidence_file(tmp_path, name="e1.json")
    ev2 = _evidence_file(tmp_path, {**EVIDENCE, "source_chunk_id": 10,
                                    "quote": "另一端引文 <b>加粗</b>"}, name="e2.json")
    request_file = tmp_path / "request.json"
    invoke(endpoint, capsys, "wiki", "object", "add",
           "--kind", "concept", "--title", "增量维护", "--summary", "只提交必要增量",
           "--body-file", str(body), "--alias", "增量修订", "--alias", "增量更新",
           "--status", "disputed", "--evidence-file", str(ev1), "--evidence-file", str(ev2),
           "--stance", "context", "--request-file", str(request_file), "--reason", "记录用户要求")
    assert len(_apply_calls(endpoint)) == 1, "对象与证据必须在同一次 apply 提交"
    payload = _apply_calls(endpoint)[0]["payload"]
    assert payload["objects"] == [{
        "ref": "item", "kind": "concept", "title": "增量维护", "summary": "只提交必要增量",
        "body": "正文第一行\n正文第二行", "aliases": ["增量修订", "增量更新"], "status": "disputed"}]
    assert payload["evidence"] == [
        {**EVIDENCE, "object": {"ref": "item"}, "stance": "context"},
        {**EVIDENCE, "source_chunk_id": 10, "quote": "另一端引文 <b>加粗</b>",
         "object": {"ref": "item"}, "stance": "context"}]
    assert payload.get("reason") == "记录用户要求"
    assert payload.get("dry_run") in (None, False)


def test_object_add_minimal_fixes_item_ref(endpoint, capsys, tmp_path):
    rf = tmp_path / "request.json"
    invoke(endpoint, capsys, "wiki", "object", "add", "--kind", "entity", "--title", "实体",
           "--evidence-file", str(_evidence_file(tmp_path)), "--request-file", str(rf), "--reason", "新增")
    obj = _apply_calls(endpoint)[0]["payload"]["objects"][0]
    assert obj["ref"] == "item" and obj["kind"] == "entity" and obj["title"] == "实体"
    assert "id" not in obj and "body" not in obj


def test_object_update_sends_only_changed_fields_with_id_owner(endpoint, capsys, tmp_path):
    rf = tmp_path / "request.json"
    invoke(endpoint, capsys, "wiki", "object", "update", OBJECT_UUID,
           "--expected-revision", "3", "--title", "新标题", "--evidence-file", str(_evidence_file(tmp_path)),
           "--request-file", str(rf), "--reason", "局部修订")
    payload = _apply_calls(endpoint)[0]["payload"]
    assert payload["objects"] == [{"id": OBJECT_UUID, "expected_revision": 3, "title": "新标题"}]
    assert payload["evidence"][0]["object"] == {"id": OBJECT_UUID}
    assert payload["evidence"][0]["quote"] == EVIDENCE["quote"]


def test_object_update_requires_at_least_one_field(endpoint, capsys, tmp_path):
    rf = tmp_path / "request.json"
    code, result, _ = invoke(endpoint, capsys, "wiki", "object", "update", OBJECT_UUID,
                             "--expected-revision", "3", "--request-file", str(rf), "--reason", "空更新")
    assert code == 2 and not result["success"]
    assert _apply_calls(endpoint) == []


def test_relation_add_sends_endpoints_and_relation_owner(endpoint, capsys, tmp_path):
    rf = tmp_path / "request.json"
    invoke(endpoint, capsys, "wiki", "relation", "add",
           "--source", SOURCE_UUID, "--predicate", "depends_on", "--target", TARGET_UUID,
           "--description", "增量修订依赖稳定身份", "--basis", "explicit",
           "--evidence-file", str(_evidence_file(tmp_path)), "--stance", "supports",
           "--request-file", str(rf), "--reason", "保存关系")
    payload = _apply_calls(endpoint)[0]["payload"]
    assert payload["relations"] == [{
        "ref": "item", "source": {"id": SOURCE_UUID}, "predicate": "depends_on",
        "target": {"id": TARGET_UUID}, "description": "增量修订依赖稳定身份", "basis": "explicit"}]
    assert payload["evidence"][0]["relation"] == {"ref": "item"}
    assert "object" not in payload["evidence"][0]


def test_relation_update_partial_and_id_owner(endpoint, capsys, tmp_path):
    rf = tmp_path / "request.json"
    invoke(endpoint, capsys, "wiki", "relation", "update", RELATION_UUID,
           "--expected-revision", "2", "--predicate", "supports",
           "--evidence-file", str(_evidence_file(tmp_path)), "--stance", "contradicts",
           "--request-file", str(rf), "--reason", "修订关系")
    payload = _apply_calls(endpoint)[0]["payload"]
    assert payload["relations"] == [{"id": RELATION_UUID, "expected_revision": 2, "predicate": "supports"}]
    assert payload["evidence"][0]["relation"] == {"id": RELATION_UUID}
    assert payload["evidence"][0]["stance"] == "contradicts"


@pytest.mark.parametrize("stance", ["supports", "contradicts", "context"])
def test_stance_accepts_three_kinds(endpoint, capsys, tmp_path, stance):
    rf = tmp_path / f"request-{stance}.json"
    invoke(endpoint, capsys, "wiki", "object", "add", "--kind", "entity", "--title", "实体",
           "--evidence-file", str(_evidence_file(tmp_path, name=f"{stance}.json")),
           "--stance", stance, "--request-file", str(rf), "--reason", "标注立场")
    assert _apply_calls(endpoint)[0]["payload"]["evidence"][0]["stance"] == stance


def test_stance_rejects_unknown_value(endpoint, capsys, tmp_path):
    code, result, _ = invoke(endpoint, capsys, "wiki", "object", "add", "--kind", "entity",
                             "--title", "实体", "--evidence-file", str(_evidence_file(tmp_path)),
                             "--stance", "guessing", "--request-file", str(tmp_path / "r.json"),
                             "--reason", "非法立场")
    assert code == 2 and _apply_calls(endpoint) == []


def test_high_level_write_requires_request_file(endpoint, capsys, tmp_path):
    code, result, _ = invoke(endpoint, capsys, "wiki", "object", "add", "--kind", "entity",
                             "--title", "实体", "--evidence-file", str(_evidence_file(tmp_path)),
                             "--reason", "缺少请求文件")
    assert code == 2 and _apply_calls(endpoint) == []


# ---------------------------------------------------------------------------
# 3. request-file 保存与复用
# ---------------------------------------------------------------------------

def _add(tmp_path, request_file, title="概念", **extra):
    args = ["wiki", "object", "add", "--kind", "concept", "--title", title,
            "--evidence-file", str(_evidence_file(tmp_path)), "--request-file", str(request_file),
            "--reason", "保存"]
    for key, value in extra.items():
        flag = "--" + key.replace("_", "-")
        if value is True:
            args.append(flag)  # store_true 开关不接受值
        else:
            args += [flag, str(value)]
    return args


def test_request_file_saved_before_submit_without_dry_run(endpoint, capsys, tmp_path):
    rf = tmp_path / "request.json"
    observed = []

    def submit(handler, payload):
        observed.append(json.loads(rf.read_text(encoding="utf-8")))
        return _fake_apply(handler, payload)

    endpoint.routes["/api/v1/knowledge/apply"] = submit
    endpoint.routes["/api/v1/knowledge/get"] = _get_route()
    assert invoke(endpoint, capsys, *_add(tmp_path, rf))[0] == 0
    saved = json.loads(rf.read_text(encoding="utf-8"))
    assert observed == [saved]
    assert set(saved) == {"target_id", "request"}
    assert saved["target_id"] == endpoint.identity["target_id"]
    request = saved["request"]
    assert request["request_key"] and "dry_run" not in request
    assert request["objects"][0]["ref"] == "item" and request["objects"][0]["title"] == "概念"
    assert request["evidence"][0]["object"] == {"ref": "item"}
    # 提交发生在文件落盘之后：apply 用的键就是文件里的键。
    assert _apply_calls(endpoint)[0]["payload"]["request_key"] == request["request_key"]


def test_request_file_reuses_key_for_identical_content(endpoint, capsys, tmp_path):
    rf = tmp_path / "request.json"
    invoke(endpoint, capsys, *_add(tmp_path, rf))
    first = _apply_calls(endpoint)[0]["payload"]["request_key"]
    invoke(endpoint, capsys, *_add(tmp_path, rf))
    calls = _apply_calls(endpoint)
    assert len(calls) == 2 and calls[0]["payload"]["request_key"] == calls[1]["payload"]["request_key"] == first
    assert json.loads(rf.read_text(encoding="utf-8"))["request"]["request_key"] == first


def test_request_file_refuses_same_file_different_content(endpoint, capsys, tmp_path):
    rf = tmp_path / "request.json"
    invoke(endpoint, capsys, *_add(tmp_path, rf, title="原概念"))
    code, result, _ = invoke(endpoint, capsys, *_add(tmp_path, rf, title="改概念"))
    assert code != 0 and not result["success"]
    assert len(_apply_calls(endpoint)) == 1, "同一请求文件内容变化不得重提交"
    assert json.loads(rf.read_text(encoding="utf-8"))["request"]["objects"][0]["title"] == "原概念"


def test_request_file_refuses_different_target(endpoint, capsys, tmp_path):
    rf = tmp_path / "request.json"
    invoke(endpoint, capsys, *_add(tmp_path, rf))
    saved = json.loads(rf.read_text(encoding="utf-8"))
    saved["target_id"] = "0" * 64
    _write_json(rf, saved)
    code, result, _ = invoke(endpoint, capsys, *_add(tmp_path, rf))
    assert code != 0 and not result["success"]
    assert len(_apply_calls(endpoint)) == 1


def test_different_request_files_same_content_use_distinct_keys(endpoint, capsys, tmp_path):
    rf1, rf2 = tmp_path / "r1.json", tmp_path / "r2.json"
    invoke(endpoint, capsys, *_add(tmp_path, rf1))
    invoke(endpoint, capsys, *_add(tmp_path, rf2))
    keys = [call["payload"]["request_key"] for call in _apply_calls(endpoint)]
    assert len(keys) == 2 and keys[0] != keys[1]
    assert json.loads(rf1.read_text(encoding="utf-8"))["request"]["request_key"] == keys[0]
    assert json.loads(rf2.read_text(encoding="utf-8"))["request"]["request_key"] == keys[1]


def test_dry_run_saves_file_without_dry_run_and_skips_read_back(endpoint, capsys, tmp_path):
    rf = tmp_path / "request.json"
    invoke(endpoint, capsys, *_add(tmp_path, rf, dry_run=True))
    saved = json.loads(rf.read_text(encoding="utf-8"))["request"]
    assert saved["request_key"] and "dry_run" not in saved, "保存的是不带 dry_run 的正常批次"
    assert _apply_calls(endpoint)[0]["payload"]["dry_run"] is True
    assert _get_calls(endpoint) == []


def test_network_loss_keeps_request_file_for_recovery(endpoint, capsys, tmp_path):
    import socket

    def disconnect(handler, payload):
        handler.connection.shutdown(socket.SHUT_RDWR)
        handler.connection.close()

    endpoint.routes["/api/v1/knowledge/apply"] = disconnect
    rf = tmp_path / "request.json"
    code, result, _ = invoke(endpoint, capsys, *_add(tmp_path, rf))
    assert code == 3 and result["data"]["outcome_unknown"]
    lost = result["data"]["request_key"]
    assert rf.exists(), "提交前已落盘，网络异常可从文件恢复"
    assert json.loads(rf.read_text(encoding="utf-8"))["request"]["request_key"] == lost
    assert len(_apply_calls(endpoint)) == 1, "不得生成新键重提"


def test_apply_input_reads_request_file_wrapper_and_reads_back(endpoint, capsys, tmp_path):
    endpoint.routes["/api/v1/knowledge/apply"] = _fake_apply
    endpoint.routes["/api/v1/knowledge/get"] = _get_route()
    rf = tmp_path / "request.json"
    invoke(endpoint, capsys, *_add(tmp_path, rf))
    wrapper = json.loads(rf.read_text(encoding="utf-8"))
    endpoint.calls.clear()
    code, result, _ = invoke(endpoint, capsys, "wiki", "apply", "--input", str(rf), "--read-back")
    assert code == 0 and result["success"]
    assert _apply_calls(endpoint)[0]["payload"] == wrapper["request"], "原样重放请求文件中的批次"
    assert result["data"]["read_back"]["complete"] is True


# ---------------------------------------------------------------------------
# 4. 读回
# ---------------------------------------------------------------------------

def test_high_level_command_reads_back_by_default(endpoint, capsys, tmp_path):
    endpoint.routes["/api/v1/knowledge/apply"] = _fake_apply
    endpoint.routes["/api/v1/knowledge/get"] = _get_route(revision=1)
    code, result, _ = invoke(endpoint, capsys, *_add(tmp_path, tmp_path / "request.json"))
    assert code == 0 and result["success"] and result["data"]["committed"] is True
    read_back = result["data"]["read_back"]
    assert set(read_back) >= {"complete", "records", "total", "limit", "truncated", "semantic_review_required"}
    assert read_back["complete"] is True and read_back["limit"] == 20 and read_back["truncated"] is False
    assert isinstance(read_back["semantic_review_required"], bool)
    assert read_back["total"] == len(read_back["records"]) >= 1
    object_record = next(record for record in read_back["records"] if record["kind"] == "object")
    assert object_record["record"]["title"] == "概念"
    assert object_record["submitted_revision"] == 1 and object_record["revision_matches"] is True
    assert _get_calls(endpoint), "默认读回必须经 /api/v1/knowledge/get 取记录"


def test_read_back_failure_keeps_committed_and_does_not_rewrite(endpoint, capsys, tmp_path):
    endpoint.routes["/api/v1/knowledge/apply"] = _fake_apply
    endpoint.routes["/api/v1/knowledge/get"] = lambda *a: failure("NOT_FOUND")
    code, result, _ = invoke(endpoint, capsys, *_add(tmp_path, tmp_path / "request.json"))
    assert code == 1 and not result["success"]
    assert result["error"]["code"] == "READ_BACK_INCOMPLETE"
    assert result["data"]["committed"] is True
    assert len(_apply_calls(endpoint)) == 1, "读回失败不自动重写"


def test_read_back_version_change_reports_incomplete(endpoint, capsys, tmp_path):
    endpoint.routes["/api/v1/knowledge/apply"] = _fake_apply
    endpoint.routes["/api/v1/knowledge/get"] = _get_route(revision=99)
    code, result, _ = invoke(endpoint, capsys, *_add(tmp_path, tmp_path / "request.json"))
    assert code == 1 and result["error"]["code"] == "READ_BACK_INCOMPLETE"
    assert result["data"]["committed"] is True


def test_read_back_over_limit_reports_incomplete(endpoint, capsys, tmp_path):
    endpoint.routes["/api/v1/knowledge/apply"] = _fake_apply
    endpoint.routes["/api/v1/knowledge/get"] = _get_route(revision=1)
    batch = {
        "reason": "超过读回上限",
        "request_key": "over-limit",
        "objects": [{"ref": f"o{i}", "kind": "entity", "title": f"实体{i}"} for i in range(20)],
        "relations": [{"ref": "r", "source": {"ref": "o0"}, "predicate": "related_to",
                       "target": {"ref": "o1"}, "description": "有界读回", "basis": "explicit"}],
    }
    source = _write_json(tmp_path / "batch.json", batch)
    code, result, _ = invoke(endpoint, capsys, "wiki", "apply", "--input", str(source), "--read-back")
    assert code == 1 and result["error"]["code"] == "READ_BACK_INCOMPLETE"
    assert result["data"]["committed"] is True


def test_wiki_request_read_back_is_read_only(endpoint, capsys):
    endpoint.routes["/api/v1/knowledge/request"] = lambda handler, payload: make_envelope(True, "", {
        "committed": True, "refs": {"item": {"id": OBJECT_UUID, "kind": "object", "revision": 1}},
        "objects": [{"id": OBJECT_UUID, "action": "created", "revision": 1}],
        "relations": [], "links": [], "evidence": [], "library_id": LIBRARY_UUID,
        "counts": {"created": 1, "updated": 0, "reused": 0}})
    endpoint.routes["/api/v1/knowledge/get"] = _get_route(revision=1)
    code, result, _ = invoke(endpoint, capsys, "wiki", "request", "stable-key", "--read-back")
    assert code == 0 and result["success"]
    request_call = next(call for call in endpoint.calls if call["path"] == "/api/v1/knowledge/request")
    assert request_call["payload"] == {"request_key": "stable-key"}
    assert _apply_calls(endpoint) == [], "wiki request 只读，不得提交"
    assert result["data"]["read_back"]["complete"] is True
    assert "submitted_fields" not in result["data"]["read_back"]["records"][0]  # 旧回执不能推断提交字段。


def test_apply_without_read_back_keeps_legacy_shape(endpoint, capsys, tmp_path):
    endpoint.routes["/api/v1/knowledge/apply"] = _fake_apply
    source = _write_json(tmp_path / "batch.json", {
        "reason": "旧行为", "objects": [{"ref": "a", "kind": "entity", "title": "实体"}]})
    code, result, _ = invoke(endpoint, capsys, "wiki", "apply", "--input", str(source), "--request-id", "legacy")
    assert code == 0 and result["success"]
    assert "read_back" not in result["data"]
    assert _get_calls(endpoint) == []


# ---------------------------------------------------------------------------
# 5. 失败恢复：next_command / next_argv
# ---------------------------------------------------------------------------

def test_quote_mismatch_offers_readonly_chunk_get(endpoint, capsys, tmp_path):
    # 真实服务的定位错误带 section/index，CLI 据此给出 chunk get 恢复命令。
    endpoint.routes["/api/v1/knowledge/apply"] = lambda *a: failure(
        "QUOTE_MISMATCH", {"section": "evidence", "index": 0})
    code, result, _ = invoke(endpoint, capsys, *_add(tmp_path, tmp_path / "request.json"))
    assert code == 1 and not result["success"]
    argv = result["data"]["next_argv"]
    command = result["data"]["next_command"]
    assert argv and all(isinstance(part, str) for part in argv)
    assert _option_value(argv, "--port") == str(endpoint.port)
    assert Path(_option_value(argv, "--data-dir")).resolve() == endpoint.config.resolve()
    assert "chunk" in argv and "get" in argv and "9" in argv
    assert "chunk" in command and "get" in command
    assert len(_apply_calls(endpoint)) == 1, "只读恢复，不自动改 revision 或重提"


def test_version_conflict_offers_wiki_get(endpoint, capsys, tmp_path):
    endpoint.routes["/api/v1/knowledge/apply"] = lambda *a: failure(
        "VERSION_CONFLICT", {"section": "objects", "index": 0, "id": OBJECT_UUID, "current_revision": 4})
    code, result, _ = invoke(endpoint, capsys, "wiki", "object", "update", OBJECT_UUID,
                             "--expected-revision", "1", "--title", "冲突标题",
                             "--request-file", str(tmp_path / "request.json"), "--reason", "冲突")
    assert code == 1 and not result["success"]
    argv = result["data"]["next_argv"]
    assert "wiki" in argv and "get" in argv and OBJECT_UUID in argv
    assert _option_value(argv, "--port") == str(endpoint.port)
    assert len(_apply_calls(endpoint)) == 1


def test_lost_response_points_at_request_read_back_without_new_key(endpoint, capsys, tmp_path):
    import socket

    def disconnect(handler, payload):
        handler.connection.shutdown(socket.SHUT_RDWR)
        handler.connection.close()

    endpoint.routes["/api/v1/knowledge/apply"] = disconnect
    code, result, _ = invoke(endpoint, capsys, *_add(tmp_path, tmp_path / "request.json"))
    assert code == 3
    key = result["data"]["request_key"]
    # 现有 data.recovery 结构保持不变。
    assert result["data"]["recovery"] == {"command": "piece wiki request", "request_id": key,
        "same_target_required": True, "do_not_resubmit": False, "retry_identical_only": True}
    argv = result["data"]["next_argv"]
    assert "wiki" in argv and "request" in argv and "--read-back" in argv
    assert Path(_option_value(argv, "--input")) == tmp_path / "request.json"
    assert key not in argv  # 从文件读键，不要求模型复制不透明值。
    assert "wiki request" in result["data"]["next_command"] and "--read-back" in result["data"]["next_command"]
    assert len(_apply_calls(endpoint)) == 1, "用原键只读恢复，不生成新键重提"


def test_recovery_command_bash_escapes_spaced_paths(endpoint, capsys, tmp_path):
    spaced = endpoint.config.parent / "配置 目录"
    endpoint.config.rename(spaced)
    endpoint.config = spaced
    _write_json(spaced / "config.json", {"api": {"admin_key": "a" * 32}, "data_path": str(spaced)})
    endpoint.identity["target_id"] = target_id(spaced, spaced / "kb.db")
    endpoint.routes["/api/v1/knowledge/apply"] = lambda *a: failure(
        "QUOTE_MISMATCH", {"section": "evidence", "index": 0})
    code, result, _ = invoke(endpoint, capsys, *_add(spaced, spaced / "request.json"))
    argv, command = result["data"]["next_argv"], result["data"]["next_command"]
    assert Path(_option_value(argv, "--data-dir")).resolve() == spaced.resolve()
    variants = [str(spaced), spaced.as_posix()]
    matched = next((value for value in variants if value in command), None)
    assert matched is not None, "next_command 必须包含数据目录路径"
    assert re.search(r"""["']%s["']""" % re.escape(matched), command), "含空格的路径必须被引号包裹"


# ---------------------------------------------------------------------------
# 6. status 增加 cli_version
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("missing", ["expected_content_hash", "source_library_id", "source_chunk_id"])
def test_incomplete_evidence_file_cannot_bypass_source_checks(endpoint, capsys, tmp_path, missing):
    evidence = {key: value for key, value in EVIDENCE.items() if key != missing}
    source = _evidence_file(tmp_path, evidence)
    request_file = tmp_path / "request.json"
    code, result, _ = invoke(endpoint, capsys, "wiki", "object", "add", "--kind", "concept", "--title", "知识",
                             "--evidence-file", str(source), "--request-file", str(request_file))
    assert code == 2 and result["error"]["code"] == "INVALID_ARGUMENT"
    assert not request_file.exists() and not _apply_calls(endpoint)


def test_recovery_treats_request_key_as_one_literal_argument(endpoint, capsys, tmp_path):
    import shlex

    key = "retry '; $(touch injected)"
    source = _write_json(tmp_path / "batch.json", {
        "reason": "转义", "objects": [{"ref": "a", "kind": "concept", "title": "知识"}]})
    endpoint.routes["/api/v1/knowledge/apply"] = lambda *args: failure("REQUEST_CONFLICT")
    code, result, _ = invoke(endpoint, capsys, "wiki", "apply", "--input", str(source), "--request-id", key)
    assert code == 1
    argv = result["data"]["next_argv"]
    assert argv[argv.index("request") + 1] == key
    assert shlex.split(result["data"]["next_command"]) == argv


def test_request_from_file_looks_up_saved_key_without_submission(endpoint, capsys, tmp_path):
    request = {"request_key": "saved-key", "reason": "保存", "objects": [{"ref": "item", "kind": "concept", "title": "知识"}]}
    source = _write_json(tmp_path / "request.json", {"target_id": endpoint.identity["target_id"], "request": request})
    receipt = _fake_apply(None, request)
    endpoint.routes["/api/v1/knowledge/request"] = lambda *args: receipt
    endpoint.routes["/api/v1/knowledge/get"] = _get_route()
    code, result, _ = invoke(endpoint, capsys, "wiki", "request", "--input", str(source), "--read-back")
    assert code == 0 and result["data"]["read_back"]["complete"]
    query = next(call for call in endpoint.calls if call["path"] == "/api/v1/knowledge/request")
    assert query["payload"] == {"request_key": "saved-key"}
    assert not _apply_calls(endpoint)


@pytest.mark.parametrize("case", ["both", "neither", "wrong-target", "missing-key"])
def test_request_file_query_rejects_ambiguous_or_unbound_inputs(endpoint, capsys, tmp_path, case):
    saved = {"target_id": endpoint.identity["target_id"], "request": {"request_key": "saved-key"}}
    if case == "wrong-target":
        saved["target_id"] = "another-instance"
    if case == "missing-key":
        saved["request"] = {}
    source = _write_json(tmp_path / "request.json", saved)
    args = ([] if case == "neither" else ["--input", str(source)]) + (["other-key"] if case == "both" else [])
    code, result, _ = invoke(endpoint, capsys, "wiki", "request", *args)
    assert code != 0 and not result["success"]
    assert not endpoint.calls


@pytest.mark.parametrize("restored", [False, True])
def test_delete_receipt_read_back_checks_absence_without_redeleting(endpoint, capsys, restored):
    endpoint.routes["/api/v1/knowledge/request"] = lambda *args: make_envelope(True, "", {
        "committed": True, "dry_run": False, "kind": "object", "id": OBJECT_UUID,
        "impact_token": "a" * 64, "counts": {"object": 1}, "revision": 1})
    endpoint.routes["/api/v1/knowledge/get"] = _get_route() if restored else lambda *args: failure("NOT_FOUND")
    code, result, _ = invoke(endpoint, capsys, "wiki", "request", "delete-key", "--read-back")
    assert result["data"]["committed"] is True
    assert result["data"]["read_back"]["complete"] is not restored
    if restored:
        assert code == 1 and result["error"]["code"] == "READ_BACK_INCOMPLETE"
    else:
        assert code == 0 and result["data"]["read_back"]["records"][0]["deleted"] is True
    assert not _apply_calls(endpoint)
    assert not any(call["path"] == "/api/v1/knowledge/delete" for call in endpoint.calls)


@pytest.mark.parametrize("inputs", [["--body-file", "-", "--evidence-file", "-"],
                                     ["--evidence-file", "-", "--evidence-file", "-"]])
def test_intent_rejects_multiple_stdin_consumers(endpoint, capsys, tmp_path, inputs):
    code, result, _ = invoke(endpoint, capsys, "wiki", "object", "add", "--kind", "concept", "--title", "知识",
                             "--request-file", str(tmp_path / "request.json"), *inputs)
    assert code == 2 and result["error"]["code"] == "INVALID_ARGUMENT"
    assert not endpoint.calls and not (tmp_path / "request.json").exists()


def test_cli_status_reports_cli_version(endpoint, capsys):
    endpoint.routes["/api/v1/status"] = lambda *a: make_envelope(True, "ok", {"ready": True})
    code, result, _ = invoke(endpoint, capsys, "status")
    assert code == 0 and result["success"]
    assert isinstance(result["data"].get("cli_version"), str) and result["data"]["cli_version"]
