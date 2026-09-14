"""
Indexing 公共工具函数

职责:
- 提供可复用的工具函数
- 向量序列化等底层操作
"""

import asyncio
import struct
import time
from typing import List


async def await_completion(awaitable):
    """取消调用方时仍等待已开始的操作结束，然后把取消交回调用方。"""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # 取走同步操作的异常，取消仍交回调用方处理
        raise


async def run_sync(func, /, *args, **kwargs):
    """取消时排空线程，不遗留写库或生成器推进操作。"""
    return await await_completion(asyncio.to_thread(func, *args, **kwargs))


def wait_for_stop(stop_check, timeout: float) -> bool:
    """可中断的阻塞退避；返回 True 表示收到了停止请求。"""
    deadline = time.monotonic() + timeout
    while True:
        if stop_check is not None and stop_check():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(remaining, 0.1))


def serialize_float32(vector: List[float]) -> bytes:
    """
    将浮点数列表序列化为 sqlite-vec 需要的二进制格式

    Args:
        vector: 浮点数列表（如 embedding 向量）

    Returns:
        二进制格式的向量数据
    """
    return struct.pack(f"{len(vector)}f", *vector)
