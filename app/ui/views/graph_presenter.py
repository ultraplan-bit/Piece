"""知识图谱工作台的纯展示逻辑；不导入 GUI、不执行来源链接，不涉及 Wiki 正文。

全局采用确定性的二维拓扑布局与可展开结构分组；局部保留按关系跳数分层的稳定环布局。
单类关系的有向层级保留多父节点与有向环。三维球面仅作为显式选择的辅助展示。
"""
from copy import deepcopy
from collections import defaultdict, deque
from functools import lru_cache
import hashlib
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
# 全局 3D 概览只显示枢纽标签；密集图仍可通过悬停读取名称。
OVERVIEW_LABEL_MIN_DEGREE = 2
HIERARCHY_PREDICATES = ("is_a", "part_of", "depends_on")


def _components(adjacency):
    remaining = set(adjacency)
    result = []
    while remaining:
        seed = min(remaining)
        remaining.remove(seed)
        component, queue = [], deque([seed])
        while queue:
            node = queue.popleft()
            component.append(node)
            for neighbor in sorted(set(adjacency[node]) & remaining):
                remaining.remove(neighbor)
                queue.append(neighbor)
        result.append(component)
    return result


@lru_cache(maxsize=16)
def _topology_positions(node_ids, edge_pairs):
    """Deterministic spring layout, computed once rather than moving during reading."""
    adjacency = {node: set() for node in node_ids}
    for source, target in edge_pairs:
        if source in adjacency and target in adjacency and source != target:
            adjacency[source].add(target)
            adjacency[target].add(source)
    components = sorted(_components(adjacency), key=lambda items: (-len(items), min(items)))
    result, tiles = {}, []
    for component in components:
        ordered = sorted(component, key=lambda node: (-len(adjacency[node]), node))
        count = len(ordered)
        if count == 1:
            tiles.append(({ordered[0]: (0.0, 0.0)}, 110.0, 110.0))
            continue
        points = {node: [100 * math.cos(i * math.tau / count),
                         100 * math.sin(i * math.tau / count)] for i, node in enumerate(ordered)}
        links = [(a, b) for a in ordered for b in sorted(adjacency[a]) if a < b]
        # ponytail: bounded O(n²) layout; larger overviews are collapsed before layout.
        for iteration in range(48):
            displacement = {node: [0.0, 0.0] for node in ordered}
            for index, first in enumerate(ordered):
                for second in ordered[index + 1:]:
                    dx, dy = points[first][0] - points[second][0], points[first][1] - points[second][1]
                    distance = max(math.hypot(dx, dy), 0.1)
                    force = 8500 / (distance * distance)
                    fx, fy = dx * force, dy * force
                    displacement[first][0] += fx
                    displacement[first][1] += fy
                    displacement[second][0] -= fx
                    displacement[second][1] -= fy
            for first, second in links:
                dx, dy = points[first][0] - points[second][0], points[first][1] - points[second][1]
                force = math.hypot(dx, dy) / 210
                fx, fy = dx * force, dy * force
                displacement[first][0] -= fx
                displacement[first][1] -= fy
                displacement[second][0] += fx
                displacement[second][1] += fy
            temperature = 22 * (1 - iteration / 48) + 1
            for node in ordered:
                dx, dy = displacement[node]
                distance = max(math.hypot(dx, dy), 0.1)
                points[node][0] += dx / distance * min(distance, temperature)
                points[node][1] += dy / distance * min(distance, temperature)
        # Preserve readable hit targets after the graph is fitted into a desktop pane.
        for _ in range(12):
            for index, first in enumerate(ordered):
                for second in ordered[index + 1:]:
                    dx, dy = points[first][0] - points[second][0], points[first][1] - points[second][1]
                    distance = max(math.hypot(dx, dy), 0.1)
                    if distance < 96:
                        shift = (96 - distance) / (distance * 2)
                        points[first][0] += dx * shift
                        points[first][1] += dy * shift
                        points[second][0] -= dx * shift
                        points[second][1] -= dy * shift
        xs, ys = zip(*points.values())
        left, top = min(xs), min(ys)
        tiles.append(({node: (x - left, y - top) for node, (x, y) in points.items()},
                      max(xs) - left + 180, max(ys) - top + 180))
    target_width = max(500, math.sqrt(sum(width * height for _, width, height in tiles)) * 1.3)
    x = y = row_height = 0.0
    for points, width, height in tiles:
        if x and x + width > target_width:
            x, y, row_height = 0.0, y + row_height, 0.0
        result.update({node: (px + x, py + y) for node, (px, py) in points.items()})
        x += width
        row_height = max(row_height, height)
    return result


