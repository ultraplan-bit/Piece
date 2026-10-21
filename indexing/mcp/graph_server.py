"""独立图谱 MCP 服务（piece-graph）：实体、关系、证据的读写与结构核查。

与 piece-wiki 共用 shared 里的 params 包装与意图式写入辅助；本服务只碰图谱，
不读文件/卡片，也不管理 Wiki 页面。读写在同一个服务内，是否允许写入由用户
选择接入哪个服务决定（图谱与 Wiki 都是可选服务）。
"""

from typing import Annotated, Literal, Union

from fastmcp import FastMCP
from pydantic import Field

from indexing import knowledge_models as km
from indexing.mcp import shared
from indexing.mcp.auth import apply_bearer_auth
from indexing.mcp.shared import ContentHash, Evidence, Reason, Revision, Stances
from indexing.services import knowledge_service as knowledge

mcp = FastMCP(
    "piece-graph",
    instructions="""用户个人知识图谱：实体、有证据的语义关系，以及只读的结构核查。不读文件/卡片，也不管理 Wiki 页面。

读：knowledge-list/search 找候选，knowledge-get 读一条实体/关系/证据（实体没有正文），knowledge-graph 取有界邻域，
knowledge-references 按来源文件反查引用，knowledge-lint 做只读结构检查，knowledge-history/request 读修订与已提交结果。
这些工具都收一个 params JSON 对象，ID 是 UUID（不是整数 file/chunk ID）。同名对象是不同记录，不要按名字合并。
证据的 location_status 为 current/changed/missing/unresolved/unverified，即使 current 也不代表断言已被证实。

写：高频路径用扁平意图工具 knowledge_object_add/update、knowledge_relation_add/update（字段直接作为顶层参数，
证据写 "CHUNK_ID:行号" 或 "CHUNK_ID:grep=子串"，request_key 自动生成并回传）；一次提交多条或需要字段级控制时用
knowledge_apply/delete 批次工具（只收一个 params JSON 对象，正式提交必填 request_key，dry_run 只校验不消耗键）。
不要给扁平工具套 params。图谱写入是同步本地事务，不调用模型或嵌入，没有 task_id。

写前先读：用 knowledge-search 查重，写后用 knowledge-get 读回；更新一律带 expected_revision，冲突时重读再决定，
不要盲改。删除必须先 dry_run 预览并取得用户确认，返回的 impact_token 要在正式删除时原样带回。
超时或响应丢失用 knowledge-request 按 request_key 查原结果，或原键原样重试，不要换键重复新建。

从文档建图（一次读一段、边读边写，不要先读完整篇再动手）：
1. 定位来源：piece-index 的 query_files / query_file_info 拿 file_id，list_file_chunks 分页读卡片。先用默认大纲
   （只给 chunk_index/doc_title/heading_path）看结构，再按 include_text=true 取要读的那几页正文。
2. 分批：按 chunk_index 连续区间分批（每批约 5–10 张卡片）。有没有标题都用同一套规则，标题/页码只用来给批次命名；
   不要依赖文档大纲，很多文档没有标题。批次之间不重叠——重叠只会让同一关系被抽两次。
3. 每批：读完这批卡片后，先 knowledge-search 查重（同名/别名可能是同一实体，但同名不等于同一记录，不要按名字合并），
   再用 knowledge_object_add / knowledge_relation_add 提交本批的实体、批内关系和证据。证据写 "CHUNK_ID:行号" 或
   "CHUNK_ID:grep=子串"，服务切出精确引文。一批提交后进入下一批。
4. 跨批关系：两个端点分别落在不同批的关系，第一批看不全，不要硬凑。等实体都已入库后，第二遍用 knowledge-search
   找到已有实体再补关系——实体是持久的，第二遍不必重读原文。
5. 续建：进度存在图里。新会话先 knowledge-references（库 UUID + file_id）查该文件已写入的证据，按 source_chunk_id
   判断哪些卡片已覆盖，从第一张未覆盖的卡片接着做，不要从头重来。空卡片（抽不出实体）不产生证据，会被重读一次，属正常。
6. 完成判据：该文件所有卡片都读过、且跨批关系已补，才可报告完成；不要只建了前几批就说建好。

跨服务：文件/卡片/集合的导入与整理在 piece-index；精确引文用 piece-kb 的 extract-quote（按 chunk_id 切出行号或匹配），
或 piece-index 的 query_chunk_info 读回正文。Wiki 页面是另一套独立数据，在 piece-wiki。
""",
    strict_input_validation=False,
)

