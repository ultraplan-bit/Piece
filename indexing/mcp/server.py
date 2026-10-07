"""
索引 MCP 服务主入口 (piece-index)

基于 FastMCP 实现的文件和切片管理服务。
"""

import logging
import json
from typing import Annotated, List, Union

from fastmcp import FastMCP
from pydantic import Field, Json
from indexing.utils import run_sync
from indexing import knowledge_models as km
from indexing.services import knowledge_service as knowledge
from indexing import wiki_models as wm
from indexing.services import wiki_service as wiki
from indexing.services.maintenance_service import extract_chunk
from indexing.services.errors import BusinessError

from indexing.mcp.auth import apply_bearer_auth
from indexing.mcp.tools.file_tools import MAX_IMPORT_CHARS, create_empty_file, delete_file, import_markdown as _import_markdown
from indexing.mcp.tools.chunk_tools import (
    ChunkInput,
    MAX_BATCH_CHUNKS,
    create_chunk,
    create_chunks,
    update_chunk_content,
    delete_chunk,
    batch_delete_chunks,
)
from indexing.mcp.tools.task_tools import MAX_TASKS_PER_QUERY, get_task_status, get_tasks_status
from indexing.mcp.tools.query_tools import (
    list_files,
    get_file_info,
    get_chunk_info,
    get_storage_stats,
)
from indexing.mcp.tools.collection_tools import (
    list_collections,
    create_collection,
    move_collection,
    delete_collection,
    assign_file_collections,
)

logger = logging.getLogger(__name__)

ChunkBatch = Annotated[List[ChunkInput], Field(min_length=1, max_length=MAX_BATCH_CHUNKS)]
TaskIds = Annotated[
    List[Annotated[int, Field(gt=0, strict=True)]],
    Field(min_length=1, max_length=MAX_TASKS_PER_QUERY),
]
ImportContent = Annotated[str, Field(min_length=1, max_length=MAX_IMPORT_CHARS)]
CollectionNames = Annotated[List[Annotated[str, Field(min_length=1, max_length=200)]], Field(max_length=100)]