def graph_projection(graph, *, expanded_groups=(), group_threshold=80, grouping=(), label=lambda value: value):
    """Collapse bounded, connected neighborhoods; these are structural groups, not topics.

    Group IDs use their highest-degree seed. Degree-ordered overview pagination keeps
    existing seeds ahead of newly fetched nodes. Original relation IDs remain inspectable.
    """
    result = {**graph, "groups": [], "node_to_group": {}}
    if len(graph["nodes"]) <= group_threshold:
        return result
    nodes = {node["id"]: node for node in graph["nodes"]}
    adjacency = _adjacency(graph)
    rank = lambda node: (-nodes[node].get("neighbor_count", len(set(adjacency[node]))), node)
    remaining, groups = set(nodes), []
    for old in grouping:
        members = [node for node in old.get("member_ids", ()) if node in remaining]
        if old.get("isolated"):
            members = [node for node in members if nodes[node].get("neighbor_count", len(adjacency[node])) == 0]
        if len(members) < 2:
            continue
        groups.append({"id": old["id"], "member_ids": members, "isolated": old.get("isolated", False),
                       "seed_id": old.get("seed_id") if old.get("seed_id") in members else min(members, key=rank)})
        remaining.difference_update(members)
    isolated = sorted(node for node in remaining if nodes[node].get("neighbor_count", len(adjacency[node])) == 0)
    for group in (group for group in groups if group["isolated"]):
        capacity = max(0, 48 - len(group["member_ids"]))
        additions, isolated = isolated[:capacity], isolated[capacity:]
        group["member_ids"].extend(additions)
        remaining.difference_update(additions)
    for start in range(0, len(isolated), 48):
        members = isolated[start:start + 48]
        if len(members) > 1:
            groups.append({"id": "group:isolated:" + members[0], "member_ids": members, "isolated": True})
            remaining.difference_update(members)
    while remaining:
        seed = min(remaining, key=rank)
        members, queue = [], deque([seed])
        queued = {seed}
        while queue and len(members) < 12:
            node = queue.popleft()
            if node not in remaining:
                continue
            remaining.remove(node)
            members.append(node)
            for neighbor in sorted(set(adjacency[node]) & remaining, key=rank):
                if neighbor not in queued:
                    queue.append(neighbor)
                    queued.add(neighbor)
        if len(members) == 1 and adjacency[seed]:
            # Leaves sharing an already grouped hub still form a useful structural group.
            boundary = set(adjacency[seed])
            siblings = sorted((node for node in remaining if boundary & set(adjacency[node])), key=rank)
            members.extend(siblings[:11])
            remaining.difference_update(members)
        if len(members) > 1:
            groups.append({"id": "group:" + seed, "member_ids": members, "seed_id": seed, "isolated": False})
    by_member = {member: group["id"] for group in groups for member in group["member_ids"]}
    by_group = {group["id"]: group for group in groups}
    for group in groups:
        member_ids = set(group["member_ids"])
        group["member_count"] = len(member_ids)
        group["title"] = label("isolated_nodes") if group["isolated"] else label("structure_group") + " · " + nodes[group["seed_id"]]["title"]
        group["internal_edge_ids"] = [edge["id"] for edge in graph["edges"]
                                      if edge["source_id"] in member_ids and edge["target_id"] in member_ids]
        group["internal_edge_count"] = len(group["internal_edge_ids"])
    expanded = set(expanded_groups)
    mapped = {node: by_member[node] if node in by_member and by_member[node] not in expanded else node for node in nodes}
    projected_nodes = [node for node in graph["nodes"] if mapped[node["id"]] == node["id"]]
    projected_nodes.extend({**group, "kind": "group", "status": "active"} for group in groups if group["id"] not in expanded)
    bundles = {}
    for edge in graph["edges"]:
        source, target = mapped[edge["source_id"]], mapped[edge["target_id"]]
        if source == target and source in by_group:
            continue
        if source == edge["source_id"] and target == edge["target_id"]:
            bundles[("original", edge["id"])] = deepcopy(edge)
            continue
        if edge.get("symmetric") and source > target:
            source, target = target, source
        key = (source, target, edge.get("predicate", "related_to"), bool(edge.get("symmetric")))
        if key not in bundles:
            bundles[key] = {"id": "group-edge:" + hashlib.sha1(repr(key).encode()).hexdigest()[:16],
                            "kind": "relation", "source_id": source, "target_id": target,
                            "predicate": key[2], "symmetric": key[3], "aggregated": True,
                            "relation_ids": [], "relation_count": 0, "evidence_count": 0,
                            "statuses": [], "bases": []}
        bundle = bundles[key]
        bundle["relation_ids"].append(edge["id"])
        bundle["relation_count"] += 1
        bundle["evidence_count"] += edge.get("evidence_count", 0)
        for field, plural in (("status", "statuses"), ("basis", "bases")):
            if edge.get(field) and edge[field] not in bundle[plural]:
                bundle[plural].append(edge[field])
    # Compute the same collapsed layout before opening a group, so unrelated groups stay put.
    collapsed_ids = tuple(sorted(set(by_member.get(node, node) for node in nodes)))
    collapsed_edges = tuple(sorted(set((by_member.get(edge["source_id"], edge["source_id"]),
                                        by_member.get(edge["target_id"], edge["target_id"])) for edge in graph["edges"])))
    collapsed_positions = _topology_positions(collapsed_ids, collapsed_edges)
    positions = {node["id"]: collapsed_positions[node["id"]] for node in projected_nodes if node["id"] in collapsed_positions}
    for group in groups:
        if group["id"] not in expanded:
            continue
        x, y = collapsed_positions[group["id"]]
        count = group["member_count"]
        for index, member in enumerate(group["member_ids"]):
            angle = index * math.tau / count
            radius = max(65, 16 * math.sqrt(count))
            positions[member] = (x + radius * math.cos(angle), y + radius * math.sin(angle))
    result.update(nodes=projected_nodes, edges=list(bundles.values()), groups=groups,
                  node_to_group=by_member, layout_positions=positions)
    return result


