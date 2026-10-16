"""独立 Wiki MCP 服务（piece-wiki）：Markdown 知识页与证据的读写、恢复副本与结构核查。

与 piece-graph 共用 shared 里的 params 包装与意图式写入辅助；本服务只碰 Wiki，
不管理图谱实体与语义关系。页面以 Markdown 为正式存储，页面内链是导航，不是语义断言。
读写在同一个服务内，是否允许写入由用户选择接入哪个服务决定。
"""

from typing import Annotated, List, Literal, Union

from fastmcp import FastMCP
from pydantic import Field

from indexing import wiki_models as wm
from indexing.mcp import shared
from indexing.mcp.auth import apply_bearer_auth
from indexing.mcp.shared import ContentHash, Evidence, Reason, Revision, Stances
from indexing.services import wiki_service as wiki

mcp = FastMCP(
    "piece-wiki",
    instructions="""用户个人 Wiki：以 Markdown 为正式存储的知识页与有证据的引用，以及恢复副本管理。不管理图谱实体与语义关系。

读：wiki-list/search 找候选，wiki-get 读页面正文、分页证据、页面链接与反链，wiki-references 按来源文件反查引用，
wiki-lint 做只读结构检查，wiki-history/request 读修订与已提交结果。这些工具都收一个 params JSON 对象，ID 是 UUID
（不是整数 file/chunk ID）。同名页面是不同记录；页面内链只是导航，不是语义关系。

写：高频路径用扁平意图工具 wiki-page-add/update（字段直接作为顶层参数，证据写 "CHUNK_ID:行号" 或 "CHUNK_ID:grep=子串"，
request_key 自动生成并回传）；一次提交多页或需要字段级控制时用 wiki-apply/delete 批次工具（只收一个 params JSON 对象，
正式提交必填 request_key，dry_run 只校验不消耗键）。不要给扁平工具套 params。

并发与冲突：更新页面必须带 expected_revision 和 expected_content_hash 两个并发条件，缺一不可，因为 MD 可能被外部编辑器改过；
哈希不符返回 CONTENT_CONFLICT、版本不符返回 VERSION_CONFLICT，重读后再决定，不要只换条件覆盖。body 是整体替换，不是追加。

恢复：更新或删除页面会把被替换的原文件留作本地恢复副本 .wiki-recovery-<operation_id>.bak，回执给出路径。
用 wiki-list-recovery 只读列出状态（needed 的副本是未提交操作的唯一旧内容，永不清理），确认后用 wiki-purge-recovery 清理，
默认 dry_run 预览。索引未更新或外部改了 MD 时用 wiki-rebuild-index 从 MD 重建派生索引，绝不从数据库旧正文反写 MD。
超时或响应丢失用 wiki-request 按 request_key 查原结果，或原键原样重试。

跨服务：文件/卡片/集合的导入与整理在 piece-index；精确引文用 piece-kb 的 extract-quote 或 piece-index 的 query_chunk_info。
图谱实体与语义关系是另一套独立数据，在 piece-graph；写页不建图，删页不删实体。
""",
    strict_input_validation=False,
)

apply_bearer_auth(mcp, "wiki")


# ==================== 只读查询 ====================


@mcp.tool(name="wiki-list", annotations=shared.READ)
async def wiki_list(params: wm.ListInput) -> dict:
    """List Wiki pages, not graph entities or document chunks. params: optional kind/status, limit 1..100 (default 50), offset >=0.

    Returns library_id and pages [{id,kind,title,summary,status,revision,content_hash,index_status}], total/limit/offset.
    Same-name pages are distinct; read the body and evidence to disambiguate. No full bodies here.
    """
    return await shared.read(wiki.list_pages, params)


@mcp.tool(name="wiki-search", annotations=shared.READ)
async def wiki_search(params: wm.SearchInput) -> dict:
    """Search Wiki page titles/aliases and body/index without embedding calls.

    params: required query (plain text), optional kind/status, limit/offset as wiki-list.
    Returns library_id, pages, total/limit/offset. Use wiki-get for bodies, evidence and page links; do not merge by name.
    """
    return await shared.read(wiki.search_pages, params)


