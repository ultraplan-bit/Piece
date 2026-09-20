"""
测试分块保护功能：验证表格和公式不被截断
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from indexing.services.chunking.utils import (
    recursive_split,
    find_table_boundaries,
    find_formula_boundaries,
)


def test_table_boundaries():
    """测试表格边界检测"""
    text = """
这是前面的内容。

| 列1 | 列2 | 列3 |
|-----|-----|-----|
| 数据1 | 数据2 | 数据3 |
| 数据4 | 数据5 | 数据6 |

这是后面的内容。
"""
    boundaries = find_table_boundaries(text)
    print("表格边界检测测试:")
    print(f"  找到 {len(boundaries)} 个表格")
    for i, (start, end) in enumerate(boundaries):
        print(f"  表格 {i+1}: 位置 {start}-{end}")
        print(f"  内容: {repr(text[start:end])}")
    print()


def test_formula_boundaries():
    """测试公式边界检测"""
    text = r"""
这是行内公式 $E = mc^2$ 的例子。

这是块级公式：
$$
\int_a^b f(x)dx = F(b) - F(a)
$$

后面还有内容。
"""
    boundaries = find_formula_boundaries(text)
    print("公式边界检测测试:")
    print(f"  找到 {len(boundaries)} 个公式")
    for i, (start, end) in enumerate(boundaries):
        print(f"  公式 {i+1}: 位置 {start}-{end}")
        print(f"  内容: {repr(text[start:end])}")
    print()


def test_table_not_split():
    """测试表格不被截断"""
    # 构造一个包含表格的长文本，表格位置在切分点附近
    prefix = "前面的内容。" * 50  # 约 600 字符
    table = """
| 产品名称 | 价格 | 库存 | 备注 |
|---------|------|------|------|
| 苹果 | 5.00 | 100 | 新鲜 |
| 香蕉 | 3.00 | 200 | 进口 |
| 橙子 | 4.50 | 150 | 多汁 |
"""
    suffix = "后面的内容。" * 50  # 约 600 字符

    text = prefix + table + suffix

    print("表格保护测试:")
    print(f"  文本总长度: {len(text)} 字符")
    print(f"  表格位置: {len(prefix)}-{len(prefix) + len(table)}")

    chunks = recursive_split(text, chunk_size=800, overlap=150)

    print(f"  切分为 {len(chunks)} 个块\n")

    # 检查每个切片
    table_found = False
    for i, chunk in enumerate(chunks):
        has_table_start = "|" in chunk and "产品名称" in chunk
        has_table_end = "多汁" in chunk

        if has_table_start or has_table_end:
            print(f"  块 {i+1} 包含表格内容:")
            print(f"    长度: {len(chunk)} 字符")
            print(f"    有表格开头: {has_table_start}")
            print(f"    有表格结尾: {has_table_end}")

            # 检查表格是否完整
            if has_table_start and has_table_end:
                print(f"    ✓ 表格完整")
                table_found = True
            else:
                print(f"    ✗ 表格被截断！")

            # 显示包含表格的部分
            lines = chunk.split('\n')
            table_lines = [l for l in lines if '|' in l]
            if table_lines:
                print(f"    表格内容 ({len(table_lines)} 行):")
                for line in table_lines[:5]:  # 只显示前5行
                    print(f"      {line}")
            print()

    if table_found:
        print("  ✓ 测试通过：表格未被截断\n")
    else:
        print("  ✗ 测试失败：表格被截断\n")


def test_formula_not_split():
    """测试公式不被截断"""
    # 构造包含公式的长文本
    prefix = "这是数学推导过程。" * 40  # 约 400 字符
    formula = r"""
重要的积分公式：
$$
\int_0^\infty e^{-x^2} dx = \frac{\sqrt{\pi}}{2}
$$
"""
    middle = "中间有一些说明文字。" * 30  # 约 300 字符
    formula2 = "行内公式 $E = mc^2$ 很著名。"
    suffix = "后面的内容。" * 40  # 约 400 字符

    text = prefix + formula + middle + formula2 + suffix

    print("公式保护测试:")
    print(f"  文本总长度: {len(text)} 字符")

    chunks = recursive_split(text, chunk_size=600, overlap=100)

    print(f"  切分为 {len(chunks)} 个块\n")

    # 检查每个切片
    for i, chunk in enumerate(chunks):
        has_block_formula = "$$" in chunk
        has_inline_formula = "$E = mc^2$" in chunk

        if has_block_formula or has_inline_formula:
            print(f"  块 {i+1} 包含公式:")
            print(f"    长度: {len(chunk)} 字符")

            if has_block_formula:
                # 检查块级公式是否完整（两个$$）
                count = chunk.count("$$")
                if count == 2:
                    print(f"    ✓ 块级公式完整 ($$成对出现)")
                else:
                    print(f"    ✗ 块级公式被截断！($$出现 {count} 次)")

            if has_inline_formula:
                print(f"    ✓ 行内公式完整: $E = mc^2$")

            print()

    print()


def test_mixed_content():
    """测试混合内容（表格 + 公式）"""
    text = """
