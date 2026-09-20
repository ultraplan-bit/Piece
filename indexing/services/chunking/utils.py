"""
分块工具函数

提供递归切分、表格/公式保护等通用功能。
"""

import re
from typing import List, Optional, Tuple


# OCR 服务对含合并单元格的复杂表格只输出 HTML 表格，整张表压在同一行里，
# 既没有换行可供切分，每个单元格还挂着 style 属性
_HTML_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.I | re.S)
_HTML_TABLE_OPEN_RE = re.compile(r"<table\b[^>]*>", re.I)
_HTML_ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.I | re.S)

# 表格标签上的 style（text-align、word-wrap 之类）纯属渲染修饰，界面 CSS 已用
# !important 覆盖，却占掉表格文本约三分之二的篇幅——既挤占切片容量，又让向量
# 编码的大半是 CSS 而非表格内容，切分前统一去掉
_TABLE_STYLE_RE = re.compile(
    r"(<(?:table|thead|tbody|tr|td|th)\b[^>]*?)\s+style\s*=\s*(?:\"[^\"]*\"|'[^']*')",
    re.I,
)

# 表格之外，OCR 还会输出 <div style=...>图 1-20 咳纹肝</div>、<img ... /> 这类行内
# HTML。从标签中间切开会在切片首尾留下 'le="text-align: center;">' 这种碎片，
# 既污染向量又会原样显示在知识卡片上
_HTML_TAG_RE = re.compile(r"<[^<>]{1,400}>")
_HTML_DIV_RE = re.compile(r"<div\b[^>]*>.*?</div>", re.I | re.S)

# 一个段落（连续的非空行）。MinerU 把插图和它的图注、子图标签用单换行连成
# 一个段落，整段受保护，图和说明才不会被拆到两张卡片上
_PARAGRAPH_RE = re.compile(r"[^\n]+(?:\n[^\n]+)*")
_IMG_TAG_RE = re.compile(r"<img\b[^<>]*>", re.I)

# 切片除去 <img> 标签后的正文少于这个字符数，就视为"只有图片"的切片：
# 没有可检索的文字，卡片上也只是一串没说明的图，并入相邻切片才有上下文
IMAGE_ONLY_MAX_TEXT = 80


# Obsidian 语法中不属于正文的部分：%% 注释在预览里不显示，dataview /
# dataviewjs 代码块是查询而非内容，两者都不该进入切片和向量
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})[^\n]*\n.*?^\1[ \t]*$", re.M | re.S)
_QUERY_FENCE_RE = re.compile(r"^(`{3,}|~{3,})[ \t]*dataview(?:js)?[ \t]*\n.*?^\1[ \t]*$", re.M | re.S | re.I)
_OBSIDIAN_COMMENT_RE = re.compile(r"%%.*?%%", re.S)


def strip_obsidian_noise(text: str) -> str:
    """去掉 Obsidian 的 %% 注释与 dataview 查询代码块；普通代码块内的内容原样保留。

    只在 %% 成对出现时处理，孤立的 %% 视为正文。
    """
    if "%%" not in text and "dataview" not in text.lower():
        return text
    pieces = []
    position = 0
    for fence in _FENCE_RE.finditer(text):
        pieces.append(_OBSIDIAN_COMMENT_RE.sub("", text[position:fence.start()]))
        pieces.append("" if _QUERY_FENCE_RE.fullmatch(fence.group(0)) else fence.group(0))
        position = fence.end()
    pieces.append(_OBSIDIAN_COMMENT_RE.sub("", text[position:]))
    return "".join(pieces)


# 分隔符优先级列表（从高到低）
SEPARATORS = [
    "\n\n",  # 段落边界
    "\n",  # 换行
    "。",
    "！",
    "？",
    ".",
    "!",
    "?",  # 句子边界
    "；",
    ";",  # 分号
    "，",
    ",",  # 逗号
    " ",  # 空格
]


