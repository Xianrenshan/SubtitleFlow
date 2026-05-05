import asyncio
import time
from typing import Callable, Awaitable


async def heartbeat_updater(
    step_name: str,
    progress_callback: Callable[[str, int, float, float], Awaitable[None]],
    get_progress: Callable[[], int],
    get_eta: Callable[[], float] = None,
    interval: float = 1.0
):
    """
    周期性地调用 progress_callback，确保已用时间和 ETA 持续更新。
    ETA 仅在进度百分比变化时重新计算，其余时刻保持不变，避免波动。
    """
    started_at = time.time()
    last_progress = -1
    cached_eta = None

    async def heartbeat():
        nonlocal last_progress, cached_eta
        while True:
            elapsed = time.time() - started_at
            progress = get_progress()
            if progress != last_progress:
                # 进度变化，重新计算 ETA
                last_progress = progress
                if get_eta:
                    cached_eta = get_eta()
                else:
                    if progress > 0 and elapsed > 0:
                        total_estimated = (elapsed / progress) * 100
                        cached_eta = max(0, total_estimated - elapsed)
                    else:
                        cached_eta = None
            # 每次心跳都发送当前已用时间和缓存的 ETA
            await progress_callback(step_name, progress, elapsed, cached_eta)
            await asyncio.sleep(interval)

    task = asyncio.create_task(heartbeat())
    return task