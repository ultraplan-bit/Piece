"""
SQLite检索MCP服务 - 主入口文件
基于FastMCP实现的智能知识检索服务：
1. resolve-keywords: 智能关键词解析（三路检索+RRF重排，返回20个关键词）
2. get-docs: 精确文档获取（最多接受3个关键词，支持图片续取）
3. list-collections: 查询文件集合
4. get-source-page: 按文件和页码查看原文页面
"""

import json
from fastmcp import FastMCP, Context
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations
from typing import List, Annotated, Union, Optional
from pydantic import Field
from indexing.utils import run_sync

from .tools import collect_chunk_images, resolve_database_keywords, get_docs
from indexing.mcp.auth import apply_bearer_auth
from indexing.services.errors import BusinessError
from indexing.settings import get_mcp_config


# 创建FastMCP服务实例
# FastMCP 2.14.0+ 支持 strict_input_validation 参数
# 设置为 False 启用灵活验证模式，自动转换字符串参数（如 "5" -> 5）
mcp = FastMCP(
    name="piece-kb",
    instructions="Piece searches user's personal documents. Call resolve-keywords to find relevant documents, then get-docs to retrieve content. Use get-source-page with a file_id and 1-based page_number to inspect a full source page when OCR, tables, formulas, or layout need verification.",
    strict_input_validation=False,
)

apply_bearer_auth(mcp, "retrieval")


def _coerce_str_list(value: Union[List[str], str, None]) -> Optional[List[str]]:
    """把模型可能传来的 JSON 字符串或单个字符串统一成字符串列表。"""
    if value is None or isinstance(value, list):
        return value or None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value] if value else None
        if isinstance(parsed, list):
            return [str(item) for item in parsed] or None
        return [str(parsed)] if parsed else None
    return None


@mcp.tool(
    name="list-collections",
    description="Lists the user's collections (named groups of documents, e.g. 'papers', 'work notes'). Call this before resolve-keywords when the user refers to a topic area, so you can pass the exact collection name and keep unrelated domains out of the search.",
    tags={"retrieval", "collections"},
)
async def list_collections_tool(ctx: Context) -> dict:
    from indexing.services.collection_service import list_collections

    collections = await run_sync(list_collections)
    await ctx.info(f"[Tool 3] Listed {len(collections)} collections")
    return {
        "collections": [
            {
                "name": item["name"],
                "description": item.get("description"),
                "file_count": item["file_count"],
            }
            for item in collections
        ]
    }


@mcp.tool(
    name="resolve-keywords",
    description="Resolves queries to relevant document keywords (doc_title). Returns up to 20 keyword candidates with confidence scores. IMPORTANT: When user mentions a specific book, document, or file name (e.g., 'find in the Python book', 'from my ML notes'), you SHOULD use the filenames parameter to narrow search scope for more accurate results. When the user names a topic area or library section (e.g., 'in my papers', 'from the work notes'), use the collections parameter instead - call list-collections first if you are unsure which collections exist.",
    tags={"retrieval", "keywords"},
)
async def resolve_keywords_tool(
    ctx: Context,
    query: Annotated[
        str, Field(description="Query text to search for relevant documents")
    ],
    filenames: Annotated[
        Optional[Union[List[str], str]],
        Field(description="Filter search to specific files. USE THIS when user mentions a book/document name. Supports fuzzy match - no need for exact filename or extension. Example: 'machine learning' matches 'machine learning.md', 'machine learning notes.md', etc. Just extract the key name from user's query.")
    ] = None,
    collections: Annotated[
        Optional[Union[List[str], str]],
        Field(description="Filter search to whole collections (user-defined groups of files, e.g. 'papers', 'work', 'recipes'). USE THIS when the user refers to a topic area rather than one document - it keeps unrelated domains out of the results. Supports fuzzy match. Use list-collections to discover available names.")
    ] = None,
    max_results: Annotated[
        int,
        Field(
            description="Maximum number of keywords to return (default: 20, range: 1-50)"
        ),
    ] = 20,
) -> dict:
    # 处理字符串形式的列表（某些模型会传递 '["name1","name2"]' 而不是 ["name1","name2"]）
    filenames = _coerce_str_list(filenames)
    collections = _coerce_str_list(collections)

    await ctx.info(
        f"[Tool 1] Resolving query: {query}, max_results: {max_results}, "
        f"filenames: {filenames}, collections: {collections}"
    )

    try:
        # 调用核心工作流（传递可选的文件名/集合过滤）
        result = await resolve_database_keywords(
            query, filenames=filenames, collections=collections
        )

        keywords = result.get("keywords", [])
        # 如果需要，可以根据max_results截断结果
        if max_results and max_results < len(keywords):
            keywords = keywords[:max_results]
            result["keywords"] = keywords
            await ctx.info(f"[Tool 1] Truncated to max_results={max_results}")

        await ctx.info(f"[Tool 1] Resolved {len(keywords)} keywords")

        # 通过 ctx.info() 输出调试信息（不返回给 AI 客户端）
        debug_stats = result.pop("debug_stats", {})
        file_ids_filter = debug_stats.get("file_ids_filter")
        await ctx.info(
            f"[Tool 1] Debug - Query: {debug_stats.get('query', '')}, "
            f"Cleaned: {debug_stats.get('cleaned_query', '')}, "
            f"Tokens: {debug_stats.get('tokens', [])}"
        )
        await ctx.info(
            f"[Tool 1] Stats - Title: {debug_stats.get('exact_recall_count', 0)}, "
            f"BM25: {debug_stats.get('bm25_recall_count', 0)}, "
            f"Vector: {debug_stats.get('vector_recall_count', 0)}, "
            f"Fused: {result.get('stats', {}).get('total_fused_results', 0)}, "
            f"FileFilter: {file_ids_filter if file_ids_filter else 'None (global)'}"
        )

        # 返回精简结果（不包含 debug_stats）
        return result

    except Exception as e:
        await ctx.error(f"[Tool 1] Failed: {str(e)}")
        raise


