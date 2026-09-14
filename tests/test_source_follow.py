"""原页跟随：最新目标、请求合并、组件复用及浏览器阅读缓冲。"""

import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ui.handlers import chunk_handlers
from app.ui.views.files_view import _SOURCE_SCROLL_HANDLER, render_files_source


def _handlers():
    return chunk_handlers.ChunkHandlers({
        "selected_file_id": 1,
        "source_pane_open": True,
        "source_page": {"file_id": 1, "page": 1, "url": "/pages/1.png", "caption": "page 1"},
        "files_data": [{"id": 1, "filename": "book.pdf"}],
        "chunks_data": [{"id": page * 10, "heading_path": f"book / 第{page}页"} for page in (1, 2, 3, 4)],
    }, {}, lambda: None)


def test_pending_same_page_is_merged_and_intermediate_pages_are_skipped(monkeypatch):
    async def scenario():
        handler = _handlers()
        started, release = asyncio.Event(), asyncio.Event()
        rendered, shown = [], []

        async def render(_function, _file, page):
            rendered.append(page)
            if page == 2:
                started.set()
                await release.wait()
            return Path(f"page-{page}.png")

        monkeypatch.setattr(chunk_handlers, "run_sync", render)
        handler._refresh_source_column = lambda: shown.append(handler.state["source_page"]["page"])
        first = asyncio.create_task(handler._show_source_page(2))
        await started.wait()
        await handler._show_source_page(2)
        middle = asyncio.create_task(handler._show_source_page(3))
        await asyncio.sleep(0)
        latest = asyncio.create_task(handler._show_source_page(4))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, middle, latest)
        assert rendered == [2, 4]
        assert shown == [4], "旧页不能先显示或在新页之后倒跳回来"
        assert handler._source_request is None

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["back_to_displayed", "close", "switch_away_and_back"])
def test_invalidated_render_cannot_update_the_pane(monkeypatch, action):
    async def scenario():
        handler = _handlers()
        started, release = asyncio.Event(), asyncio.Event()

        async def render(*_args):
            started.set()
            await release.wait()
            return Path("page-2.png")

        monkeypatch.setattr(chunk_handlers, "run_sync", render)
        first = asyncio.create_task(handler._show_source_page(2))
        await started.wait()
        if action == "back_to_displayed":
            await handler.handle_chunk_scroll(SimpleNamespace(args={"file_id": 1, "chunk_id": 10}))
        elif action == "close":
            handler.close_source_page()
        else:
            handler.invalidate_source_requests()
            handler.state["selected_file_id"] = 2
            handler.state["selected_file_id"] = 1
        release.set()
        await first
        source = handler.state["source_page"]
        assert source is None if action == "close" else source["page"] == 1

    asyncio.run(scenario())


def test_old_file_or_removed_chunk_event_is_ignored(monkeypatch):
    handler = _handlers()
    monkeypatch.setattr(chunk_handlers, "run_sync", lambda *_: pytest.fail("过期事件不能渲染"))

    async def scenario():
        for args in ({"file_id": 2, "chunk_id": 20}, {"file_id": 1, "chunk_id": 99}, 0, None):
            await handler.handle_chunk_scroll(SimpleNamespace(args=args))

    asyncio.run(scenario())


def test_failed_render_can_be_retried_and_manual_request_keeps_notification(monkeypatch):
    async def scenario():
        handler = _handlers()
        started, release = asyncio.Event(), asyncio.Event()
        calls, notices = [], []

        async def render(*_args):
            calls.append(1)
            started.set()
            await release.wait()
            return None if len(calls) == 1 else Path("page-2.png")

        monkeypatch.setattr(chunk_handlers, "run_sync", render)
        monkeypatch.setattr(chunk_handlers.ui, "notify", lambda *args, **kwargs: notices.append(args))
        automatic = asyncio.create_task(handler._show_source_page(2))
        await started.wait()
        await handler.handle_view_source_page(2)
        release.set()
        await automatic
        assert len(calls) == len(notices) == 1
        await handler._show_source_page(2)
        assert handler.state["source_page"]["page"] == 2
        assert len(calls) == 2

    asyncio.run(scenario())