# 物理学基础

这是一段介绍文字，讲述了物理学的基础知识。" * 30

## 能量公式

最著名的公式是 $E = mc^2$，这是爱因斯坦提出的质能方程。

更详细的推导：
$$
E^2 = (mc^2)^2 + (pc)^2
$$

## 实验数据

下面是实验测量的数据表格：

| 样本编号 | 质量(kg) | 速度(m/s) | 动能(J) |
|---------|---------|----------|---------|
| A001 | 2.5 | 10.0 | 125.0 |
| A002 | 3.0 | 12.0 | 216.0 |
| A003 | 2.8 | 11.5 | 185.5 |

从表格可以看出，动能与质量和速度的平方成正比。

后续内容继续讨论..." * 20
"""

    print("混合内容测试:")
    print(f"  文本总长度: {len(text)} 字符")

    chunks = recursive_split(text, chunk_size=500, overlap=100)

    print(f"  切分为 {len(chunks)} 个块\n")

    issues = []
    for i, chunk in enumerate(chunks):
        # 检查公式
        dollar_count = chunk.count("$")
        double_dollar_count = chunk.count("$$")

        if dollar_count > 0:
            # 行内公式应该成对（偶数个$，排除$$）
            inline_dollars = dollar_count - double_dollar_count * 2
            if inline_dollars % 2 != 0:
                issues.append(f"块 {i+1}: 行内公式$不成对")

        if double_dollar_count > 0 and double_dollar_count % 2 != 0:
            issues.append(f"块 {i+1}: 块级公式$$不成对")

        # 检查表格
        has_table_header = "样本编号" in chunk or "质量" in chunk
        has_table_data = "A001" in chunk or "A002" in chunk
        if (has_table_header and not has_table_data) or (has_table_data and not has_table_header):
            issues.append(f"块 {i+1}: 表格被截断（头部和数据分离）")

    if issues:
        print("  发现问题:")
        for issue in issues:
            print(f"    ✗ {issue}")
        print()
    else:
        print("  ✓ 测试通过：所有表格和公式都完整\n")


if __name__ == "__main__":
    print("=" * 60)
    print("分块保护功能测试")
    print("=" * 60)
    print()

    test_table_boundaries()
    test_formula_boundaries()
    test_table_not_split()
    test_formula_not_split()
    test_mixed_content()

    print("=" * 60)
    print("测试完成")
    print("=" * 60)


def test_strip_obsidian_noise_keeps_code_and_lone_markers():
    from indexing.services.chunking.utils import strip_obsidian_noise
    text = (
        "正文 %%行内注释%% 继续\n"
        "%%\n多行\n注释\n%%\n"
        "```dataview\nTABLE file.name FROM #tag\n```\n"
        "```python\nx = '%%' + '%%'\n```\n"
        "~~~dataviewjs\ndv.list([])\n~~~\n"
        "百分比 50%% 不成对时保留\n"
    )
    result = strip_obsidian_noise(text)
    assert "行内注释" not in result and "多行" not in result
    assert "TABLE file.name" not in result and "dv.list" not in result
    assert "x = '%%' + '%%'" in result
    assert "正文  继续" in result
    assert "百分比 50%% 不成对时保留" in result
    assert strip_obsidian_noise("普通文本") == "普通文本"


def test_image_paragraph_is_not_split_and_image_only_chunks_are_merged():
    """MinerU 把插图和图注用单换行连成一段，切分不能从段落中间切开，
    也不能产出只剩若干 <img> 没有任何说明文字的切片。"""
    from indexing.services.chunking.utils import merge_image_only_chunks

    body = "这是正文句子。" * 60
    figure = '<img src="d/a.jpg">\n(a) 甲\n<img src="d/b.jpg">\n图 1 组合图说明'
    chunks = recursive_split(f"{body}\n\n{figure}\n\n{body}", chunk_size=500, overlap=100)

    holders = [chunk for chunk in chunks if "<img" in chunk]
    assert len(holders) == 1
    assert figure in holders[0]
    # 承载插图的切片必须带有正文，而不是一张只有图的卡片
    assert "这是正文句子" in holders[0]

    merged = merge_image_only_chunks(["前文" * 60, '<img src="x.jpg">\n<img src="y.jpg">', "后文" * 90], 500)
    # 并入较短的那个邻居
    assert merged == ['前文' * 60 + '\n\n<img src="x.jpg">\n<img src="y.jpg">', "后文" * 90]
    # 相邻两段只有图片的切片各自并入，不会连锁把整页卷成一片
    merged = merge_image_only_chunks(["前文" * 60, '<img src="x.jpg">', '<img src="y.jpg">', "后文" * 90], 500)
    assert merged == ['前文' * 60 + '\n\n<img src="x.jpg">\n\n<img src="y.jpg">', "后文" * 90]
    # 邻居都装不下时保留原样，宁可一张图卡也不产出超长切片
    huge = "字" * 800
    assert merge_image_only_chunks([huge, '<img src="x.jpg">'], 500) == [huge, '<img src="x.jpg">']
