"""应用导航与文件库集合树，共用一栏，不叠加常驻导航列。"""

from typing import Callable

from nicegui import ui

from app.i18n import t
from app.skills import get_version
from app.ui.components import collection_path_label


def collection_tree_nodes(collections: list) -> list:
    """从稳定 ID 组装显示树，并对异常结构给出有限、明确的失败。"""
    nodes = {
        item["id"]: {"id": str(item["id"]), "label": f"{item['name']} ({item['file_count']})",
                     "path": collection_path_label(item), "icon": "folder", "children": []}
        for item in collections
    }
    roots = []
    for item in collections:
        node = nodes[item["id"]]
        parent_id = item["parent_id"]
        if parent_id is None:
            roots.append(node)
        elif parent_id in nodes:
            nodes[parent_id]["children"].append(node)
        else:
            raise ValueError(t("collections.invalid_tree"))
    visited = set()
    pending = list(roots)
    while pending:
        node = pending.pop()
        if node["id"] in visited:
            raise ValueError(t("collections.invalid_tree"))
        visited.add(node["id"])
        pending.extend(node["children"])
    if len(visited) != len(collections):
        raise ValueError(t("collections.invalid_tree"))
    return roots


def render_sidebar(
    current_view: dict,
    switch_to_files: Callable,
    switch_to_recall_test: Callable,
    switch_to_cloud_sync: Callable,
    switch_to_mcp_config: Callable,
    switch_to_skills: Callable,
    switch_to_logs: Callable,
    switch_to_settings: Callable,
    ui_refs: dict,
    state: dict,
    file_handlers,
    switch_to_wiki: Callable | None = None,
    switch_to_graph: Callable | None = None,
):
    navigation = [
        ("files", "folder", switch_to_files),
        ("recall_test", "manage_search", switch_to_recall_test),
        ("cloud_sync", "cloud_sync", switch_to_cloud_sync),
        ("mcp_config", "settings_input_component", switch_to_mcp_config),
        ("skills", "extension", switch_to_skills),
        ("logs", "description", switch_to_logs),
        ("settings", "settings", switch_to_settings),
    ]

    # Wiki 与知识图谱是两个独立功能，各自有导航入口，不共用同一个知识视图。
    if switch_to_graph:
        navigation.insert(1, ("graph", "hub", switch_to_graph))
    if switch_to_wiki:
        navigation.insert(1, ("wiki", "menu_book", switch_to_wiki))

    @ui.refreshable
    def sidebar_nav():
        with ui.column().classes("w-full h-full theme-sidebar gap-0 overflow-hidden"):
            with ui.row().classes("w-full px-3 items-center justify-between flex-nowrap library-heading"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("auto_stories", size="sm").classes("theme-text-accent")
                    ui.label(t("app.name")).classes("text-base font-semibold theme-text")
                ui.label(f"v{get_version()}").classes("text-xs theme-text-muted")

            # 应用导航折叠为当前视图标题，为集合树留下纵向空间。
            with ui.expansion(t(f"sidebar.{current_view['value']}"), icon="apps",
                              value=state.get("navigation_expanded", False),
                              on_value_change=lambda e: state.update(navigation_expanded=e.value)).props("dense").classes("w-full"):
                with ui.column().classes("w-full gap-0.5 px-2 pb-2"):
                    for key, icon, callback in navigation:
                        selected = key == current_view["value"]
                        ui.button(t(f"sidebar.{key}"), icon=icon, on_click=callback).props("flat no-caps align=left").classes(
                            "w-full text-sm " + ("theme-selected" if selected else "theme-hover theme-text-muted")
                        )

            if current_view["value"] == "files":
                with ui.row().classes("w-full px-3 items-center justify-between pt-3 pb-1 flex-nowrap"):
                    ui.label(t("collections.tree_title")).classes("text-xs font-semibold theme-text-muted").tooltip(t("collections.keyboard_hint"))
                    ui.button(icon="create_new_folder", on_click=file_handlers.handle_manage_collections).props(
                        f'flat dense round size=sm aria-label="{t("collections.manage")}"'
                    ).classes("theme-text-accent").tooltip(t("collections.manage"))
                with ui.scroll_area(on_scroll=lambda e: state.update(tree_scroll=e.vertical_position)).classes("flex-1 w-full scroll-flush") as scroll:
                    @ui.refreshable
                    def collection_tree():
                        nodes = [
                            {"id": "all", "label": t("files.all_files"), "icon": "inventory_2"},
                            {"id": "uncategorized", "label": t("collections.none"), "icon": "folder_off"},
                        ]
                        try:
                            nodes.extend(collection_tree_nodes(state.get("collections", [])))
                        except ValueError as exc:
                            ui.label(str(exc)).classes("text-sm text-red-400 px-3")
                        tree = ui.tree(nodes, on_select=file_handlers.on_tree_select,
                                       on_expand=lambda e: state.update(expanded_collection_ids=list(e.value))).props(
                            "dense no-transition selected-color=primary"
                        ).classes("w-full px-2 collection-tree theme-text")
                        tree.add_slot("default-header", '<q-icon :name="props.node.icon" size="xs" class="q-mr-xs" /><span class="break-words" :title="props.node.path || props.node.label">{{ props.node.label }}</span>')
                        tree.select("uncategorized" if state.get("uncategorized") else
                                    str(state["active_collection_ids"][0]) if state.get("active_collection_ids") else "all")
                        tree.expand(state.get("expanded_collection_ids", []))
                        ui_refs["tree_element"] = tree
                    ui_refs["collection_tree"] = collection_tree
                    collection_tree()
                scroll.scroll_to(pixels=state.get("tree_scroll", 0))
            else:
                ui_refs["collection_tree"] = None
                ui_refs["tree_element"] = None
                ui.space()

            with ui.row().classes("w-full px-3 py-2 items-center gap-1 library-footer"):
                ui.icon("storage", size="xs").classes("theme-text-muted")
                ui_refs["stats_label"] = ui.label(state.get("stats_text", t("stats.loading"))).classes("text-xs theme-text-muted")

    sidebar_nav()
    return sidebar_nav