def test_changing_source_reuses_image_caption_and_scroll_container():
    from nicegui import ui

    handler = _handlers()
    with ui.column() as container:
        render_files_source(handler.state, handler.ui_refs, handler)
    try:
        refs = handler.ui_refs.copy()
        image = refs["source_image"]
        scroll = image.parent_slot.parent
        handler.state["source_page"] = {
            "file_id": 1, "page": 2, "url": "/pages/2.png", "caption": "page 2",
        }
        handler._refresh_source_column()
        assert handler.ui_refs == refs
        assert image.parent_slot.parent is scroll and not scroll.is_deleted
        assert image.source == "/pages/2.png"
        assert refs["source_caption"].text == refs["source_tooltip"].text == "page 2"
        assert refs["source_pane"]._props["data-page"] == "2"
        assert image._props["no-transition"] is True
    finally:
        container.delete()


def test_browser_follow_buffer_and_dwell():
    node = shutil.which("node")
    if node is None:
        pytest.skip("浏览器跟随逻辑的离线检查需要 Node.js")
    script = r"""
const assert = require('node:assert/strict');
let now = 0, nextTimer = 0;
const timers = new Map(), events = [];
global.setTimeout = (callback, delay) => {
    const id = ++nextTimer;
    timers.set(id, {callback, at: now + delay});
    return id;
};
global.clearTimeout = id => timers.delete(id);
function advance(ms) {
    now += ms;
    for (const [id, timer] of [...timers]) {
        if (timer.at <= now) {timers.delete(id); timer.callback();}
    }
}
const card = (page, top, bottom) => ({
    dataset: {fileId: '1', page: String(page), chunkId: String(page * 10)},
    isConnected: true, top, bottom,
    getBoundingClientRect() {return {top: this.top, bottom: this.bottom};}
});
const cards = [card(1, -500, 140), card(2, 148, 700), card(3, 708, 1000)];
const area = {
    isConnected: true,
    querySelectorAll: () => cards,
    getBoundingClientRect: () => ({top: 0, height: 600})
};
const pane = {isConnected: true, dataset: {fileId: '1', page: '1', followRevision: '0'}};
global.document = {querySelector: selector => selector === '.chunk-scroll' ? area : pane};
const emit = event => events.push(event);
const scroll = HANDLER;
const boundary = y => {cards[0].bottom = y; cards[1].top = y + 8; scroll({});};
// 阅读线 200px，缓冲带 128..272px：没有越出带时不切页。
boundary(140); advance(500); assert.equal(events.length, 0);
boundary(90); advance(249); assert.equal(events.length, 0);
// 候选未稳定就滚回去，不发出短暂经过的页。
boundary(150); advance(1); assert.equal(events.length, 0);
boundary(90); advance(200);
boundary(80); advance(50); // 同一候选持续滚动不重置停留计时。
assert.deepEqual(events, [{file_id: 1, chunk_id: 20}]);
pane.dataset.page = '2';
boundary(12); advance(500); assert.equal(events.length, 1); // 上一页只露一点。
boundary(240); advance(500); assert.equal(events.length, 1); // 反向仍留有缓冲。
boundary(300); advance(100);
boundary(260); advance(200); assert.equal(events.length, 1); // 边界抖动被取消。
boundary(300); advance(250); assert.equal(events[1].chunk_id, 10);
pane.dataset.page = '1';
// 手动切页、关闭或卡片被替换后，旧定时器不能再上报。
boundary(90); pane.dataset.followRevision = '1'; advance(250);
assert.equal(events.length, 2);
boundary(90); cards[1].isConnected = false; advance(250);
assert.equal(events.length, 2);
cards[1].isConnected = true;
boundary(90); pane.isConnected = false; advance(250);
assert.equal(events.length, 2);
""".replace("HANDLER", _SOURCE_SCROLL_HANDLER)
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
