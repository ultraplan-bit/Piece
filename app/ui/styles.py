"""
主题样式模块

职责:
- 定义 CSS 变量和主题样式
- 提供主题初始化和切换功能
"""

from nicegui import ui


# 少女粉主题的主色。CSS 变量 --text-accent 与 Quasar primary 共用同一色值，
# 避免出现「粉色按钮 + 另一种粉的强调文字」的错配。
PINK_PRIMARY = "#e5628e"


# 主题 CSS 样式
THEME_CSS = '''
<style>
    .knowledge-list-item { border-bottom: 1px solid var(--border-color); }
    .knowledge-evidence {
        border-left: 2px solid var(--border-color);
        border-radius: 0;
        background: var(--bg-panel);
    }
    .knowledge-detail { max-width: 820px; }
    .knowledge-prose { font-size: 16px; line-height: 1.85; overflow-wrap: anywhere; }
    .knowledge-prose a { color: var(--text-accent); text-decoration: underline; text-underline-offset: 3px; }
    .knowledge-prose table { display: block; max-width: 100%; overflow-x: auto; }
    .knowledge-prose pre { max-width: 100%; overflow-x: auto; }
    @media (max-width: 760px) {
        .knowledge-prose { font-size: 15px; }
    }
    :root {
        /* 字体栈：桌面端离线运行，不加载 Web 字体，
           按平台依次回退到各自的系统 UI 字体（含中文字形） */
        --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                     "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
        --font-mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, "Courier New", monospace;

        /* 浅色主题（默认） */
        --bg-sidebar: #f3f4f5;
        --bg-panel: #fafbfc;
        --bg-content: #ffffff;
        --bg-card: #ffffff;
        --bg-hover: #eef1f3;
        --bg-selected: #e8f1fb;
        --bg-selected-inactive: #e9ecef;
        --focus-ring: #2b6cb0;
        --border-color: #e3e6ea;
        --border-selected: #5b8def;
        --text-primary: #1f2328;
        --text-secondary: #5a646e;
        /* 次级小字在各浅色面板上仍需满足 WCAG AA 4.5:1。 */
        --text-muted: #606a76;
        --text-accent: #2b6cb0;
        --code-bg: #f3f4f6;
        --code-text: #b42318;
        --pre-bg: #f6f7f9;
        --pre-text: #1f2328;
        --shadow-card: 0 1px 2px rgba(15, 23, 42, 0.06);
        --text-danger: #b42318;
        --toolbar-height: 44px;
        --control-radius: 5px;
        --control-height: 30px;
    }

    body {
        font-family: var(--font-sans);
    }

    body.body--dark {
        /* 深色主题 - 更柔和的层次 */
        --bg-sidebar: #25272b;
        --bg-panel: #272a2e;
        --bg-content: #202226;
        --bg-card: #25272b;
        --bg-hover: #363b42;
        --bg-selected: #223a52;
        --bg-selected-inactive: #34383f;
        --focus-ring: #7db6e8;
        --border-color: #3a4048;
        --border-selected: #6b93d8;
        --text-primary: #e6e9ee;
        --text-secondary: #c7ccd3;
        --text-muted: #9aa3ad;
        --text-accent: #7db6e8;
        --code-bg: #23272e;
        --code-text: #e07a5f;
        --pre-bg: #23272e;
        --pre-text: #e6e9ee;
        --shadow-card: 0 1px 2px rgba(0, 0, 0, 0.35);
        --text-danger: #f2a0a0;
    }

    body.theme-pink {
        /* 少女粉主题 - 仅替换强调色（--text-accent 需与 PINK_PRIMARY 保持一致） */
        --bg-selected: #fbe8ee;
        --border-selected: #f2a3ba;
        --text-accent: #e5628e;
    }

    /* 全局导航稳定留在左侧，顶栏只显示位置和低频工具。 */
    .app-header {
        height: 44px;
        flex-shrink: 0;
        padding: 0 10px;
        background: var(--bg-panel);
        border-bottom: 1px solid var(--border-color);
    }
    .app-brand { font-size: 13px; flex-shrink: 0; }
    .app-body { height: calc(100dvh - 44px); flex-shrink: 0; }
    .app-view-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .workspace-primary { padding: 10px 8px; flex-shrink: 0; }
    .workspace-primary .q-btn__content { width: 100%; min-width: 0; flex-wrap: nowrap; gap: 10px; justify-content: flex-start; }
    .workspace-sidebar .navigation-label { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .workspace-sidebar .workspace-primary .q-icon, .workspace-sidebar .library-heading .q-btn { flex-shrink: 0; }
    .workspace-nav-active { background: var(--bg-hover); color: var(--text-primary); font-weight: 600; }
    .workspace-sidebar .library-heading { height: 36px; border-bottom: 0; }
    .workspace-sidebar .library-footer { margin-top: auto; }
    .navigation-collapsed .workspace-primary { padding: 10px 6px; }
    .navigation-collapsed .workspace-nav-item { padding: 6px; }
    .navigation-collapsed .q-btn__content { justify-content: center; gap: 0; }
    .navigation-collapsed .navigation-label, .navigation-collapsed .navigation-context { display: none; }
    .navigation-collapsed .library-footer { justify-content: center; }
    .help-hint.q-btn { color: var(--text-muted); min-width: 24px; min-height: 24px; width: 24px; height: 24px; padding: 2px; flex-shrink: 0; }
    .help-hint .q-icon { font-size: 16px; }
    .help-tooltip { max-width: min(340px, calc(100vw - 24px)); padding: 10px 12px; white-space: pre-line;
        font-size: 13px; line-height: 1.65; color: var(--text-primary); background: var(--bg-content);
        border: 1px solid var(--border-color); border-radius: var(--control-radius); box-shadow: 0 4px 16px #0002; }
    .q-btn:focus-visible, .q-field__native:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
    .app-header .q-btn, .workspace-toolbar .q-btn { min-height: var(--control-height); }
    .q-menu { background: var(--bg-content); color: var(--text-primary); border: 1px solid var(--border-color); border-radius: 8px; box-shadow: 0 8px 24px #0002; }
    .q-menu .q-item { min-height: 32px; font-size: 13px; }
    .q-dialog__inner > .q-card { border: 1px solid var(--border-color); border-radius: 8px; box-shadow: 0 12px 40px #0003; }
    .task-activity-panel { width: 360px; max-width: calc(100vw - 32px); padding: 4px 0; }
    .task-activity-row { border-bottom: 1px solid var(--border-color); }
    .task-activity-row:last-child { border-bottom: 0; }
    .task-activity-row[data-status=failed] > .q-icon { color: var(--text-danger); }
    .task-activity-badge { font-size: 10px; min-height: 15px; }
    .library-catalog, .wiki-workbench { container-type: inline-size; }
    .catalog-heading.workspace-toolbar { padding: 0 8px; gap: 4px; }
    .catalog-actions .q-btn { width: 28px; min-width: 28px; }
    .export-menu-hint { padding: 8px 16px; max-width: 240px; line-height: 1.5; white-space: normal; }
    .library-view-button.q-btn {
        min-height: 28px; min-width: 0; padding: 3px 4px; border-radius: 3px;
        font-size: 12px; color: var(--text-muted); flex-shrink: 1;
    }
    .library-view-button[aria-pressed=true] { background: var(--bg-hover); color: var(--text-primary); font-weight: 600; }
    .library-view-button .q-btn__content, .library-view-button .block { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .library-location { padding: 0 10px 4px 4px; min-height: 28px; }
    .library-location .q-btn { min-height: 24px; padding: 2px 6px; }
    .library-catalog .theme-selected { background: var(--bg-selected-inactive); }
    .resource-tree { padding: 4px; }
    .resource-collection-row {
        display: flex; align-items: center; gap: 4px; min-height: 32px;
        padding: 4px 4px 4px calc(6px + var(--tree-depth, 0) * 14px); border-radius: 4px; cursor: default;
    }
    .resource-tree .tree-expander { min-width: 20px; width: 20px; min-height: 22px; padding: 0; flex-shrink: 0; }
    .resource-tree .collection-tree-icon { margin-right: 0; }
    .resource-file-row.library-row {
        min-height: 32px; border-bottom: 0; border-radius: 4px;
        padding: 5px 6px 5px calc(30px + var(--tree-depth, 0) * 14px);
    }
    .resource-tree .library-title-cell .text-xs { display: none; }
    .collection-row-action { opacity: 0; flex-shrink: 0; }
    .resource-collection-row:hover .collection-row-action,
    .resource-collection-row:focus-within .collection-row-action { opacity: 1; }
    .collection-inline-editor { padding: 3px 4px 3px calc(6px + var(--tree-depth, 0) * 14px); }
    .collection-inline-editor .q-field__control { height: 30px; }
    .resource-branch-footer { padding-left: calc(30px + var(--tree-depth, 0) * 14px); }
    .resource-tree-pagination { padding: 3px 4px 7px 0; }
    .resource-tree-pagination > :first-child { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    @container (max-width: 640px) {
        .resource-tree .library-row-meta { display: none; }
        .resource-tree .library-file-name { display: block; white-space: nowrap; -webkit-line-clamp: unset; }
    }
    @media (hover: none) { .collection-row-action { opacity: 1; } }
    .collection-drop-target { display: flex; align-items: center; gap: 6px; min-width: 0; width: 100%; }
    .file-drop-highlight { outline: 2px dashed var(--text-accent); outline-offset: -2px; background: var(--bg-selected); }
    .file-drop-hint {
        position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); z-index: 10000;
        max-width: calc(100vw - 32px); padding: 10px 16px; pointer-events: none;
        color: var(--text-primary); background: var(--bg-content); border: 1px solid var(--text-accent);
        border-radius: var(--control-radius); box-shadow: var(--shadow-card); font-size: 13px;
    }
    [data-catalog]:not(:focus-within) .theme-selected { background: var(--bg-selected-inactive); box-shadow: none; }
    [data-catalog-row] { scroll-margin: 44px 0 8px; }
    [data-catalog-row]:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: -2px; }
    /* 表头与行共用列宽；窄目录把元数据移到标题下方，不丢失格式和日期。 */
    .library-grid, .library-table-header {
        display: grid;
        grid-template-columns: minmax(0, 1fr) 52px 78px 88px 28px;
        align-items: center;
        column-gap: 12px;
        width: 100%;
    }
    .library-grid-batch, .library-header-batch {
        grid-template-columns: 24px minmax(0, 1fr) 52px 78px 88px;
    }
    .library-table-header {
        padding: 8px;
        background: var(--bg-panel);
        border-bottom: 1px solid var(--border-color);
        position: sticky;
        top: 0;
        z-index: 1;
    }
    .library-th { font-size: 11px; font-weight: 600; }
    .library-row { cursor: pointer; border-bottom: 1px solid var(--border-color); min-height: 44px; padding: 8px; }
    .library-file-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; line-height: 1.5; font-weight: 500; }
    .library-row-meta { display: contents; }
    .library-row-meta > * { min-width: 0; max-width: 100%; }
    .library-col-date { font-variant-numeric: tabular-nums; }
    .library-row-action { opacity: 0; transition: opacity 120ms ease; }
    .library-row:hover .library-row-action, .library-row:focus-within .library-row-action { opacity: 1; }
    .source-target { background: var(--bg-selected); box-shadow: 0 0 0 8px var(--bg-selected); border-radius: var(--control-radius); }
    .library-reader-title { min-width: 0; }
    .reader-toolbar { gap: 8px; }
    .reader-view-switch { border: 1px solid var(--border-color); border-radius: 3px; padding: 1px; flex-shrink: 0; }
    .reader-view-switch .q-btn { min-height: 25px; padding: 2px 8px; border-radius: 1px; font-size: 12px; }
    .reader-mode-active { background: var(--bg-hover); color: var(--text-primary); font-weight: 600; }
    .file-info-drawer {
        position: absolute;
        top: var(--toolbar-height);
        right: 0;
        bottom: 0;
        width: min(340px, 100%);
        z-index: 3;
        border-left: 1px solid var(--border-color);
        box-shadow: -4px 0 16px #0001;
    }
    .source-pane { container-type: inline-size; background: var(--bg-sidebar); border-left: 1px solid var(--border-color); }
    .source-toolbar.library-heading {
        height: auto; min-height: var(--toolbar-height); padding: 6px 8px;
        flex-wrap: wrap; align-content: center; background: var(--bg-panel);
    }
    .source-toolbar > .row { flex-shrink: 0; }
    .source-caption { min-width: 0; }
    .source-page-input { width: 38px; }
    .source-page-input .q-field__control { height: 28px; }
    .source-page-input .q-field__native { padding: 0; text-align: center; font-size: 12px; }
    .source-scroll { padding: 16px; overscroll-behavior: contain; }
    .source-image { display: block; flex-shrink: 0; background: white; }
    .source-zoom-label { min-width: 32px; text-align: center; }
    @container (max-width: 620px) {
        .source-caption { display: none; }
    }
    .library-document { padding: 30px clamp(16px, 3vw, 44px); max-width: 900px; margin: 0 auto; }
    .library-reading-block { margin-bottom: 30px; gap: 12px; }
    .library-reading-title { font-size: 18px; line-height: 1.5; font-weight: 600; }
    .library-block-actions { opacity: 0; transition: opacity 120ms ease; }
    .library-reading-block:hover .library-block-actions,
    .library-reading-block:focus-within .library-block-actions { opacity: 1; }
    .library-cards { padding: 16px; background: var(--bg-panel); }
    .library-cards .chunk-sheet.q-card {
        background: var(--bg-content);
        border: 1px solid var(--border-color);
        border-radius: 6px;
        margin-bottom: 12px;
    }
    .wiki-paper { padding: 32px clamp(20px, 4vw, 56px) 64px; max-width: 900px; }
    .wiki-paper .knowledge-title { font-size: 28px; line-height: 1.4; }
    .wiki-kind-label { font-size: 12px; font-weight: 600; color: var(--text-muted); }
    .wiki-summary { font-size: 17px; line-height: 1.7; padding: 8px 0 16px; }
    .wiki-empty { gap: 16px; padding-top: 15vh; }
    .wiki-reader-inspector { width: 300px; flex-shrink: 0; border-left: 1px solid var(--border-color); }
    .wiki-reader-inspector .knowledge-evidence { padding: 12px 0 12px 10px; }
    .wiki-reader-inspector .knowledge-links { margin-top: 16px; }
    .editor-command-bar { padding: 8px 16px; border-bottom: 1px solid var(--border-color); flex-shrink: 0; background: var(--bg-panel); }
    .editor-draft-status { min-width: 0; max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .editor-draft-status[data-state=unsaved] { color: var(--text-primary); }
    .editor-draft-status:is([data-state=failed], [data-state=conflict], [data-state=uncertain]) { color: var(--text-danger); }
    .markdown-toolbar { border-top: 1px solid var(--border-color); border-bottom: 1px solid var(--border-color); padding: 4px 0; }
    .wiki-editor-layout { display: grid; grid-template-columns: minmax(0, 1fr) 260px; width: 100%; min-height: 100%; }
    .wiki-editor-paper { padding: 28px clamp(20px, 4vw, 56px) 60px; gap: 16px; }
    .wiki-title-input .q-field__native { font-size: 28px; font-weight: 600; line-height: 1.5; }
    .wiki-summary-input textarea { font-size: 16px; line-height: 1.7; color: var(--text-secondary); }
    .wiki-editor-body textarea { min-height: 48vh; font-family: var(--font-mono); font-size: 15px; line-height: 1.9; }
    .wiki-editor-properties { padding: 20px; gap: 18px; background: var(--bg-panel); border-left: 1px solid var(--border-color); }
    .editor-feedback:empty { display: none; }
    .editor-feedback:not(:empty) { padding: 16px; background: var(--bg-panel); border-left: 2px solid var(--text-accent); }
    @container (max-width: 640px) {
        .library-table-header { display: none; }
        .library-grid { grid-template-columns: minmax(0, 1fr) 28px; align-items: start; row-gap: 4px; }
        .library-grid-batch { grid-template-columns: 24px minmax(0, 1fr); }
        .library-title-cell { grid-column: 1; grid-row: 1; }
        .library-file-name {
            display: -webkit-box;
            -webkit-box-orient: vertical;
            -webkit-line-clamp: 2;
            white-space: normal;
            overflow-wrap: anywhere;
        }
        .library-row-meta {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 4px 12px;
            grid-column: 1 / -1;
            grid-row: 2;
            padding-left: 24px;
        }
        .library-grid-batch .library-title-cell, .library-grid-batch .library-row-meta { grid-column: 2; }
        .library-row-check { grid-column: 1; grid-row: 1; }
        .library-row-action { grid-column: 2; grid-row: 1; }
    }
    @container (max-width: 800px) {
        .wiki-reader-inspector { position: absolute; right: 0; top: 0; width: min(320px, 90%); z-index: 2; box-shadow: -4px 0 16px #0001; }
        .wiki-editor-layout { grid-template-columns: 1fr; }
        .wiki-editor-properties { border-left: 0; border-top: 1px solid var(--border-color); }
        .editor-command-bar { flex-wrap: wrap; }
        .editor-draft-status { order: 10; flex-basis: 100%; max-width: none; }
    }
    @media (max-width: 760px) {
        .app-tools { font-size: 12px; }
        .app-tools .q-icon { display: none; }
        .app-header { gap: 4px; }
    }
    @media (hover: none), (prefers-reduced-motion: reduce) {
        .library-block-actions, .library-row-action { opacity: 1; transition: none; }
    }

    .workspace-splitter > .q-splitter__panel {
        overflow: hidden;
        min-width: 0;
    }
    .workspace-splitter > .q-splitter__before > .nicegui-column {
        width: 100%;
    }
    .results-splitter > .q-splitter__before { max-width: max(220px, calc(100% - 280px)); }
    .results-splitter > .q-splitter__after { container-type: inline-size; }
    @container (max-width: 560px) {
        .workspace-content:has(.source-pane) { flex-direction: column; }
        .workspace-content:has(.source-pane) > .reader-panel,
        .workspace-content > .source-pane { width: 100%; height: 50%; min-height: 0; flex: 1; }
        .workspace-content > .source-pane { border-left: 0; border-top: 1px solid var(--border-color); }
    }
    .workspace-splitter > .q-splitter__separator {
        background: var(--border-color);
    }
    .workspace-splitter > .q-splitter__separator:hover {
        background: var(--text-accent);
    }
    .library-heading {
        height: var(--toolbar-height);
        flex-shrink: 0;
        border-bottom: 1px solid var(--border-color);
    }
    .workspace-toolbar {
        min-height: var(--toolbar-height);
        padding: 0 12px;
        gap: 6px;
        background: var(--bg-panel);
        flex-wrap: nowrap;
    }
    .workspace-toolbar .q-btn { border-radius: var(--control-radius); }
    .workspace-nav-item {
        min-height: 32px;
        padding: 4px 10px;
        border-radius: var(--control-radius);
        font-size: 13px;
    }
    .workspace-nav-item .q-icon { font-size: 18px; }
    .workspace-section-label {
        padding: 5px 10px;
        font-size: 11px;
        font-weight: 600;
        color: var(--text-muted);
    }
    .workspace-utilities { max-height: 45vh; overflow-y: auto; flex-shrink: 0; }
    .workspace-tools > .q-expansion-item__container > .q-item {
        min-height: 32px;
        padding: 4px 10px;
        border-radius: var(--control-radius);
        color: var(--text-secondary);
        font-size: 13px;
    }
    .workspace-tools .q-item__section--avatar { min-width: 30px; }
    .workspace-tools .q-icon { font-size: 18px; }
    .workspace-list-item {
        padding: 9px 10px;
        border-radius: var(--control-radius);
        cursor: pointer;
        transition: background-color 120ms ease;
    }
    .workspace-list-item.theme-selected { box-shadow: inset 2px 0 var(--border-selected); }
    .workspace-list-title { font-size: 13px; line-height: 1.5; font-weight: 500; }
    .workspace-search .q-field__control { border-radius: var(--control-radius); }
    .workspace-search .q-field__native { font-size: 13px; }
    .workspace-document { padding: 28px clamp(16px, 4vw, 56px); }
    .knowledge-title { font-size: 26px; font-weight: 600; line-height: 1.4; }
    .knowledge-metadata { border-bottom: 1px solid var(--border-color); padding-bottom: 12px; }
    .knowledge-links { border-top: 1px solid var(--border-color); }
    .knowledge-links .q-item { min-height: 36px; padding-left: 0; font-size: 13px; }
    .chunk-sheet.q-card {
        border: 0;
        border-bottom: 1px solid var(--border-color);
        border-radius: 0;
        box-shadow: none;
        background: transparent;
    }
    .chunk-sheet .chunk-heading { border: 0; }
    .editor-dialog { padding: 0; gap: 0; border-radius: 10px; }
    .editor-header, .editor-footer { padding: 12px 20px; }
    .editor-header { border-bottom: 1px solid var(--border-color); }
    .editor-footer { border-top: 1px solid var(--border-color); }
    .editor-fields { padding: 20px; gap: 16px; overflow-y: auto; }
    .editor-body textarea { min-height: 280px; font-family: var(--font-mono); line-height: 1.7; }
    .theme-danger { color: var(--text-danger); }
    @media (prefers-reduced-motion: reduce) {
        .workspace-list-item { transition: none; }
    }
    .library-footer {
        height: var(--toolbar-height);
        flex-shrink: 0;
        align-items: center;
        border-top: 1px solid var(--border-color);
    }
    .collection-tree .q-tree__node-header {
        border-radius: 6px;
        padding: 5px 4px;
    }
    .collection-tree .q-tree__node-header.q-tree__node--selected {
        background: var(--bg-selected);
    }
    .collection-tree .q-tree__node-header:focus-visible,
    [role="button"]:focus-visible {
        outline: 2px solid var(--text-accent);
        outline-offset: -2px;
    }
    .collection-tree .q-tree__node-header-content {
        color: var(--text-primary);
        min-width: 0;
        flex-wrap: nowrap;
        font-size: 0.8125rem;
    }
    .collection-tree-icon { flex-shrink: 0; margin-right: 4px; }
    .collection-tree-label { display: flex; align-items: center; flex: 1; min-width: 0; gap: 4px; }
    .collection-tree-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .collection-tree-count { flex-shrink: 0; white-space: nowrap; font-size: 12px; font-variant-numeric: tabular-nums; color: var(--text-muted); }
    .breadcrumb-label { max-width: 100%; }
    .breadcrumb-label .q-btn__content { overflow-wrap: anywhere; }

    /* 选中边框样式 */
    .theme-border-selected {
        border-left-color: var(--border-selected) !important;
    }

    html, body {
        overflow: hidden !important;
        height: 100vh !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    .nicegui-content {
        height: 100vh !important;
        overflow: hidden !important;
        padding: 0 !important;
        gap: 0 !important;
    }
    .chunk-content h1, .chunk-content h2, .chunk-content h3 {
        color: var(--text-primary) !important;
    }
    .chunk-content h1 {
        font-size: 1.2rem !important;
        line-height: 1.6rem !important;
        font-weight: 600 !important;
        margin-top: 0.4rem !important;
        margin-bottom: 0.4rem !important;
    }
    .chunk-content h2 {
        font-size: 1.05rem !important;
        line-height: 1.45rem !important;
        font-weight: 600 !important;
        margin-top: 0.4rem !important;
        margin-bottom: 0.4rem !important;
    }
    .chunk-content h3 {
        font-size: 0.98rem !important;
        font-weight: 600 !important;
        margin-top: 0.35rem !important;
        margin-bottom: 0.35rem !important;
    }
    .chunk-content p {
        font-size: 0.875rem !important;
        color: var(--text-secondary) !important;
        margin-bottom: 0.45rem !important;
        white-space: pre-wrap !important;
    }
    .chunk-content ul, .chunk-content ol {
        font-size: 0.875rem !important;
        padding-left: 1.4rem !important;
        margin-bottom: 0.45rem !important;
        color: var(--text-secondary) !important;
    }
    .chunk-content code {
        font-size: 0.8rem !important;
        background-color: var(--code-bg) !important;
        color: var(--code-text) !important;
        padding: 0.1rem 0.25rem !important;
        border-radius: 0.2rem !important;
    }
    .chunk-content pre {
        font-size: 0.8rem !important;
        background-color: var(--pre-bg) !important;
        color: var(--pre-text) !important;
        padding: 0.65rem !important;
        border-radius: 0.3rem !important;
        overflow-x: hidden !important;
        white-space: pre-wrap !important;
        word-wrap: break-word !important;
    }

    .chunk-content.library-reading-body :is(p, ul, ol) {
        font-size: 16px !important;
        line-height: 1.85 !important;
        color: var(--text-primary) !important;
        margin-bottom: 0.8em !important;
    }

    /* Wiki 使用文档排版，不继承切片管理视图的小字号。 */
    .chunk-content.knowledge-prose p,
    .chunk-content.knowledge-prose ul,
    .chunk-content.knowledge-prose ol {
        font-size: 1em !important;
        line-height: 1.85 !important;
        color: var(--text-primary) !important;
        margin-bottom: 1em !important;
    }
    .chunk-content.knowledge-prose h1 { font-size: 1.65em !important; }
    .chunk-content.knowledge-prose h2 { font-size: 1.35em !important; }
    .chunk-content.knowledge-prose h3 { font-size: 1.12em !important; }
    .chunk-content.knowledge-prose :is(h1, h2, h3) {
        line-height: 1.5 !important;
        margin-top: 1.5em !important;
        margin-bottom: 0.65em !important;
    }

    /* 切片卡片内容区域 - 禁止水平滚动 */
    .chunk-content {
        overflow-x: hidden !important;
        overflow: hidden !important;
        word-wrap: break-word !important;
        overflow-wrap: break-word !important;
        word-break: break-word !important;
        max-width: 100% !important;
        width: 100% !important;
        /* 允许文本选择和复制 */
        user-select: text !important;
        -webkit-user-select: text !important;
        -moz-user-select: text !important;
        -ms-user-select: text !important;
        cursor: text !important;
    }
    .chunk-content * {
        max-width: 100% !important;
        word-wrap: break-word !important;
        overflow-wrap: break-word !important;
        word-break: break-word !important;
        /* 允许所有子元素的文本选择 */
        user-select: text !important;
        -webkit-user-select: text !important;
        -moz-user-select: text !important;
        -ms-user-select: text !important;
    }
    .chunk-content a {
        /* 长 URL 需要能在任意位置断行，但不用 break-all —— 那会把正文里的
           普通英文单词也从中间劈开 */
        overflow-wrap: anywhere !important;
        cursor: pointer !important;  /* 链接保持指针样式 */
    }
    .chunk-content button {
        cursor: pointer !important;  /* 按钮保持指针样式 */
        user-select: none !important;  /* 按钮文本不可选 */
    }
    /* 表格样式 */
    .chunk-content table {
        width: 100% !important;
        max-width: 100% !important;
        border-collapse: collapse !important;
        margin: 0.5rem 0 !important;
        font-size: 0.875rem !important;
        display: block !important;
        overflow-x: auto !important;
        overflow-wrap: normal !important;
        word-break: normal !important;
    }
    .chunk-content table::-webkit-scrollbar { display: block; height: 8px; }
    .chunk-content table::-webkit-scrollbar-track { background: var(--bg-panel); }
    .chunk-content table::-webkit-scrollbar-thumb { background: var(--text-muted); border-radius: 4px; }
    .chunk-content :is(th, td) {
        min-width: 7.5rem;
        max-width: none !important;
        vertical-align: top;
        white-space: normal !important;
    }
    .chunk-content :is(th, td), .chunk-content :is(th, td) * {
        word-break: normal !important;
        overflow-wrap: normal !important;
        word-wrap: normal !important;
    }
    .chunk-content th {
        background-color: var(--bg-hover) !important;
        color: var(--text-primary) !important;
        font-weight: 600 !important;
        padding: 0.5rem !important;
        border: 1px solid var(--border-color) !important;
        text-align: left !important;
    }
    .chunk-content td {
        color: var(--text-secondary) !important;
        padding: 0.5rem !important;
        border: 1px solid var(--border-color) !important;
        text-align: left !important;
    }
    /* 表格容器支持横向滚动（超宽时） */
    .chunk-content > div:has(> table) {
        overflow-x: auto !important;
        max-width: 100% !important;
    }

    /* 滚动区内容层必须锁死为栏宽。
       Quasar 把 .q-scrollarea__content 渲染成 position:absolute + min-width:100%，
       宽度是 shrink-to-fit：一旦内部有不可断行的元素（如切片标题的 truncate 会带上
       white-space:nowrap），最小内容宽度就把它撑得比栏还宽，里面所有 w-full 的卡片
       跟着按这个虚宽布局，最后被卡片的 overflow-hidden 从右侧裁掉——表现为标题不出
       省略号、正文被拦腰切断。定死 width 后 truncate 和 max-width:100% 才会生效。 */
    .nicegui-scroll-area .q-scrollarea__content {
        width: 100% !important;
        max-width: 100% !important;
    }

    /* truncate（overflow:hidden + nowrap + 省略号）只有在宽度确定时才会生效，
       而 NiceGUI 的 row/column 默认 align-items:flex-start，子元素按自身内容宽度
       摆放、不会被父容器压缩，于是长标题一路顶出去而不是打省略号。补上这两条让
       所有 truncate 元素跟随父容器收缩。 */
    .truncate {
        min-width: 0 !important;
        max-width: 100% !important;
    }

    /* 列表型滚动区：归零 NiceGUI 给内容层的默认内边距和间距。
       .nicegui-scroll-area .q-scrollarea__content 自带 padding:1rem + gap:1rem，
       而 refreshable 的模板是 <slot></slot>（不产生包裹元素），列表项会直接成为
       该内容层的 flex 子元素、被撑开 16px。挂本类后由内部自行控制间距。 */
    .scroll-flush .q-scrollarea__content {
        padding: 0 !important;
        gap: 0 !important;
    }

    /* 主题化样式类 */
    .theme-sidebar { background-color: var(--bg-sidebar); border-color: var(--border-color); }
    .theme-panel { background-color: var(--bg-panel); border-color: var(--border-color); }
    .theme-content { background-color: var(--bg-content); }
    .theme-card { background-color: var(--bg-card); border-color: var(--border-color); }
    .theme-hover:hover { background-color: var(--bg-hover); }
    .theme-selected { background-color: var(--bg-selected); }
    .theme-border { border-color: var(--border-color); }
    .theme-text { color: var(--text-primary); }
    .theme-text-secondary { color: var(--text-secondary); }
    .theme-text-muted { color: var(--text-muted); }
    .theme-text-accent { color: var(--text-accent); }

    .theme-card-shadow {
        box-shadow: var(--shadow-card);
    }
    .theme-card-divider {
        border-bottom: 1px solid var(--border-color);
    }
    .theme-border-soft {
        border-color: rgba(120, 130, 140, 0.35);
    }

    .nicegui-content {
        background-color: var(--bg-content);
    }
</style>
'''


