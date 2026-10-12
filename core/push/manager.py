"""自动推送：解析配置中的推送任务（排行榜 / 条件作品 / 随机作品），按计划推送到群聊或私聊。"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Mapping, Optional

from ..logger import logger

from ..config_manager import as_bool, as_int, as_str, as_str_list, source_key_from_name
from ..errors import CollectorError, SourceError
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
            return f"每 {self.interval_minutes} 分钟（每天零点重新计算）"
        return "每天 " + "、".join(clock.strftime("%H:%M") for clock in self.times)

    def next_after(self, after: datetime) -> datetime:
        """下一次触发时刻。"""
        if self.interval_minutes > 0:
            return next_interval(after, self.interval_minutes)
        return next_daily(after, self.times)

    @classmethod
    def parse(cls, entry: Mapping[str, Any], index: int) -> Optional["PushTask"]:
        """解析 template_list 中的一条任务；停用、模板未知或没有推送时间时返回 None。"""
        reason = cls._ignored_reason(entry)
        if reason:
            if as_bool(entry.get("enabled"), True):
                logger.warning(f"推送配置第 {index + 1} 项已忽略：{reason}")
            return None
        kind = as_str(entry.get("__template_key"))
        name = as_str(entry.get("name")) or f"{KIND_LABELS[kind]}{index + 1}"
        times = sorted({clock for clock in (parse_clock(t) for t in as_str_list(entry.get("times"))) if clock})
        interval = as_int(entry.get("interval_minutes"), 0, 0)
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

    @staticmethod
    def _ignored_reason(entry: Mapping[str, Any]) -> str:
        """返回任务无法加入执行列表的原因，供解析和管理说明共用。"""
        if not as_bool(entry.get("enabled"), True):
            return "已停用"
        kind = as_str(entry.get("__template_key"))
        if kind not in KIND_LABELS:
            return f"未知任务类型：{kind or '未填写'}"
        times = as_str_list(entry.get("times"))
        interval = as_int(entry.get("interval_minutes"), 0, 0)
        if interval > 1440:
            return "日内推送间隔不能超过 1440 分钟；每天零点重新计算"
        if not any(parse_clock(value) for value in times) and interval <= 0:
            return "没有有效的推送时间，请填写有效时刻或正数间隔"
        return ""


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
        self.tasks: list[PushTask] = []
        self._ignored_tasks: list[str] = []
        task_indices: dict[str, int] = {}
        for index, entry in enumerate(raw_tasks):
            task = PushTask.parse(entry, index)
            if task is not None:
                if task.task_id in task_indices:
                    reason = f"同类型任务名称重复，与配置第 {task_indices[task.task_id] + 1} 项冲突，请修改名称"
                    logger.warning(f"推送配置第 {index + 1} 项已忽略：{reason}")
                    self._ignored_tasks.append(f"配置第 {index + 1} 项「{task.name}」：{reason}")
                else:
                    self.tasks.append(task)
                    task_indices[task.task_id] = index
            else:
                name = as_str(entry.get("name")) or "未命名任务"
                self._ignored_tasks.append(
                    f"配置第 {index + 1} 项「{name}」：{PushTask._ignored_reason(entry)}"
                )
        self._schedulers: list[Scheduler] = []
        self._task_locks: dict[str, asyncio.Lock] = {}
        self.history.migrate()

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

    def _fresh(self, task: PushTask, works: list[WorkInfo], targets: list[str]) -> list[WorkInfo]:
        if not task.only_new:
            return works
        return [work for work in works if any(not self.history.seen(task.task_id, target, work.ref) for target in targets)]

    async def _select(self, task: PushTask, targets: list[str]) -> tuple[dict[str, list[WorkInfo]], str]:
        """按目标独立选取作品与数量预算，合并相同作品只用于补充详情。"""
        note = ""
        selected = {target: [] for target in targets}
        if task.kind in (KIND_RANKING, KIND_SEARCH):
            if task.kind == KIND_RANKING:
                page = await self.service.ranking(task.source, task.period, 1)
            else:
                page = await self.service.search(task.source, task.query.with_page(1))
            if not page.items:
                return {}, "没有符合条件的作品"
            candidates = list({work.ref: work for work in page.items}.values())
            for target in targets:
                selected[target] = self._fresh(task, candidates, [target])[:task.count]
            filtered = len(candidates) - len(self._fresh(task, candidates, targets))
            note = f"源站本页返回 {len(page.items)} 部"
            if filtered:
                note += f"，已过滤 {filtered} 部已推送作品"
        else:
            seen = set()
            failures, duplicates, successes = 0, 0, 0
            last_error = ""
            for _ in range(task.count * 3 * len(targets)):
                if all(len(works) >= task.count for works in selected.values()):
                    break
                try:
                    work = await self.service.random_work(task.source or None, task.query)
                except CollectorError as exc:
                    logger.warning(f"推送任务「{task.name}」随机失败：{exc}")
                    failures += 1
                    last_error = str(exc)
                    continue
                successes += 1
                eligible = [target for target in targets
                            if len(selected[target]) < task.count and self._fresh(task, [work], [target])]
                if work.ref in seen or not eligible:
                    duplicates += 1
                    continue
                seen.add(work.ref)
                for target in eligible:
                    selected[target].append(work)
            if not successes and failures:
                raise SourceError(f"{failures} 次请求失败：{last_error}")
            reasons = []
            if failures:
                reasons.append(f"{failures} 次请求失败（{last_error}）")
            if duplicates:
                reasons.append(f"跳过 {duplicates} 次重复或已推送作品")
            note = "；".join(reasons)
        works = {work.ref: work for items in selected.values() for work in items}
        if not works:
            return {}, "没有新作品" + (f"；{note}" if note else "")
        if all(len(items) >= task.count for items in selected.values()):
            note = ""
        semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def detail(work: WorkInfo) -> WorkInfo:
            async with semaphore:
                return await self.service.ensure_detail(work)

        details = await asyncio.gather(*(detail(work) for work in works.values()))
        resolved = dict(zip(works, details))
        return {target: [resolved[work.ref] for work in items] for target, items in selected.items()}, note

    def _header(self, task: PushTask, works: list[WorkInfo], note: str = "") -> str:
        if task.kind == KIND_RANKING:
            source = self.service.sources.get(task.source)
            title = f"{source.name if source else task.source} {PERIOD_LABELS[task.period]}"
        elif task.kind == KIND_SEARCH:
            title = f"条件作品：{task.query.describe()}"
        else:
            title = "随机作品"
        header = f"【定时推送】{task.name}\n{title}，共 {len(works)} 部"
        return header + (f"\n{note}" if note else "")

    # ── 执行 ──────────────────────────────────────────

    async def run_task(self, task: PushTask) -> str:
        """执行一次推送任务。

        Returns:
            执行结果说明（供管理员手动触发时回显）
        """
        lock = self._task_locks.setdefault(task.task_id, asyncio.Lock())
        async with lock:
            return await self._run_task(task)

    async def _run_task(self, task: PushTask) -> str:
        """在任务锁内完成选取、各目标投递和历史更新。"""
        targets = self.targets(task)
        if not targets:
            return f"「{task.name}」没有推送目标"
        try:
            selected, note = await self._select(task, targets)
        except CollectorError as exc:
            logger.warning(f"推送任务「{task.name}」获取作品失败：{exc}")
            return f"「{task.name}」获取作品失败：{exc}"
        if not selected:
            logger.info(f"推送任务「{task.name}」{note}，跳过本次推送")
            return f"「{task.name}」{note}"

        card_successes = file_successes = file_failures = files_not_sent = 0
        sent_refs = set()
        active_targets = 0
        for umo in targets:
            target_works = selected[umo]
            if not target_works:
                continue
            active_targets += 1
            delivery = Delivery.for_session(self.context, umo, self.use_forward)
            target_note = ""
            if len(target_works) < task.count:
                target_note = f"本次获得 {len(target_works)}/{task.count} 部" + (f"：{note}" if note else "")
            try:
                await self.presenter.send_cards(delivery, target_works, self._header(task, target_works, target_note))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 单个目标失败（平台拒发、会话失效）不影响其他目标
                logger.warning(f"推送任务「{task.name}」发送到 {umo} 失败：{exc!r}")
                if task.with_files:
                    files_not_sent += len(target_works)
                continue
            card_successes += 1
            sent_refs.update(work.ref for work in target_works)
            failures = []
            if task.with_files:
                for work in target_works:
                    try:
                        result = await self.presenter.send_fetch(delivery, work, quiet=True)
                    except asyncio.CancelledError:
                        raise
                    except CollectorError as exc:
                        logger.warning(f"推送《{work.title}》文件到 {umo} 失败：{exc}")
                        reason = str(exc)
                    except Exception as exc:  # 单部作品的平台发送失败不应阻断其他作品与目标
                        logger.exception(f"推送《{work.title}》文件到 {umo} 失败：{exc!r}")
                        reason = "文件处理或发送失败，请联系管理员检查日志"
                    else:
                        if result.missing > 0:
                            reason = f"已发送的文件缺少 {result.missing} 页，本次未记为已推送；再次获取会重新下载"
                            logger.warning(f"推送《{work.title}》文件到 {umo} 不完整：{reason}")
                        else:
                            file_successes += 1
                            self.history.add(task.task_id, umo, [work.ref])
                            continue
                    file_failures += 1
                    failures.append(f"《{work.title}》：{reason}\n{self.presenter.retry_hint(work)}")
            else:
                self.history.add(task.task_id, umo, [work.ref for work in target_works])
            if failures:
                await delivery.safe_text("本次推送有文件未完成：\n" + "\n".join(failures))
        summary = f"「{task.name}」已推送 {len(sent_refs)} 部作品卡片；卡片成功 {card_successes}/{active_targets} 个会话"
        if task.with_files:
            summary += f"；文件成功 {file_successes} 次，文件失败 {file_failures} 次"
            if files_not_sent:
                summary += f"，因卡片失败未发送 {files_not_sent} 次"
        if note:
            summary += f"\n{note}"
        return summary

    def describe(self) -> str:
        """列出全部推送任务。"""
        lines = [f"自动推送：{'已开启' if self.enabled else '未开启（仅可手动执行）'}"]
        if not self.tasks:
            lines.append("没有可执行的推送任务（请在插件配置“自动推送”中添加或修正）")
        for index, task in enumerate(self.tasks, 1):
            source = self.service.sources.get(task.source)
            source_name = source.name if source else ("全部源站" if not task.source else f"{task.source}（未启用）")
            lines.append(
                f"[{index}] {task.name} ｜ {KIND_LABELS[task.kind]} ｜ {source_name} ｜ "
                f"{task.schedule_text()} ｜ {len(self.targets(task))} 个会话"
            )
        if self._ignored_tasks:
            lines.append("以下配置未加入执行列表，不占用上方任务编号：")
            lines.extend(f"· {reason}" for reason in self._ignored_tasks)
        return "\n".join(lines)