# 创建 FastMCP 实例
# FastMCP 2.14.0+ 支持 strict_input_validation 参数
# 设置为 False 启用灵活验证模式，自动转换字符串参数（如 "5" -> 5）
mcp = FastMCP(
    "piece-index",
    instructions="""
Piece 索引 MCP 服务 - 用户个人知识库文件和切片管理工具

整篇文档（网页、公众号文章、视频字幕、导图大纲等由客户端自行取得的内容）优先用 import_markdown 一次导入：
Piece 会保存原件、按标题自动切片并保留 properties 中的出处（如 title、source_url）。
只有需要手工控制每张卡片时才用 create_file + add_chunk / batch_add_chunks。
注意：add_chunk / batch_add_chunks 需要文件 ID，如果文件不存在请先使用 create_file 创建。
多张卡片优先用 batch_add_chunks，一项一个 task_id；受理成功不等于索引完成，勿重复提交已受理项。
用 check_tasks_status 一次查询多个任务，后续仅查询 unfinished_task_ids，并遵守 poll_after_ms 间隔。
任务完成后直接从状态结果取得 chunk_id，不需要另行检索。

工具分类：
- 文件管理：import_markdown, create_file, remove_file, query_files, query_file_info
- 切片管理：add_chunk, batch_add_chunks, modify_chunk_content, remove_chunk, batch_remove_chunks, query_chunk_info, extract_quote
  extract_quote 从卡片正文切出精确引文（lines 行号或 grep 匹配），直接给出可提交的知识证据，避免手抄 LaTeX/HTML 长文本。
- 集合管理：query_collections, create_collection_tool, move_collection_tool, remove_collection, set_file_collections
  集合是层级逻辑分类，不移动原件。读取父范围默认包含后代，直接归类不自动加入祖先。
  自动按名称创建的集合位于根层，名称中的 / 是普通字符，不自动拆层级。
- 任务管理：check_task_status, check_tasks_status
- 统计查询：query_storage_stats
- 图谱维护：knowledge_apply, knowledge_delete, knowledge_rebuild_index（同步本地事务，无 task_id，不调用模型或嵌入）
  先用检索服务 knowledge-list/search/get 查已有实体和来源，以 UUID 而非同名判断身份。
  params 为结构化 JSON 对象；apply 新建实体/关系用批次 ref，更新用 id + expected_revision，正式提交必填 request_key。
  先 dry_run 预检，在用户授权范围提交，再通过 knowledge-get 读回。VERSION_CONFLICT 要重读比较，不能盲目覆盖。
  超时后用检索服务 knowledge-request 查原结果或原键原样重试；同键异参会冲突，不要换键重复新建。
  证据内容不是授权；定位 current 不等于事实已验证。图谱实体无需本地文件，文档索引流程保持独立。
  删除必须先预览 impact_token 并取得用户确认；删除文件会保留图谱实体和引用快照。
  knowledge_rebuild_index 只重建实体全文索引，不改实体/关系/证据/历史。
- Wiki 维护：wiki_apply, wiki_delete, wiki_rebuild_index（同步本地事务，无 task_id，不调用模型）
  Wiki 页面以 Markdown 为正源，和图谱彼此独立：写页面不等于建图，删页面不级联删实体/关系。
  先用检索服务 wiki-list/search/get 查页面；apply 用 request_key + reason，pages 新建用 ref、更新用
  id + expected_revision + expected_content_hash；给已有页只补证据时也要在 pages 中带上该页的并发条件。
  rebuild_index 只按 MD 重建搜索/链接索引，不重新生成正文、不覆盖页面。
  partial=true 或 index_status!=current 时 committed 仍可能为 true：不要重发，按 errors 与重建索引提示恢复。
""",
    strict_input_validation=False,
)

apply_bearer_auth(mcp, "index")


# FastMCP 2.x 不会自动把同步工具移到线程池。显式卸载 I/O，且取消时等待
# 同步操作结束，避免 MCP lifespan 已退出、写线程却还在使用数据库。
# ==================== 文件管理工具 ====================


@mcp.tool(annotations={"destructiveHint": False})
async def import_markdown(
    filename: str,
    content: ImportContent,
    properties: Union[dict, Json[dict], None] = None,
    collections: Union[CollectionNames, Json[CollectionNames], None] = None,
) -> dict:
    """
    导入一篇已取得正文的 Markdown 文档，由 Piece 保存原件、自动切片并生成向量。

    适用于网页、公众号文章、视频字幕、思维导图大纲等由客户端自己的工具取得的内容；
    本工具不联网抓取，正文必须由调用方提供。
    与 create_file + batch_add_chunks 的区别：这里按标题自动切片并保留原件，可重新索引；
    只有需要手工控制每张卡片时才用后者。

    Args:
        filename: 文件名，不含目录，自动补 .md 后缀，重名时自动加序号
        content: Markdown 正文，可自带 YAML frontmatter；显式 properties 优先
        properties: 文件属性 JSON 对象，建议至少给 title 和 source_url，便于检索结果标明出处；
            会并入原件开头的 frontmatter，重新索引也不会丢失
        collections: 集合名列表，不存在的集合自动创建

    Returns:
        - success: 是否受理
        - data.file_id / data.filename: 新建或已有的文件
        - data.task_id / data.task_ids: 索引任务，受理成功不等于索引完成，用 check_task_status 查询终态
        - data.duplicate: 相同正文已存在时为 true，返回已有 file_id 且不创建新任务

    Example:
        >>> import_markdown(
        ...     filename="某公众号文章",
        ...     content="# 标题\\n\\n正文……",
        ...     properties={"title": "标题", "source_url": "https://mp.weixin.qq.com/s/xxx", "author": "作者"},
        ...     collections=["网页收藏"],
        ... )
    """
    logger.info("[MCP Tool] import_markdown: filename=%s, chars=%s", filename, len(content))
    return await run_sync(_import_markdown, filename, content, properties, collections)


