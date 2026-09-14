"""
请求速率限制模块

职责:
- 按服务商额度控制嵌入请求的发送速率
- 同时约束 RPM（每分钟请求数）和 TPM（每分钟 token 数）
- 采用 60 秒滑动窗口，额度充足时立即放行，不强制固定间隔
"""

import asyncio
import logging
import time
from collections import deque
from typing import Deque, Iterable, Optional, Tuple

from ..settings import get_performance_config

logger = logging.getLogger(__name__)

# 滑动窗口长度：与服务商按分钟计算的额度对齐
WINDOW_SECONDS = 60.0

# 字符→token 的保守换算系数，与 settings.get_chunking_config 的取值保持一致
CHARS_PER_TOKEN = 1.5


def estimate_tokens(texts: Iterable[str]) -> int:
    """按字符数保守估算一批文本消耗的 token 数（至少为 1）。"""
    total_chars = sum(len(text) for text in texts)
    return max(1, int(total_chars / CHARS_PER_TOKEN))


class RateLimiter:
    """RPM + TPM 双维度的滑动窗口限流器。

    与固定最小间隔的实现不同，窗口内额度充足时请求立即放行，
    只有真正逼近服务商额度时才等待，避免小请求被无谓拖慢。
    """

    def __init__(self, rpm: int = 200, tpm: int = 400000):
        """
        初始化速率限制器

        Args:
            rpm: 每分钟最大请求数
            tpm: 每分钟最大 token 数
        """
        self.rpm = max(1, rpm)
        self.tpm = max(1, tpm)

        # 窗口内已放行的请求：(放行时刻, 消耗的 token 数)
        self._window: Deque[Tuple[float, int]] = deque()
        self._used_tokens = 0

        # 互斥锁：保证额度预留是原子的
        self._lock = asyncio.Lock()

        logger.info("[RateLimiter] 初始化: RPM=%s, TPM=%s", self.rpm, self.tpm)

    def _prune(self, now: float) -> None:
        """移出滑动窗口的记录并归还其占用的额度。"""
        deadline = now - WINDOW_SECONDS
        while self._window and self._window[0][0] <= deadline:
            self._used_tokens -= self._window.popleft()[1]

    async def acquire(self, tokens: int = 1) -> None:
        """
        申请一次请求许可，额度不足时等到窗口内最早的记录过期

        Args:
            tokens: 本次请求预计消耗的 token 数
        """
        # 单个请求超过整分钟预算时按满额计费，否则会永远等不到许可
        tokens = min(max(1, tokens), self.tpm)

        async with self._lock:
            while True:
                now = time.monotonic()
                self._prune(now)

                if (
                    len(self._window) < self.rpm
                    and self._used_tokens + tokens <= self.tpm
                ):
                    self._window.append((now, tokens))
                    self._used_tokens += tokens
                    return

                wait_for = self._window[0][0] + WINDOW_SECONDS - now
                logger.debug(
                    "[RateLimiter] 额度不足，等待 %.3f 秒（窗口内 %s 请求 / %s tokens）",
                    wait_for,
                    len(self._window),
                    self._used_tokens,
                )
                await asyncio.sleep(max(wait_for, 0.01))

    def get_stats(self) -> dict:
        """
        获取当前速率统计

        Returns:
            {"rpm": int, "tpm": int, "window_requests": int, "window_tokens": int}
        """
        self._prune(time.monotonic())
        return {
            "rpm": self.rpm,
            "tpm": self.tpm,
            "window_requests": len(self._window),
            "window_tokens": self._used_tokens,
        }


# 全局单例
_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """获取全局速率限制器实例（额度取自用户配置）"""
    global _rate_limiter
    if _rate_limiter is None:
        config = get_performance_config()
        _rate_limiter = RateLimiter(
            rpm=config.embedding_rpm,
            tpm=config.embedding_tpm,
        )
    return _rate_limiter


def reset_rate_limiter() -> None:
    """清除限流器实例，下次调用时按最新配置重建"""
    global _rate_limiter
    _rate_limiter = None