def _hierarchy_positions(graph, canvas_size):
    """Condense strongly connected components for ranking, but keep every node and edge."""
    ids = sorted(node["id"] for node in graph["nodes"])
    adjacency, reverse = {node: set() for node in ids}, {node: set() for node in ids}
    for edge in graph["edges"]:
        source, target = edge["source_id"], edge["target_id"]
        adjacency[source].add(target)
        reverse[target].add(source)
    visited, order = set(), []
    for start in ids:
        stack = [(start, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                order.append(node)
            elif node not in visited:
                visited.add(node)
                stack.append((node, True))
                stack.extend((neighbor, False) for neighbor in sorted(adjacency[node], reverse=True) if neighbor not in visited)
    components, membership, visited = [], {}, set()
    for start in reversed(order):
        if start in visited:
            continue
        members, stack = [], [start]
        visited.add(start)
        while stack:
            node = stack.pop()
            members.append(node)
            for neighbor in sorted(reverse[node]):
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
        component = tuple(sorted(members))
        components.append(component)
        membership.update({node: component for node in component})
    outgoing, incoming = {group: set() for group in components}, {group: set() for group in components}
    for source in ids:
        for target in adjacency[source]:
            a, b = membership[source], membership[target]
            if a != b:
                outgoing[a].add(b)
                incoming[b].add(a)
    pending = {group: len(targets) for group, targets in outgoing.items()}
    ranks = {group: 0 for group in components}
    queue = deque(sorted(group for group, count in pending.items() if count == 0))
    while queue:
        group = queue.popleft()
        for source in sorted(incoming[group]):
            ranks[source] = max(ranks[source], ranks[group] + 1)
            pending[source] -= 1
            if pending[source] == 0:
                queue.append(source)
    layers = defaultdict(list)
    for group in components:
        layers[ranks[group]].append(group)
    names = {node["id"]: node["title"] for node in graph["nodes"]}
    ordered_layers, cycles = {}, []
    for rank, groups in sorted(layers.items()):
        # 相同父项靠近，随后按标题排序；UUID 不决定阅读顺序。
        groups.sort(key=lambda group: (tuple(sorted(names[target] for node in group for target in adjacency[node])),
                                       tuple(names[node] for node in group)))
        ordered_layers[rank] = [node for group in groups for node in sorted(group, key=lambda node: (names[node], node))]
        for group in groups:
            cyclic = len(group) > 1 or group[0] in adjacency[group[0]]
            if cyclic:
                cycle_id = "cycle:" + group[0]
                cycles.append({"id": cycle_id, "member_ids": list(group),
                               "relation_ids": [edge["id"] for edge in graph["edges"]
                                                if membership[edge["source_id"]] == group == membership[edge["target_id"]]]})

    width, height = canvas_size
    available_width, available_height = max(200, width - 40), max(180, height - 160)
    largest = max((len(nodes) for nodes in ordered_layers.values()), default=1)
    candidates = []
    for columns in range(1, min(8, largest) + 1):
        card_width = min(184, (available_width - (columns - 1) * 18) / columns)
        if card_width < 90:
            continue
        content_height = sum(math.ceil(len(nodes) / columns) * 68 - 20 for nodes in ordered_layers.values())
        content_height += max(0, len(ordered_layers) - 1) * 56
        scale = min(1, available_height / max(content_height, 1))
        # 优先保证两行文字的字号，再给名称更宽的空间。
        score = min(48 * scale, 36) * 20 + card_width
        candidates.append((score, columns, card_width, scale))
    _, columns, card_width, scale = max(candidates, default=(0, 1, 160, 1))
    positions, node_ranks, y = {}, {}, 0.0
    for rank, nodes in ordered_layers.items():
        rows = math.ceil(len(nodes) / columns)
        for index, node in enumerate(nodes):
            row, column = divmod(index, columns)
            row_count = min(columns, len(nodes) - row * columns)
            positions[node] = ((column - (row_count - 1) / 2) * (card_width + 18), y + row * 68)
            node_ranks[node] = rank
        y += rows * 68 - 20 + 56
    card_height = max(32, 48 * scale)
    minimum_zoom = 1
    if positions:
        xs, ys = zip(*positions.values())
        span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
        fit_x = max(1, width - 40 - card_width) / span_x if span_x else 1
        fit_y = max(1, height - 136 - card_height) / span_y if span_y else 1
        # 先把层间留白分配到真实画布，再由 ECharts 等比渲染；节点卡片不会被拉扁。
        positions = {node: (x * fit_x, y * fit_y) for node, (x, y) in positions.items()}
        row_steps = [b - a for a, b in zip(sorted(set(ys)), sorted(set(ys))[1:])]
        if row_steps:
            minimum_zoom = max(1, (card_height + 4) / (min(row_steps) * fit_y))
    return positions, cycles, {"ranks": node_ranks, "columns": columns,
                                "card_width": card_width, "card_height": card_height,
                                "minimum_zoom": minimum_zoom,
                                "font_size": max(11, min(13, 13 * scale))}


def _hierarchy_title(title, width, font_size):
    """两行卡片标题；完整名称仍用于 tooltip 与详情，纯文本不解释为模板。"""
    lines, current, used = [], "", 0.0
    capacity = max(30, width - 20)
    characters = list(str(title).replace("\n", " "))
    for index, char in enumerate(characters):
        unit = font_size if ord(char) > 0x2E80 else font_size * 0.58
        if current and used + unit > capacity:
            lines.append(current)
            current, used = "", 0
            if len(lines) == 2:
                lines[-1] = lines[-1][:-1] + "…"
                return "\n".join(lines)
        current += char
        used += unit
    if current:
        lines.append(current)
    return "\n".join(lines[:2])

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


def _visible_graph_labels(graph, positions, degree, selected, label, zoom=1.0):
    """Hide estimated initial-view collisions; emphasis still reveals hidden labels."""
    if not positions:
        return set(), set()
    xs, ys = zip(*positions.values())
    # A conservative fit estimate keeps the heuristic independent of the browser viewport.
    scale = min(760 / max(max(xs) - min(xs), 1), 600 / max(max(ys) - min(ys), 1)) * max(0.1, zoom)
    padding = 4 / scale
    occupied = []
    visible_nodes, visible_edges = set(), set()

    def add(kind, record_id, box):
        x1, y1, x2, y2 = box
        box = (x1 - padding, y1 - padding, x2 + padding, y2 + padding)
        if any(not (box[2] <= other[0] or other[2] <= box[0]
                    or box[3] <= other[1] or other[3] <= box[1]) for other in occupied):
            return
        occupied.append(box)
        (visible_nodes if kind == "node" else visible_edges).add(record_id)

    def text_width(text, limit=160):
        return min(limit, max(12, sum(12 if ord(char) > 0x2E80 else 7 for char in text))) / scale

    candidates = []
    root = graph.get("root_id")
    focus = selected if selected in degree else root
    for node in graph["nodes"]:
        node_id = node["id"]
        x, y = positions[node_id]
        radius = _node_size(degree.get(node_id, 0)) / 2
        width, height, gap = text_width(node["title"]), 16 / scale, 6 / scale
        side = _label_position(x, y)
        if side == "right":
            box = (x + radius + gap, y - height / 2, x + radius + gap + width, y + height / 2)
        elif side == "left":
            box = (x - radius - gap - width, y - height / 2, x - radius - gap, y + height / 2)
        elif side == "top":
            box = (x - width / 2, y - radius - gap - height, x + width / 2, y - radius - gap)
        else:
            box = (x - width / 2, y + radius + gap, x + width / 2, y + radius + gap + height)
        rank = 5 if node_id == selected else 4 if node_id == root else 3 if degree.get(node_id, 0) >= 2 else 1
        candidates.append(((rank, degree.get(node_id, 0), node_id), "node", node_id, box))

    for edge in graph["edges"]:
        source, target = edge["source_id"], edge["target_id"]
        sx, sy = positions[source]
        tx, ty = positions[target]
        width = text_width(label(edge.get("predicate") or "relation"), 140)
        height = 18 / scale
        box = ((sx + tx - width) / 2, (sy + ty - height) / 2,
               (sx + tx + width) / 2, (sy + ty + height) / 2)
        rank = 2 if focus in (source, target) else 0
        candidates.append(((rank, degree.get(source, 0) + degree.get(target, 0), edge["id"]),
                           "edge", edge["id"], box))

    # ponytail: O((nodes + edges)^2); graph results are capped, so a spatial index is unnecessary.
    for _, kind, record_id, box in sorted(candidates, reverse=True):
        add(kind, record_id, box)
    return visible_nodes, visible_edges


def _overview_options(graph, *, dark, selected, label):
    """可旋转的 3D 球面全局概览，隐藏球面外壳。"""
    surface, ink = ("#202226", "#e6e9ee") if dark else ("#ffffff", "#1f2328")
    adjacency = _adjacency(graph)
    degree = {node_id: len(neighbors) for node_id, neighbors in adjacency.items()}
    nodes = {node["id"]: node for node in graph["nodes"]}
    focus = selected if selected in nodes else max(nodes, key=lambda node_id: (degree[node_id], node_id), default=None)
    ordered = sorted((node for node in graph["nodes"] if node["id"] != focus), key=lambda node: node["id"])
    count = len(ordered)
    points = {focus: (0.0, 0.0, 1)} if focus else {}
    for index, item in enumerate(ordered):
        depth = 1 - 2 * (index + 0.5) / max(1, count)
        angle = (index + 0.5) * math.pi * (3 - math.sqrt(5))
        longitude = (math.degrees(angle) + 180) % 360 - 180
        points[item["id"]] = (longitude, math.degrees(math.asin(depth)), 1)

    names = {node["id"]: node["title"] for node in graph["nodes"]}
    edges = []
    for edge in graph["edges"]:
        edge_name = label(edge.get("predicate") or "relation")
        arrow = " ↔ " if edge.get("symmetric") else " → "
        evidence = min(int(edge.get("evidence_count") or 0), 5)
        source, target = edge["source_id"], edge["target_id"]
        edges.append({"name": edge_name,
                      "coords": [points[source], points[target]],
                      "tooltip_text": names[source] + arrow + names[target] + " · " + edge_name,
                      "lineStyle": {"color": "#87929c" if dark else "#77838e",
                                    "opacity": 0.17 + 0.025 * evidence,
                                    "width": 0.75 + 0.1 * evidence},
                      "emphasis": {"lineStyle": {"opacity": 0.82, "width": 1.3}}})

    node_series = []
    for kind in KINDS:
        color, symbol = TYPE_MARKS[kind]
        data = []
        for item in graph["nodes"]:
            if item["kind"] != kind:
                continue
            node_id = item["id"]
            is_focus = node_id == focus
            data.append({"id": node_id, "name": item["title"], "value": points[node_id],
                         "symbolSize": _node_size(degree.get(node_id, 0)) * (1.15 if is_focus else 0.72),
                         "tooltip_text": item["title"] + " · " + label(kind),
                         "label": {"show": is_focus or degree.get(node_id, 0) >= OVERVIEW_LABEL_MIN_DEGREE,
                                   "formatter": "{b}",
                                   "textStyle": {"color": ink, "textBorderColor": surface, "textBorderWidth": 3}},
                         "itemStyle": {"color": PALETTES[dark][color],
                                       "borderColor": ink if is_focus else surface,
                                       "borderWidth": 2 if is_focus else 0.8}})
        node_series.append({"name": label(kind), "type": "scatter3D", "symbol": symbol,
                            "data": data, "label": {"show": True},
                            "emphasis": {"itemStyle": {"borderColor": ink, "borderWidth": 1.6},
                                         "label": {"show": True, "textStyle": {"color": ink}}}})

    return {"backgroundColor": surface, "animation": True,
            "animationDuration": 180, "animationDurationUpdate": 420,
            "animationEasingUpdate": "cubicOut",
            "tooltip": {"renderMode": "html", "enterable": False, "confine": True,
                        "extraCssText": "max-width:320px;white-space:normal;overflow-wrap:anywhere",
                        ":formatter": "p => { const el = document.createElement('div'); el.textContent = p.data.tooltip_text || ''; return el; }"},
            "legend": {"data": [label(kind) for kind in KINDS], "bottom": 4,
                       "textStyle": {"color": ink}, "selectedMode": False},
            "globe": {"globeRadius": 82, "globeOuterRadius": 82, "show": False,
                      "environment": "none", "baseColor": surface,
                      "viewControl": {"autoRotate": False, "alpha": 22, "beta": 35,
                                      "animationDurationUpdate": 320, "animationEasingUpdate": "cubicOut",
                                      "damping": 0.86, "distance": 170, "rotateSensitivity": 0.9,
                                      "rotateMouseButton": "left", "panMouseButton": "right",
                                      "zoomSensitivity": 0.9, "panSensitivity": 0}},
            "series": [{"type": "lines3D", "coordinateSystem": "globe", "polyline": True,
                        "silent": True, "data": edges,
                        "lineStyle": {"color": "#87929c" if dark else "#77838e", "opacity": 0.22, "width": 0.8}},
                      *[{**series, "coordinateSystem": "globe", "silent": False}
                        for series in node_series]]}


def graph_options(graph, *, dark=False, selected=None, selected_edge=None, positions=None,
                  label=lambda value: value, layout="auto", expanded_groups=(),
                  label_density="auto", zoom=1.0, reduced_motion=False, grouping=(), canvas_size=(900, 650)):
    """Stable 2D exploration by default; graph metadata never becomes executable HTML.

    ``piece_meta`` is UI metadata and may be removed before sending options to ECharts.
    Groups are projected only in the topology overview; sphere3d remains opt-in.
    """
    layout = ("topology" if graph.get("overview") else "radial") if layout == "auto" else layout
    metadata = {"layout": layout, "layout_warning": None, "groups": [], "cycles": []}
    if layout == "sphere3d":
        options = _overview_options(graph, dark=dark, selected=selected, label=label)
        options["animation"] = not reduced_motion
        options["piece_meta"] = metadata
        return options
    if layout == "hierarchy":
        predicates = {edge.get("predicate") for edge in graph["edges"]}
        if len(predicates) > 1 or (predicates and not predicates <= set(HIERARCHY_PREDICATES)):
            metadata["layout_warning"] = "hierarchy_requires_single_relation"
            layout = "topology"
        else:
            metadata["hierarchy_predicate"] = next(iter(predicates), None)
            endpoints = {edge[key] for edge in graph["edges"] for key in ("source_id", "target_id")}
            metadata["excluded_nodes"] = [node["id"] for node in graph["nodes"] if node["id"] not in endpoints]
            metadata["excluded_count"] = len(metadata["excluded_nodes"])
            graph = {**graph, "nodes": [node for node in graph["nodes"] if node["id"] in endpoints]}
    if layout == "topology" and graph.get("overview"):
        graph = graph_projection(graph, expanded_groups=expanded_groups, grouping=grouping, label=label)
        metadata["groups"] = graph["groups"]
        metadata["node_to_group"] = graph["node_to_group"]
    metadata["layout"] = layout
    surface, ink = ("#202226", "#e6e9ee") if dark else ("#ffffff", "#1f2328")
    adjacency = _adjacency(graph)
    degree = {node_id: len(neighbors) for node_id, neighbors in adjacency.items()}
    hierarchy = None
    if layout == "hierarchy":
        computed, metadata["cycles"], hierarchy = _hierarchy_positions(graph, canvas_size)
        metadata["hierarchy_columns"] = hierarchy["columns"]
        metadata["hierarchy_overflow"] = hierarchy["minimum_zoom"] > 1
    elif layout == "topology":
        computed = graph.get("layout_positions") or _topology_positions(
            tuple(sorted(adjacency)), tuple(sorted((edge["source_id"], edge["target_id"]) for edge in graph["edges"])))
    else:
        computed = _layered_positions(graph, adjacency, _depth_layers(graph, adjacency))
    cycle_by_node = {node: cycle["id"] for cycle in metadata["cycles"] for node in cycle["member_ids"]}
    override = positions or {}
    layout_positions = {node["id"]: override.get(node["id"], computed.get(node["id"], (0.0, 0.0)))
                        for node in graph["nodes"]}
    visible_nodes, visible_edges = _visible_graph_labels(graph, layout_positions, degree, selected, label, zoom)
    if label_density == "all":
        visible_nodes = set(adjacency)
    elif label_density == "focus":
        visible_nodes = {selected, graph.get("root_id")}
        visible_edges = set()
    if graph.get("overview"):
        visible_edges = set()
    metadata.update(visible_nodes=len(graph["nodes"]), visible_edges=len(graph["edges"]))
    categories = [*KINDS, *(["group"] if any(node["kind"] == "group" for node in graph["nodes"]) else [])]
    nodes, names = [], {n["id"]: n["title"] for n in graph["nodes"]}
    for item in graph["nodes"]:
        node_id = item["id"]
        grouped = item["kind"] == "group"
        color, symbol = TYPE_MARKS.get(item["kind"], (0, "roundRect"))
        x, y = override.get(node_id, computed.get(node_id, (0.0, 0.0)))
        size = min(42, 26 + 3 * math.log2(item["member_count"])) if grouped else _node_size(degree.get(node_id, 0))
        tooltip = item["title"] + " · " + (str(item["member_count"]) if grouped else label(item["kind"]))
        if node_id in cycle_by_node:
            tooltip += " · " + label("cycle_detected")
        nodes.append({**item, "id": node_id, "name": (str(item["member_count"]) + " · " if grouped else "") + item["title"],
                      "category": categories.index(item["kind"]), "cycle_id": cycle_by_node.get(node_id),
                      "symbol": symbol, "symbolSize": size, "x": x, "y": y,
                      "itemStyle": {"color": ("#697b8d" if dark else "#62768b") if grouped else PALETTES[dark][color],
                                    "borderColor": ink if node_id == selected else surface,
                                    "borderWidth": 2.5 if node_id == selected else 1.5},
                      # 文字描边光晕代替实心底块：标签压在连线上仍可读，又不遮挡图形。
                      "label": {"show": node_id in visible_nodes, "color": ink, "position": _label_position(x, y),
                                "distance": 6, "width": 160, "overflow": "truncate",
                                "textBorderColor": surface, "textBorderWidth": 3, "textBorderRadius": 3},
                      "tooltip_text": tooltip})
        if hierarchy:
            card_width, card_height = hierarchy["card_width"], hierarchy["card_height"]
            accent = PALETTES[dark][color]
            nodes[-1].update({"hierarchy_rank": hierarchy["ranks"][node_id],
                              "hierarchy_label": _hierarchy_title(item["title"], card_width, hierarchy["font_size"]),
                              "symbol": "roundRect" if item["kind"] == "concept" else "rect",
                              "symbolSize": [card_width, card_height],
                              "itemStyle": {"color": (("#273442", "#3b302b") if dark else ("#eef5fc", "#fff5ee"))[color],
                                            "borderColor": accent if node_id == selected else (("#617c99", "#96745f") if dark else ("#aac1da", "#dfbda7"))[color],
                                            "borderWidth": 2 if node_id == selected else 1},
                              "label": {"show": True, "position": "inside", "color": ink,
                                        "fontSize": hierarchy["font_size"], "lineHeight": max(14, hierarchy["font_size"] + 2),
                                        "width": card_width - 16, "height": card_height - 4,
                                        "overflow": "truncate"},
                              "emphasis": {"itemStyle": {"borderColor": accent, "borderWidth": 2},
                                           "label": {"show": True}}})

    edges, pairs = [], {}
    for edge in graph["edges"]:
        pair = tuple(sorted((edge["source_id"], edge["target_id"])))
        ordinal = pairs.get(pair, 0)
        pairs[pair] = ordinal + 1
        edge_name = label(edge.get("predicate") or "relation")
        if edge.get("aggregated"):
            edge_name += " × " + str(edge["relation_count"])
        arrow = " ↔ " if edge.get("symmetric") else " → "
        focused = edge["id"] == selected_edge
        cycle = cycle_by_node.get(edge["source_id"])
        cycle = cycle if cycle and cycle == cycle_by_node.get(edge["target_id"]) else None
        edges.append({**edge, "id": edge["id"], "source": edge["source_id"], "target": edge["target_id"], "name": edge_name,
                      "relation_ids": edge.get("relation_ids", [edge["id"]]), "cycle_id": cycle,
                      "label": {"show": focused or edge["id"] in visible_edges, "formatter": edge_name},
                      "emphasis": {"label": {"show": True}},
                      "symbol": ["none", "none" if edge.get("symmetric") else "arrow"],
                      "lineStyle": {"width": 1.8 if focused else 0.9, "opacity": 0.9 if focused else 0.42,
                                    "curveness": (0.06 if layout == "hierarchy" else 0.12) + ordinal * 0.12,
                                    "type": "dashed" if cycle else "solid",
                                    "color": ("#c34a44" if edge.get("predicate") == "contradicts" else "#32826b") if focused and edge.get("predicate") in ("supports", "contradicts") else ("#8b9299" if dark else "#7b858e")},
                      "tooltip_text": names[edge["source_id"]] + arrow + names[edge["target_id"]] + " · " + edge_name})
    return {"backgroundColor": surface, "animation": not reduced_motion, "piece_meta": metadata,
            "animationDuration": 160, "animationDurationUpdate": 320,
            "animationEasingUpdate": "cubicOut",
            # HTML tooltip 支持点击穿透；只返回 textContent 节点，模型文本不能成为 HTML。
            "tooltip": {"renderMode": "html", "enterable": False, "confine": True,
                        "extraCssText": "max-width:320px;white-space:normal;overflow-wrap:anywhere",
                        ":formatter": "p => { const el = document.createElement('div'); el.textContent = p.data.tooltip_text || ''; return el; }"},
            "legend": {"data": [{"name": label(k), "icon": ("roundRect" if k == "concept" else "rect") if hierarchy else TYPE_MARKS.get(k, (0, "roundRect"))[1]} for k in categories],
                       "bottom": 4, "textStyle": {"color": ink}, "selectedMode": False},
            "series": [{"id": "knowledge", "type": "graph", "layout": "none", "roam": True,
                        "pieceHierarchy": bool(hierarchy),
                        "pieceTopology": layout == "topology",
                        "pieceHierarchySize": list(canvas_size) if hierarchy else None,
                        "zoom": hierarchy["minimum_zoom"] if hierarchy else 1,
                        "scaleLimit": {"min": 0.4, "max": max(5, hierarchy["minimum_zoom"] * 2) if hierarchy else 5},
                        # 等比适配，避免共线节点在零高度范围内被拉伸成长条。
                        "preserveAspect": True,
                        "nodeScaleRatio": 0 if hierarchy else 0.6,
                        # 上下留白要给径向朝外的标签留出空间，否则靠边的标签会被画布裁掉。
                        "top": 88 + hierarchy["card_height"] / 2 if hierarchy else 48,
                        "bottom": 48 + hierarchy["card_height"] / 2 if hierarchy else 64,
                        "left": 20 + hierarchy["card_width"] / 2 if hierarchy else "10%",
                        "right": 20 + hierarchy["card_width"] / 2 if hierarchy else "10%",
                        "draggable": True, "data": nodes, "links": edges,
                        # 全局标签从首次渲染起避让；不能等缩放改变标签数量后才启用。
                        **({"labelLayout": {"hideOverlap": True}} if layout == "topology" else {}),
                        "label": {":formatter": "p => p.data.hierarchy_label ?? p.data.name"},
                        "edgeLabel": {"show": False, "position": "middle", "distance": 0,
                                      "fontSize": 12, "color": ink, "opacity": 0.8,
                                      "textBorderColor": surface, "textBorderWidth": 3},
                        "categories": [{"name": label(k), "symbol": TYPE_MARKS.get(k, (0, "roundRect"))[1],
                                        "itemStyle": {"color": "#697b8d" if k == "group" else PALETTES[dark][TYPE_MARKS[k][0]]}} for k in categories],
                        "emphasis": {"focus": "adjacency", "label": {"show": True, "color": ink},
                                     "edgeLabel": {"show": not bool(hierarchy), "opacity": 1},
                                     "lineStyle": {"opacity": 0.85, "width": 1.6}},
                        "blur": {"itemStyle": {"opacity": 0.8 if hierarchy else 0.34},
                                 "label": {"opacity": 0.8 if hierarchy else 0.34}, "lineStyle": {"opacity": 0.12}},
                        "lineStyle": {"color": "#8b9299" if dark else "#7b858e", "opacity": 0.42}}]}


def merge_graph(current, incoming):
    result = deepcopy(current)
    nodes_by_id = {node["id"]: node for node in result["nodes"]}
    edges_by_id = {edge["id"]: edge for edge in result["edges"]}
    ids, edges = set(nodes_by_id), set(edges_by_id)
    result["truncated"] = bool(current.get("truncated") or incoming.get("truncated"))
    for node in incoming["nodes"]:
        if node["id"] in nodes_by_id:
            nodes_by_id[node["id"]].update(deepcopy(node))
        else:
            if len(ids) >= current["max_nodes"]:
                result["truncated"] = True
                continue
            ids.add(node["id"])
            nodes_by_id[node["id"]] = deepcopy(node)
            result["nodes"].append(nodes_by_id[node["id"]])
    for edge in incoming["edges"]:
        if edge["id"] in edges_by_id:
            edges_by_id[edge["id"]].update(deepcopy(edge))
        else:
            if len(edges) >= current["max_edges"] or not {edge["source_id"], edge["target_id"]} <= ids:
                result["truncated"] = True
                continue
            edges.add(edge["id"])
            edges_by_id[edge["id"]] = deepcopy(edge)
            result["edges"].append(edges_by_id[edge["id"]])
    result["depth"] = max(current.get("depth", 0), incoming.get("depth", 0))
    for key in ("next_neighbor_offset", "neighbor_offset", "total_neighbors", "offset", "next_offset",
                "edge_offset", "next_edge_offset", "edge_scope", "scope_edges", "total_nodes", "total_edges",
                "filtered_nodes", "total_isolated"):
        if key in incoming:
            result[key] = deepcopy(incoming[key])
    if "next_neighbor_offset" in incoming:
        result["neighbor_root_id"] = incoming["root_id"]
        result.setdefault("neighbor_pages", {})[incoming["root_id"]] = {
            key: incoming[key] for key in ("next_neighbor_offset", "neighbor_offset", "total_neighbors") if key in incoming}
    return result
