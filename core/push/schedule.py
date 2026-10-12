"""推送调度：计算下一次触发时刻，并在后台循环中按时执行任务。"""

import asyncio
import re
from datetime import datetime, time, timedelta
from typing import Awaitable, Callable, Optional

from ..logger import logger


_TIME_RE = re.compile(r"^\s*(\d{1,2})[:：](\d{2})\s*$")


def parse_clock(text: str) -> Optional[time]:
    """解析 ``HH:MM``，格式无效时返回 None。"""
    match = _TIME_RE.match(str(text or ""))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def next_interval(after: datetime, interval_minutes: int, start: time = time(0, 0)) -> datetime:
    """按间隔推送：每天从 ``start`` 起每隔 ``interval_minutes`` 分钟触发，返回 ``after`` 之后的下一时刻。

    间隔只支持 1 至 1440 分钟；不能整除一天时，最后一段到次日 ``start`` 截止，次日重新计算。
    """
    if not 1 <= interval_minutes <= 1440:
        raise ValueError("日内推送间隔必须为 1 至 1440 分钟")
    anchor = datetime.combine(after.date(), start)
    if anchor > after:
        anchor -= timedelta(days=1)
    step = timedelta(minutes=interval_minutes)
    fire = anchor + step * ((after - anchor) // step + 1)
    return min(fire, anchor + timedelta(days=1))


def next_daily(after: datetime, times: list[time]) -> datetime:
    """按时间点推送：返回 ``after`` 之后最近的时间点（``times`` 已排序且非空）。"""
    for offset in (0, 1):
        day = after.date() + timedelta(days=offset)
        for clock in times:
            fire = datetime.combine(day, clock)
            if fire > after:
                return fire
    raise ValueError("没有推送时间")


class Scheduler:
    """后台循环：等待到下一触发时刻执行任务；任务耗时过长时跳过错过的时刻。"""

    def __init__(
        self,
        name: str,
        next_after: Callable[[datetime], datetime],
        job: Callable[[datetime], Awaitable[None]],
    ):
        self.name = name
        self.next_after = next_after
        self.job = job
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """启动调度循环。"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """停止调度循环并等待其退出。"""
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _loop(self) -> None:
        last = datetime.now()
        while True:
            # 睡眠可能提前几毫秒醒来，从上一次触发时刻往后算，避免同一时刻触发两次
            fire = self.next_after(max(datetime.now(), last))
            await asyncio.sleep(max(0.0, (fire - datetime.now()).total_seconds()))
            last = fire
            try:
                await self.job(fire)
            except asyncio.CancelledError:
                raise
            except Exception:  # 单次推送失败不应终止调度循环
                logger.exception(f"推送任务「{self.name}」执行出错")