def _find_markdown_tables(text: str) -> List[Tuple[int, int]]:
    """
    查找文本中所有 Markdown 表格的起始和结束位置

    Markdown 表格格式:
    | 列1 | 列2 |
    |-----|-----|
    | 数据1 | 数据2 |

    Returns:
        [(start_pos, end_pos), ...] 表格边界列表
    """
    boundaries = []
    lines = text.split('\n')
    in_table = False
    table_start = 0
    line_pos = 0

    for i, line in enumerate(lines):
        # 判断是否为表格行（以|开头和结尾，且包含至少2个|）
        stripped = line.strip()
        is_table_line = (
            stripped.startswith('|') and
            stripped.endswith('|') and
            stripped.count('|') >= 2
        )

        if is_table_line and not in_table:
            # 表格开始
            in_table = True
            table_start = line_pos
        elif not is_table_line and in_table:
            # 表格结束（当前行不是表格行）
            table_end = line_pos
            boundaries.append((table_start, table_end))
            in_table = False

        # 更新位置（+1 是换行符）
        line_pos += len(lines[i]) + 1

    # 处理文本末尾的表格
    if in_table:
        boundaries.append((table_start, len(text)))

    return boundaries


def find_table_boundaries(text: str) -> List[Tuple[int, int]]:
    """
    查找文本中所有表格的起始和结束位置

    同时覆盖 Markdown 管道表格和 HTML 表格：OCR 解析出的复杂表格只有 HTML 形态，
    漏掉它就等于整份 OCR 文档的表格都没有保护。

    Returns:
        [(start_pos, end_pos), ...] 表格边界列表
    """
    boundaries = _find_markdown_tables(text)
    boundaries.extend(
        (match.start(), match.end()) for match in _HTML_TABLE_RE.finditer(text)
    )
    return boundaries


def find_html_boundaries(text: str, max_span: int) -> List[Tuple[int, int]]:
    """
    查找不应被切开的行内 HTML 片段

    - 图注/居中容器 <div>...</div>：切开会让图和图注分家（超过一个切片的除外，
      那种只能切）
    - 含 <img> 的段落：插图与紧随的图注、子图标签是一个整体
    - 任意单个标签 <...>：切开会留下 'le="text-align: center;">' 这种碎片

    Returns:
        [(start_pos, end_pos), ...] 边界列表
    """
    boundaries = [
        (match.start(), match.end())
        for match in _HTML_DIV_RE.finditer(text)
        if match.end() - match.start() <= max_span
    ]
    boundaries.extend(
        (match.start(), match.end())
        for match in _PARAGRAPH_RE.finditer(text)
        if match.end() - match.start() <= max_span and _IMG_TAG_RE.search(match.group(0))
    )
    boundaries.extend(
        (match.start(), match.end()) for match in _HTML_TAG_RE.finditer(text)
    )
    return boundaries


def find_formula_boundaries(text: str) -> List[Tuple[int, int]]:
    """
    查找文本中所有 LaTeX 公式的起始和结束位置

    支持:
    - 块级公式: $$...$$
    - 行内公式: $...$

    Returns:
        [(start_pos, end_pos), ...] 公式边界列表
    """
    boundaries = []

    # 匹配块级公式 $$...$$ (非贪婪模式，支持多行)
    for match in re.finditer(r'\$\$[\s\S]+?\$\$', text):
        boundaries.append((match.start(), match.end()))

    # 匹配行内公式 $...$ (排除 $$，单行模式)
    # 使用负向前瞻和负向后顾排除 $$
    for match in re.finditer(r'(?<!\$)\$(?!\$)([^\$\n]+?)\$(?!\$)', text):
        boundaries.append((match.start(), match.end()))

    return boundaries


def _region_containing(
    pos: int, boundaries: List[Tuple[int, int]]
) -> Optional[Tuple[int, int]]:
    """返回包含 pos 的受保护区域；pos 不在任何区域内部时返回 None"""
    for start, end in boundaries:
        if start < pos < end:
            return start, end
    return None


def is_safe_split_point(pos: int, boundaries: List[Tuple[int, int]]) -> bool:
    """
    检查某个位置是否可以安全切分（不在受保护区域内部）

    Args:
        pos: 待检查的切分位置
        boundaries: 受保护区域列表 [(start, end), ...]

    Returns:
        True 表示可以安全切分，False 表示在受保护区域内
    """
    return _region_containing(pos, boundaries) is None