apply_bearer_auth(mcp, "graph")


# ==================== 只读查询 ====================


@mcp.tool(name="knowledge-list", annotations=shared.READ)
async def knowledge_list(params: km.ListInput) -> dict:
    """List knowledge objects, not document chunks. params: optional kind/status, limit 1..100 (default 50), offset >=0.

    Returns library_id and objects [{id,kind,title,summary,status,revision,created_at,updated_at}], total/limit/offset.
    Same-name objects are distinct; read title/summary and evidence to disambiguate. Graph entities carry no body.
    """
    return await shared.read(knowledge.list_objects, params)


@mcp.tool(name="knowledge-search", annotations=shared.READ)
async def knowledge_search(params: km.SearchInput) -> dict:
    """Search entity names/aliases and Chinese-tokenized title/summary without embedding calls.

    params: required query (plain text, not SQL/FTS syntax), optional kind/status, limit/offset as knowledge-list.
    Returns library_id, objects, total/limit/offset. Graph entities have no body; use knowledge-get for details and sources; do not merge by name.
    """
    return await shared.read(knowledge.search_objects, params)


@mcp.tool(name="knowledge-relation-search", annotations=shared.READ)
async def knowledge_relation_search(params: km.RelationSearchInput) -> dict:
    """Search relations by their own description and qualifier text, without embedding calls.

    params: required query (plain text, not SQL/FTS syntax), optional status, limit/offset as knowledge-list.
    Returns library_id, relations [{id,source_id,source_title,predicate,target_id,target_title,description,
    qualifier,basis,status,revision,display}], total/limit/offset. Endpoint titles are read live, not copied
    into the index. Use it to find relations by meaning; entity names are matched by knowledge-search.
    """
    return await shared.read(knowledge.search_relations, params)


@mcp.tool(name="knowledge-get", annotations=shared.READ)
async def knowledge_get(params: km.GetInput) -> dict:
    """Read a knowledge record by UUID: params.id is required; params.kind=object/relation/evidence is optional and is
    detected from the UUID when omitted (the response always reports the actual kind). limit/offset paginate evidence.

    Returns library_id, kind, record (entity summary/aliases or relation endpoints/predicate/basis/qualifier/revision).
    Entities/relations also include has_evidence and evidence {items,total,limit,offset}; evidence location_status is
    current/changed/missing/unresolved/unverified, never fact verification. No evidence means unsupported, not false.
    Use record.id and record.revision as id/expected_revision for the *_update tools.
    Piece evidence carries file/chunk IDs and server-derived page_number; use piece-kb get-source-page for available original pages.
    """
    return await shared.read(knowledge.get_record, params)


@mcp.tool(name="knowledge-graph", annotations=shared.READ)
async def knowledge_graph(params: km.GraphInput) -> dict:
    """Get bounded neighbors of params.root_id (entity UUID). depth=1 or 2; edge_types is only [relation].

    Optional predicates/statuses filter semantic relations; [] means none. max_nodes<=100, max_edges<=300.
    Returns nodes, edges with complete endpoints, truncated and budgets. No bodies/quotes; not full-graph statistics.
    related_to/contradicts are symmetric; other predicates are directed. Page links live in the separate Wiki feature.
    """
    return await shared.read(knowledge.graph, params)


@mcp.tool(name="knowledge-references", annotations=shared.READ)
async def knowledge_references(params: km.ReferencesInput) -> dict:
    """Find source backlinks: params.source_library_id=library UUID, source_file_id=integer, optional limit/offset.

    Returns evidence with owner_kind and owner object/relation summaries, location_status, total/limit/offset.
    Always use both library and file ID; other libraries with the same file ID are not interchangeable.
    """
    return await shared.read(knowledge.references, params)


@mcp.tool(name="knowledge-lint", annotations=shared.READ)
async def knowledge_lint(params: km.LintInput) -> dict:
    """Read-only structural checks; params.object_ids optionally limits UUID scope ([] checks none), limit/offset paginate objects.

    Returns issues, checked_objects, total_objects, truncated, read_only=true, semantic_review=false.
    Checks missing evidence, isolated objects, changed/missing sources and ambiguous names/aliases.
    No network, automatic fixes, merging or semantic truth judgement. Read candidate entities and evidence before proposing edits.
    """
    return await shared.read(knowledge.lint, params)


