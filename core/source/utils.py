"""源站共享的纯函数工具；只接纳与任何源站无关的通用逻辑。"""

import asyncio
import re
import time
from datetime import datetime
from html import unescape
from typing import Any, Optional


_TAG_RE = re.compile(r"<[^>]+>")
_COUNT_RE = re.compile(r"^([\d.,]+)\s*([KkMmWw萬万億亿]?)")
_COUNT_UNITS = {
    "k": 1_000,
    "m": 1_000_000,
    "w": 10_000,
    "万": 10_000,
    "萬": 10_000,
    "亿": 100_000_000,
    "億": 100_000_000,
}


def strip_html(text: str) -> str:
    """去除 HTML 标签并反转义实体，合并空白。"""
    cleaned = _TAG_RE.sub(" ", str(text or ""))
    cleaned = unescape(cleaned).replace("\xa0", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", cleaned).strip()


def to_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """宽松转换整数，失败返回默认值。"""
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError, OverflowError):
        return default


def to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """宽松转换浮点数，失败返回默认值。"""
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError, OverflowError):
        return default


def parse_count(text: str) -> Optional[int]:
    """解析 ``7K``、``1.2M``、``185.8萬`` 这类带单位的数量。"""
    match = _COUNT_RE.match(str(text or "").strip())
    if not match:
        return None
    number = to_float(match.group(1))
    if number is None:
        return None
    unit = match.group(2).lower()
    return int(number * _COUNT_UNITS.get(unit, 1))


def format_timestamp(value: Any) -> str:
    """把秒级时间戳格式化为 ``YYYY-MM-DD``；无法解析时原样返回字符串。"""
    seconds = to_int(value)
    if seconds is None or seconds <= 0:
        return str(value or "")
    if seconds > 10**11:  # 毫秒
        seconds //= 1000
    try:
        return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return str(value)


def iso_date(value: Any) -> str:
    """截取 ISO 时间字符串的日期部分。"""
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else text


class RateLimiter:
    """保证两次请求之间至少间隔 ``interval`` 秒（协程安全）。"""

    def __init__(self, interval: float):
        self.interval = max(0.0, float(interval))
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        """等待到允许发出下一次请求。"""
        if self.interval <= 0:
            return
        async with self._lock:
            delay = self._last + self.interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()