@mcp.tool()
async def create_file(filename: str) -> dict:
    """
    创建空白 Markdown 文件

    Args:
        filename: 文件名（自动补充 .md 后缀，处理重名冲突）

    Returns:
        包含文件信息的字典：
        - success: 是否成功
        - message: 结果消息
        - data: 文件数据（file_id, filename, file_path, status）

    Example:
        创建一个名为"学术论文_2024研究"的空文件：
        >>> create_file("学术论文_2024研究")
    """
    logger.info(f"[MCP Tool] create_file: filename={filename}")
    return await run_sync(create_empty_file, filename)


@mcp.tool(annotations={"destructiveHint": True})
async def remove_file(file_id: int, dry_run: bool = False, confirmed: bool = False) -> dict:
    """删除文件、卡片和原件副本；知识对象和引用快照保留，来源标记 missing。

    先 dry_run=true 预览 data.knowledge_evidence_count 及删除影响，用户确认后传 confirmed=true。
    文件删除不等于全库知识擦除；彻底移除引用需显式删除相关证据和知识内容。
    返回 success/message/data，data 包含 file_id/filename/deleted_chunks 及保留快照提示。
    """
    return await run_sync(delete_file, file_id, dry_run, confirmed)


@mcp.tool()
async def query_files(limit: int = 20, offset: int = 0, status: str = None,
                      collections: Union[CollectionNames, Json[CollectionNames], None] = None,
                      include_descendants: bool = True, uncategorized: bool = False,
                      name: str = None) -> dict:
    """
    分页列出文件，集合读取默认包含后代并去重；不附带正文。

    Args:
        collections: 完整集合名的并集；省略/null 不限定，[] 无结果，未知名报错，不回退全库。
        include_descendants: 默认 true；false 仅匹配直接归属，不展开子集合。
        uncategorized: 只返回没有任何直接集合关联的文件（不是不属于某父集合）。
        name: 文件名包含的文字（不是通配符）。
        limit: 每页数量（默认 20，最大 100）
        offset: 偏移量（默认 0）
        status: 可选，按状态筛选 ('pending', 'indexed', 'error', 'empty')

    Returns:
        包含文件列表的字典：
        - success: 是否成功
        - message: 结果消息
        - data: 文件数据（files, total, limit, offset）

    Example:
        查询前 10 个已索引的文件：
        >>> query_files(limit=10, offset=0, status="indexed")
    """
    logger.info(f"[MCP Tool] query_files: limit={limit}, offset={offset}, status={status}")
    return await run_sync(list_files, limit=limit, offset=offset, status=status, collections=collections,
                          include_descendants=include_descendants, uncategorized=uncategorized, name=name)


@mcp.tool()
async def query_file_info(file_id: int) -> dict:
    """
    获取文件详情（包含切片数量等统计信息）

    Args:
        file_id: 文件 ID

    Returns:
        包含文件详情的字典：
        - success: 是否成功
        - message: 结果消息
        - data: 文件详细信息（包含 chunks_count）

    Example:
        获取文件 123 的详情：
        >>> query_file_info(123)
    """
    logger.info(f"[MCP Tool] query_file_info: file_id={file_id}")
    return await run_sync(get_file_info, file_id)


# ==================== 切片管理工具 ====================


