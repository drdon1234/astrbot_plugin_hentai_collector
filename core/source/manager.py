"""源站注册表：别名解析、消息链接路由、作品引用解析、聚合候选与随机源选择。"""

import random
from dataclasses import replace
from typing import Iterable, Optional

from ..errors import SourceUnavailable, UnsupportedOperation
from ..types import PERIOD_LABELS, SORT_LABELS, MediaType, Period, SearchQuery, SortKey, WorkRef
from .platform import BaseSource


class SourceManager:
    """按固定顺序持有已启用的源站实例。"""

    def __init__(self, sources: Iterable[BaseSource], random_pool: Iterable[str] = ()):
        self._sources: dict[str, BaseSource] = {source.key: source for source in sources}
        self._aliases: dict[str, str] = {}
        for source in self._sources.values():
            for alias in (source.key, source.name, source.short, *source.aliases):
                if alias:
                    self._aliases.setdefault(alias.lower(), source.key)
        self.random_pool = [key for key in random_pool if key in self._sources]

    # ── 查询 ──────────────────────────────────────────

    def __iter__(self):
        return iter(self._sources.values())

    def __len__(self) -> int:
        return len(self._sources)

    def keys(self) -> list[str]:
        """已启用源站键，按注册顺序。"""
        return list(self._sources)

    def get(self, key: str) -> Optional[BaseSource]:
        """按键获取源站。"""
        return self._sources.get(key)

    def resolve_alias(self, word: str) -> Optional[str]:
        """把用户输入的源站名 / 别名解析为源站键。"""
        return self._aliases.get(str(word or "").strip().lower())

    def require(self, key: str) -> BaseSource:
        """获取可用源站，未启用或不可用时抛出说明原因的异常。

        Raises:
            SourceUnavailable: 源站未启用或缺少必要配置
        """
        source = self._sources.get(key)
        if source is None:
            raise SourceUnavailable(f"源站 {key} 未启用")
        reason = source.unavailable_reason
        if reason:
            raise SourceUnavailable(f"{source.name} 暂不可用：{reason}")
        return source

    # ── 链接与引用 ────────────────────────────────────

    def extract_refs(self, text: str) -> list[WorkRef]:
        """从消息文本中提取所有源站的作品引用（按源站顺序，再按出现顺序）。"""
        refs: list[WorkRef] = []
        for source in self._sources.values():
            refs.extend(WorkRef(source.key, work_id) for work_id in source.extract_ids(text))
        return list(dict.fromkeys(refs))

    def resolve_reference(self, tokens: list[str], source_key: str = "") -> Optional[WorkRef]:
        """解析用户显式给出的作品引用。

        任意源站的链接与 ``JM123456`` 总是优先识别；未识别时：
        指定 ``source_key`` 则把参数当作该源站的作品 ID，否则支持 ``<源站别名> <ID>``。

        Args:
            tokens: 指令参数
            source_key: 站点专属指令对应的源站键

        Returns:
            作品引用，无法识别时返回 None
        """
        text = " ".join(tokens).strip()
        if not text:
            return None
        refs = self.extract_refs(text)
        if refs:
            return refs[0]
        # 显式 JM 编号不受“消息自动解析 JM 编号”开关影响；纯数字仍需要站点或列表序号。
        jm_source = self._sources.get("jmcomic")
        if jm_source is not None and text.lower().startswith("jm"):
            work_id = jm_source.normalize_id(text)
            if work_id:
                return WorkRef(jm_source.key, work_id)
        if source_key:
            source = self._sources.get(source_key)
            work_id = source.normalize_id(text) if source is not None else None
            return WorkRef(source_key, work_id) if work_id else None
        if len(tokens) >= 2:
            key = self.resolve_alias(tokens[0])
            source = self._sources.get(key) if key else None
            if source is not None:
                work_id = source.normalize_id(" ".join(tokens[1:]))
                if work_id:
                    return WorkRef(source.key, work_id)
        return None

    # ── 能力筛选 ──────────────────────────────────────

    @staticmethod
    def normalize_query(source: BaseSource, query: SearchQuery) -> tuple[SearchQuery, list[str]]:
        """根据源站能力生成实际查询和忽略说明，不修改用户原始条件。

        Args:
            source: 目标源站
            query: 用户输入的查询

        Returns:
            实际生效查询与面向用户的简短说明
        """
        effective = replace(query, exclude=list(query.exclude))
        notes: list[str] = []
        if query.sort != SortKey.DEFAULT and query.sort not in source.sort_options:
            effective.sort = SortKey.DEFAULT
            notes.append(f"不支持排序「{SORT_LABELS[query.sort]}」，已使用默认排序")
        if query.min_rating and not source.supports_min_rating:
            effective.min_rating = 0
            notes.append(f"不支持最低评分，已忽略评分≥{query.min_rating}")
        if query.min_pages and not source.supports_min_pages:
            effective.min_pages = 0
            notes.append(f"不支持最少页数，已忽略页数≥{query.min_pages}")
        category = source.normalize_category(query.category)
        if category is None:
            category = source.normalize_category("") or ""
            fallback = f"已使用默认分类「{category}」" if category else "已忽略"
            notes.append(f"不支持分类「{query.category}」，{fallback}")
        effective.category = category
        source.validate_query(effective)
        return effective, notes

    def unavailable(self, media_type: Optional[MediaType] = None, action: str = "search") -> list[str]:
        """列出已启用源站不能参加指定操作的原因。"""
        reasons: list[str] = []
        for source in self._sources.values():
            if media_type is not None and source.media_type != media_type:
                continue
            reason = source.unavailable_reason
            if not reason:
                if action == "search" and not source.supports_search:
                    reason = "不支持搜索"
                elif action == "ranking" and not source.ranking_periods:
                    reason = "没有排行榜"
                elif action == "random" and not source.supports_random:
                    reason = "不支持随机"
            if reason:
                reasons.append(f"{source.name}：{reason}")
        return reasons

    def _usable(self, media_type: Optional[MediaType]) -> list[BaseSource]:
        return [
            source
            for source in self._sources.values()
            if not source.unavailable_reason and (media_type is None or source.media_type == media_type)
        ]

    def searchable(self, media_type: Optional[MediaType] = None) -> list[BaseSource]:
        """当前可用且支持搜索的源站（聚合搜索的参与者）。"""
        return [s for s in self._usable(media_type) if s.supports_search]

    def with_ranking(self, media_type: Optional[MediaType] = None) -> list[BaseSource]:
        """当前可用且有排行榜的源站（聚合排行的参与者）。"""
        return [s for s in self._usable(media_type) if s.ranking_periods]

    def random_candidates(self, media_type: Optional[MediaType] = None) -> list[BaseSource]:
        """全随机候选源站：候选池中可用的源站；候选池没有该类型源站时使用全部可用源站。"""
        usable = [s for s in self._usable(media_type) if s.supports_random]
        pooled = [s for s in usable if s.key in self.random_pool]
        return pooled or usable

    def pick_random_source(self, media_type: Optional[MediaType] = None) -> BaseSource:
        """从全随机候选源站中挑一个。

        Raises:
            SourceUnavailable: 没有可用的候选源站
        """
        pool = self.random_candidates(media_type)
        if not pool:
            raise SourceUnavailable("没有可用于随机的源站，请检查“源站开关”与“随机设置”")
        return random.choice(pool)

    @staticmethod
    def check_period(source: BaseSource, period: Period) -> None:
        """校验源站是否支持该排行榜周期。

        Raises:
            UnsupportedOperation: 不支持时附带可用周期说明
        """
        if not source.ranking_periods:
            raise UnsupportedOperation(f"{source.name} 没有排行榜")
        if period not in source.ranking_periods:
            options = "、".join(PERIOD_LABELS[p] for p in source.ranking_periods)
            raise UnsupportedOperation(f"{source.name} 不支持{PERIOD_LABELS[period]}，可选：{options}")

    async def close(self) -> None:
        """关闭所有源站的网络资源。"""
        for source in self._sources.values():
            await source.close()
