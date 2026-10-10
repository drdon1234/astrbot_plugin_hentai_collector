"""源站注册表：别名解析、消息链接路由、作品引用解析与随机源选择。"""

import random
from typing import Iterable, Optional

from ..errors import SourceUnavailable, UnsupportedOperation
from ..types import PERIOD_LABELS, MediaType, Period, WorkRef
from .platform import BaseSource


ALL_SOURCE_WORDS = frozenset({"全部", "所有", "任意", "all", "any", "*"})


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

    @staticmethod
    def is_all_word(word: str) -> bool:
        """是否表示“全部源站”。"""
        return str(word or "").strip().lower() in ALL_SOURCE_WORDS

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

    def resolve_reference(self, tokens: list[str], default_source: str = "") -> Optional[WorkRef]:
        """解析用户显式给出的作品引用。

        支持 ``<链接>``、``JM123456``、``<源站> <ID>`` 以及仅 ``<ID>``（使用默认源站）。

        Args:
            tokens: 指令参数
            default_source: 只给出 ID 时使用的源站键

        Returns:
            作品引用，无法识别时返回 None
        """
        text = " ".join(tokens).strip()
        if not text:
            return None
        refs = self.extract_refs(text)
        if refs:
            return refs[0]
        if len(tokens) >= 2:
            key = self.resolve_alias(tokens[0])
            source = self._sources.get(key) if key else None
            if source is not None:
                work_id = source.normalize_id(" ".join(tokens[1:]))
                if work_id:
                    return WorkRef(source.key, work_id)
        source = self._sources.get(default_source)
        if source is not None and len(tokens) == 1:
            work_id = source.normalize_id(tokens[0])
            if work_id:
                return WorkRef(source.key, work_id)
        return None

    # ── 能力筛选 ──────────────────────────────────────

    def with_ranking(self) -> list[BaseSource]:
        """支持排行榜的源站。"""
        return [s for s in self._sources.values() if s.ranking_periods]

    def pick_random_source(self, media_type: Optional[MediaType] = None) -> BaseSource:
        """从全随机候选池中挑一个当前可用的源站。

        Raises:
            SourceUnavailable: 没有可用的候选源站
        """
        pool = [
            self._sources[key]
            for key in (self.random_pool or self.keys())
            if self._sources[key].supports_random
            and not self._sources[key].unavailable_reason
            and (media_type is None or self._sources[key].media_type == media_type)
        ]
        if not pool:
            raise SourceUnavailable("没有可用于全随机的源站，请检查“随机设置”中的候选源站")
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
