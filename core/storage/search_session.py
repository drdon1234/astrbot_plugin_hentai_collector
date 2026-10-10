"""用户搜索会话：缓存最近一次列表结果（单站或聚合），支持按全局序号取作品与翻页。"""

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from ..constants import Config
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

    def pick(self, key: str, index: int) -> Optional[WorkInfo]:
        """按 1 起始的序号取作品。"""
        session = self.get(key)
        if session is None or not 1 <= index <= len(session.items):
            return None
        return session.items[index - 1]
