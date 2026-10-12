"""AstrBot 本子与里番聚合插件入口：注册站点专属指令、聚合指令与自动解析，把请求委托给业务层与展示层。"""

import asyncio
from collections import OrderedDict
from dataclasses import replace
from typing import Awaitable, Callable, Optional

from .core.logger import logger

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.event_message_type import EventMessageType

from .core.commands import COMMAND_SPECS
from .core.config_manager import SOURCE_DISPLAY_NAMES, ConfigManager, resolve_plugin_data_dir
from .core.constants import Config
from .core.downloader import ImageDownloader, VideoDownloader
from .core.errors import CollectorError, SourceUnavailable
from .core.message_adapter import Delivery, PresenterOptions, WorkPresenter
from .core.message_adapter.command_binding import CommandBindings
from .core.packager import Packager
from .core.push import PushManager
from .core.service import MAX_RANDOM_COUNT, RandomResult, SiteResult, WorkService, default_period, parse_args, split_command
from .core.storage import MODE_RANKING, MODE_SEARCH, JobStore, PushHistory, SearchSession, SearchSessionStore
from .core.types import PERIOD_LABELS, MediaType, WorkInfo, WorkRef


# ── 指令语义 ────────────────────────────────────────────

ACTION_SEARCH = "search"
ACTION_RANKING = "ranking"
ACTION_RANDOM = "random"
ACTION_GET = "get"
MEDIA_WORDS = {MediaType.COMIC: "本子", MediaType.VIDEO: "里番"}

MAX_LINKS_PER_MESSAGE = 3
MAX_SESSION_INDEX_DIGITS = 3


def _command_filter(key: str):
    """从唯一指令目录注册原生过滤器，运行时绑定用户配置。"""
    words = COMMAND_SPECS[key].default_words
    return filter.command(words[0], alias=set(words[1:]))