@mcp.tool()
async def add_chunk(file_id: int, doc_title: str, chunk_text: str) -> dict:
    """
    新增切片（异步生成向量）

    Args:
        file_id: 文件 ID
        doc_title: 文档标题（用于一阶段检索返回目标）
        chunk_text: 切片文本内容

    Returns:
        包含切片信息的字典：
        - success: 是否成功
        - message: 结果消息
        - data: 切片数据（task_id, doc_title, chunk_text）
          注意：task_id 是异步任务ID，不是 chunk_id
          切片创建后会异步生成向量，chunk 在向量生成完成后才会被创建

    Workflow:
        1. 调用 add_chunk() 创建切片，返回 task_id
        2. 使用 check_task_status(task_id) 轮询任务状态
        3. 任务完成后，切片已创建并可以通过检索 MCP 查询
        4. 如需修改切片，必须等任务完成后才能获取 chunk_id

    Example:
        为文件 123 添加一个切片：
        >>> result = add_chunk(
        ...     file_id=123,
        ...     doc_title="论文_摘要",
        ...     chunk_text="本文提出了一种新的方法..."
        ... )
        >>> task_id = result["data"]["task_id"]
        >>> # 轮询任务状态
        >>> check_task_status(task_id)
        >>> # 任务完成后，切片已创建

    Note:
        - 返回的 task_id 用于查询向量生成进度，不是 chunk_id
        - 切片在向量生成完成后才会被写入数据库
        - 如需后续操作（修改/删除），请等待任务完成后从 check_task_status 的结果取得 chunk_id
    """
    logger.info(
        f"[MCP Tool] add_chunk: file_id={file_id}, " f"doc_title={doc_title[:50]}..."
    )
    return await run_sync(create_chunk, file_id, doc_title, chunk_text)


@mcp.tool(annotations={"destructiveHint": False, "idempotentHint": False})
async def batch_add_chunks(
    file_id: Annotated[int, Field(gt=0, strict=True)],
    chunks: Union[ChunkBatch, Json[ChunkBatch]],
) -> dict:
    """
    批量新增同一文件的卡片，逐项受理并返回 task_id，不等待向量生成。

    Args:
        file_id: 已存在的文件或卡片盒 ID
        chunks: 1-50 个 {doc_title, chunk_text} 对象，支持数组或 JSON 字符串。
            整批标题和正文合计最多 200000 个字符。

    Returns:
        success 表示是否全部受理，不代表索引已完成。
        data.items 按输入顺序返回 index（从 0 开始）、doc_title、task_id；
        受理失败项的 task_id 为 null，并附 error_message。
        data.task_ids 可直接交给 check_tasks_status；另附 accepted_count/rejected_count。
        不重复返回正文。结构错误或整批超限时不会创建任何任务。

    Note:
        允许部分受理；即使 success=false，也必须保留已返回的 task_ids。
        此操作非幂等：只重试受理失败项，勿重复提交已受理项或整批重发。
        同一文件的任务按队列顺序执行，不同文件可并发。
    """
    logger.info("[MCP Tool] batch_add_chunks: file_id=%s, count=%s", file_id, len(chunks))
    return await run_sync(create_chunks, file_id, chunks)


@mcp.tool()
async def modify_chunk_content(chunk_id: int, new_content: str) -> dict:
    """
    修改切片内容（异步重新生成向量）

    Args:
        chunk_id: 切片 ID（注意：不是 task_id，是已存在的切片ID）
        new_content: 新的切片文本内容

    Returns:
        包含更新结果的字典：
        - success: 是否成功
        - message: 结果消息
        - data: 更新信息（chunk_id, task_id, new_content）
          注意：task_id 是异步任务ID，用于查询向量重新生成进度

    Workflow:
        1. 调用 modify_chunk_content(chunk_id, new_content) 修改内容
        2. 使用返回的 task_id 通过 check_task_status() 监控向量生成
        3. 任务完成后，切片内容和向量都已更新

    Example:
        修改切片 456 的内容：
        >>> result = modify_chunk_content(456, "更详细的描述...")
        >>> task_id = result["data"]["task_id"]
        >>> # 轮询任务状态
        >>> check_task_status(task_id)

    Note:
        - chunk_id 必须是已存在的切片ID（通过检索 MCP 查询获得）
        - 修改内容后会异步重新生成向量，可通过返回的 task_id 查询进度
        - 不要将 add_chunk 返回的 task_id 用作 chunk_id
    """
    logger.info(
        f"[MCP Tool] modify_chunk_content: chunk_id={chunk_id}, "
        f"new_content={new_content[:50]}..."
    )
    return await run_sync(update_chunk_content, chunk_id, new_content)


