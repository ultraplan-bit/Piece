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
    :root {
        /* 字体栈：桌面端离线运行，不加载 Web 字体，
           按平台依次回退到各自的系统 UI 字体（含中文字形） */
        --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                     "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
        --font-mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, "Courier New", monospace;

        /* 浅色主题（默认） */
        --bg-sidebar: #f6f7f9;
        --bg-panel: #ffffff;
        --bg-content: #f0f2f4;
        --bg-card: #ffffff;
        --bg-hover: #eef1f3;
        --bg-selected: #e8f1fb;
        --border-color: #e3e6ea;
        --border-selected: #5b8def;
        --text-primary: #1f2328;
        --text-secondary: #5a646e;
        /* 该色大量用于 12px 小字，需在最深的浅色背景（内容区 #f0f2f4）上
           仍满足 WCAG AA 4.5:1 —— 实测白底 5.5:1 / 侧栏 5.1:1 / 内容区 4.9:1 */
        --text-muted: #606a76;
        --text-accent: #2b6cb0;
        --code-bg: #f3f4f6;
        --code-text: #b42318;
        --pre-bg: #f6f7f9;
        --pre-text: #1f2328;
        --shadow-card: 0 1px 2px rgba(15, 23, 42, 0.06);
    }

    body {
        font-family: var(--font-sans);
    }

    body.body--dark {
        /* 深色主题 - 更柔和的层次 */
        --bg-sidebar: #26292e;
        --bg-panel: #2b2f35;
        --bg-content: #1f2328;
        --bg-card: #2f343a;
        --bg-hover: #363b42;
        --bg-selected: #223a52;
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
    }

    body.theme-pink {
        /* 少女粉主题 - 仅替换强调色（--text-accent 需与 PINK_PRIMARY 保持一致） */
        --bg-selected: #fbe8ee;
        --border-selected: #f2a3ba;
        --text-accent: #e5628e;
    }

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
        display: table !important;
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
        word-wrap: break-word !important;
        overflow-wrap: break-word !important;
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
