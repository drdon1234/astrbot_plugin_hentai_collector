"""自动推送：解析配置中的推送任务（排行榜 / 条件作品 / 随机作品），按计划推送到群聊或私聊。"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Mapping, Optional

from ..logger import logger

from ..config_manager import as_bool, as_int, as_str, as_str_list, source_key_from_name
from ..errors import CollectorError
from ..message_adapter import Delivery, WorkPresenter
from ..service import WorkService, parse_period, parse_sort
from ..storage import PushHistory
from ..types import PERIOD_LABELS, Period, SearchQuery, SortKey, WorkInfo
from .schedule import Scheduler, next_daily, next_interval, parse_clock


KIND_RANKING = "ranking"
KIND_SEARCH = "search"
KIND_RANDOM = "random"
KIND_LABELS = {KIND_RANKING: "排行榜推送", KIND_SEARCH: "条件作品推送", KIND_RANDOM: "随机作品推送"}
MAX_PUSH_COUNT = 20
DETAIL_CONCURRENCY = 4
GROUP_MESSAGE = "GroupMessage"
FRIEND_MESSAGE = "FriendMessage"


@dataclass
class PushTask:
    """一条推送任务。"""

    task_id: str
    name: str
    kind: str
    source: str = ""
    period: Period = Period.DAY
    query: SearchQuery = field(default_factory=SearchQuery)
    count: int = 5
    only_new: bool = True
    with_files: bool = False
    times: list[time] = field(default_factory=list)
    interval_minutes: int = 0
    groups: list[str] = field(default_factory=list)
    users: list[str] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)

    def schedule_text(self) -> str:
        """推送计划的文字说明。"""
        if self.interval_minutes > 0:
            return f"每 {self.interval_minutes} 分钟"
        return "每天 " + "、".join(clock.strftime("%H:%M") for clock in self.times)

    def next_after(self, after: datetime) -> datetime:
        """下一次触发时刻。"""
        if self.interval_minutes > 0:
            return next_interval(after, self.interval_minutes)
        return next_daily(after, self.times)

    @classmethod
    def parse(cls, entry: Mapping[str, Any], index: int) -> Optional["PushTask"]:
        """解析 template_list 中的一条任务；停用、模板未知或没有推送时间时返回 None。"""
        kind = as_str(entry.get("__template_key"))
        if kind not in KIND_LABELS or not as_bool(entry.get("enabled"), True):
            return None
        name = as_str(entry.get("name")) or f"{KIND_LABELS[kind]}{index + 1}"
        times = sorted({clock for clock in (parse_clock(t) for t in as_str_list(entry.get("times"))) if clock})
        interval = as_int(entry.get("interval_minutes"), 0, 0, 7 * 24 * 60)
        if not times and interval <= 0:
            logger.warning(f"推送任务「{name}」没有有效的推送时间，已忽略")
            return None
        source_text = as_str(entry.get("source"))
        source = "" if source_text in ("全部", "") and kind == KIND_RANDOM else source_key_from_name(source_text, "ehentai")
        query = SearchQuery(
            keyword=as_str(entry.get("keyword")),
            category=as_str(entry.get("category")),
            sort=parse_sort(as_str(entry.get("sort"))) or SortKey.DEFAULT,
            min_rating=as_int(entry.get("min_rating"), 0, 0, 5),
            min_pages=as_int(entry.get("min_pages"), 0, 0, 10000),
        )
        return cls(
            task_id=f"{kind}:{name}",
            name=name,
            kind=kind,
            source=source,
            period=parse_period(as_str(entry.get("period"))) or Period.DAY,
            query=query,
            count=as_int(entry.get("count"), 1 if kind == KIND_RANDOM else 5, 1, MAX_PUSH_COUNT),
            only_new=as_bool(entry.get("only_new"), kind != KIND_RANDOM),
            with_files=as_bool(entry.get("with_files"), False),
            times=times,
            interval_minutes=interval,
            groups=as_str_list(entry.get("groups")),
            users=as_str_list(entry.get("users")),
            sessions=as_str_list(entry.get("sessions")),
        )


class PushManager:
    """持有全部推送任务及其调度器。"""

    def __init__(
        self,
        context: Any,
        service: WorkService,
        presenter: WorkPresenter,
        history: PushHistory,
        *,
        enabled: bool,
        platform_id: str,
        raw_tasks: list[Mapping[str, Any]],
        use_forward: bool,
    ):
        self.context = context
        self.service = service
        self.presenter = presenter
        self.history = history
        self.enabled = enabled
        self.platform_id = platform_id
        self.use_forward = use_forward
        self.tasks = [task for task in (PushTask.parse(e, i) for i, e in enumerate(raw_tasks)) if task]
        self._schedulers: list[Scheduler] = []

    # ── 生命周期 ──────────────────────────────────────

    def start(self) -> None:
        """启动所有任务的调度循环。"""
        if not self.enabled:
            return
        for task in self.tasks:

            async def job(_: datetime, task: PushTask = task) -> None:
                await self.run_task(task)

            scheduler = Scheduler(task.name, task.next_after, job)
            scheduler.start()
            self._schedulers.append(scheduler)
        if self._schedulers:
            logger.info(f"自动推送已启动：{len(self._schedulers)} 个任务")

    async def stop(self) -> None:
        """停止全部调度循环。"""
        for scheduler in self._schedulers:
            await scheduler.stop()
        self._schedulers.clear()

    # ── 目标解析 ──────────────────────────────────────

    def _resolve_platform_id(self) -> str:
        if self.platform_id:
            return self.platform_id
        insts = list(getattr(self.context.platform_manager, "platform_insts", []))
        for inst in insts:
            if inst.meta().name == "aiocqhttp":
                return inst.meta().id
        for inst in insts:
            if inst.meta().name != "webchat":
                return inst.meta().id
        return ""

    def targets(self, task: PushTask) -> list[str]:
        """把群号、用户 ID 与会话 ID 转换为会话 ID 列表（去重保序）。"""
        sessions = list(task.sessions)
        if task.groups or task.users:
            platform_id = self._resolve_platform_id()
            if not platform_id:
                logger.warning(f"推送任务「{task.name}」：没有可用的消息平台，群号 / 用户 ID 无法推送")
            else:
                sessions += [f"{platform_id}:{GROUP_MESSAGE}:{gid}" for gid in task.groups]
                sessions += [f"{platform_id}:{FRIEND_MESSAGE}:{uid}" for uid in task.users]
        return list(dict.fromkeys(sessions))

    # ── 选取作品 ──────────────────────────────────────

    def _fresh(self, task: PushTask, works: list[WorkInfo]) -> list[WorkInfo]:
        if not task.only_new:
            return works
        return [work for work in works if not self.history.seen(task.task_id, work.ref)]

    async def _select(self, task: PushTask) -> list[WorkInfo]:
        if task.kind == KIND_RANKING:
            page = await self.service.ranking(task.source, task.period, 1)
            works = self._fresh(task, page.items)[: task.count]
        elif task.kind == KIND_SEARCH:
            page = await self.service.search(task.source, task.query.with_page(1))
            works = self._fresh(task, page.items)[: task.count]
        else:
            works, seen = [], set()
            for _ in range(task.count * 3):
                if len(works) >= task.count:
                    break
                try:
                    work = await self.service.random_work(task.source or None, task.query)
                except CollectorError as exc:
                    logger.warning(f"推送任务「{task.name}」随机失败：{exc}")
                    continue
                if work.ref in seen or not self._fresh(task, [work]):
                    continue
                seen.add(work.ref)
                works.append(work)
        semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def detail(work: WorkInfo) -> WorkInfo:
            async with semaphore:
                return await self.service.ensure_detail(work)

        return list(await asyncio.gather(*(detail(work) for work in works)))

    def _header(self, task: PushTask, works: list[WorkInfo]) -> str:
        if task.kind == KIND_RANKING:
            source = self.service.sources.get(task.source)
            title = f"{source.name if source else task.source} {PERIOD_LABELS[task.period]}"
        elif task.kind == KIND_SEARCH:
            title = f"条件作品：{task.query.describe()}"
        else:
            title = "随机作品"
        return f"【定时推送】{task.name}\n{title}，共 {len(works)} 部"

    # ── 执行 ──────────────────────────────────────────

    async def run_task(self, task: PushTask) -> str:
        """执行一次推送任务。

        Returns:
            执行结果说明（供管理员手动触发时回显）
        """
        targets = self.targets(task)
        if not targets:
            return f"「{task.name}」没有推送目标"
        try:
            works = await self._select(task)
        except CollectorError as exc:
            logger.warning(f"推送任务「{task.name}」获取作品失败：{exc}")
            return f"「{task.name}」获取作品失败：{exc}"
        if not works:
            logger.info(f"推送任务「{task.name}」没有新作品，跳过本次推送")
            return f"「{task.name}」没有新作品"

        delivered = 0
        for umo in targets:
            delivery = Delivery.for_session(self.context, umo, self.use_forward)
            try:
                await self.presenter.send_cards(delivery, works, self._header(task, works))
                if task.with_files:
                    for work in works:
                        try:
                            await self.presenter.send_fetch(delivery, work, quiet=True)
                        except CollectorError as exc:
                            logger.warning(f"推送《{work.title}》文件失败：{exc}")
                delivered += 1
            except Exception as exc:  # 单个目标失败（平台拒发、会话失效）不影响其他目标
                logger.warning(f"推送任务「{task.name}」发送到 {umo} 失败：{exc!r}")
        if delivered:
            self.history.add(task.task_id, [work.ref for work in works])
        return f"「{task.name}」已推送 {len(works)} 部作品到 {delivered}/{len(targets)} 个会话"

    def describe(self) -> str:
        """列出全部推送任务。"""
        if not self.tasks:
            return "没有启用的推送任务（在插件配置“自动推送”中添加）"
        lines = [f"自动推送：{'已开启' if self.enabled else '未开启（仅可手动执行）'}"]
        for index, task in enumerate(self.tasks, 1):
            source = self.service.sources.get(task.source)
            source_name = source.name if source else ("全部源站" if not task.source else f"{task.source}（未启用）")
            lines.append(
                f"[{index}] {task.name} ｜ {KIND_LABELS[task.kind]} ｜ {source_name} ｜ "
                f"{task.schedule_text()} ｜ {len(self.targets(task))} 个会话"
            )
        return "\n".join(lines)
