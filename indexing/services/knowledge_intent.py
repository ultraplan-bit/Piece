"""意图式写入的共享构造：把扁平字段与内联证据定位组装成 apply 批次。

CLI 的 ``graph object add`` 与索引 MCP 的 ``knowledge_object_add`` 共用这一层；
两者都不直接拼嵌套 JSON。本模块只做组装与定位解析，不访问业务记录。
"""

from .errors import BusinessError
from .maintenance_service import extract_chunk

SECTIONS = {"page": "pages", "object": "objects", "relation": "relations"}
INTENT_REF = "item"


def parse_locator(spec):
    """解析 ``CHUNK_ID:LOCATOR``；LOCATOR 为行号 N / N-M、``grep=子串`` 或 ``re=正则``。"""
    chunk_text, separator, locator = spec.partition(":")
    if not separator or not chunk_text.strip().isdigit() or int(chunk_text) < 1 or not locator.strip():
        raise BusinessError("INVALID_ARGUMENT", f"证据定位须为 CHUNK_ID:行号 或 CHUNK_ID:grep=子串：{spec}")
    payload = {"chunk_id": int(chunk_text), "max_matches": 2}
    locator = locator.strip()
    if locator.startswith("grep="):
        payload.update(grep=locator[5:], regex=False, context=0)
    elif locator.startswith("re="):
        payload.update(grep=locator[3:], regex=True, context=0)
    else:
        payload["lines"] = locator
    if payload.get("grep") == "":
        raise BusinessError("INVALID_ARGUMENT", f"证据定位的匹配文本不能为空：{spec}")
    return payload


def resolve_evidence(specs, extract=None):
    """按定位切出精确引文；恰好一个匹配才采用，否则拒绝并说明原因。

    ``extract`` 可替换为远程调用（CLI 走 HTTP），默认直接调用本地服务。
    """
    extract = extract or (lambda payload: extract_chunk(**payload))
    resolved = []
    for spec in specs:
        payload = parse_locator(spec)
        data = extract(payload)
        matches = data.get("matches") or []
        if len(matches) != 1 or data.get("truncated"):
            raise BusinessError("EVIDENCE_SELECTION_REQUIRED", f"内联证据匹配不唯一（{len(matches)} 条）：{spec}",
                                data={"spec": spec, "chunk_id": payload["chunk_id"], "match_count": len(matches),
                                      "truncated": bool(data.get("truncated"))})
        resolved.append(dict(matches[0]["evidence"]))
    return resolved


def build_intent(kind, action, values, evidence, stances=None, *, reason=None, record_id=None,
                 expected_revision=None, expected_content_hash=None, actor="intent"):
    """把单条新增/更新与已解析的证据组装成 apply 批次（不含 request_key）。

    ``values`` 只放调用方显式给出的字段；``evidence`` 是 ``resolve_evidence`` 或证据文件的输出。
    ``stances`` 给一个作用于全部证据，给多个按顺序逐一对应。
    """
    adding = action == "add"
    values = {key: value for key, value in values.items() if value is not None}
    if not adding and not values:
        # 图谱 Update 模型要求至少一个待改字段；Wiki 页面允许只带并发条件补证据。
        if not (kind == "page" and evidence):
            raise BusinessError("INVALID_ARGUMENT", "update 至少需要一个待修改字段；只添加证据请用 apply")
    for field in ("source", "target"):
        if field in values and isinstance(values[field], str):
            values[field] = {"id": values[field]}
    if adding:
        owner = {"ref": INTENT_REF}
        values.update(owner)
    else:
        owner = {"id": record_id}
        values.update(owner, expected_revision=expected_revision)
        if kind == "page":
            values["expected_content_hash"] = expected_content_hash
    stances = list(stances or ["supports"])
    if evidence and len(stances) not in {1, len(evidence)}:
        raise BusinessError("INVALID_ARGUMENT",
                            f"stance 要么给一次作用于全部证据，要么与证据数量一致（{len(evidence)} 条）")
    attached = [{**item, kind: owner, "stance": stances[0] if len(stances) == 1 else stances[index]}
                for index, item in enumerate(evidence)]
    return {"reason": reason or f"{actor} {kind} {action}", SECTIONS[kind]: [values], "evidence": attached}