@mcp.tool(name="wiki-get", annotations=shared.READ)
async def wiki_get(params: wm.GetInput) -> dict:
    """Read a Wiki record by UUID: params.id is required; params.kind=page/evidence is optional and is detected from the
    UUID when omitted (the response always reports the actual kind). limit/offset paginate evidence.

    Returns library_id, kind, record (page includes title/summary/body/aliases/status/revision/content_hash/path/index_status).
    Pages also include evidence {items,total,limit,offset}, page links and backlinks; evidence location_status is
    current/changed/missing/unresolved/unverified, never fact verification. A page has no graph identity.
    Use record.id, record.revision and record.content_hash as id/expected_revision/expected_content_hash for wiki-page-update.
    """
    return await shared.read(wiki.get_record, params)


@mcp.tool(name="wiki-references", annotations=shared.READ)
async def wiki_references(params: wm.ReferencesInput) -> dict:
    """Find source backlinks to Wiki pages: params.source_library_id=library UUID, source_file_id=integer, optional limit/offset.

    Returns evidence with owner page id/title and location_status, total/limit/offset. Use both library and file ID.
    """
    return await shared.read(wiki.references, params)


@mcp.tool(name="wiki-lint", annotations=shared.READ)
async def wiki_lint(params: wm.LintInput) -> dict:
    """Read-only structural checks on Wiki pages; params.page_ids optionally limits UUID scope ([] checks none).

    Returns issues, checked_pages, total_pages, truncated, read_only=true, semantic_review=false.
    Checks missing evidence, broken/through page links, changed/missing sources and ambiguous names.
    No network, automatic fixes or semantic truth judgement; read candidate pages before proposing edits.
    """
    return await shared.read(wiki.lint, params)


@mcp.tool(name="wiki-history", annotations=shared.READ)
async def wiki_history(params: wm.HistoryInput) -> dict:
    """Read Wiki page revisions: params.kind=page, id=UUID, optional limit/offset.

    Returns history [{before,after,before_revision,after_revision,reason,batch_id,actor,...}], total/limit/offset.
    Read-only; corrections require a new revision using current expected_revision/expected_content_hash, not rollback.
    Wiki page deletion retains online history, so history stays readable after a page is deleted.
    """
    return await shared.read(wiki.history, params)


@mcp.tool(name="wiki-request", annotations=shared.READ)
async def wiki_request(params: wm.RequestInput) -> dict:
    """Recover a committed Wiki apply/delete result using params.request_key after a timeout or lost response.

    Returns original IDs/revisions/counts and partial/index_status, not input bodies. NOT_FOUND does not prove failure:
    retry only the identical payload and key on the same library, then verify with wiki-get.
    """
    return await shared.read(wiki.request_result, params)


# ==================== 批次写入 ====================


@mcp.tool(name="wiki-apply", annotations=shared.WRITE)
async def wiki_apply(params: wm.ApplyInput) -> dict:
    """逐页安全提交 Wiki 页面与证据（整批非原子：单页失败不影响其他页）；MD 为正源，无 task_id、不调用模型。

    params 为结构化 JSON：request_key + reason，pages 为新建 Create 或更新 Update，evidence 附属于页面。
    新建页面填 ref/kind/title，可选 summary/body/aliases/status；kind 为 concept/entity/topic/synthesis/source_summary。
    更新页面填 id/expected_revision/expected_content_hash 与待改字段；省略保留，空字符串/数组清空，null 拒绝。
    给已有页面只追加证据时，pages 中仍须包含该页 {id,expected_revision,expected_content_hash} 并发条件（可无其他改动字段）。
    evidence 的 page 必须是本批新建 ref 或更新 id，不自动创建页面、不跨功能映射到图谱实体；来源字段与图谱证据一致。
    dry_run=true 只校验不写入；正式提交必填 request_key。返回 data 含 committed/partial/index_status/errors：
    committed 只表示至少一页真实落盘；partial=true 或 index_status!=current（部分页未写入或索引未更新）时
    success=false 且 error.code=PARTIAL_FAILURE，但 data 保留已提交回执，请按 errors 逐页核对，不要重发。
    一页都没写入（committed=false）时不报 PARTIAL_FAILURE，error.code 直接是首条错误的真实失败码
    （VERSION_CONFLICT/CONTENT_CONFLICT/SOURCE_CHANGED 等），此时按该码重读后再决定，可重新提交。
    索引未更新优先 wiki-rebuild-index，绝不从数据库覆盖 MD。
    提交后用 wiki-get 读回；VERSION_CONFLICT 重读比较，SOURCE_CHANGED/QUOTE_MISMATCH 重读来源，不伪造引文。
    更新会保留被替换页面为本地恢复副本 .wiki-recovery-<operation_id>.bak（回执给出路径，不参与扫描）。
    """
    return await shared.write(wiki.apply, params, actor="mcp:wiki")