@register(
    Config.PLUGIN_NAME,
    "drdon1234",
    "本子与里番聚合：E-Hentai / nhentai / JMComic / 哔咔 / Hanime1 的解析、搜索、排行、随机与自动推送",
    Config.PLUGIN_VERSION,
)
class HentaiCollectorPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        data_dir = resolve_plugin_data_dir()
        self.cfg = ConfigManager(config, data_dir)
        cfg = self.cfg
        self.commands = CommandBindings(cfg.commands, __name__)

        self.sources = cfg.create_source_manager()
        self.job_store = JobStore(cfg.cache.cache_dir)
        self.service = WorkService(
            self.sources,
            self.job_store,
            Packager(cfg.output.package),
            ImageDownloader(cfg.network.image_concurrency, cfg.network.retries),
            VideoDownloader(cfg.hanime1.max_size_mb),
            max_pages=cfg.output.max_pages,
            max_concurrent_jobs=cfg.limits.max_concurrent_jobs,
            retention_seconds=cfg.cache.retention_seconds,
            cache_available=cfg.cache.cache_dir_available,
            cache_error=cfg.cache.cache_dir_error,
        )
        self.presenter = WorkPresenter(
            self.service,
            self.sources,
            PresenterOptions(
                show_cover=cfg.display.show_cover,
                cover_blur=cfg.display.cover_blur,
                search_cover_grid=cfg.display.search_cover_grid,
                max_tags=cfg.display.max_tags,
                show_description=cfg.display.show_description,
                video_as_file=cfg.hanime1.send_mode == "文件",
                send_progress=cfg.basic.send_progress,
                nested_forward=cfg.display.nested_forward,
                command=self.commands.preferred,
                session_minutes=cfg.limits.session_minutes,
            ),
        )
        self.sessions = SearchSessionStore(cfg.limits.session_minutes * 60)
        self.push_manager = PushManager(
            context,
            self.service,
            self.presenter,
            PushHistory(data_dir / "push_history.json"),
            enabled=cfg.push.enable,
            platform_id=cfg.push.platform_id,
            raw_tasks=cfg.push.tasks,
            use_forward=cfg.display.use_forward,
        )
        self._claimed: OrderedDict[str, None] = OrderedDict()
        self._cooldowns: dict[str, float] = {}
        self._cleanup_task: Optional[asyncio.Task] = None

    async def initialize(self) -> None:
        """插件加载：清理过期缓存、启动周期清理与自动推送。"""
        await self.commands.initialize()
        if not self.cfg.cache.cache_dir_available:
            logger.warning(f"缓存目录不可用，文件下载功能将无法使用：{self.cfg.cache.cache_dir_error}")
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self.push_manager.start()

    async def terminate(self) -> None:
        """插件卸载：停止推送与清理循环，关闭网络会话。"""
        await self.push_manager.stop()
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
        await self.sources.close()
        self.commands.close()

    # ── 内部辅助 ────────────────────────────────────────

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                cleaned = await asyncio.to_thread(self.job_store.cleanup_expired)
                if cleaned:
                    logger.info(f"已清理 {cleaned} 个过期任务目录")
            except OSError as exc:
                logger.warning(f"清理过期缓存失败：{exc}")
            await asyncio.sleep(Config.EXPIRED_CLEANUP_INTERVAL)

    def _allowed(self, event: AstrMessageEvent) -> bool:
        try:
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return False
        except AttributeError:
            return False
        if not self.cfg.permission.configuration_valid:
            return False
        return self._is_admin(event) or self.cfg.permission.check(
            event.is_private_chat(), event.get_sender_id(), event.get_group_id(),
        )

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        return self.cfg.permission.is_admin(event.get_sender_id()) or event.is_admin()

    def _command(self, key: str) -> str:
        """获取当前实际生效的指令名，已停用的操作返回空字符串。"""
        return self.commands.preferred(key)

    def _command_tokens(self, event: AstrMessageEvent, key: str) -> list[str]:
        """按实际触发词提取参数，支持指令管理中保存的多词名称。"""
        return split_command(event.message_str, command_words=self.commands.words(key, include_disabled=True))

    def _claim(self, event: AstrMessageEvent) -> bool:
        """同一条消息只处理一次（指令与自动解析可能同时命中）。"""
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        if not message_id:
            return True
        key = f"{event.unified_msg_origin}:{message_id}"
        if key in self._claimed:
            return False
        self._claimed[key] = None
        while len(self._claimed) > Config.MESSAGE_DEDUPE_SIZE:
            self._claimed.popitem(last=False)
        return True

    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"

    def _delivery(self, event: AstrMessageEvent) -> Delivery:
        return Delivery.for_event(event, self.cfg.display.use_forward)

    def _cooldown_left(self, event: AstrMessageEvent) -> int:
        """返回该用户获取文件的剩余冷却秒数，并在可用时开始新的冷却。"""
        seconds = self.cfg.limits.user_cooldown_seconds
        if seconds <= 0 or self._is_admin(event):
            return 0
        loop = asyncio.get_running_loop()
        key = str(event.get_sender_id())
        left = self._cooldowns.get(key, 0) - loop.time()
        if left > 0:
            return int(left) + 1
        self._cooldowns[key] = loop.time() + seconds
        return 0

    async def _guarded(
        self, event: AstrMessageEvent, action: Callable[[], Awaitable[None]], *, listing: bool = False,
    ) -> None:
        """统一的权限、去重与异常处理。"""
        if not self._allowed(event) or not self._claim(event):
            return
        event.stop_event()
        async def notice() -> None:
            await asyncio.sleep(Config.OPERATION_NOTICE_DELAY)
            await self._delivery(event).safe_text("已收到，正在处理，请稍候；无需重复发送。")

        pending_notice = asyncio.create_task(notice())
        try:
            if listing:
                async with self.sessions.hold(self._session_key(event)):
                    await action()
            else:
                await action()
        except asyncio.CancelledError:
            raise
        except CollectorError as exc:
            await self._delivery(event).safe_text(str(exc))
        except Exception as exc:  # 兜底：任何未预期异常都要告知用户并保留堆栈
            logger.exception("指令处理失败")
            await self._delivery(event).safe_text(self.presenter.describe_error(exc))
        finally:
            pending_notice.cancel()
            await asyncio.gather(pending_notice, return_exceptions=True)

    def _source_name(self, key: str) -> str:
        source = self.sources.get(key)
        return source.name if source else SOURCE_DISPLAY_NAMES.get(key, key)

    def _require_site(self, key: str):
        """站点专属指令的源站检查：未启用时提示开启方式。"""
        if self.sources.get(key) is None:
            raise SourceUnavailable(f"{self._source_name(key)} 未启用，请在插件配置“源站开关”中开启")
        return self.sources.require(key)

    def _no_source(self, media: MediaType, what: str) -> SourceUnavailable:
        reasons = [f"{source.name}：{source.unavailable_reason}" for source in self.sources
                   if source.media_type == media and source.unavailable_reason]
        detail = "；".join(reasons) if reasons else "请管理员检查“源站开关”及该站是否支持此操作"
        return SourceUnavailable(f"没有可{what}的{MEDIA_WORDS[media]}源站。{detail}")

    async def _load_work(self, target: WorkInfo | WorkRef) -> WorkInfo:
        if isinstance(target, WorkRef):
            return await self.service.get_work(target)
        return await self.service.ensure_detail(target)

    async def _fetch_batch(self, event: AstrMessageEvent, works: list[WorkInfo]) -> None:
        """同一请求只检查一次冷却，单部失败不会打断其余作品。"""
        if not works:
            return
        delivery = self._delivery(event)
        left = self._cooldown_left(event)
        if left:
            hints = "\n".join(self.presenter.retry_hint(work) for work in works)
            await delivery.text(f"获取作品冷却中，请 {left} 秒后再试。\n{hints}")
            return
        failed = []
        for work in works:
            try:
                result = await self.presenter.send_fetch(delivery, work)
                if len(works) > 1 and result is not None and result.missing:
                    failed.append(f"《{work.title}》：文件已发送，但有 {result.missing} 页缺失。\n{self.presenter.retry_hint(work)}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 每部作品是独立投递目标，失败后继续同批其他作品。
                logger.warning(f"批量获取《{work.title}》失败：{exc!r}")
                failed.append(f"《{work.title}》：{self.presenter.describe_error(exc)}\n{self.presenter.retry_hint(work)}")
        if failed:
            await delivery.safe_text(
                f"文件完整发送 {len(works) - len(failed)}/{len(works)} 部。\n" + "\n".join(failed)
            )

    # ── 指令分发 ────────────────────────────────────────

    async def _site_action(self, event: AstrMessageEvent, action: str, key: str) -> None:
        """站点专属指令：搜 / 排行 / 随机 / 看 / 详情 + 站点标识。"""
        self._require_site(key)
        if action == ACTION_SEARCH:
            await self._site_search(event, key)
        elif action == ACTION_RANKING:
            await self._site_ranking(event, key)
        elif action == ACTION_RANDOM:
            await self._site_random(event, key)
        else:
            await self._get_or_detail(event, action == ACTION_GET, key)

    async def _aggregate_action(self, event: AstrMessageEvent, action: str, media: MediaType) -> None:
        """聚合指令：搜 / 排行 / 随机 + 本子 / 里番，查询该类型的全部可用源站。"""
        if action == ACTION_SEARCH:
            await self._aggregate_search(event, media)
        elif action == ACTION_RANKING:
            await self._aggregate_ranking(event, media)
        else:
            await self._aggregate_random(event, media)

    # ── 自动解析 ────────────────────────────────────────

    @filter.event_message_type(EventMessageType.ALL)
    async def auto_parse(self, event: AstrMessageEvent):
        """识别消息中的作品链接 / JM 编号，发送作品信息（按配置继续发送文件）。"""
        if not self.cfg.basic.auto_parse:
            return
        text = str(event.message_str or "").strip()
        if not text or self.commands.is_command(text):
            return
        refs = self.sources.extract_refs(text)[:MAX_LINKS_PER_MESSAGE]
        if not refs:
            return

        async def action() -> None:
            delivery = self._delivery(event)
            works = []
            for ref in refs:
                try:
                    work = await self.service.get_work(ref)
                    works.append(work)
                    await self.presenter.send_card(delivery, work)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # 每个链接是独立目标，卡片失败不阻断其余解析和文件获取。
                    logger.warning(f"自动解析或发送 {ref} 失败：{exc!r}")
                    await delivery.safe_text(f"{ref} 解析或信息发送失败：{self.presenter.describe_error(exc)}")
            if self.cfg.basic.send_files_on_link:
                await self._fetch_batch(event, works)

        await self._guarded(event, action)

    # ── 帮助与会话 ──────────────────────────────────────

    def _help_text(self, admin: bool = False) -> str:
        """根据有效指令、权限及当前配置生成操作说明。"""
        lines = [
            '本子 / 里番聚合插件',
            '指令与参数之间留空格；群聊请按 AstrBot 唤醒设置添加前缀或 @机器人。',
            self._start_hint(),
            '【聚合查询】',
        ]
        for media in MEDIA_WORDS:
            for action in (ACTION_SEARCH, ACTION_RANKING, ACTION_RANDOM):
                key = f'{action}_{media.value}'
                command = self._command(key)
                if command:
                    spec = COMMAND_SPECS[key]
                    lines.append(f'· {command} {spec.usage}  {spec.label}')
        lines.append('【列表与作品】')
        for key in ('get', 'detail', 'page', 'session', 'help'):
            words = self.commands.words(key)
            if not words:
                continue
            spec = COMMAND_SPECS[key]
            aliases = f'（也可用：{"、".join(words[1:])}）' if len(words) > 1 else ''
            lines.append(f'· {words[0]} {spec.usage}  {spec.label}{aliases}')
        lines.extend([
            f'序号仅对应本人在当前聊天中的最近一次搜索或排行列表，{self.cfg.limits.session_minutes} 分钟后失效。',
            '随机结果、链接解析和推送卡片：复制卡片中的获取指令；这些卡片不生成列表序号。',
            '【单站指令与状态】',
        ])
        for key, name in SOURCE_DISPLAY_NAMES.items():
            source = self.sources.get(key)
            if source is None:
                lines.append(f'· {name} 未启用')
                continue
            if source.unavailable_reason:
                lines.append(f'· {name} 不可用：{source.unavailable_reason}')
                continue
            operations = []
            for action in ('search', 'ranking', 'random', 'get', 'detail'):
                if action == 'ranking' and not source.ranking_periods:
                    continue
                if action == 'search' and not source.supports_search:
                    continue
                if action == 'random' and not source.supports_random:
                    continue
                command = self._command(f'{action}_{key}')
                if command:
                    operations.append(command)
            lines.append(f'· {name}：{" / ".join(operations) or "指令已停用"}')
            if source.ranking_periods:
                lines.append('  榜单：' + '、'.join(PERIOD_LABELS[p] for p in source.ranking_periods))
        lines.extend([
            '【条件】p2 / 第2页、排序=最新|热门|观看|点赞、评分=4、页数=20、分类=xxx、-排除词。',
            '带空格的分类可写 分类="Motion Anime"。各站支持不同，不生效的条件会在结果中说明。',
            f'自动解析：{"开启" if self.cfg.basic.auto_parse else "关闭"}；JM 编号识别：{"开启" if self.cfg.basic.parse_jm_code else "关闭"}。',
            f'解析后：{self.cfg.basic.link_action}；随机后：{"自动获取文件" if self.cfg.random.auto_fetch else "仅发送信息"}；聚合随机：{self.cfg.random.mode}。',
        ])
        if admin:
            lines.append('【管理员】')
            for key in ('push', 'clean'):
                command = self._command(key)
                if command:
                    spec = COMMAND_SPECS[key]
                    lines.append(f'· {command} {spec.usage}  {spec.label}')
        return '\n'.join(lines)

    @_command_filter('help')
    async def cmd_help(self, event: AstrMessageEvent):
        """查看插件帮助。"""

        async def action() -> None:
            await self._delivery(event).text(self._help_text(self._is_admin(event)))

        await self._guarded(event, action)

    @_command_filter("session")
    async def cmd_session(self, event: AstrMessageEvent):
        """查看当前会话 ID，用于填写推送目标。"""

        async def action() -> None:
            await self._delivery(event).text(
                f"当前会话 ID：\n{event.unified_msg_origin}\n可填入推送任务的“推送会话 ID”。"
            )

        await self._guarded(event, action)

    # ── 列表展示 ────────────────────────────────────────

    def _start_hint(self) -> str:
        """提供可复制的起步操作。"""
        hints = []
        for media in MEDIA_WORDS:
            command = self._command(f"search_{media.value}")
            if command and self.sources.searchable(media):
                hints.append(f"先发送「{command} 关键词」搜索，再按列表提示选作品。")
                break
        help_command = self._command("help")
        if help_command:
            hints.append(f"发送「{help_command}」查看可用指令。")
        return "\n".join(hints) or "请联系管理员检查源站与指令是否启用。"

    def _require_session(self, event: AstrMessageEvent) -> SearchSession:
        """在用户尝试连续操作时解释列表缺失或过期。"""
        try:
            return self.sessions.require(self._session_key(event))
        except CollectorError as exc:
            raise CollectorError(f"{exc}\n{self._start_hint()}") from exc

    def _retained_hint(self, event: AstrMessageEvent) -> str:
        """说明失败后仍可操作的原列表。"""
        previous = self.sessions.get(self._session_key(event))
        if previous is None:
            return self._start_hint()
        title = previous.title or "最近的列表"
        return f"仍保留「{title}」第 {previous.page} 页，序号 1～{len(previous.items)} 仍对应该页。"

    async def _show_results(
        self, event: AstrMessageEvent, session: SearchSession, results: list[SiteResult], *, turning: bool = False,
    ) -> None:
        """展示单站或聚合列表并保存会话；聚合时序号在各站之间连续。"""
        delivery = self._delivery(event)
        single = len(session.sources) == 1 and len(results) == 1
        limit = self.cfg.display.list_size if single else self.cfg.display.aggregate_list_size
        for result in results:
            if result.page is not None:
                result.page.items = result.page.items[:limit]
        items = [work for result in results for work in result.items]
        if not items:
            errors = [result.error if single else f"{self._source_name(result.source)}：{result.error}"
                      for result in results if result.error]
            all_failed = bool(results) and all(result.error for result in results)
            if turning or all_failed:
                reason = "\n".join(errors) if errors else f"第 {session.page} 页没有作品。"
                await delivery.text(f"{reason}\n{self._retained_hint(event)}")
            else:
                reason = "没有找到符合条件的作品，已清除旧列表序号。请减少筛选条件或更换关键词。"
                await delivery.text("\n".join([reason, *errors, self._start_hint()]))
                self.sessions.discard(self._session_key(event))
            return
        session.items = items
        # 失败源站的后续页数未知，不能据其他源站的末页阻断继续查询。
        session.has_next = any(result.error or (result.page is not None and result.page.has_next) for result in results)
        if single:
            session.title = results[0].page.title or session.title
        try:
            if single:
                await self.presenter.send_list(delivery, results[0].page)
            else:
                await self.presenter.send_aggregate_list(delivery, session.title, session.page, results)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 列表投递失败时保留上一份可见结果，并给出恢复提示。
            logger.warning(f"列表发送失败，未替换会话：{exc!r}")
            raise CollectorError(f"列表发送失败，请重新发送本次指令。\n{self._retained_hint(event)}") from exc
        self.sessions.save(self._session_key(event), session)

    async def _turn_page(self, event: AstrMessageEvent) -> None:
        tokens = self._command_tokens(event, "page")
        session = self._require_session(event)
        word = tokens[0] if tokens else "下一页"
        command = self._command("page")
        usage = f"用法：{command} 正整数页码|下一页|上一页，例如「{command} 2」。"
        if len(tokens) > 1:
            raise CollectorError(usage)
        if word.isascii() and word.isdigit() and int(word) > 0:
            target = int(word)
        elif word in ("上一页", "上", "prev"):
            target = session.page - 1
        elif word in ("下一页", "下", "next"):
            target = session.page + 1
        else:
            raise CollectorError(usage)
        if target < 1:
            await self._delivery(event).text("已经是第一页")
            return
        if target > session.page and not session.has_next:
            await self._delivery(event).text(f"已经是最后一页（第 {session.page} 页）。可查看本页作品或重新搜索。")
            return
        if session.mode == MODE_RANKING:
            results = await self.service.ranking_many(session.sources, session.period, target)
        else:
            results = await self.service.search_many(session.sources, session.query.with_page(target))
        await self._show_results(event, replace(session, page=target), results, turning=True)

    @_command_filter("page")
    async def cmd_page(self, event: AstrMessageEvent):
        """翻阅最近一次搜索或排行榜。"""
        await self._guarded(event, lambda: self._turn_page(event), listing=True)

    # ── 搜索 ────────────────────────────────────────────

    async def _site_search(self, event: AstrMessageEvent, key: str) -> None:
        args = parse_args(self._command_tokens(event, f"search_{key}"))
        if args.count:
            raise CollectorError("数量仅用于随机操作；搜索请去掉数量条件，使用翻页查看更多结果。")
        source = self.sources.require(key)
        if source.search_hint and args.query.is_empty():
            await self._delivery(event).safe_text(f"未填写关键词，按站点默认条件浏览。{source.search_hint}")
        session = SearchSession(sources=[key], mode=MODE_SEARCH, query=args.query, page=args.query.page)
        await self._show_results(event, session, await self.service.search_many([key], args.query))

    async def _aggregate_search(self, event: AstrMessageEvent, media: MediaType) -> None:
        tokens = self._command_tokens(event, f"search_{media.value}")
        word = MEDIA_WORDS[media]
        if not tokens:
            command = self._command(f"search_{media.value}")
            await self._delivery(event).text(
                f"用法：{command} 关键词 [条件]  同时搜索可用的{word}源站\n"
                f"例：{command} 原神\n{self._start_hint()}"
            )
            return
        args = parse_args(tokens)
        if args.count:
            raise CollectorError("数量仅用于随机操作；搜索请去掉数量条件，使用翻页查看更多结果。")
        keys = [source.key for source in self.sources.searchable(media)]
        if not keys:
            raise self._no_source(media, "搜索")
        session = SearchSession(
            sources=keys,
            mode=MODE_SEARCH,
            title=f"{word}聚合搜索，输入条件：{args.query.describe()}（各站生效条件见分组）",
            query=args.query,
            page=args.query.page,
        )
        results = await self.service.search_many(keys, args.query)
        await self._report_unavailable(event, media, ACTION_SEARCH)
        await self._show_results(event, session, results)

    # ── 排行 ────────────────────────────────────────────

    async def _site_ranking(self, event: AstrMessageEvent, key: str) -> None:
        args = parse_args(self._command_tokens(event, f"ranking_{key}"), allow_period=True)
        source = self.sources.require(key)
        if not source.ranking_periods:
            options = "、".join(
                command for s in self.sources.with_ranking() if (command := self._command(f"ranking_{s.key}"))
            )
            await self._delivery(event).text(
                f"{source.name} 没有排行榜。" + (f"可用：{options}" if options else self._start_hint())
            )
            return
        period = args.period or default_period(source)
        self.sources.check_period(source, period)
        page = max(1, args.query.page)
        session = SearchSession(sources=[key], mode=MODE_RANKING, period=period, page=page)
        await self._show_results(event, session, await self.service.ranking_many([key], period, page))

    async def _aggregate_ranking(self, event: AstrMessageEvent, media: MediaType) -> None:
        args = parse_args(self._command_tokens(event, f"ranking_{media.value}"), allow_period=True)
        word = MEDIA_WORDS[media]
        sources = self.sources.with_ranking(media)
        if not sources:
            raise self._no_source(media, "查看排行榜")
        period = args.period
        if period is None:
            title = f"{word}聚合排行：各站默认榜单"
            keys = [source.key for source in sources]
        else:
            label = PERIOD_LABELS[period]
            keys = [source.key for source in sources if period in source.ranking_periods]
            skipped = [source.name for source in sources if period not in source.ranking_periods]
            if not keys:
                available = sorted({PERIOD_LABELS[p] for source in sources for p in source.ranking_periods})
                command = self._command(f"ranking_{media.value}")
                await self._delivery(event).text(
                    f"没有支持{label}的{word}源站，可选：{'、'.join(available)}。\n"
                    f"发送「{command}」可查看各站默认榜单。"
                )
                return
            note = f"{'、'.join(skipped)} 没有{label}" if skipped else ""
            title = f"{word}聚合排行：{label}" + (f"（{note}）" if note else "")
            if note and len(keys) == 1:  # 只剩一个站时按单站列表展示，说明单独发送
                await self._delivery(event).safe_text(note)
        page = max(1, args.query.page)
        session = SearchSession(sources=keys, mode=MODE_RANKING, title=title, period=period, page=page)
        await self._report_unavailable(event, media, ACTION_RANKING)
        await self._show_results(event, session, await self.service.ranking_many(keys, period, page))

    async def _report_unavailable(self, event: AstrMessageEvent, media: MediaType, action: str) -> None:
        """说明聚合操作跳过的已启用源站及其原因。"""
        notes = self.sources.unavailable(media, action)
        if notes:
            await self._delivery(event).safe_text("本次未查询：" + "；".join(notes))

    # ── 获取与详情 ──────────────────────────────────────

    def _resolve_target(self, event: AstrMessageEvent, tokens: list[str], key: str) -> Optional[WorkInfo | WorkRef]:
        """通用指令解析“序号 / 链接 / JM 编号”，站点指令解析“该站 ID / 链接”。"""
        token = tokens[0]
        if not key and len(tokens) == 1 and token.isdecimal() and len(token) <= MAX_SESSION_INDEX_DIGITS:
            session = self._require_session(event)
            index = int(token)
            if not 1 <= index <= len(session.items):
                raise CollectorError(f"序号超出范围：当前第 {session.page} 页可选 1～{len(session.items)}。")
            return session.items[index - 1]
        return self.sources.resolve_reference(tokens, key)

    async def _get_or_detail(self, event: AstrMessageEvent, with_files: bool, key: str = "") -> None:
        operation = "get" if with_files else "detail"
        tokens = self._command_tokens(event, f"{operation}_{key}" if key else operation)
        # 只等待列表展示与提交，选定作品后立即释放，下载不阻塞后续翻页。
        async with self.sessions.hold(self._session_key(event)):
            target = self._resolve_target(event, tokens, key) if tokens else None
        if target is None:
            if key:
                command = self._command(f"{operation}_{key}")
                usage = f"用法：{command} 作品ID|链接"
                if key == "ehentai":
                    usage += "\nE-Hentai 请粘贴完整画廊链接，或填写 gid_token；仅数字 ID 无法定位作品。"
            else:
                command = self._command(operation)
                usage = (
                    f"用法：{command} 序号|链接\n序号来自本人最近一次搜索或排行列表；"
                    f"也可粘贴完整作品链接。\n{self._start_hint()}"
                )
            await self._delivery(event).text(usage)
            return
        work = await self._load_work(target)
        if with_files:
            await self._fetch_batch(event, [work])
        else:
            await self.presenter.send_card(self._delivery(event), work)

    @_command_filter("get")
    async def cmd_get(self, event: AstrMessageEvent):
        """下载并发送作品文件，参数：序号或作品链接。"""
        await self._guarded(event, lambda: self._get_or_detail(event, True))

    @_command_filter("detail")
    async def cmd_detail(self, event: AstrMessageEvent):
        """查看作品信息，参数：序号或作品链接。"""
        await self._guarded(event, lambda: self._get_or_detail(event, False))

    # ── 随机 ────────────────────────────────────────────

    async def _send_random(self, event: AstrMessageEvent, result: RandomResult) -> None:
        """展示随机结果，解释数量不足和未生效的筛选条件。"""
        delivery = self._delivery(event)
        works = result.items
        if result.notes:
            await delivery.safe_text("\n".join(result.notes))
        if len(works) == 1:
            await self.presenter.send_card(delivery, works[0])
        else:
            await self.presenter.send_cards(delivery, works, f"随机作品 {len(works)} 部")
        await self._auto_fetch(event, works)

    async def _auto_fetch(self, event: AstrMessageEvent, works: list[WorkInfo]) -> None:
        if self.cfg.random.auto_fetch:
            await self._fetch_batch(event, works)

    @staticmethod
    def _random_count(count: int) -> int:
        if count and not 1 <= count <= MAX_RANDOM_COUNT:
            raise CollectorError(f"每次随机数量应为 1～{MAX_RANDOM_COUNT} 部。")
        return count or 1

    async def _site_random(self, event: AstrMessageEvent, key: str) -> None:
        args = parse_args(self._command_tokens(event, f"random_{key}"))
        if args.page_given:
            raise CollectorError("随机操作不使用页码，请去掉页码条件后重试。")
        works = await self.service.random_works(key, args.query, self._random_count(args.count))
        await self._send_random(event, works)

    async def _aggregate_random(self, event: AstrMessageEvent, media: MediaType) -> None:
        args = parse_args(self._command_tokens(event, f"random_{media.value}"))
        if args.page_given:
            raise CollectorError("随机操作不使用页码，请去掉页码条件后重试。")
        count = self._random_count(args.count)
        await self._report_unavailable(event, media, ACTION_RANDOM)
        if not self.cfg.random.per_site:
            await self._send_random(event, await self.service.random_works(None, args.query, count, media))
            return
        keys = [source.key for source in self.sources.random_candidates(media)]
        if not keys:
            raise self._no_source(media, "随机")
        if len(keys) == 1:
            await self._send_random(event, await self.service.random_works(keys[0], args.query, count))
            return
        results = await self.service.random_many(keys, args.query, count)
        works = [work for result in results for work in result.items]
        if not works:
            errors = [f"{self._source_name(result.source)}：{result.error}" for result in results if result.error]
            await self._delivery(event).text("随机失败\n" + "\n".join(errors))
            return
        title = f"随机{MEDIA_WORDS[media]}：每站 {count} 部"
        await self.presenter.send_grouped_cards(self._delivery(event), title, results)
        await self._auto_fetch(event, works)

    # ── 管理员 ──────────────────────────────────────────

    async def _push(self, event: AstrMessageEvent) -> None:
        delivery = self._delivery(event)
        if not self._is_admin(event):
            await delivery.text("只有管理员可以管理推送任务")
            return
        tokens = self._command_tokens(event, "push")
        command = self._command("push")
        if len(tokens) == 2 and tokens[0] in ("执行", "run") and tokens[1].isascii() and tokens[1].isdigit():
            index = int(tokens[1])
            if not 1 <= index <= len(self.push_manager.tasks):
                count = len(self.push_manager.tasks)
                options = f"可选 1～{count}" if count else "当前没有可执行任务，请在“自动推送”中添加有效任务"
                await delivery.text(f"任务序号无效，{options}。发送「{command}」查看任务。")
                return
            await delivery.text("正在执行推送任务…")
            await delivery.text(await self.push_manager.run_task(self.push_manager.tasks[index - 1]))
            return
        usage = f"手动执行：{command} 执行 序号" if self.push_manager.tasks else ""
        invalid = "参数无效，请按下面的写法操作。\n" if tokens else ""
        await delivery.text(invalid + self.push_manager.describe() + (f"\n{usage}" if usage else ""))

    @_command_filter("push")
    async def cmd_push(self, event: AstrMessageEvent):
        """查看或手动执行推送任务（管理员）。"""
        await self._guarded(event, lambda: self._push(event))

    async def _clean(self, event: AstrMessageEvent) -> None:
        delivery = self._delivery(event)
        if not self._is_admin(event):
            await delivery.text("只有管理员可以清理缓存")
            return
        cleaned = await asyncio.to_thread(self.job_store.cleanup_expired, True)
        await delivery.text(f"已清理 {cleaned} 个缓存目录")

    @_command_filter("clean")
    async def cmd_clean(self, event: AstrMessageEvent):
        """清理全部已生成的缓存文件（管理员）。"""
        await self._guarded(event, lambda: self._clean(event))


# ── 生成站点与聚合指令 ──────────────────────────────────


def _register_command(
    key: str,
    handler_name: str,
    doc: str,
    run: Callable[[HentaiCollectorPlugin, AstrMessageEvent], Awaitable[None]],
) -> None:
    """生成指令处理函数并注册到 AstrBot。

    AstrBot 按“模块名_函数名”登记处理函数，加载插件时再绑定实例，
    因此每个生成的函数必须有唯一的 ``__name__``；``__doc__`` 会显示在 WebUI 指令管理中。
    """

    async def handler(self: HentaiCollectorPlugin, event: AstrMessageEvent):
        await self._guarded(
            event, lambda: run(self, event), listing=handler_name.startswith(("cmd_search_", "cmd_ranking_")),
        )

    handler.__name__ = handler_name
    handler.__qualname__ = f"{HentaiCollectorPlugin.__name__}.{handler_name}"
    handler.__doc__ = doc
    decorated = _command_filter(key)(handler)
    setattr(HentaiCollectorPlugin, handler_name, decorated)


def _register_generated_commands() -> None:
    """从固定操作目录生成单站与聚合处理器，名称不随触发词配置变化。"""
    for spec in COMMAND_SPECS.values():
        if spec.source:
            run = lambda self, event, spec=spec: self._site_action(event, spec.action, spec.source)
        elif spec.media:
            run = lambda self, event, spec=spec: self._aggregate_action(event, spec.action, MediaType(spec.media))
        else:
            continue
        _register_command(spec.key, spec.handler_name, f"{spec.label}，参数：{spec.usage}", run)


_register_generated_commands()