def strip_table_styles(text: str) -> str:
    """去掉表格标签上的 style 属性（详见 _TABLE_STYLE_RE 的说明）"""
    return _TABLE_STYLE_RE.sub(r"\1", text)


def _is_separator_row(line: str) -> bool:
    """判断是否为 Markdown 表格的表头分隔行（形如 |---|:--:|）"""
    stripped = line.strip()
    return bool(stripped) and set(stripped) <= set("|-: ")


def _split_html_table(table: str, chunk_size: int) -> List[str]:
    """把 HTML 表格按 <tr> 拆成多张小表，每张都补回表头行"""
    open_match = _HTML_TABLE_OPEN_RE.match(table)
    open_tag = open_match.group(0) if open_match else "<table>"
    rows = _HTML_ROW_RE.findall(table)
    if len(rows) <= 1:
        return [table]

    # 表头：有 <th> 就取所有含 <th> 的前导行；OCR 输出的表格常全用 <td>，
    # 此时第一行即列名
    header_count = 0
    for row in rows:
        if "<th" not in row.lower():
            break
        header_count += 1
    header_count = header_count or 1

    header = "".join(rows[:header_count])
    body_rows = rows[header_count:]
    if not body_rows:
        return [table]

    fixed_size = len(open_tag) + len(header) + len("</table>")
    parts: List[str] = []
    current: List[str] = []
    current_size = 0
    for row in body_rows:
        if current and fixed_size + current_size + len(row) > chunk_size:
            parts.append(f"{open_tag}{header}{''.join(current)}</table>")
            current, current_size = [], 0
        current.append(row)
        current_size += len(row)
    if current:
        parts.append(f"{open_tag}{header}{''.join(current)}</table>")
    return parts


def _split_markdown_table(table: str, chunk_size: int) -> List[str]:
    """把 Markdown 表格按行拆成多张小表，每张都补回表头和分隔行"""
    lines = [line for line in table.strip().split("\n") if line.strip()]
    if len(lines) <= 2:
        return [table.strip()]

    header = lines[:2] if _is_separator_row(lines[1]) else lines[:1]
    body_lines = lines[len(header) :]
    if not body_lines:
        return [table.strip()]

    fixed_size = sum(len(line) + 1 for line in header)
    parts: List[str] = []
    current: List[str] = []
    current_size = 0
    for line in body_lines:
        if current and fixed_size + current_size + len(line) + 1 > chunk_size:
            parts.append("\n".join(header + current))
            current, current_size = [], 0
        current.append(line)
        current_size += len(line) + 1
    if current:
        parts.append("\n".join(header + current))
    return parts


def split_oversized_tables(text: str, chunk_size: int) -> str:
    """
    把装不进一个切片的表格按行拆成若干张带表头的小表，用空行分隔

    表格整体保护不下时只能切开，但按行切并补回表头，比从标签中间硬切出
    "半个属性 + 一堆裸单元格" 有检索价值得多；拆完每张小表都能被保护机制整块保住。
    """

    def _rewrite_html(match: re.Match) -> str:
        table = match.group(0)
        if len(table) <= chunk_size:
            return table
        return "\n\n".join(_split_html_table(table, chunk_size))

    text = _HTML_TABLE_RE.sub(_rewrite_html, text)

    # Markdown 表格按边界原地替换，从后往前改以免前面的替换移动后面的位置
    for start, end in reversed(_find_markdown_tables(text)):
        table = text[start:end]
        if len(table) <= chunk_size:
            continue
        replacement = "\n\n".join(_split_markdown_table(table, chunk_size))
        text = f"{text[:start]}{replacement}\n{text[end:]}"
    return text


def normalize_tables(text: str, chunk_size: int) -> str:
    """
    切分前的表格归一化：去掉 style 噪声，并把超长表格拆成带表头的小表

    幂等，重复调用只是多跑一遍正则。
    """
    return split_oversized_tables(strip_table_styles(text), chunk_size)


