"""统一资源树浏览器测试的小型定位助手。"""

from playwright.sync_api import expect

from app.i18n import t
from app.ui.resource_tree import document_key


def collection_row(page, collection_id):
    return page.locator(f'.resource-collection-row[data-collection-id="{collection_id}"]')


def expand_collection(page, collection_id):
    row = collection_row(page, collection_id)
    expect(row).to_be_visible()
    if row.get_attribute("aria-expanded") != "true":
        row.focus()
        row.press("ArrowRight")
    expect(row).to_have_attribute("aria-expanded", "true")
    expect(row).to_have_attribute("data-loaded", "true")
    return row


def document_row(page, file_id, *, collection_id=None):
    if collection_id is None:
        return page.locator(f'.library-row[data-document-id="{file_id}"]')
    return page.locator(f'[data-catalog-row="{document_key(collection_id, file_id)}"]')


def browse_mode(page, mode):
    label = t({"tree": "files.tree_view", "all": "files.all_files", "uncategorized": "collections.none"}[mode])
    button = page.get_by_role("group", name=t("files.view"), exact=True).get_by_role("button", name=label, exact=True)
    expect(button).to_be_enabled()
    button.click()
    expect(button).to_have_attribute("aria-pressed", "true")
    expect(page.locator('[data-catalog="files"]')).to_have_attribute("data-library-mode", mode)
