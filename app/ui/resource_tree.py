"""资料集与文档的统一浏览树；只展开可见分支，不把后代文档平铺到父级。"""

from app.i18n import t
from app.ui.components import collection_path_label


def branch_key(collection_id):
    return "root" if collection_id is None else str(collection_id)


def document_key(collection_id, file_id):
    return f"f:{branch_key(collection_id)}:{file_id}"


def is_tree_view(state):
    return state.get("library_mode", "tree") == "tree" and not state.get("search_keyword")


def collection_tree_nodes(collections: list) -> list:
    """稳定 ID 组装层级；重复 ID、孤儿或循环不能静默丢掉节点。"""
    nodes = {
        item["id"]: {"id": str(item["id"]), "name": item["name"],
                     "label": f"{item['name']} ({item['file_count']})",
                     "file_count": item["file_count"], "path": collection_path_label(item),
                     "collection": item, "children": []}
        for item in collections
    }
    if len(nodes) != len(collections):
        raise ValueError(t("collections.invalid_tree"))
    roots = []
    for item in collections:
        if item["parent_id"] is None:
            roots.append(nodes[item["id"]])
        elif item["parent_id"] in nodes:
            nodes[item["parent_id"]]["children"].append(nodes[item["id"]])
        else:
            raise ValueError(t("collections.invalid_tree"))
    visited, pending = set(), list(roots)
    while pending:
        node = pending.pop()
        if node["id"] in visited:
            raise ValueError(t("collections.invalid_tree"))
        visited.add(node["id"])
        node["children"].sort(key=lambda child: child["name"].casefold())
        pending.extend(node["children"])
    if len(visited) != len(collections):
        raise ValueError(t("collections.invalid_tree"))
    return sorted(roots, key=lambda node: node["name"].casefold())


def visible_tree_rows(state):
    """深度优先、同层资料集在前；文档行身份包含归属，文件本体 ID 不变。"""
    expanded = set(state.get("expanded_collection_ids", []))
    pages = state.get("tree_pages", {})
    pending = [{"kind": "branch", "scope": None, "depth": 0,
                "children": collection_tree_nodes(state.get("collections", []))}]
    while pending:
        row = pending.pop()
        yield row
        if row["kind"] == "collection":
            node = row["node"]
            if node["id"] in expanded:
                pending.append({"kind": "branch", "scope": int(node["id"]),
                                "depth": row["depth"] + 1, "children": node["children"]})
        elif row["kind"] == "branch":
            scope, depth = row["scope"], row["depth"]
            page = pages.get(branch_key(scope))
            pending.append({"kind": "page", "scope": scope, "depth": depth,
                            "page": page, "has_children": bool(row["children"])})
            for file in reversed((page or {}).get("files", [])):
                pending.append({"kind": "file", "file": file, "scope": scope,
                                "depth": depth, "key": document_key(scope, file["id"])})
            for node in reversed(row["children"]):
                pending.append({"kind": "collection", "node": node, "scope": scope,
                                "depth": depth, "key": f"c:{node['id']}"})
