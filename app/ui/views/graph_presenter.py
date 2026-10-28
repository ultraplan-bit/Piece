"""知识图谱工作台的纯展示逻辑；不导入 GUI、不执行来源链接，不涉及 Wiki 正文。

布局是确定性的分层环：根居中，按到根的最短跳数向外分环，环内按相邻节点的平均角度
排序以减少连线交叉。这样比把节点摆成一圈更能看出结构，又保持稳定——选中或读证据
时不会像力导向那样重新抖动。
"""
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

# 分层环半径与节点尺寸：度数越高（连接越多）节点越大，一眼能看出枢纽。
RING_FIRST = 170.0
RING_STEP = 150.0
NODE_BASE = 18.0
NODE_MAX = 34.0
# 同一环上相邻节点的最小弧距：小于它节点会挤在一起，按节点数放大环半径。
RING_MIN_ARC = 46.0
# 少于这个节点数就全部显示标签；超过后只标注根、选中和连接较多的节点，其余靠悬停。
# 上限内让 ECharts 的 labelLayout.hideOverlap 处理碰撞，比按度数硬性裁剪更接近主流图工具。
LABEL_ALL_MAX = 60
LABEL_MIN_DEGREE = 2


def _adjacency(graph):
    """无向邻接表；布局与度数都按无向计算，方向仍由箭头表达。"""
    adj = {node["id"]: [] for node in graph["nodes"]}
    for edge in graph["edges"]:
        source, target = edge.get("source_id"), edge.get("target_id")
        if source in adj and target in adj:
            adj[source].append(target)
            adj[target].append(source)
    return adj


def _depth_layers(graph, adjacency):
    """按到根的最短跳数分层；与根不连通的节点排到最外一环，不丢节点。"""
    root = graph["root_id"]
    depth = {root: 0} if root in adjacency else {}
    frontier = [root] if root in adjacency else []
    while frontier:
        following = []
        for node_id in frontier:
            for neighbor in adjacency.get(node_id, ()):
                if neighbor not in depth:
                    depth[neighbor] = depth[node_id] + 1
                    following.append(neighbor)
        frontier = following
    unreached = [node["id"] for node in graph["nodes"] if node["id"] not in depth]
    if unreached:
        deepest = max(depth.values(), default=0) + 1
        for node_id in unreached:
            depth[node_id] = deepest
    layers = {}
    for node_id, level in depth.items():
        layers.setdefault(level, []).append(node_id)
    return layers


def _layered_positions(graph, adjacency, layers):
    """环内按相邻已放置节点的平均角度排序，减少交叉；结果确定、可复现。

    每环整体再旋转一个固定偏移，避免每层节点少时都落在同一条水平轴上、
    整张图被压成一条线。
    """
    positions = {}
    if graph["nodes"]:
        positions[graph["root_id"]] = (0.0, 0.0)
    previous_radius = 0.0
    for level in sorted(layers):
        if level == 0:
            continue
        count = len(layers[level])
        # 环半径要同时满足三点：随跳数外扩、环上弧距不小于 RING_MIN_ARC（否则同环节点重叠）、
        # 与内一环的径向间距也不小于 RING_MIN_ARC（否则相邻环的节点重叠）。
        radius = max(RING_FIRST + (level - 1) * RING_STEP,
                     count * RING_MIN_ARC / (2 * math.pi),
                     previous_radius + RING_MIN_ARC)

        def order_key(node_id):
            angles = [math.atan2(*reversed(positions[parent]))
                      for parent in adjacency.get(node_id, ()) if parent in positions]
            return (sum(angles) / len(angles)) if angles else float("inf")

        ordered = sorted(layers[level], key=lambda node_id: (order_key(node_id), node_id))
        # 每环旋转 60° 的整数倍，单节点环不再压在水平轴上。
        base = math.radians(60 * level)
        for index, node_id in enumerate(ordered):
            angle = base + 2 * math.pi * index / max(1, count)
            positions[node_id] = (radius * math.cos(angle), radius * math.sin(angle))
        previous_radius = radius
    return positions


def _node_size(degree):
    return min(NODE_MAX, NODE_BASE + 3.2 * math.log2(degree + 1))