def inject_theme_css():
    """注入主题 CSS 到页面"""
    ui.add_head_html(THEME_CSS)


def init_theme(dark_mode, theme: str):
    """
    初始化主题

    Args:
        dark_mode: NiceGUI dark_mode 对象
        theme: 主题名称 ('light', 'dark', 'pink')
    """
    if theme == "dark":
        dark_mode.enable()
    elif theme == "pink":
        dark_mode.disable()
        ui.colors(primary=PINK_PRIMARY)
        # 用 ui.query 而非「延迟 100ms 跑 JS」：query 的 class 随首帧一起下发，
        # 否则页面会先以默认蓝色强调色渲染约 100ms 再突变成粉色。
        ui.query("body").classes("theme-pink")
    else:  # light
        dark_mode.disable()


def apply_theme(dark_mode, theme: str):
    """
    切换主题

    Args:
        dark_mode: NiceGUI dark_mode 对象
        theme: 主题名称 ('light', 'dark', 'pink')
    """
    # 移除所有主题类
    ui.query("body").classes(remove="theme-pink")

    if theme == "dark":
        dark_mode.enable()
        ui.colors()  # 恢复默认颜色
    elif theme == "pink":
        dark_mode.disable()
        ui.query("body").classes("theme-pink")
        ui.colors(primary=PINK_PRIMARY)  # 设置粉色主色
    else:  # light
        dark_mode.disable()
        ui.colors()  # 恢复默认颜色
