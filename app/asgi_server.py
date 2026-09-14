"""Uvicorn 请求排空适配；固定超时后取消不等于同步工作已经停止。"""

import asyncio
import logging

import uvicorn

logger = logging.getLogger(__name__)


class DrainingServer(uvicorn.Server):
    async def shutdown(self, sockets=None):
        """先停止连接并排空请求，最后退出 lifespan（包括 MCP 长连接）。

        Uvicorn 0.52.4 默认在超时后 cancel 请求但不 await 就退出 lifespan；
        Piece 的取消安全同步操作可能仍在收尾，必须等它们真正结束再关库。
        """
        for listener in self.servers:
            listener.close()
        for sock in sockets or []:
            sock.close()
        for connection in list(self.server_state.connections):
            connection.shutdown()
        await asyncio.sleep(0.1)
        while tasks := list(self.server_state.tasks):
            _, pending = await asyncio.wait(tasks, timeout=self.config.timeout_graceful_shutdown)
            if pending:
                logger.info("[Shutdown] 取消 %s 个在途请求并等待其工作收尾", len(pending))
                for task in pending:
                    task.cancel()
            # 不设第二个超时，线程必须结束；这里结束前不会调用 lifespan.shutdown。
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
        for listener in self.servers:
            await listener.wait_closed()
        await self.lifespan.shutdown()