@mcp.tool()
async def remove_chunk(chunk_id: int) -> dict:
    """
    删除切片（同步删除向量）

    Args:
        chunk_id: 切片 ID

    Returns:
        包含删除结果的字典：
        - success: 是否成功
        - message: 结果消息
        - data: 删除信息（chunk_id, doc_title, file_deleted）

    Example:
        删除切片 456：
        >>> remove_chunk(456)

    Note:
        如果删除的是文件的最后一个切片，文件会被自动删除。
    """
    logger.info(f"[MCP Tool] remove_chunk: chunk_id={chunk_id}")
    return await run_sync(delete_chunk, chunk_id)


@mcp.tool()
async def batch_remove_chunks(chunk_ids: Union[list, str]) -> dict:
    """
    批量删除切片（同步删除向量）

    Args:
        chunk_ids: 切片 ID 列表（支持列表或字符串形式）

    Returns:
        包含批量删除结果的字典：
        - success: 是否成功
        - message: 结果消息
        - data: 删除统计（deleted_count, failed_count, deleted_files, errors）

    Example:
        批量删除切片 456, 457, 458：
        >>> batch_remove_chunks([456, 457, 458])

    Note:
        如果删除的切片数量等于文件的总切片数，会自动删除整个文件。
    """
    # 处理字符串形式的列表（某些模型会传递 "[1,2,3]" 而不是 [1,2,3]）
    if isinstance(chunk_ids, str):
        try:
            chunk_ids = json.loads(chunk_ids)
        except json.JSONDecodeError:
            logger.error(f"[MCP Tool] batch_remove_chunks: 无效的 chunk_ids 格式: {chunk_ids}")
            return {
                "success": False,
                "message": f"无效的 chunk_ids 格式。期望 JSON 数组如 [1, 2, 3]，实际收到: {repr(chunk_ids)}",
                "data": None
            }

    logger.info(f"[MCP Tool] batch_remove_chunks: chunk_ids={chunk_ids}")
    return await run_sync(batch_delete_chunks, chunk_ids)


@mcp.tool()
async def query_chunk_info(chunk_id: int) -> dict:
    """
    获取切片详情

    Args:
        chunk_id: 切片 ID

    Returns:
        包含切片详情的字典：
        - success: 是否成功
        - message: 结果消息
        - data: 切片详细信息（id, file_id, doc_title, chunk_text）

    Example:
        获取切片 456 的详情：
        >>> query_chunk_info(456)
    """
    logger.info(f"[MCP Tool] query_chunk_info: chunk_id={chunk_id}")
    return await run_sync(get_chunk_info, chunk_id)


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False})
async def extract_quote(chunk_id: int, lines: str = None, grep: str = None,
                        context: int = 0, max_matches: int = 20, regex: bool = False) -> dict:
    """从卡片正文切出精确引文，直接得到可提交为知识证据的字段，避免手抄长文本出错。

    lines 与 grep 必须且只能提供一个：lines 用 "12" 或 "12-14"（行号从 1 起）；
    grep 按行匹配，默认子串、regex=true 用正则，context 为前后各取行数，max_matches 上限 50。
    返回 matches[].quote（正文精确子串）与 matches[].evidence（含 source_library_id/file_id/chunk_id/
    expected_content_hash/quote），可直接放入 knowledge_apply 的 evidence。定位宽松，引文精确；
    引文是否支持结论仍须自行判断。
    """
    payload = km.ChunkExtractInput(chunk_id=chunk_id, lines=lines, grep=grep,
                                   context=context, max_matches=max_matches, regex=regex)
    logger.info(f"[MCP Tool] extract_quote: chunk_id={chunk_id}")
    return await run_sync(extract_chunk, **payload.model_dump())