@mcp.tool(name="knowledge-history", annotations=shared.READ)
async def knowledge_history(params: km.HistoryInput) -> dict:
    """Read revisions: params.kind=object/relation, id=UUID, optional limit/offset.

    Returns history [{before,after,before_revision,after_revision,reason,batch_id,actor,...}], total/limit/offset.
    Read-only; corrections require a new revision using the current expected_revision, not rollback.
    Deleting an object/relation clears its online history; backups are outside online deletion.
    """
    return await shared.read(knowledge.history, params)


@mcp.tool(name="knowledge-request", annotations=shared.READ)
async def knowledge_request(params: km.RequestInput) -> dict:
    """Recover a committed apply/delete result using params.request_key after a timeout or lost response.

    Returns original IDs/revisions/counts, not input bodies. NOT_FOUND does not prove failure: retry only the identical
    payload and key on the same library. Returned IDs may since have been deleted; verify with knowledge-get.
    """
    return await shared.read(knowledge.request_result, params)


# ==================== 批次写入 ====================

_APPLY = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}


@mcp.tool(name="knowledge-apply", annotations=_APPLY)
async def knowledge_apply(params: km.ApplyInput) -> dict:
    """原子新增/修订知识；同步返回 success/message/data，不创建索引任务、不联网。

    params.reason 必填，正式提交必填 request_key；dry_run=true 只校验，不消耗键，预览 UUID 不可用于正式引用。
    objects 新增填 ref/kind/title，可选 summary/aliases/status；kind 仅 concept/entity，图谱实体不存正文。
    更新只填 id/expected_revision 与待改字段；省略保留，空字符串/数组清空，null 拒绝。覆盖人工内容须先确认。
    relations 新增填 ref/source/predicate/target/description/basis，可选 qualifier/status；不再接受页面链接。
    source/target 或证据 owner 用 {id:UUID} 引用已有实体，{ref:批内唯一名} 引用新增记录，不能两者并用。
    谓词 is_a/part_of/depends_on/applies_to/supports 有方向；contradicts/related_to 对称。related_to 须说明关联原因。
    basis=explicit/synthesis/inference/user_statement 区分原文明示、综合、推断、用户陈述，不是可信度认证。
    evidence 恰好填 object 或 relation；source_kind=piece/external/user，quote 必填，stance=supports/contradicts/context。
    piece 必填 source_library_id/source_file_id/source_chunk_id；本库校验归属和精确引文，服务计算 hash/页码；
    可填 expected_content_hash 拒绝旧正文。其他库还需 source_title，标 unresolved，不能宣称已验证哈希。
    external 需 source_title + 安全 http(s) source_url，不抓取网址；user 明确用户说明；两者均 unverified。
    每批最多 20 实体、100 关系、200 证据、512 KiB；未知字段拒绝，任何错误整批回滚。
    data 含 committed/dry_run/library_id/refs、各类 ID/revision/action 和 counts。同键同内容返回原结果；
    REQUEST_CONFLICT 不能改输入复用键；VERSION_CONFLICT 必须重读比较；响应丢失用 knowledge-request 查询或原样重试。
    提交后用 knowledge-get 读回。仅有引文不代表引文支持断言；保留双方证据与适用条件，不自动合并同名对象。
    """
    return await shared.write(knowledge.apply, params, actor="mcp:graph")


@mcp.tool(name="knowledge-delete", annotations=_APPLY)
async def knowledge_delete(params: km.DeleteInput) -> dict:
    """删除知识记录，不删除原始文件；必须先预览并经用户确认。

    params.kind=object/relation/evidence，id=知识 UUID；实体/关系必填 expected_revision，证据不可变、不接受版本。
    默认 dry_run=true，返回 data.counts（关联边、证据及在线历史清理数）和 impact_token，不写入。
    确认后以同 kind/id/expected_revision 加 dry_run=false、confirmed=true、request_key、原 impact_token 正式删除。
    依赖/版本变化返回 IMPACT_CONFLICT/VERSION_CONFLICT，必须重读、重新预览和确认，不能自动扩大删除范围。
    删除实体/关系清理附属证据和在线历史；证据删除不会在请求日志藏引用副本。备份不属于在线删除范围。
    返回 success/message/data（committed/counts/deletes_files=false/backups_affected=false）；失败含 error.code。
    超时用 knowledge-request 查询或原键原样重试；再用 knowledge-get 确认 NOT_FOUND。
    """
    return await shared.write(knowledge.delete, params, actor="mcp:graph")


