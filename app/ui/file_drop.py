"""工作区拖放：外部文件复用上传通道，内部文档只追加资料集归属。"""

import json

from nicegui import ui

from app.i18n import t
from app.utils import MAX_TOTAL_UPLOAD_SIZE, MAX_UPLOAD_FILES
from indexing.services.chunking import ChunkerFactory
from indexing.services.file_service import get_max_file_size


_DROP_SCRIPT = r"""(() => {
    const messages = __MESSAGES__;
    const documentType = 'application/x-piece-documents';
    let draggedDocuments = null;
    const hint = document.createElement('div');
    hint.className = 'file-drop-hint';
    hint.setAttribute('role', 'status');
    hint.setAttribute('aria-live', 'polite');
    hint.hidden = true;
    document.body.appendChild(hint);
    let highlighted = null;

    const uploader = () => document.querySelector('[data-workspace-upload]');
    const hasType = (event, type) => Array.from(event.dataTransfer?.types || []).includes(type);
    const reset = () => {
        highlighted?.classList.remove('file-drop-highlight');
        highlighted = null;
        hint.hidden = true;
    };
    const dropTarget = element => {
        if (!(element instanceof Element) || element.closest('.q-dialog, .q-menu')) return null;
        const target = element.closest('[data-import-collection]');
        return target ? {element: target, id: target.dataset.importCollection, name: target.dataset.importLabel} : null;
    };
    const reject = (input, entries) => getElement(input).$emit('rejected', entries);

    document.addEventListener('dragstart', event => {
        if (!(event.target instanceof Element) || !event.dataTransfer) return;
        if (event.target.closest('button, input, textarea, a')) return;
        const row = event.target.closest('[data-document-id]');
        const catalog = row?.closest('[data-catalog="files"]');
        if (!catalog) return;
        if (catalog.dataset.collectionEditing === 'true') { event.preventDefault(); return; }
        const id = Number(row.dataset.documentId);
        const selected = JSON.parse(catalog.dataset.selectedFileIds || '[]');
        draggedDocuments = {client_id: window.clientId, file_ids: selected.includes(id) ? selected : [id]};
        event.dataTransfer.effectAllowed = 'copy';
        event.dataTransfer.setData(documentType, JSON.stringify(draggedDocuments));
    }, true);

    const dragover = event => {
        const internal = hasType(event, documentType);
        if (!internal && !hasType(event, 'Files')) return;
        event.preventDefault();
        const input = uploader();
        const target = dropTarget(event.target);
        const allowed = target && (!internal || (draggedDocuments && /^\d+$/.test(target.id)));
        const busy = !input || (!internal && input._pieceUploadBusy);
        event.dataTransfer.dropEffect = allowed && !busy ? 'copy' : 'none';
        if (highlighted !== (allowed ? target.element : null)) reset();
        if (allowed && !busy) {
            highlighted = target.element;
            highlighted.classList.add('file-drop-highlight');
        }
        const count = internal ? draggedDocuments?.file_ids.length || 0
            : Array.from(event.dataTransfer.items || []).filter(item => item.kind === 'file').length || event.dataTransfer.files.length;
        const message = internal && !draggedDocuments ? messages.otherWindow : busy ? messages.busy : allowed
            ? (internal ? messages.addTarget : messages.target).replace('{count}', String(count)).replace('{name}', () => target.name)
            : internal ? messages.addChoose : messages.choose;
        if (hint.textContent !== message) hint.textContent = message;
        hint.hidden = false;
    };
    document.addEventListener('dragenter', dragover, true);
    document.addEventListener('dragover', dragover, true);
    document.addEventListener('dragleave', event => { if (!event.relatedTarget) reset(); }, true);
    document.addEventListener('drop', event => {
        const internal = hasType(event, documentType);
        if (!internal && !hasType(event, 'Files')) return;
        event.preventDefault(); event.stopPropagation();
        const target = dropTarget(event.target);
        reset();
        if (!target) return;
        const input = uploader();
        if (!input) return;
        if (internal) {
            if (!/^\d+$/.test(target.id)) return;
            let data;
            try { data = JSON.parse(event.dataTransfer.getData(documentType)); } catch { return; }
            draggedDocuments = null;
            getElement(input).$emit('document-drop', {...data, collection_id: Number(target.id)});
            return;
        }
        if (input._pieceUploadBusy) {
            getElement(input).$emit('drop-busy');
            return;
        }
        const items = Array.from(event.dataTransfer.items || []).filter(item => item.kind === 'file');
        const files = [];
        const rejected = [];
        for (const item of items) {
            const entry = item.webkitGetAsEntry?.();
            if (entry?.isDirectory) {
                rejected.push({file: {name: entry.name}, failedPropValidation: 'directory'});
            } else {
                const file = item.getAsFile();
                if (file) files.push(file);
            }
        }
        if (!items.length) files.push(...event.dataTransfer.files);
        if (rejected.length) reject(input, rejected);
        if (!files.length) return;
        input._pieceImportCollection = target.id;
        // 先锁定归属，再由服务端确认启动传输。
        runMethod(input.id.slice(1), 'addFiles', [files]);
    }, true);
    document.addEventListener('dragend', () => { reset(); draggedDocuments = null; }, true);
    document.addEventListener('keydown', event => { if (event.key === 'Escape') reset(); }, true);
    window.addEventListener('blur', reset);
})();"""


def render_workspace_upload(ui_refs: dict, file_handlers) -> None:
    """上传控件跟随页面而非资料列表存活，切换工作区不会中断传输。"""
    @ui.refreshable
    def upload_control():
        upload = ui.upload(
            on_multi_upload=file_handlers.handle_multi_upload,
            auto_upload=False, multiple=True,
            max_files=MAX_UPLOAD_FILES,
            max_file_size=get_max_file_size(),
            max_total_size=MAX_TOTAL_UPLOAD_SIZE,
        ).props(
            f"accept={','.join(ChunkerFactory.get_supported_extensions())} data-workspace-upload"
        ).classes("hidden")
        file_handlers.set_upload_input(upload)
        upload.on("added", file_handlers.on_upload_added, js_handler=f"""files => {{
            const input = getHtmlElement({upload.id});
            input._pieceUploadBusy = true;
            emit({{count: files.length, collection_id: input._pieceImportCollection ?? null,
                  files: files.map(file => file.name)}});
        }}""")
        upload.on("rejected", file_handlers.on_upload_rejected, js_handler="""entries => emit(
            entries.map(entry => ({name: entry.file.name, reason: entry.failedPropValidation})))""")
        upload.on("failed", file_handlers.on_upload_failed, args=[])
        upload.on("drop-busy", lambda: ui.notify(t("files.upload_busy"), type="warning"), args=[])
        upload.on("document-drop", file_handlers.handle_document_drop)

    ui_refs["upload_control"] = upload_control
    upload_control()
    ui.timer(1.0, file_handlers.refresh_upload_limit)
    messages = json.dumps({
        "target": t("files.drop_target"), "choose": t("files.drop_choose"), "busy": t("files.upload_busy"),
        "addTarget": t("collections.drop_target"), "addChoose": t("collections.drop_choose"),
        "otherWindow": t("collections.drop_other_window"),
    }, ensure_ascii=False)
    ui.add_body_html("<script>" + _DROP_SCRIPT.replace("__MESSAGES__", messages) + "</script>")