def _label_position(x, y):
    """标签朝远离中心的方向摆放，避免压住中心节点和向外的连线。

    ECharts 的 y 轴向下：角度 0 指向右，正角度向下。
    """
    if x == 0 and y == 0:
        return "bottom"
    angle = math.degrees(math.atan2(y, x))
    if -45 <= angle < 45:
        return "right"
    if 45 <= angle < 135:
        return "bottom"
    if -135 <= angle < -45:
        return "top"
    return "left"


def _overview_options(graph, *, dark, selected, label):
    """可旋转的 3D 球面全局概览。"""
    surface, ink = ("#1a1a19", "#ffffff") if dark else ("#fcfcfb", "#0b0b0b")
    adjacency = _adjacency(graph)
    degree = {node_id: len(neighbors) for node_id, neighbors in adjacency.items()}
    ordered = sorted(graph["nodes"], key=lambda node: (-degree.get(node["id"], 0), node["id"]))
    count = max(1, len(ordered))
    points = {}
    for index, item in enumerate(ordered):
        depth = 1 - 2 * (index + 0.5) / count
        angle = index * math.pi * (3 - math.sqrt(5))
        longitude = (math.degrees(angle) + 180) % 360 - 180
        points[item["id"]] = (longitude, math.degrees(math.asin(depth)), 1)
    if len(ordered) == 1:
        points[ordered[0]["id"]] = (0.0, 0.0, 1)

    names = {node["id"]: node["title"] for node in graph["nodes"]}
    edges = []
    for edge in graph["edges"]:
        edge_name = label(edge.get("predicate") or "relation")
        arrow = " ↔ " if edge.get("symmetric") else " → "
        edges.append({"name": edge_name,
                      "coords": [points[edge["source_id"]], points[edge["target_id"]]],
                      "tooltip_text": names[edge["source_id"]] + arrow + names[edge["target_id"]] + " · " + edge_name,
                      "lineStyle": {"color": "#93a6b4" if dark else "#718795", "opacity": 0.62,
                                    "width": 1.2 + 0.7 * min(int(edge.get("evidence_count") or 0), 5)}})

    node_series = []
    for kind in KINDS:
        color, symbol = TYPE_MARKS[kind]
        data = []
        for item in graph["nodes"]:
            if item["kind"] != kind:
                continue
            node_id = item["id"]
            data.append({"id": node_id, "name": item["title"], "value": points[node_id],
                         "symbolSize": _node_size(degree.get(node_id, 0)) * 0.72,
                         "tooltip_text": item["title"] + " · " + label(kind),
                         "label": {"show": node_id == selected or degree.get(node_id, 0) >= LABEL_MIN_DEGREE,
                                   "formatter": "{b}",
                                   "textStyle": {"color": ink, "textBorderColor": surface, "textBorderWidth": 3}},
                         "itemStyle": {"color": PALETTES[dark][color], "borderColor": surface, "borderWidth": 1}})
        node_series.append({"name": label(kind), "type": "scatter3D", "symbol": symbol,
                            "data": data, "label": {"show": True},
                            "emphasis": {"label": {"show": True, "textStyle": {"color": ink}}}})

    return {"backgroundColor": surface, "animation": False,
            "tooltip": {"renderMode": "html", "enterable": False, "confine": True,
                        "extraCssText": "max-width:320px;white-space:normal;overflow-wrap:anywhere",
                        ":formatter": "p => { const el = document.createElement('div'); el.textContent = p.data.tooltip_text || ''; return el; }"},
            "legend": {"data": [label(kind) for kind in KINDS], "bottom": 4,
                       "textStyle": {"color": ink}, "selectedMode": False},
            "globe": {"globeRadius": 82, "globeOuterRadius": 82, "show": False,
                      "environment": "none", "baseColor": surface,
                      "viewControl": {"autoRotate": False, "alpha": 22, "beta": 35,
                                      "distance": 170, "rotateSensitivity": 1,
                                      "rotateMouseButton": "left", "panMouseButton": "right",
                                      "zoomSensitivity": 1, "panSensitivity": 0}},
            "series": [{"type": "lines3D", "coordinateSystem": "globe", "polyline": True,
                        "silent": True, "data": edges,
                        "lineStyle": {"color": "#93a6b4" if dark else "#718795", "opacity": 0.62, "width": 1}},
                      *[{**series, "coordinateSystem": "globe", "silent": False}
                        for series in node_series]]}