@mcp.tool(name="knowledge-rebuild-index", annotations=shared.WRITE_ADD)
async def knowledge_rebuild_index() -> dict:
    """重建图谱实体的派生全文索引（FTS）；不修改实体、关系、证据或历史，不调用模型。

    用于索引缺失或查询结果异常后的恢复。无参数。返回 data 含 index_status 与 indexed_objects；
    失败不会清空或改写已有实体/关系/证据，重试同一命令即可。
    """
    return await shared.rebuild_index(knowledge.rebuild_index)


# ==================== 意图式写入（扁平参数，自动 request_key） ====================
# 与 CLI 的 `graph object add` / `graph relation add` 共用 knowledge_intent 组装逻辑；
# 面向只会填扁平参数的模型：不用拼 objects/relations/evidence 嵌套，也不用自己造 request_key。


@mcp.tool(name="knowledge-object-add", annotations=shared.INTENT)
async def knowledge_object_add(
    title: Annotated[str, Field(min_length=1, max_length=500)],
    kind: Literal["concept", "entity"] = "concept",
    summary: Annotated[str, Field(max_length=4000)] = "",
    aliases: Union[list[str], str, None] = None,
    status: Literal["active", "disputed", "outdated"] = "active",
    evidence: Union[Evidence, str, None] = None,
    stance: Union[Stances, str, None] = None,
    reason: Reason | None = None,
    dry_run: bool = False,
) -> dict:
    """一步新增一个图实体并附带证据；不用拼嵌套批次，request_key 自动生成并在 data.request_key 回传。

    参数全部是顶层扁平字段，不要套在 params 里（params 只属于 knowledge-apply/delete 批次工具）。
    前置：先用 knowledge-search 确认没有同义实体；证据需要已索引的卡片 CHUNK_ID（piece-index 的 query_chunk_info 可读回正文）。
    evidence 每项为 "CHUNK_ID:定位"，定位是行号 "12" / "12-14"、"grep=子串" 或 "re=正则"；
    服务按定位切出正文精确子串，匹配不唯一就拒绝并返回 next_tool，不猜第一条。
    stance 给一个作用于全部证据（默认 supports），给多个按证据顺序逐一对应。
    用 dry_run=true 预检；提交后用 knowledge-get 读回 data.refs.item.id。
    需要一次提交多条记录或更新已有记录的多字段时改用 knowledge-apply。
    """
    values = {"kind": kind, "title": title, "summary": summary or None, "status": status,
              "aliases": shared.intent_coerce(aliases)}
    return await shared.intent_write(knowledge, "object", "add", values, evidence, stance,
                                     reason=reason, dry_run=dry_run, actor="mcp:graph")


@mcp.tool(name="knowledge-object-update", annotations=shared.INTENT_UPDATE)
async def knowledge_object_update(
    id: Annotated[str, Field(min_length=36, max_length=36)],
    expected_revision: Revision,
    title: Annotated[str, Field(min_length=1, max_length=500)] | None = None,
    kind: Literal["concept", "entity"] | None = None,
    summary: Annotated[str, Field(max_length=4000)] | None = None,
    aliases: Union[list[str], str, None] = None,
    status: Literal["active", "disputed", "outdated"] | None = None,
    evidence: Union[Evidence, str, None] = None,
    stance: Union[Stances, str, None] = None,
    reason: Reason | None = None,
    dry_run: bool = False,
) -> dict:
    """增量修订一个图实体或只给它补证据；只填要改的字段，省略保留原值，空字符串/数组清空。

    前置：id 与 expected_revision 必须来自 knowledge-get（record.id / record.revision），不要按标题猜。
    版本不符返回 VERSION_CONFLICT，重读后再决定，不要盲改；至少要有一个待改字段，只补证据请走 knowledge-apply。
    evidence/stance 用法同 knowledge-object-add。
    """
    values = {"kind": kind, "title": title, "summary": summary, "status": status,
              "aliases": shared.intent_coerce(aliases)}
    return await shared.intent_write(knowledge, "object", "update", values, evidence, stance,
                                     reason=reason, dry_run=dry_run, actor="mcp:graph",
                                     record_id=id, expected_revision=expected_revision)