# ==================== 任务管理工具 ====================


@mcp.tool()
async def check_task_status(task_id: int) -> dict:
    """
    查询任务状态（用于监控异步向量生成任务）

    Args:
        task_id: 任务 ID

    Returns:
        包含任务状态的字典：
        - success: 是否成功
        - message: 结果消息
        - data: 任务数据（task_id, status, progress, chunk_id, error_message等）
          重要：任务完成后会返回 chunk_id（新创建或修改的切片ID）

    Example:
        查询任务 789 的状态：
        >>> result = check_task_status(789)
        >>> if result["data"]["status"] == "completed":
        ...     chunk_id = result["data"]["chunk_id"]  # 获取切片ID
        ...     # 现在可以使用 chunk_id 进行后续操作

    Note:
        - 任务状态包括：pending（待处理）、processing（处理中）、
          completed（已完成）、failed（失败）、cancelled（已取消）
        - 任务完成（status="completed"）后，data.chunk_id 会返回创建的切片ID
        - 可以使用返回的 chunk_id 调用 modify_chunk_content 或 remove_chunk
    """
    logger.info(f"[MCP Tool] check_task_status: task_id={task_id}")
    return await run_sync(get_task_status, task_id)


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def check_tasks_status(task_ids: Union[TaskIds, Json[TaskIds]]) -> dict:
    """
    一次查询多个异步任务的状态，不阻塞等待任务完成。

    Args:
        task_ids: 1-100 个正整数任务 ID，支持数组或 JSON 字符串；重复 ID 会去重。

    Returns:
        data.tasks 按请求顺序返回 task_id、status、progress、chunk_id、error_message。
        data.summary 汇总各状态数量；不存在的 ID 单列在 data.not_found。
        all_done 表示所有请求的任务均存在且已结束（含 failed/cancelled），
        all_succeeded 仅在所有任务都 completed 时为 true；两者不可混淆。
        success 仅表示查询是否成功，不表示任务执行成功；缺失 ID 时为 false。

    Workflow:
        保存每轮已结束任务的 chunk_id 或错误，只用 unfinished_task_ids 继续查询。
        至少等待 poll_after_ms 毫秒再查询；该列表为空时无需继续轮询。
        not_found 不是待完成任务，需要核对 ID，不应反复轮询。
    """
    logger.info("[MCP Tool] check_tasks_status: task_ids=%s", task_ids)
    return await run_sync(get_tasks_status, task_ids)


# ==================== 统计查询工具 ====================


@mcp.tool()
async def query_storage_stats() -> dict:
    """
    获取存储统计信息

    Returns:
        包含存储统计的字典：
        - success: 是否成功
        - message: 结果消息
        - data: 统计数据（total_files, indexed_files, total_chunks, total_size）

    Example:
        获取存储统计：
        >>> query_storage_stats()
        {
            "success": True,
            "message": "查询成功",
            "data": {
                "total_files": 50,
                "indexed_files": 45,
                "total_chunks": 1250,
                "total_size": 52428800
            }
        }
    """
    logger.info(f"[MCP Tool] query_storage_stats")
    return await run_sync(get_storage_stats)


# ==================== 集合管理工具 ====================


@mcp.tool()
async def query_collections() -> dict:
    """
    列出所有层级集合。集合是逻辑分类，不移动文件；读取父范围默认包含后代。

    Returns:
        包含集合列表的字典：
        - success: 是否成功
        - message: 结果消息
        - data: collections 含 id, name, description, parent_id, path（{id,name} 数组）, child_count,
          direct_file_count（直接归属数）, subtree_file_count（子树唯一文件数）, file_count（等于 subtree_file_count）。

    Example:
        >>> query_collections()
    """
    logger.info("[MCP Tool] query_collections")
    return await run_sync(list_collections)


