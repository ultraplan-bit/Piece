"""集合 MCP 的薄输入输出适配。"""

from indexing.services import collection_service


def list_collections():
    try:
        return {"success": True, "message": "查询成功", "data": {"collections": collection_service.list_collections()}}
    except Exception as exc:
        return {"success": False, "message": f"查询集合失败: {exc}", "data": None}


def create_collection(name, description=None):
    try:
        result = collection_service.create_collection(name, description)
        return {"success": result["success"], "message": result["message"],
                "data": {"collection_id": result["collection_id"], "name": name.strip()} if result["success"] else None}
    except Exception as exc:
        return {"success": False, "message": f"创建集合失败: {exc}", "data": None}


def assign_file_collections(file_id, collection_names):
    try:
        result = collection_service.assign_collections([file_id], collection_names or [])
        return {"success": True, "message": "集合归类已更新", "data": result["files"][0]}
    except Exception as exc:
        return {"success": False, "message": f"设置文件集合失败: {exc}", "data": None}