@mcp.tool(name="knowledge-relation-add", annotations=shared.INTENT)
async def knowledge_relation_add(
    source: Annotated[str, Field(min_length=36, max_length=36)],
    predicate: Literal["is_a", "part_of", "depends_on", "applies_to", "supports", "contradicts", "related_to"],
    target: Annotated[str, Field(min_length=36, max_length=36)],
    description: Annotated[str, Field(min_length=1, max_length=4000)],
    basis: Literal["explicit", "synthesis", "inference", "user_statement"],
    qualifier: Annotated[str, Field(max_length=4000)] = "",
    status: Literal["active", "disputed", "outdated"] = "active",
    evidence: Union[Evidence, str, None] = None,
    stance: Union[Stances, str, None] = None,
    reason: Reason | None = None,
    dry_run: bool = False,
) -> dict:
    """一步在两个已有实体间新增语义关系并附带证据；source/target 是实体 UUID，不接受标题。

    前置：两端实体必须已存在（先 knowledge-search 查重，没有再 knowledge-object-add）。
    is_a/part_of/depends_on/applies_to/supports 有方向；contradicts/related_to 对称，related_to 须在 description 说明关联原因。
    basis 区分原文明示 / 综合 / 推断 / 用户陈述，不是可信度认证。evidence/stance 用法同 knowledge-object-add。
    相同端点、谓词、qualifier 的关系只保留一条：内容一致时返回 action=reused，内容不同时返回 KNOWLEDGE_CONFLICT，
    data.id 是已有关系 UUID，data.existing 是它的可读摘要；要改它就按 data.next_tool 调 knowledge-relation-update。
    """
    values = {"source": source, "predicate": predicate, "target": target, "description": description,
              "basis": basis, "qualifier": qualifier or None, "status": status}
    return await shared.intent_write(knowledge, "relation", "add", values, evidence, stance,
                                     reason=reason, dry_run=dry_run, actor="mcp:graph")


@mcp.tool(name="knowledge-relation-update", annotations=shared.INTENT_UPDATE)
async def knowledge_relation_update(
    id: Annotated[str, Field(min_length=36, max_length=36)],
    expected_revision: Revision,
    source: Annotated[str, Field(min_length=36, max_length=36)] | None = None,
    predicate: Literal["is_a", "part_of", "depends_on", "applies_to", "supports", "contradicts", "related_to"] | None = None,
    target: Annotated[str, Field(min_length=36, max_length=36)] | None = None,
    description: Annotated[str, Field(min_length=1, max_length=4000)] | None = None,
    basis: Literal["explicit", "synthesis", "inference", "user_statement"] | None = None,
    qualifier: Annotated[str, Field(max_length=4000)] | None = None,
    status: Literal["active", "disputed", "outdated"] | None = None,
    evidence: Union[Evidence, str, None] = None,
    stance: Union[Stances, str, None] = None,
    reason: Reason | None = None,
    dry_run: bool = False,
) -> dict:
    """增量修订一条已有语义关系或只给它补证据；只填要改的字段，省略保留原值，qualifier 传空字符串可清空。

    id 与 expected_revision 来自 knowledge-get（kind=relation）或 knowledge-relation-add 冲突回执的 data.id；
    版本不符返回 VERSION_CONFLICT，重读后再决定。改 description 不改结构：纠正谓词传 predicate，
    纠正方向传 source/target。改成与另一条关系相同的端点、谓词和 qualifier 会返回 KNOWLEDGE_CONFLICT。
    evidence/stance 用法同 knowledge-object-add。
    """
    values = {"source": source, "predicate": predicate, "target": target, "description": description,
              "basis": basis, "qualifier": qualifier, "status": status}
    return await shared.intent_write(knowledge, "relation", "update", values, evidence, stance,
                                     reason=reason, dry_run=dry_run, actor="mcp:graph",
                                     record_id=id, expected_revision=expected_revision)