@mcp.tool()
async def create_collection_tool(name: str, description: str = None,
                                 parent_id: Annotated[int, Field(gt=0)] | None = None) -> dict:
    """
    创建集合

    Args:
        name: 集合名（唯一，如"论文""工作笔记"）
        description: 集合说明（可选）
        parent_id: 父集合 ID；省略或 null 创建根集合，不从名称中的 / 推导层级。

    Returns:
        包含创建结果的字典：
        - success: 是否成功
        - message: 结果消息
        - data: 集合数据（collection_id, name）

    Example:
        >>> create_collection_tool("论文", "研究方向相关的文献")
    """
    logger.info(f"[MCP Tool] create_collection: name={name}")
    return await run_sync(create_collection, name, description, parent_id)


@mcp.tool(annotations={"destructiveHint": False})
async def move_collection_tool(collection_id: Annotated[int, Field(gt=0)],
                               parent_id: Annotated[int, Field(gt=0)] | None) -> dict:
    """移动集合到明确的父 ID；parent_id 必传，null 表示根层。

    拒绝移动到自身或任意后代。只改变逻辑父子关系，不修改文件的直接归属、ID 或原件路径。
    返回 success/message/data，data 含 collection_id 和新 parent_id；移动后可用 query_collections 读回路径。
    """
    return await run_sync(move_collection, collection_id, parent_id)


@mcp.tool(annotations={"destructiveHint": True})
async def remove_collection(collection_id: Annotated[int, Field(gt=0)],
                            dry_run: bool = False, confirmed: bool = False) -> dict:
    """删除叶子集合，绝不删除文件或原件；有子集合时拒绝。

    先以 dry_run=true 预览 data.direct_file_count（解除归类数）和 unclassified_file_count（变为未归类数）。
    用户明确同意后才以 confirmed=true 执行；没有确认返回 CONFIRMATION_REQUIRED。最终执行会重新检查子集合。
    data.collection 保留集合信息，deletes_files 始终 false；success 仅表示此操作成功。
    """
    return await run_sync(delete_collection, collection_id, dry_run, confirmed)


@mcp.tool()
async def set_file_collections(file_id: int, collection_names: Union[list, str]) -> dict:
    """
    设置文件所属集合（覆盖式），集合不存在时自动创建

    一个文件可同时属于多个集合，只写明确名称的直接关联，不自动加入祖先或后代。
    传空列表表示取消所有归类；自动创建的集合位于根层，名称中的 / 是普通字符。

    Args:
        file_id: 文件 ID
        collection_names: 集合名列表，支持 JSON 字符串

    Returns:
        包含归类结果的字典：
        - success: 是否成功
        - message: 结果消息
        - data: 归类信息（file_id, filename, collections）

    Example:
        把文件 123 同时归入"论文"和"技术文档"：
        >>> set_file_collections(123, ["论文", "技术文档"])
    """
    if isinstance(collection_names, str):
        try:
            collection_names = json.loads(collection_names)
        except json.JSONDecodeError:
            collection_names = [collection_names] if collection_names else []
    if not isinstance(collection_names, list) or any(not isinstance(name, str) for name in collection_names):
        return {"success": False, "message": "collection_names 必须是字符串数组", "data": None}

    logger.info(
        f"[MCP Tool] set_file_collections: file_id={file_id}, names={collection_names}"
    )
    return await run_sync(assign_file_collections, file_id, collection_names)


