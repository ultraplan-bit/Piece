"""知识工作台的纯展示逻辑；不导入 GUI 或执行来源链接。"""
from copy import deepcopy
from html import escape
from html.parser import HTMLParser
import math
from urllib.parse import urlsplit
from uuid import UUID, uuid4

KINDS = ("concept", "entity", "topic", "synthesis", "source_summary")
STATUSES = ("active", "disputed", "outdated")
PREDICATES = ("is_a", "part_of", "depends_on", "applies_to", "supports", "contradicts", "related_to")
BASES = ("explicit", "synthesis", "inference", "user_statement")
# 三个经 all-pairs 验证的色组，五种类型由固定色组 + 独特形状共同编码。
TYPE_MARKS = {"concept": (0, "circle"), "entity": (0, "rect"), "topic": (2, "diamond"),
              "synthesis": (1, "triangle"), "source_summary": (1, "roundRect")}
PALETTES = {False: ("#2a78d6", "#eb6834", "#1baf7a"), True: ("#3987e5", "#d95926", "#199e70")}


def safe_destination(value):
    if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "piece" and parsed.netloc == "knowledge" and not parsed.query and not parsed.fragment:
            ident = str(UUID(parsed.path[1:]))
            if parsed.path[1:].lower() == ident:
                return ("knowledge", ident)
        if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password:
            parsed.port
            return ("external", value)
    except (ValueError, TypeError):
        pass
    return None


class _SafeKnowledgeHTML(HTMLParser):
    tags = set("p br hr h1 h2 h3 h4 h5 h6 em strong del blockquote ul ol li pre code table thead tbody tr th td a span div math semantics mrow mi mn mo mtext mspace mfrac msqrt mroot msup msub msubsup munder mover munderover mtable mtr mtd annotation".split())

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.tags:
            return
        attributes = ""
        if tag == "a":
            destination = safe_destination(dict(attrs).get("href"))
            if destination:
                kind, value = destination
                if kind == "knowledge":
                    attributes = f' href="#knowledge/{value}" data-knowledge-id="{value}"'
                else:
                    attributes = f' href="{escape(value, quote=True)}" target="_blank" rel="noopener noreferrer"'
        self.parts.append(f"<{tag}{attributes}>")

    def handle_endtag(self, tag):
        if tag in self.tags:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(escape(data))


def safe_knowledge_html(rendered):
    parser = _SafeKnowledgeHTML()
    parser.feed(rendered)
    return "".join(parser.parts)


def local_source(evidence, library_id):
    if (evidence.get("source_kind") == "piece" and evidence.get("source_library_id") == library_id
            and evidence.get("location_status") in ("current", "changed")
            and type(evidence.get("source_file_id")) is int and type(evidence.get("source_chunk_id")) is int):
        return evidence["source_file_id"], evidence["source_chunk_id"]
    return None


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
        edge_name = label("link" if edge["kind"] == "link" else edge["predicate"])
        arrow = " ↔ " if edge.get("symmetric") else " → "
        edges.append({"id": edge["id"], "edge_kind": edge["kind"], "source": edge["source_id"],
                      "target": edge["target_id"], "name": edge_name,
                      "symbol": ["none", "none" if edge.get("symmetric") else "arrow"],
                      "lineStyle": {"width": 1.5, "opacity": 0.55, "curveness": 0.12 + ordinal * 0.12,
                                    "type": "dashed" if edge["kind"] == "link" else "solid"},
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


class PendingWrite:
    """提交不可变副本；未知结果只能查原键/原样重试。"""
    def __init__(self, payload):
        self.payload = deepcopy(payload)
        self.payload["request_key"] = str(uuid4())
        self.uncertain = False

    @property
    def key(self):
        return self.payload["request_key"]
