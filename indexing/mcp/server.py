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
- 切片管理：add_chunk, batch_add_chunks, modify_chunk_content, remove_chunk, batch_remove_chunks, query_chunk_info
- 集合管理：query_collections, create_collection_tool, set_file_collections
- 任务管理：check_task_status, check_tasks_status
- 统计查询：query_storage_stats
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


@mcp.tool()
async def remove_file(file_id: int) -> dict:
    """
    删除文件（级联删除所有切片和物理文件）

    Args:
        file_id: 文件 ID

    Returns:
        包含删除结果的字典：
        - success: 是否成功
        - message: 结果消息
        - data: 删除信息（file_id, filename, deleted_chunks）

    Example:
        删除 ID 为 123 的文件：
        >>> remove_file(123)
    """
    logger.info(f"[MCP Tool] remove_file: file_id={file_id}")
    return await run_sync(delete_file, file_id)


@mcp.tool()
async def query_files(limit: int = 20, offset: int = 0, status: str = None) -> dict:
    """
    列出所有文件（支持分页和状态过滤）

    Args:
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
    return await run_sync(list_files, limit=limit, offset=offset, status=status)


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
    列出所有集合（按领域归类文件的分组）

    Returns:
        包含集合列表的字典：
        - success: 是否成功
        - message: 结果消息
        - data: 集合数据（collections: [{id, name, description, file_count}]）

    Example:
        >>> query_collections()
    """
    logger.info("[MCP Tool] query_collections")
    return await run_sync(list_collections)


@mcp.tool()
async def create_collection_tool(name: str, description: str = None) -> dict:
    """
    创建集合

    Args:
        name: 集合名（唯一，如"论文""工作笔记"）
        description: 集合说明（可选）

    Returns:
        包含创建结果的字典：
        - success: 是否成功
        - message: 结果消息
        - data: 集合数据（collection_id, name）

    Example:
        >>> create_collection_tool("论文", "研究方向相关的文献")
    """
    logger.info(f"[MCP Tool] create_collection: name={name}")
    return await run_sync(create_collection, name, description)


@mcp.tool()
async def set_file_collections(file_id: int, collection_names: Union[list, str]) -> dict:
    """
    设置文件所属集合（覆盖式），集合不存在时自动创建

    一个文件可同时属于多个集合。传空列表表示取消所有归类。

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
    if not isinstance(collection_names, list):
        collection_names = []

    logger.info(
        f"[MCP Tool] set_file_collections: file_id={file_id}, names={collection_names}"
    )
    return await run_sync(assign_file_collections, file_id, collection_names)