@mcp.tool(
    name="get-docs",
    description="Retrieves full document content and metadata for specified doc_titles (up to 3). Returns chunk_id, file_id, filename, chunk_text, heading_path (where the chunk sits in the document outline), total_chunks_in_file, chunk_index_in_file, plus collections and properties (frontmatter fields such as author/date/source) when available - cite those when the user asks where an answer came from. Input exact doc_title strings from resolve-keywords results. Set include_images=true when the content references figures/charts you need to actually see - the referenced images are returned as inline image data. Image batches follow the user's per-call limit (default 6). If next_image_offset is not null, retrieve more images with include_images=true and image_offset=next_image_offset, keeping doc_titles in the same order and max_docs unchanged; null means no images remain. For full-page source verification, use get-source-page with file_id and page_number.",
    tags={"docs", "retrieval"},
    # 返回类型含 ToolResult 时 FastMCP 不再自动推导 schema，这里显式补回原有声明
    output_schema={"type": "object", "additionalProperties": True},
)
async def get_docs_tool(
    ctx: Context,
    doc_titles: Annotated[
        Union[List[str], str], Field(description="List of doc_titles to retrieve (max 3), supports list or JSON string")
    ],
    include_metadata: Annotated[
        bool,
        Field(description="Include document metadata in response (default: false)"),
    ] = False,
    include_images: Annotated[
        Optional[bool],
        Field(
            description="Return the figures referenced by the retrieved content as inline images. Omit to follow the user's app setting; pass true/false to override it for this call. Costs extra tokens, so only force it on when the figures matter."
        ),
    ] = None,
    max_docs: Annotated[
        int,
        Field(
            description="Maximum number of documents to retrieve (default: 3, max: 3)"
        ),
    ] = 3,
    image_offset: Annotated[
        int,
        Field(
            ge=0,
            description="Number of usable images to skip across all requested documents (default: 0). Only used when images are included. To continue, pass the returned next_image_offset with the same doc_titles/order and max_docs.",
        ),
    ] = 0,
) -> Union[dict, ToolResult]:
    # 处理字符串形式的列表（某些模型会传递 '["title1","title2"]' 而不是 ["title1","title2"]）
    if isinstance(doc_titles, str):
        try:
            doc_titles = json.loads(doc_titles)
        except json.JSONDecodeError:
            await ctx.error(f"[Tool 2] Invalid doc_titles format: {doc_titles}")
            return {
                "documents": {},
                "not_found": [],
                "error": f"Invalid doc_titles format. Expected JSON array like [\"title1\", \"title2\"], got: {repr(doc_titles)}"
            }

    # 限制最多3个doc_title
    limit = min(max_docs, 3) if max_docs else 3
    if len(doc_titles) > limit:
        await ctx.warning(
            f"[Tool 2] Truncated {len(doc_titles)} doc_titles to top {limit}"
        )
        doc_titles = doc_titles[:limit]

    # 未显式传参时取用户在设置页选择的默认值
    mcp_config = get_mcp_config()
    if include_images is None:
        include_images = mcp_config.include_images_default

    await ctx.info(
        f"[Tool 2] Retrieving {len(doc_titles)} documents, "
        f"include_metadata: {include_metadata}, include_images: {include_images}"
    )

    try:
        # 数据库读取是同步 I/O，不能阻塞与 UI、索引 MCP 共享的事件循环。
        docs_mapping = await run_sync(get_docs, doc_titles)

        # 分离找到的和未找到的
        documents = {}
        not_found = []

        for title in doc_titles:
            doc_info = docs_mapping.get(title)
            if doc_info is not None:
                documents[title] = doc_info
            else:
                not_found.append(title)

        await ctx.info(
            f"[Tool 2] Completed - Found: {len(documents)}, Not found: {len(not_found)}"
        )

        if not_found:
            await ctx.warning(f"[Tool 2] Doc titles not found: {', '.join(not_found)}")

        # 正文中的插图按 file_path 解析，无插图的 PDF 回退到用 original_file_path
        # 渲染原页，解析完即剔除这两个本机路径，不返回给客户端
        images = []
        skipped_images = 0
        if include_images:
            images, skipped_images = await run_sync(
                collect_chunk_images, documents,
                max_images=mcp_config.max_images_per_call, image_offset=image_offset,
            )
            await ctx.info(
                f"[Tool 2] Images collected: {len(images)}, skipped: {skipped_images}"
            )
        for doc in documents.values():
            doc.pop("file_path", None)
            doc.pop("original_file_path", None)

        result = {"documents": documents, "not_found": not_found}

        # 如果需要元数据，可以在这里添加
        if include_metadata:
            result["metadata"] = {
                "total_requested": len(doc_titles),
                "total_found": len(documents),
                "total_not_found": len(not_found),
            }
            await ctx.info(f"[Tool 2] Metadata included")

        if not include_images:
            # 直接返回纯数据，让FastMCP自动序列化为结构化JSON
            return result

        # 图片清单同时写入结构化结果，供不渲染 image content 的客户端降级使用
        result["images"] = [
            {"doc_title": img["doc_title"], "ref": img["ref"]} for img in images
        ]
        result["next_image_offset"] = (
            image_offset + len(images) if skipped_images else None
        )
        if skipped_images:
            result["images_truncated"] = skipped_images

        # 文本块保留原有 JSON，其后逐张附上图片；每张图前用一行说明归属，
        # 否则多篇文档一起返回时无法分辨图片出自哪个切片
        content: list[Union[str, Image]] = [json.dumps(result, ensure_ascii=False)]
        for img in images:
            content.append(f"[image] {img['doc_title']} -> {img['ref']}")
            content.append(Image(path=img["path"]))

        return ToolResult(content=content, structured_content=result)

    except Exception as e:
        await ctx.error(f"[Tool 2] Failed: {str(e)}")
        raise