async def _knowledge_write(function, params):
    try:
        result = await run_sync(function, params, actor="mcp:index")
    except BusinessError as exc:
        return {"success": False, "message": str(exc), "data": exc.data,
                "error": {"code": exc.code, "message": str(exc)}}
    # 部分提交不能返回 success=true 冒充全成功；committed 只表示至少一条已写入，不预设失败原因。
    partial = bool(result.get("partial")) or result.get("index_status") not in (None, "current")
    if partial:
        message = "部分提交：committed 只表示至少一条已写入；请按 data.errors 与逐页回执核对，不要整批复发"
        return {"success": False, "message": message, "data": result,
                "error": {"code": "PARTIAL_FAILURE", "message": message}}
    return {"success": True, "message": "已提交" if result.get("committed") else "预检通过，未写入", "data": result}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
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
    提交后用检索服务 knowledge-get 读回。仅有引文不代表引文支持断言；保留双方证据与适用条件，不自动合并同名对象。
    """
    return await _knowledge_write(knowledge.apply, params)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
async def knowledge_delete(params: km.DeleteInput) -> dict:
    """删除知识记录，不删除原始文件；必须先预览并经用户确认。

    params.kind=object/relation/evidence，id=知识 UUID；实体/关系必填 expected_revision，证据不可变、不接受版本。
    默认 dry_run=true，返回 data.counts（关联边、证据及在线历史清理数）和 impact_token，不写入。
    确认后以同 kind/id/expected_revision 加 dry_run=false、confirmed=true、request_key、原 impact_token 正式删除。
    依赖/版本变化返回 IMPACT_CONFLICT/VERSION_CONFLICT，必须重读、重新预览和确认，不能自动扩大删除范围。
    删除实体/关系清理附属证据和在线历史；证据删除不会在请求日志藏引用副本。备份不属于在线删除范围。
    返回 success/message/data（committed/counts/deletes_files=false/backups_affected=false）；失败含 error.code。
    超时用检索服务 knowledge-request 查询或原键原样重试；再用 knowledge-get 确认 NOT_FOUND。
    """
    return await _knowledge_write(knowledge.delete, params)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
async def knowledge_rebuild_index() -> dict:
    """重建图谱实体的派生全文索引（FTS）；不修改实体、关系、证据或历史，不调用模型。

    用于索引缺失或查询结果异常后的恢复。无参数。返回 data 含 index_status 与 indexed_objects；
    失败不会清空或改写已有实体/关系/证据，重试同一命令即可。
    """
    try:
        result = await run_sync(knowledge.rebuild_index)
    except BusinessError as exc:
        return {"success": False, "message": str(exc), "data": exc.data,
                "error": {"code": exc.code, "message": str(exc)}}
    partial = bool(result.get("partial")) or result.get("index_status") not in (None, "current")
    if partial:
        message = "索引重建未全部完成，请按 data.errors 处理后可原样重试"
        return {"success": False, "message": message, "data": result,
                "error": {"code": "PARTIAL_FAILURE", "message": message}}
    return {"success": True, "message": "索引已重建", "data": result}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
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
    索引未更新优先 wiki_rebuild_index，绝不从数据库覆盖 MD。
    提交后用检索服务 wiki-get 读回；VERSION_CONFLICT 重读比较，SOURCE_CHANGED/QUOTE_MISMATCH 重读来源，不伪造引文。
    更新会保留被替换页面为本地恢复副本 .wiki-recovery-<operation_id>.bak（回执给出路径，不参与扫描）。
    """
    return await _knowledge_write(wiki.apply, params)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
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
    return await _knowledge_write(wiki.delete, params)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
async def wiki_rebuild_index() -> dict:
    """按现有 MD 重建 Wiki 搜索与页面链接索引；不重新生成正文、不覆盖页面、不调用模型。

    用于索引缺失、外部修改 MD 或上次写入 index_status=stale/failed 后的恢复。无参数。
    返回 data 含 index_status（current/stale/failed）与 errors；失败时不要用数据库旧正文反写 MD，重试同一命令即可。
    """
    try:
        result = await run_sync(wiki.rebuild_index)
    except BusinessError as exc:
        return {"success": False, "message": str(exc), "data": exc.data,
                "error": {"code": exc.code, "message": str(exc)}}
    partial = bool(result.get("partial")) or result.get("index_status") not in (None, "current")
    if partial:
        message = "索引重建未全部完成，请按 data.errors 处理后可原样重试"
        return {"success": False, "message": message, "data": result,
                "error": {"code": "PARTIAL_FAILURE", "message": message}}
    return {"success": True, "message": "索引已重建", "data": result}
