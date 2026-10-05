"""集合 MCP 的薄输入输出适配。"""

from indexing.services import collection_service, maintenance_service
from indexing.services.errors import BusinessError


def list_collections():
    try:
        return {"success": True, "message": "查询成功", "data": {"collections": collection_service.list_collections()}}
    except Exception as exc:
        return {"success": False, "message": f"查询集合失败: {exc}", "data": None}


def create_collection(name, description=None, parent_id=None):
    try:
        result = collection_service.create_collection(name, description, parent_id)
        return {"success": result["success"], "message": result["message"],
                "data": {"collection_id": result["collection_id"], "name": name.strip(), "parent_id": parent_id} if result["success"] else None}
    except Exception as exc:
        return {"success": False, "message": f"创建集合失败: {exc}", "data": None}


def move_collection(collection_id, parent_id):
    try:
        result = collection_service.move_collection(collection_id, parent_id)
        return {"success": True, "message": result["message"], "data": result}
    except BusinessError as exc:
        return {"success": False, "message": str(exc), "code": exc.code, "data": exc.data}


def delete_collection(collection_id, dry_run=False, confirmed=False):
    try:
        result = maintenance_service.delete_collection(collection_id, dry_run, confirmed)
        return {"success": True, "message": "删除预览" if dry_run else "集合已删除；文件与原件保留", "data": result}
    except BusinessError as exc:
        return {"success": False, "message": str(exc), "code": exc.code, "data": exc.data}


def assign_file_collections(file_id, collection_names):
    try:
        result = collection_service.assign_collections([file_id], collection_names or [])
        return {"success": True, "message": "集合归类已更新", "data": result["files"][0]}
    except Exception as exc:
        return {"success": False, "message": f"设置文件集合失败: {exc}", "data": None}
