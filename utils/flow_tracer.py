"""
流程追踪工具 - 定位流程卡住/缓慢的位置

用法：
    from utils.flow_tracer import trace, StepTimer, async_timeout

    trace("开始分析")                # 简单打点

    timer = StepTimer()              # 手动计时
    feeds = await to_thread(search_notes, query, 15)
    timer.mark("搜索帖子完成")        # 显示上一步耗时

    # 带超时保护，防止卡死
    detail = await async_timeout(to_thread(get_note_detail, id, token, False), 30)
"""

import asyncio
import time
from contextlib import asynccontextmanager


def trace(msg: str):
    """打印带时间戳的进度（flush 确保实时输出到日志）"""
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


class StepTimer:
    """手动计时：记录每个步骤耗时，快速定位慢在哪一步"""

    def __init__(self, title: str = ""):
        self._start = time.monotonic()
        self._last = self._start
        if title:
            trace(title)

    def mark(self, name: str) -> float:
        """标记一个步骤完成，返回该步骤耗时（秒）"""
        now = time.monotonic()
        elapsed = now - self._last
        total = now - self._start
        print(f"    ⏱ [{elapsed:6.1f}s] {name}   (累计 {total:.1f}s)", flush=True)
        self._last = now
        return elapsed

    def summary(self):
        total = time.monotonic() - self._start
        print(f"  == 总耗时 {total:.1f}s ==", flush=True)
        return total


async def async_timeout(coro, timeout: float):
    """
    对协程加超时保护。
    超时抛 asyncio.TimeoutError（可被上层捕获），不会无限卡住。
    """
    return await asyncio.wait_for(coro, timeout=timeout)


@asynccontextmanager
async def watchdog(timeout: float, name: str = "操作"):
    """
    上下文管理器：包裹一段 async 代码，超时打印警告（不中断）。
    适合观察某段是否过慢。
    """
    task = None
    try:
        yield
    finally:
        pass