def graph_options(graph, *, dark=False, selected=None, positions=None, label=lambda value: value):
    """分层环布局；用户拖动过的位置优先，避免重绘把手工摆放冲掉。"""
    if graph.get("overview"):
        return _overview_options(graph, dark=dark, selected=selected, label=label)
    surface, ink = ("#1a1a19", "#ffffff") if dark else ("#fcfcfb", "#0b0b0b")
    adjacency = _adjacency(graph)
    degree = {node_id: len(neighbors) for node_id, neighbors in adjacency.items()}
    layers = _depth_layers(graph, adjacency)
    computed = _layered_positions(graph, adjacency, layers)
    override = positions or {}
    show_all_labels = len(graph["nodes"]) <= LABEL_ALL_MAX

    nodes, names = [], {n["id"]: n["title"] for n in graph["nodes"]}
    for item in graph["nodes"]:
        node_id = item["id"]
        color, symbol = TYPE_MARKS[item["kind"]]
        x, y = override.get(node_id, computed.get(node_id, (0.0, 0.0)))
        size = _node_size(degree.get(node_id, 0))
        labeled = show_all_labels or node_id in (selected, graph["root_id"]) or degree.get(node_id, 0) >= LABEL_MIN_DEGREE
        nodes.append({"id": node_id, "name": item["title"], "category": KINDS.index(item["kind"]),
                      "symbol": symbol, "symbolSize": size, "x": x, "y": y,
                      "itemStyle": {"color": PALETTES[dark][color], "borderColor": surface, "borderWidth": 2},
                      # 文字描边光晕代替实心底块：标签压在连线上仍可读，又不遮挡图形。
                      "label": {"show": labeled, "color": ink, "position": _label_position(x, y),
                                "distance": 6, "width": 160, "overflow": "truncate",
                                "textBorderColor": surface, "textBorderWidth": 3, "textBorderRadius": 3},
                      "tooltip_text": item["title"] + " · " + label(item["kind"])})

    edges, pairs = [], {}
    for edge in graph["edges"]:
        pair = tuple(sorted((edge["source_id"], edge["target_id"])))
        ordinal = pairs.get(pair, 0)
        pairs[pair] = ordinal + 1
        edge_name = label(edge.get("predicate") or "relation")
        arrow = " ↔ " if edge.get("symmetric") else " → "
        # 证据越多线越粗：边是断言，支持它的来源越多就越实。
        weight = 1.2 + 0.7 * min(int(edge.get("evidence_count") or 0), 5)
        edges.append({"id": edge["id"], "source": edge["source_id"], "target": edge["target_id"], "name": edge_name,
                      "symbol": ["none", "none" if edge.get("symmetric") else "arrow"],
                      "lineStyle": {"width": weight, "opacity": 0.55, "curveness": 0.12 + ordinal * 0.12, "type": "solid"},
                      "tooltip_text": names[edge["source_id"]] + arrow + names[edge["target_id"]] + " · " + edge_name})
    return {"backgroundColor": surface, "animation": False,
            # HTML tooltip 支持点击穿透；只返回 textContent 节点，模型文本不能成为 HTML。
            "tooltip": {"renderMode": "html", "enterable": False, "confine": True,
                        "extraCssText": "max-width:320px;white-space:normal;overflow-wrap:anywhere",
                        ":formatter": "p => { const el = document.createElement('div'); el.textContent = p.data.tooltip_text || ''; return el; }"},
            "legend": {"data": [{"name": label(k), "icon": TYPE_MARKS[k][1]} for k in KINDS],
                       "bottom": 4, "textStyle": {"color": ink}, "selectedMode": False},
            "series": [{"id": "knowledge", "type": "graph", "layout": "none", "roam": True,
                        "scaleLimit": {"min": 0.4, "max": 5},
                        # 等比适配，避免共线节点在零高度范围内被拉伸成长条。
                        "preserveAspect": True,
                        # 上下留白要给径向朝外的标签留出空间，否则靠边的标签会被画布裁掉。
                        "top": 48, "bottom": 64, "left": "10%", "right": "10%",
                        "draggable": True, "data": nodes, "links": edges,
                        # 标签重叠时自动隐藏，避免长中文标题糊成一团。
                        "labelLayout": {"hideOverlap": True},
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
