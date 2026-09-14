"""
速率限制器测试

验证 RPM + TPM 双维度滑动窗口限流器的行为：
- 额度充足时不产生额外等待
- 触及 RPM 或 TPM 上限时正确阻塞

测试不依赖 pytest-asyncio：各场景协程由 asyncio.run 驱动。
"""

import asyncio
import sys
import time
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from indexing.services.rate_limiter import (
    WINDOW_SECONDS,
    RateLimiter,
    estimate_tokens,
)


async def _no_wait_within_quota():
    """额度充足时应立即放行，不再有固定间隔"""
    limiter = RateLimiter(rpm=200, tpm=400000)

    start = time.monotonic()
    for _ in range(20):
        await limiter.acquire(1000)
    elapsed = time.monotonic() - start

    stats = limiter.get_stats()
    print(f"20 次请求耗时: {elapsed:.3f}s")
    print(f"窗口内: {stats['window_requests']} 请求 / {stats['window_tokens']} tokens")

    assert elapsed < 0.5, f"额度充足却等待了 {elapsed:.3f}s"
    assert stats["window_requests"] == 20
    assert stats["window_tokens"] == 20000
    print("[OK] 额度充足时无额外等待")


async def _rpm_limit_blocks():
    """请求数触顶时应阻塞到窗口滑出"""
    limiter = RateLimiter(rpm=3, tpm=10**9)

    for _ in range(3):
        await limiter.acquire(1)

    start = time.monotonic()
    task = asyncio.create_task(limiter.acquire(1))
    # 第 4 次请求必须等待，这里只验证它在短时间内没有完成
    await asyncio.sleep(0.2)
    pending = not task.done()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    print(f"RPM 触顶后第 4 次请求在 {time.monotonic() - start:.3f}s 内仍在等待: {pending}")
    assert pending, "RPM 已触顶却立即放行"
    print("[OK] RPM 上限生效")


async def _tpm_limit_blocks():
    """token 触顶时应阻塞，即使请求数远未触顶"""
    limiter = RateLimiter(rpm=1000, tpm=1000)

    await limiter.acquire(900)

    task = asyncio.create_task(limiter.acquire(200))
    await asyncio.sleep(0.2)
    pending = not task.done()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    print(f"TPM 触顶后请求仍在等待: {pending}")
    assert pending, "TPM 已触顶却立即放行"
    print("[OK] TPM 上限生效")


async def _oversized_request_not_deadlocked():
    """单请求超过整分钟预算时按满额计费，不能永久阻塞"""
    limiter = RateLimiter(rpm=100, tpm=100)

    await asyncio.wait_for(limiter.acquire(10**6), timeout=1.0)

    stats = limiter.get_stats()
    print(f"超额请求已按满额计费: {stats['window_tokens']} tokens")
    assert stats["window_tokens"] == 100
    print("[OK] 超额单请求不会死等")


def test_no_wait_within_quota():
    asyncio.run(_no_wait_within_quota())


def test_rpm_limit_blocks():
    asyncio.run(_rpm_limit_blocks())


def test_tpm_limit_blocks():
    asyncio.run(_tpm_limit_blocks())


def test_oversized_request_not_deadlocked():
    asyncio.run(_oversized_request_not_deadlocked())


def test_estimate_tokens():
    """token 估算按字符数保守换算，空文本也至少计 1"""
    assert estimate_tokens([]) == 1
    assert estimate_tokens([""]) == 1
    assert estimate_tokens(["a" * 150]) == 100
    print("[OK] token 估算正常")


async def main():
    print("=" * 60)
    print(f"限流器测试（滑动窗口 {WINDOW_SECONDS:.0f}s）")
    print("=" * 60)

    test_estimate_tokens()
    await _no_wait_within_quota()
    await _rpm_limit_blocks()
    await _tpm_limit_blocks()
    await _oversized_request_not_deadlocked()

    print("\n全部通过")


if __name__ == "__main__":
    asyncio.run(main())