@mcp.tool(name="wiki-delete", annotations=shared.WRITE)
async def wiki_delete(params: wm.DeleteInput) -> dict:
    """删除 Wiki 页面或证据，不删除图谱实体/关系，也不删除原始文件。

    params.kind=page/evidence，id=页面/证据 UUID；页面必填 expected_revision 和 expected_content_hash
    （证据读取会返回 page_content_hash/page_revision，删除该证据时用这个值），证据不可变、不接受版本。
    默认 dry_run=true，返回 data.counts、impact_token 及 partial/index_status/errors，不写入。
    确认后以同输入加 dry_run=false、confirmed=true、request_key、原 impact_token 正式删除；
    依赖/版本变化返回 IMPACT_CONFLICT/VERSION_CONFLICT，必须重读、重新预览和确认。
    删除页面会清理其证据与页面链接，但保留在线历史（预览 clears_online_history=false、retains_history=true，
    含 counts.history；删除后 history 仍可读），不跨功能删除图谱数据；删除证据不影响页面正文。
    被替换/删除的页面文件保留为本地恢复副本 .wiki-recovery-<operation_id>.bak（回执给出路径，不参与扫描），
    不要宣称彻底擦除。备份须同时覆盖 Markdown 文件、SQLite 审计（图谱数据也在数据库内）与这些恢复副本。
    """
    return await shared.write(wiki.delete, params, actor="mcp:wiki")


@mcp.tool(name="wiki-rebuild-index", annotations=shared.WRITE_ADD)
async def wiki_rebuild_index() -> dict:
    """按现有 MD 重建 Wiki 搜索与页面链接索引；不重新生成正文、不覆盖页面、不调用模型。

    用于索引缺失、外部修改 MD 或上次写入 index_status=stale/failed 后的恢复。无参数。
    返回 data 含 index_status（current/stale/failed）与 errors；失败时不要用数据库旧正文反写 MD，重试同一命令即可。
    """
    return await shared.rebuild_index(wiki.rebuild_index)


@mcp.tool(name="wiki-list-recovery", annotations=shared.READ)
async def wiki_list_recovery(status: Literal["needed", "releasable", "orphan"] | None = None,
                             limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50,
                             offset: Annotated[int, Field(strict=True, ge=0)] = 0) -> dict:
    """只读列出 Wiki 本地恢复副本（.wiki-recovery-<operation_id>.bak）及其是否仍被需要；不读取副本正文。

    每次页面更新/删除都会把被替换的原文件留作恢复副本，不会自动清理。data.recovery[] 每项含
    operation_id/page_id/request_key/state/size/status：needed 表示对应操作尚未提交、副本是旧内容的唯一保留，
    不能清理；releasable 表示操作已结算、审计表已有快照，副本只是额外保险；orphan 表示没有对应操作记录。
    data.counts 汇总各状态数量与总字节数。要清理用 wiki-purge-recovery，先 dry_run。
    """
    try:
        from indexing.utils import run_sync
        result = await run_sync(wiki.list_recovery, status=status, limit=limit, offset=offset)
    except Exception as exc:  # noqa: BLE001 - 与其它工具一致地回成 success=false
        from indexing.services.errors import BusinessError
        if not isinstance(exc, BusinessError):
            raise
        return {"success": False, "message": str(exc), "data": exc.data,
                "error": {"code": exc.code, "message": str(exc)}}
    return {"success": True, "message": "查询成功", "data": result}


@mcp.tool(name="wiki-purge-recovery", annotations=shared.WRITE)
async def wiki_purge_recovery(operation_ids: Union[List[str], str, None] = None, include_orphans: bool = False,
                              dry_run: bool = True, confirmed: bool = False) -> dict:
    """清理可释放的 Wiki 恢复副本；只删除本地 .bak 文件，不改页面正文、不动审计表。

    默认 dry_run=true 只返回 data.candidates 与 count/total_bytes，不删除。用户确认后传 dry_run=false、confirmed=true。
    默认只清 releasable；仍被未提交操作需要的 needed 副本永不清理；orphan（无操作记录）须显式 include_orphans=true。
    operation_ids 可限定只清指定操作的副本（来自 wiki-list-recovery）；点名的副本若不能清理会整批拒绝并返回
    RECOVERY_REQUIRED，不会静默跳过。副本删除后无法再从本地恢复被替换的旧内容，审计表的 before 快照仍可读。
    """
    from indexing.services.errors import BusinessError
    from indexing.utils import run_sync

    try:
        result = await run_sync(wiki.purge_recovery, operation_ids=shared.intent_coerce(operation_ids),
                                include_orphans=include_orphans, dry_run=dry_run, confirmed=confirmed)
    except BusinessError as exc:
        return {"success": False, "message": str(exc), "data": exc.data,
                "error": {"code": exc.code, "message": str(exc)}}
    if result.get("failed_count"):
        message = "部分副本未能删除，请按 data.failed 核对"
        return {"success": False, "message": message, "data": result,
                "error": {"code": "PARTIAL_FAILURE", "message": message}}
    return {"success": True, "message": "清理预览" if result.get("dry_run") else "恢复副本已清理", "data": result}


