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
Piece 索引 MCP 服务：用户个人知识库的文件、卡片、集合与索引任务管理。每个工具的 docstring 写明了它自己的前置条件、
参数来源和失败后的下一步；本说明只讲跨工具的全局约定。

导入路径：整篇已取得正文的文档用 import_markdown（自动切片、保留原件、可重新索引）；只有需要逐张控制卡片标题和正文时
才 create_file 拿 file_id 再 add_chunk / batch_add_chunks。正文按内容查重，重复导入返回已有文件而不新建任务。

异步：文件/卡片写入返回 task_id，受理不等于索引完成；用 check_tasks_status 查终态并从结果取 chunk_id。
允许部分受理，即使 success=false 也要保留已返回的 task_ids，只重试失败项，不要整批重发。

集合：集合是逻辑分类树，不移动原件；一个文件可直接属于多个集合。读取父范围默认包含后代，include_descendants=false 只读直接归属；
未归类用 uncategorized。名称中的 / 是普通字符，不表示父子关系。

删除与重索引：删除类工具必须先 dry_run 预览并取得用户确认；删除文件不会擦除已有知识证据，来源会标记 missing。

跨服务：只读检索与精确引文在 piece-kb（resolve-keywords / get-docs / get-source-page / extract-quote）；
图谱实体与语义关系在 piece-graph；Wiki 页面在 piece-wiki。写前先用对应服务查重，写后读回验证。
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
    创建空白 Markdown 文件（卡片盒），供 add_chunk / batch_add_chunks 手工写卡片。

    整篇已取得正文的文档不要走这里，直接用 import_markdown 一次导入（自动切片、保留原件）。
    只有需要逐张控制卡片标题和正文时才 create_file，再拿返回的 file_id 去 add_chunk / batch_add_chunks。

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
    向已有文件新增一张卡片（异步生成向量）。

    前置：file_id 必须已存在，没有就先 create_file 取得；多张卡片优先用 batch_add_chunks。
    整篇文档不要手工拆卡片，直接 import_markdown。

    Args:
        file_id: 已存在的文件 ID（create_file / import_markdown / query_files 返回）
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

    前置：file_id 必须已存在，没有就先 create_file 取得。整篇文档直接 import_markdown，不要手工拆卡片。

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
        用 check_tasks_status 查终态，后续只查 unfinished_task_ids 并遵守 poll_after_ms；完成后从结果取 chunk_id。
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