def recursive_split(
    text: str,
    chunk_size: int = 800,
    overlap: int = 150,
) -> List[str]:
    """
    递归字符切分器

    分层尝试分隔符，优先在语义边界处切分，并添加重叠窗口。
    保护表格和公式不被截断。

    Args:
        text: 待切分文本
        chunk_size: 目标切片大小（默认 800）
        overlap: 重叠窗口大小（默认 150）

    Returns:
        切分后的文本列表
    """
    text = normalize_tables(text.strip(), chunk_size)
    if not text:
        return []

    # 文本足够短，直接返回
    if len(text) <= chunk_size:
        return [text]

    # 查找受保护区域（表格、行内 HTML 和公式）
    protected_boundaries = []
    protected_boundaries.extend(find_table_boundaries(text))
    protected_boundaries.extend(find_html_boundaries(text, chunk_size))
    protected_boundaries.extend(find_formula_boundaries(text))
    # 按起始位置排序，便于后续处理
    protected_boundaries.sort(key=lambda x: x[0])

    # 尝试找到最佳分隔符
    def find_split_point(text: str, target_size: int, offset: int = 0) -> int:
        """
        在目标位置之前找到最佳切分点，避开受保护区域

        Args:
            text: 当前文本片段
            target_size: 目标切分位置（相对于text）
            offset: text在原始完整文本中的偏移量

        Returns:
            切分点位置（相对于text）
        """
        # 在 target_size 之前寻找分隔符
        search_start = max(0, target_size - 200)
        search_end = target_size
        search_range = text[search_start:search_end]

        # 收集所有分隔符的位置和优先级
        candidates = []
        for priority, sep in enumerate(SEPARATORS):
            pos = search_range.rfind(sep)
            if pos != -1:
                actual_pos = search_start + pos + len(sep)
                if actual_pos > 0 and actual_pos < len(text):
                    # 转换为在原始文本中的绝对位置
                    absolute_pos = offset + actual_pos
                    # 检查是否在受保护区域内
                    if is_safe_split_point(absolute_pos, protected_boundaries):
                        # 计算距离 target_size 的远近（越近越好）
                        distance = target_size - actual_pos
                        candidates.append((priority, distance, actual_pos))

        if candidates:
            # 排序：优先级高的分隔符，如果距离差不多（<50字符）则选优先级高的
            # 否则选距离更近的
            def score(item):
                priority, distance, pos = item
                # 距离很近（<50）时，优先考虑分隔符优先级
                # 距离较远时，位置更重要
                if distance < 50:
                    return (0, priority, distance)
                else:
                    return (1, distance, priority)

            candidates.sort(key=score)
            return candidates[0][2]

        # 没找到安全分隔符：把切分点退到受保护区域的起点，让整张表格/整条公式
        # 落进下一个切片，而不是从标签或公式中间硬切
        pos = target_size
        while True:
            region = _region_containing(offset + pos, protected_boundaries)
            if region is None:
                return pos
            pos = region[0] - offset
            if pos <= 0:
                # 受保护区域从片首就开始且仍装不下（表格按行拆过后还超长），
                # 整块作为一个切片，宁可超长也不切碎
                return min(region[1] - offset, len(text))

    # 切分文本
    chunks = []
    start = 0

    while start < len(text):
        # 计算本次切片的结束位置
        end = start + chunk_size

        if end >= len(text):
            # 剩余文本不足一个切片，直接添加
            remaining = text[start:].strip()
            if remaining:
                chunks.append(remaining)
            break

        # 找到最佳切分点（传入offset，以便在原始文本中查找受保护区域）
        split_point = find_split_point(text[start:], chunk_size, offset=start)
        actual_end = start + split_point

        # 提取切片
        chunk = text[start:actual_end].strip()
        if chunk:
            chunks.append(chunk)

        # 下一个切片的起始位置（考虑重叠）。重叠窗口不能落进表格/公式内部，
        # 否则下一片会以半张表开头；也不能不前进，否则死循环。
        # 重叠窗口里若有插图，整段插图会原样复制到下一片，同一张图出现在
        # 两张卡片上，这种情况直接不重叠
        next_start = max(actual_end - overlap, 0)
        if (
            next_start <= start
            or not is_safe_split_point(next_start, protected_boundaries)
            or _IMG_TAG_RE.search(text, next_start, actual_end)
        ):
            next_start = actual_end
        start = next_start

    return merge_image_only_chunks(chunks, chunk_size)


