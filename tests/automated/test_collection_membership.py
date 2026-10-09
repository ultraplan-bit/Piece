"""统一浏览树的归属操作：保留其它直接归属，并保证整批原子性。"""

import pytest

from indexing.services import collection_service as collections, file_service as files
from indexing.services.errors import BusinessError


def test_memberships_add_remove_and_replace_are_distinct(knowledge_base):
    first = collections.create_collection("first")["collection_id"]
    second = collections.create_collection("second")["collection_id"]
    file_id = files.create_empty_file("source", [first])["file_id"]
    members = lambda: {item["id"] for item in collections.get_file_collections(file_id)}
    collections.update_file_collections([file_id, file_id], [second, second], mode="add")
    collections.update_file_collections([file_id], [second], mode="add")
    assert members() == {first, second}
    collections.update_file_collections([file_id], [first], mode="remove")
    collections.update_file_collections([file_id], [first], mode="remove")
    assert members() == {second}
    collections.update_file_collections([file_id], [first], mode="replace")
    assert members() == {first}
    collections.update_file_collections([file_id], [], mode="replace")
    assert members() == set() and files.get_file_by_id(file_id)


@pytest.mark.parametrize("mode", ["add", "remove", "replace"])
@pytest.mark.parametrize("invalid", ["file", "collection"])
def test_membership_batch_validates_every_id_before_writing(knowledge_base, mode, invalid):
    original = collections.create_collection("original")["collection_id"]
    target = collections.create_collection("target")["collection_id"]
    ids = [files.create_empty_file(f"document-{i}", [original])["file_id"] for i in range(2)]
    with pytest.raises(BusinessError, match="不存在"):
        collections.update_file_collections(ids + ([99999] if invalid == "file" else []),
                                            [target] + ([99999] if invalid == "collection" else []), mode=mode)
    for file_id in ids:
        assert {item["id"] for item in collections.get_file_collections(file_id)} == {original}


def test_membership_operation_rejects_unknown_mode_and_boolean_ids(knowledge_base):
    with pytest.raises(BusinessError, match="操作"):
        collections.update_file_collections([], [], mode="move")
    with pytest.raises(BusinessError, match="正整数"):
        collections.update_file_collections([True], [], mode="add")
