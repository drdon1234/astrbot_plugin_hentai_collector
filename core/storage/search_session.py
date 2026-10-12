"""用户搜索会话：缓存最近一次列表结果（单站或聚合），支持按全局序号取作品与翻页。"""

import asyncio
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional

from ..constants import Config
from ..errors import CollectorError
from ..types import Period, SearchQuery, WorkInfo


MODE_SEARCH = "search"
MODE_RANKING = "ranking"


@dataclass
class SearchSession:
    """一次列表展示的上下文。

    Attributes:
        sources: 参与的源站键，单站指令只有一个
        mode: ``search`` 或 ``ranking``
        title: 聚合列表的总标题（单站列表为空）
        items: 已展示的作品，聚合时按源站顺序拼接（序号从 1 开始全局连续）
        page: 当前页码
        query: 搜索条件（搜索模式）
        period: 排行周期（排行模式；聚合排行为空表示各站默认榜单）
        has_next: 是否还有下一页
    """

    sources: list[str]
    mode: str
    title: str = ""
    items: list[WorkInfo] = field(default_factory=list)
    page: int = 1
    query: Optional[SearchQuery] = None
    period: Optional[Period] = None
    has_next: bool = False
    updated_at: float = field(default_factory=time.time)


class SearchSessionStore:
    """内存中的会话缓存，按 ``会话ID:发送者ID`` 隔离，超时自动失效。"""

    def __init__(self, ttl_seconds: int):
        self.ttl = max(60, int(ttl_seconds))
        self._sessions: OrderedDict[str, SearchSession] = OrderedDict()
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, key: str):
        """串行处理同一用户的列表操作，最后一个等待者退出后释放锁记录。"""
        lock, users = self._locks.get(key, (asyncio.Lock(), 0))
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            _, users = self._locks[key]
            if users == 1:
                self._locks.pop(key)
            else:
                self._locks[key] = (lock, users - 1)

    def discard(self, key: str) -> None:
        """清除列表，防止新搜索无结果后继续使用旧序号。"""
        self._sessions.pop(key, None)

    def save(self, key: str, session: SearchSession) -> None:
        """保存会话并淘汰最旧的条目。"""
        session.updated_at = time.time()
        self._sessions[key] = session
        self._sessions.move_to_end(key)
        while len(self._sessions) > Config.MAX_SEARCH_SESSIONS:
            self._sessions.popitem(last=False)

    def get(self, key: str) -> Optional[SearchSession]:
        """读取未过期的会话。"""
        session = self._sessions.get(key)
        if session is None:
            return None
        if time.time() - session.updated_at > self.ttl:
            self._sessions.pop(key, None)
            return None
        return session

    def require(self, key: str) -> SearchSession:
        """读取必须存在的列表，分别说明没有列表和已经过期。"""
        if key not in self._sessions:
            raise CollectorError("当前聊天中没有本人的有效列表，请先搜索或查看排行榜。")
        session = self.get(key)
        if session is None:
            raise CollectorError("最近的列表已过期，请重新搜索或查看排行榜。")
        return session