# ==================== 意图式写入（扁平参数，自动 request_key） ====================
# 与 CLI 的 `wiki page add` / `wiki page update` 共用 knowledge_intent 组装逻辑。


@mcp.tool(name="wiki-page-add", annotations=shared.INTENT)
async def wiki_page_add(
    title: Annotated[str, Field(min_length=1, max_length=500)],
    kind: Literal["concept", "entity", "topic", "synthesis", "source_summary"] = "concept",
    summary: Annotated[str, Field(max_length=4000)] = "",
    body: Annotated[str, Field(max_length=200000)] = "",
    aliases: Union[List[str], str, None] = None,
    status: Literal["active", "disputed", "outdated"] = "active",
    evidence: Union[Evidence, str, None] = None,
    stance: Union[Stances, str, None] = None,
    reason: Reason | None = None,
    dry_run: bool = False,
) -> dict:
    """一步新建一个 Wiki 页面（Markdown 正源）并附带证据；request_key 自动生成并回传。

    参数全部是顶层扁平字段，不要套在 params 里。前置：先用 wiki-search 避免重复页面。
    body 是 Markdown 正文，可直接写 [[其他页标题]] 链接（导航用，不是语义关系）。
    evidence 每项 "CHUNK_ID:定位"，用法同图谱的 knowledge-object-add。页面与图谱实体彼此独立，写页不建图；
    页面 kind 与图实体 kind 同名不代表同一条记录。
    提交后用 wiki-get 读回 data.pages[0].id；index_status!=current 时用 wiki-rebuild-index 恢复。
    """
    values = {"kind": kind, "title": title, "summary": summary or None, "body": body or None, "status": status,
              "aliases": shared.intent_coerce(aliases)}
    return await shared.intent_write(wiki, "page", "add", values, evidence, stance,
                                     reason=reason, dry_run=dry_run, actor="mcp:wiki")


@mcp.tool(name="wiki-page-update", annotations=shared.INTENT_UPDATE)
async def wiki_page_update(
    id: Annotated[str, Field(min_length=36, max_length=36)],
    expected_revision: Revision,
    expected_content_hash: ContentHash,
    title: Annotated[str, Field(min_length=1, max_length=500)] | None = None,
    kind: Literal["concept", "entity", "topic", "synthesis", "source_summary"] | None = None,
    summary: Annotated[str, Field(max_length=4000)] | None = None,
    body: Annotated[str, Field(max_length=200000)] | None = None,
    aliases: Union[List[str], str, None] = None,
    status: Literal["active", "disputed", "outdated"] | None = None,
    evidence: Union[Evidence, str, None] = None,
    stance: Union[Stances, str, None] = None,
    reason: Reason | None = None,
    dry_run: bool = False,
) -> dict:
    """增量修订一个 Wiki 页面或只补证据；只填要改的字段。body 是整体替换，不是追加。

    前置：id、expected_revision、expected_content_hash 三个都必填，且全部来自 wiki-get 的
    record.id / record.revision / record.content_hash；两个并发条件缺一不可，因为 MD 可能被外部编辑器改过。
    哈希不符返回 CONTENT_CONFLICT、版本不符返回 VERSION_CONFLICT，重读后再决定，不要只换并发条件覆盖。
    被替换的页面保留本地恢复副本（回执给出路径）。evidence/stance 用法同 wiki-page-add。
    """
    values = {"kind": kind, "title": title, "summary": summary, "body": body, "status": status,
              "aliases": shared.intent_coerce(aliases)}
    return await shared.intent_write(wiki, "page", "update", values, evidence, stance,
                                     reason=reason, dry_run=dry_run, actor="mcp:wiki",
                                     record_id=id, expected_revision=expected_revision,
                                     expected_content_hash=expected_content_hash)