@mcp.tool(
    name="get-source-page",
    description="Returns one full source page as an inline image to verify OCR, formulas, tables, or layout, even when a document already has extracted figures. Use file_id from get-docs and a 1-based page_number (look for 第N页 in heading_path when available). Supports PDF directly, and DOCX/DOC/RTF/ODT/PPTX/PPT/ODP through an available Office or LibreOffice converter. The original file must still exist. Office page numbers refer to the converted PDF. This explicit page request is independent of get-docs include_images and image_offset; it returns only the requested page, not the whole document.",
    tags={"docs", "retrieval"},
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    output_schema={"type": "object", "additionalProperties": True},
)
async def get_source_page_tool(
    ctx: Context,
    file_id: Annotated[
        int, Field(ge=1, description="File ID returned by get-docs, not a chunk_id or a file path")
    ],
    page_number: Annotated[
        int, Field(ge=1, description="Source page number starting at 1; for Office documents, the page in the converted PDF")
    ],
) -> ToolResult:
    from indexing.services.maintenance_service import source_page

    await ctx.info(f"[Tool 4] Retrieving source page: file_id={file_id}, page_number={page_number}")
    try:
        path = await run_sync(source_page, file_id, page_number)
    except BusinessError as exc:
        await ctx.warning(f"[Tool 4] {exc.code}: {exc}")
        raise ToolError(f"{exc.code}: {exc}") from exc

    result = {"file_id": file_id, "page_number": page_number}
    return ToolResult(
        content=[json.dumps(result, ensure_ascii=False), Image(path=path)],
        structured_content=result,
    )