def _is_image_only(chunk: str) -> bool:
    text = _IMG_TAG_RE.sub("", chunk)
    return bool(_IMG_TAG_RE.search(chunk)) and len("".join(text.split())) < IMAGE_ONLY_MAX_TEXT


def _join_without_overlap(first: str, second: str, overlap: int) -> str:
    """拼接相邻切片，去掉重叠窗口带来的重复尾巴。"""
    for size in range(min(len(first), len(second), overlap), 0, -1):
        if first.endswith(second[:size]):
            return first + second[size:]
    return f"{first}\n\n{second}"


def merge_image_only_chunks(chunks: List[str], chunk_size: int, overlap: int = 150) -> List[str]:
    """把只有图片没有正文的切片并入较短的那个邻居。

    插图段落受保护不被切开，装不进当前切片时会整段落到下一片，若那一片
    正好只剩这段插图，就会产出一张没有任何说明文字的卡片。并入邻居后
    超出目标大小不超过一半，向量模型仍有余量（切片大小按上下文的 80% 算）。
    """
    merged = list(chunks)
    index = 0
    while index < len(merged):
        chunk = merged[index]
        if not _is_image_only(chunk):
            index += 1
            continue
        neighbours = []
        if index > 0:
            neighbours.append(index - 1)
        if index + 1 < len(merged):
            neighbours.append(index + 1)
        neighbours = [
            n for n in sorted(neighbours, key=lambda n: len(merged[n]))
            if len(merged[n]) + len(chunk) <= chunk_size * 1.5
        ]
        if not neighbours:
            index += 1
            continue
        target = neighbours[0]
        if target < index:
            merged[target] = _join_without_overlap(merged[target], chunk, overlap)
        else:
            merged[target] = _join_without_overlap(chunk, merged[target], overlap)
        # 并入后的邻居不再重判：它已经带着正文，再合并只会把更多正文卷进来
        del merged[index]
    return merged


# 标题层级路径的分隔符（doc_title 用下划线拼接，路径用它拼接便于阅读和还原层级）
HEADING_SEPARATOR = " / "

# Markdown 标题最深 6 级
MAX_HEADING_LEVEL = 6


def build_chunk(segments: List[str], chunk_text: str) -> dict:
    """
    按标题层级片段构造切片记录

    所有分块策略的 doc_title 都是"文件名_章节_小节"形式，这里同时记录
    结构化的层级路径，避免下游再按下划线反推（文件名本身可能含下划线）。

    Args:
        segments: 层级片段，第一项为文件基础名，其后依次为各级标题
        chunk_text: 切片正文

    Returns:
        {"doc_title": ..., "chunk_text": ..., "heading_path": ..., "heading_level": ...}
    """
    parts = [str(segment).strip() for segment in segments if str(segment).strip()]
    return {
        "doc_title": "_".join(parts),
        "chunk_text": chunk_text,
        "heading_path": HEADING_SEPARATOR.join(parts),
        "heading_level": min(len(parts), MAX_HEADING_LEVEL) or 1,
    }


def heading_path_from_doc_title(doc_title: str) -> dict:
    """
    从 doc_title 反推层级路径（用于手工新增或改名的切片）

    Returns:
        {"heading_path": ..., "heading_level": ...}
    """
    parts = [part.strip() for part in doc_title.split("_") if part.strip()]
    return {
        "heading_path": HEADING_SEPARATOR.join(parts),
        "heading_level": min(len(parts), MAX_HEADING_LEVEL) or 1,
    }


def clean_heading(heading: str) -> str:
    """清理标题中的特殊字符，保留中文、英文、数字、括号、空格"""
    heading = heading.lstrip("#").strip()
    heading = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9()\（\）\s]", "", heading)
    return heading.strip()
