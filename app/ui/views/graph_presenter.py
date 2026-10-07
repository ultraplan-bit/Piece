"""知识图谱工作台的纯展示逻辑；不导入 GUI、不执行来源链接，不涉及 Wiki 正文。"""
from copy import deepcopy
import math

# 图谱节点只包含 concept / entity，与 Wiki 页面类型分开。
KINDS = ("concept", "entity")
STATUSES = ("active", "disputed", "outdated")
PREDICATES = ("is_a", "part_of", "depends_on", "applies_to", "supports", "contradicts", "related_to")
BASES = ("explicit", "synthesis", "inference", "user_statement")
# 两种类型用不同色组 + 独特形状，避免只靠颜色区分。
TYPE_MARKS = {"concept": (0, "circle"), "entity": (1, "rect")}
PALETTES = {False: ("#2a78d6", "#eb6834"), True: ("#3987e5", "#d95926")}


def graph_options(graph, *, dark=False, selected=None, positions=None, label=lambda value: value):
    """固定初始坐标；layout=none 避免选中/读证据时重新随机布局。"""
    surface, ink = ("#1a1a19", "#ffffff") if dark else ("#fcfcfb", "#0b0b0b")
    positions = positions or {}
    nodes, names = [], {n["id"]: n["title"] for n in graph["nodes"]}
    for index, item in enumerate(graph["nodes"]):
        color, symbol = TYPE_MARKS[item["kind"]]
        angle = 2 * math.pi * max(0, index - 1) / max(1, len(graph["nodes"]) - 1)
        initial = (0, 0) if item["id"] == graph["root_id"] else (250 * math.cos(angle), 250 * math.sin(angle))
        x, y = positions.get(item["id"], initial)
        nodes.append({"id": item["id"], "name": item["title"], "category": KINDS.index(item["kind"]),
                      "symbol": symbol, "symbolSize": 10 if len(graph["nodes"]) > 50 else 22, "x": x, "y": y,
                      "itemStyle": {"color": PALETTES[dark][color], "borderColor": surface, "borderWidth": 2},
                      "label": {"show": item["id"] in (selected, graph["root_id"]), "color": ink,
                                "width": 180, "overflow": "truncate", "position": "left" if x > 0 else "right",
                                "backgroundColor": surface, "padding": 3},
                      "tooltip_text": item["title"] + " · " + label(item["kind"])})
    edges, pairs = [], {}
    for edge in graph["edges"]:
        pair = tuple(sorted((edge["source_id"], edge["target_id"])))
        ordinal = pairs.get(pair, 0)
        pairs[pair] = ordinal + 1
        edge_name = label(edge.get("predicate") or "relation")
        arrow = " ↔ " if edge.get("symmetric") else " → "
        edges.append({"id": edge["id"], "source": edge["source_id"], "target": edge["target_id"], "name": edge_name,
                      "symbol": ["none", "none" if edge.get("symmetric") else "arrow"],
                      "lineStyle": {"width": 1.5, "opacity": 0.55, "curveness": 0.12 + ordinal * 0.12, "type": "solid"},
                      "tooltip_text": names[edge["source_id"]] + arrow + names[edge["target_id"]] + " · " + edge_name})
    return {"backgroundColor": surface, "animation": False,
            # HTML tooltip 支持点击穿透；只返回 textContent 节点，模型文本不能成为 HTML。
            "tooltip": {"renderMode": "html", "enterable": False, "confine": True,
                        "extraCssText": "max-width:320px;white-space:normal;overflow-wrap:anywhere",
                        ":formatter": "p => { const el = document.createElement('div'); el.textContent = p.data.tooltip_text || ''; return el; }"},
            "legend": {"data": [{"name": label(k), "icon": TYPE_MARKS[k][1]} for k in KINDS],
                       "bottom": 4, "textStyle": {"color": ink}, "selectedMode": False},
            "series": [{"id": "knowledge", "type": "graph", "layout": "none", "roam": True,
                        # 等比适配，避免共线节点在零高度范围内被拉伸成长条。
                        "preserveAspect": True,
                        "top": 24, "bottom": 60, "left": "10%", "right": "10%",
                        "draggable": True, "data": nodes, "links": edges,
                        "categories": [{"name": label(k), "symbol": TYPE_MARKS[k][1],
                                        "itemStyle": {"color": PALETTES[dark][TYPE_MARKS[k][0]]}} for k in KINDS],
                        "emphasis": {"focus": "adjacency", "label": {"show": True, "color": ink}},
                        "lineStyle": {"color": "#898781"}}]}


def merge_graph(current, incoming):
    result = deepcopy(current)
    ids = {n["id"] for n in result["nodes"]}
    edges = {e["id"] for e in result["edges"]}
    result["truncated"] |= incoming["truncated"]
    for node in incoming["nodes"]:
        if node["id"] not in ids:
            if len(ids) >= current["max_nodes"]:
                result["truncated"] = True
                continue
            ids.add(node["id"])
            result["nodes"].append(node)
    for edge in incoming["edges"]:
        if edge["id"] not in edges:
            if len(edges) >= current["max_edges"] or not {edge["source_id"], edge["target_id"]} <= ids:
                result["truncated"] = True
                continue
            edges.add(edge["id"])
            result["edges"].append(edge)
    return result
