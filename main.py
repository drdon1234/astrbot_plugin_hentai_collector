"""AstrBot 本子与里番聚合插件入口：注册站点专属指令、聚合指令与自动解析，把请求委托给业务层与展示层。"""

import asyncio
from collections import OrderedDict
from typing import Awaitable, Callable, Optional

from .core.logger import logger

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.event_message_type import EventMessageType

from .core.config_manager import SOURCE_DISPLAY_NAMES, ConfigManager, resolve_plugin_data_dir
from .core.constants import Config
from .core.downloader import ImageDownloader, VideoDownloader
from .core.errors import CollectorError, SourceUnavailable
from .core.message_adapter import Delivery, PresenterOptions, WorkPresenter
from .core.packager import Packager
from .core.push import PushManager
from .core.service import SiteResult, WorkService, default_period, parse_args, split_command
from .core.storage import MODE_RANKING, MODE_SEARCH, JobStore, PushHistory, SearchSession, SearchSessionStore
from .core.types import PERIOD_LABELS, MediaType, WorkInfo, WorkRef


# ── 指令表 ──────────────────────────────────────────────

CMD_HELP = "本子帮助"
CMD_PAGE = "翻页"
CMD_GET = "看本"
CMD_DETAIL = "本子详情"
CMD_SESSION = "本子会话"
CMD_PUSH = "本子推送"
CMD_CLEAN = "本子清理缓存"

ACTION_SEARCH = "搜"
ACTION_RANKING = "排行"
ACTION_RANDOM = "随机"
ACTION_GET = "看"
ACTION_DETAIL = "详情"
# 动作 → (处理函数名片段, 说明, 参数写法)
ACTION_INFO = {
    ACTION_SEARCH: ("search", "搜索", "关键词 [条件]"),
    ACTION_RANKING: ("ranking", "排行榜", "[日榜|周榜|月榜|年榜|总榜] [页码]"),
    ACTION_RANDOM: ("random", "随机", "[条件] [N本]"),
    ACTION_GET: ("get", "下载并发送文件", "ID|链接"),
    ACTION_DETAIL: ("detail", "作品信息", "ID|链接"),
}
SITE_ACTIONS = (ACTION_SEARCH, ACTION_RANKING, ACTION_RANDOM, ACTION_GET, ACTION_DETAIL)
AGGREGATE_ACTIONS = (ACTION_SEARCH, ACTION_RANKING, ACTION_RANDOM)

# 源站键 → (指令标识, 标识别名)；站点专属指令为“动作 + 标识”，如 搜eh、排行jm、看anime
SITE_TAGS = {
    "ehentai": ("eh", ("ex",)),
    "nhentai": ("nh", ()),
    "jmcomic": ("jm", ("禁漫",)),
    "pica": ("哔咔", ("pica", "bika")),
    "hanime1": ("anime", ("hanime", "h1")),
}
# 聚合指令为“动作 + 媒体词”，如 搜本子、排行里番
MEDIA_WORDS = {MediaType.COMIC: "本子", MediaType.VIDEO: "里番"}

COMMAND_ALIASES: dict[str, set[str]] = {
    CMD_HELP: {"hc", "hc帮助", "里番帮助"},
    CMD_PAGE: {"本子翻页"},
    CMD_GET: {"本子下载", "获取本子", "看里番"},
    CMD_DETAIL: {"详情"},
    CMD_SESSION: set(),
    CMD_PUSH: set(),
    CMD_CLEAN: set(),
    "搜本子": {"搜本"},
    "排行本子": {"本子排行", "本子排行榜", "排行榜"},
    "随机本子": {"随机本"},
    "搜里番": set(),
    "排行里番": {"里番排行", "里番排行榜"},
    "随机里番": set(),
}
for _key, (_tag, _extra) in SITE_TAGS.items():
    for _action in SITE_ACTIONS:
        COMMAND_ALIASES[f"{_action}{_tag}"] = {f"{_action}{alias}" for alias in _extra}
del _key, _tag, _extra, _action
COMMAND_WORDS = frozenset(
    word for name, aliases in COMMAND_ALIASES.items() for word in (name, *aliases)
)

MAX_LINKS_PER_MESSAGE = 3
MAX_RANDOM_COUNT = 5
MAX_SESSION_INDEX_DIGITS = 3


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
                get_command=CMD_GET,
                page_command=CMD_PAGE,
                site_get_commands={key: f"{ACTION_GET}{tag}" for key, (tag, _) in SITE_TAGS.items()},
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
        return self.cfg.permission.check(event.is_private_chat(), event.get_sender_id(), event.get_group_id())

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        return self.cfg.permission.is_admin(event.get_sender_id()) or event.is_admin()

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

    async def _guarded(self, event: AstrMessageEvent, action: Callable[[], Awaitable[None]]) -> None:
        """统一的权限、去重与异常处理。"""
        if not self._allowed(event) or not self._claim(event):
            return
        event.stop_event()
        try:
            await action()
        except asyncio.CancelledError:
            raise
        except CollectorError as exc:
            await self._delivery(event).safe_text(str(exc))
        except Exception as exc:  # 兜底：任何未预期异常都要告知用户并保留堆栈
            logger.exception("指令处理失败")
            await self._delivery(event).safe_text(self.presenter.describe_error(exc))

    def _source_name(self, key: str) -> str:
        source = self.sources.get(key)
        return source.name if source else SOURCE_DISPLAY_NAMES.get(key, key)

    def _require_site(self, key: str):
        """站点专属指令的源站检查：未启用时提示开启方式。"""
        if self.sources.get(key) is None:
            raise SourceUnavailable(f"{self._source_name(key)} 未启用，请在插件配置“源站开关”中开启")
        return self.sources.require(key)

    @staticmethod
    def _no_source(media: MediaType, what: str) -> SourceUnavailable:
        return SourceUnavailable(f"没有可{what}的{MEDIA_WORDS[media]}源站，请检查插件配置“源站开关”")

    async def _load_work(self, target: WorkInfo | WorkRef) -> WorkInfo:
        if isinstance(target, WorkRef):
            return await self.service.get_work(target)
        return await self.service.ensure_detail(target)

    async def _send_and_fetch(self, event: AstrMessageEvent, work: WorkInfo) -> None:
        delivery = self._delivery(event)
        left = self._cooldown_left(event)
        if left:
            await delivery.text(f"获取作品冷却中，请 {left} 秒后再试")
            return
        await self.presenter.send_fetch(delivery, work)

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
        first = text.lstrip("/／!！#").split(maxsplit=1)[0] if text else ""
        if not text or first in COMMAND_WORDS:
            return
        refs = self.sources.extract_refs(text)[:MAX_LINKS_PER_MESSAGE]
        if not refs:
            return

        async def action() -> None:
            delivery = self._delivery(event)
            for ref in refs:
                try:
                    work = await self.service.get_work(ref)
                    await self.presenter.send_card(delivery, work)
                    if self.cfg.basic.send_files_on_link:
                        await self._send_and_fetch(event, work)
                except CollectorError as exc:
                    await delivery.safe_text(f"{ref} 解析失败：{exc}")

        await self._guarded(event, action)

    # ── 帮助与会话 ──────────────────────────────────────

    def _help_text(self) -> str:
        search, ranking, random_ = ACTION_SEARCH, ACTION_RANKING, ACTION_RANDOM
        lines = [
            "本子 / 里番聚合插件指令：",
            "【聚合】不写站点，同时查询全部已启用源站，按源站分组发送",
            f"· {search}本子 {ACTION_INFO[search][2]}",
            f"· {ranking}本子 {ACTION_INFO[ranking][2]}",
            f"· {random_}本子 {ACTION_INFO[random_][2]}（{self.cfg.random.mode}）",
            f"· {search}里番 / {ranking}里番 / {random_}里番  同上，查询里番源站",
            "【单站】把“本子 / 里番”换成站点标识，并可按 ID 获取：",
            f"· {search}eh {ACTION_INFO[search][2]}、{ranking}jm 月榜、{random_}nh 3本",
            f"· {ACTION_GET}jm 350234（下载）、{ACTION_DETAIL}eh 链接（只看信息）",
            "【通用】",
            f"· {CMD_GET} 序号|链接  下载并发送文件（里番为视频）",
            f"· {CMD_DETAIL} 序号|链接  只看作品信息",
            f"· {CMD_PAGE} 页码|下一页|上一页  翻阅最近一次列表",
            f"· {CMD_SESSION}  查看当前会话 ID（填写推送目标用）",
            "",
            "条件写法：p2 或 第2页、排序=最新|热门|观看|点赞、评分=4、页数=20、分类=xxx、-排除词",
            "直接发送作品链接或 JM 编号会自动解析。",
            "",
            "站点标识与已启用源站：",
        ]
        for key, (tag, extra) in SITE_TAGS.items():
            source = self.sources.get(key)
            tags = "/".join((tag, *extra))
            if source is None:
                lines.append(f"· {self._source_name(key)}（{tags}）未启用")
                continue
            features = []
            if source.supports_search:
                features.append("搜索")
            if source.ranking_periods:
                features.append("排行(" + "/".join(PERIOD_LABELS[p] for p in source.ranking_periods) + ")")
            if source.supports_random:
                features.append("随机")
            status = f"（不可用：{source.unavailable_reason}）" if source.unavailable_reason else ""
            lines.append(f"· {source.name}（{tags}）{status} {' '.join(features)}")
        return "\n".join(lines)

    @filter.command(CMD_HELP, alias=COMMAND_ALIASES[CMD_HELP])
    async def cmd_help(self, event: AstrMessageEvent):
        """查看插件帮助。"""

        async def action() -> None:
            await self._delivery(event).text(self._help_text())

        await self._guarded(event, action)

    @filter.command(CMD_SESSION)
    async def cmd_session(self, event: AstrMessageEvent):
        """查看当前会话 ID，用于填写推送目标。"""

        async def action() -> None:
            await self._delivery(event).text(
                f"当前会话 ID：\n{event.unified_msg_origin}\n可填入推送任务的“推送会话 ID”。"
            )

        await self._guarded(event, action)

    # ── 列表展示 ────────────────────────────────────────

    async def _show_results(self, event: AstrMessageEvent, session: SearchSession, results: list[SiteResult]) -> None:
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
            await delivery.text("\n".join(errors) if errors else "没有找到符合条件的作品")
            return
        session.items = items
        session.has_next = any(result.page is not None and result.page.has_next for result in results)
        self.sessions.save(self._session_key(event), session)
        if single:
            await self.presenter.send_list(delivery, results[0].page)
        else:
            await self.presenter.send_aggregate_list(delivery, session.title, session.page, results)

    async def _turn_page(self, event: AstrMessageEvent) -> None:
        tokens = split_command(event.message_str)
        session = self.sessions.get(self._session_key(event))
        if session is None:
            await self._delivery(event).text("没有最近的列表，请先搜索或查看排行榜")
            return
        word = tokens[0] if tokens else "下一页"
        if word.isdigit():
            target = int(word)
        elif word in ("上一页", "上", "prev"):
            target = session.page - 1
        else:
            target = session.page + 1
        if target < 1:
            await self._delivery(event).text("已经是第一页")
            return
        if session.mode == MODE_RANKING:
            results = await self.service.ranking_many(session.sources, session.period, target)
        else:
            results = await self.service.search_many(session.sources, session.query.with_page(target))
        session.page = target
        await self._show_results(event, session, results)

    @filter.command(CMD_PAGE, alias=COMMAND_ALIASES[CMD_PAGE])
    async def cmd_page(self, event: AstrMessageEvent):
        """翻阅最近一次搜索或排行榜。"""
        await self._guarded(event, lambda: self._turn_page(event))

    # ── 搜索 ────────────────────────────────────────────

    async def _site_search(self, event: AstrMessageEvent, key: str) -> None:
        args = parse_args(split_command(event.message_str))
        source = self.sources.require(key)
        if source.search_hint and args.query.is_empty():
            await self._delivery(event).safe_text(f"未填写关键词，显示最新作品。{source.search_hint}")
        session = SearchSession(sources=[key], mode=MODE_SEARCH, query=args.query, page=args.query.page)
        await self._show_results(event, session, await self.service.search_many([key], args.query))

    async def _aggregate_search(self, event: AstrMessageEvent, media: MediaType) -> None:
        tokens = split_command(event.message_str)
        word = MEDIA_WORDS[media]
        if not tokens:
            await self._delivery(event).text(
                f"用法：{ACTION_SEARCH}{word} 关键词 [条件]  同时搜索全部{word}源站\n"
                f"例：{ACTION_SEARCH}{word} 原神 排序=热门\n"
                f"只搜一个站：{ACTION_SEARCH}eh / {ACTION_SEARCH}jm / {ACTION_SEARCH}anime …\n"
                f"发送「{CMD_HELP}」查看全部说明"
            )
            return
        args = parse_args(tokens)
        keys = [source.key for source in self.sources.searchable(media)]
        if not keys:
            raise self._no_source(media, "搜索")
        session = SearchSession(
            sources=keys,
            mode=MODE_SEARCH,
            title=f"{word}聚合搜索：{args.query.describe()}",
            query=args.query,
            page=args.query.page,
        )
        await self._show_results(event, session, await self.service.search_many(keys, args.query))

    # ── 排行 ────────────────────────────────────────────

    async def _site_ranking(self, event: AstrMessageEvent, key: str) -> None:
        args = parse_args(split_command(event.message_str), allow_period=True)
        source = self.sources.require(key)
        if not source.ranking_periods:
            options = "、".join(f"{ACTION_RANKING}{SITE_TAGS[s.key][0]}" for s in self.sources.with_ranking())
            await self._delivery(event).text(f"{source.name} 没有排行榜，可用：{options}")
            return
        period = args.period or default_period(source)
        self.sources.check_period(source, period)
        page = max(1, args.query.page)
        session = SearchSession(sources=[key], mode=MODE_RANKING, period=period, page=page)
        await self._show_results(event, session, await self.service.ranking_many([key], period, page))

    async def _aggregate_ranking(self, event: AstrMessageEvent, media: MediaType) -> None:
        args = parse_args(split_command(event.message_str), allow_period=True)
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
                await self._delivery(event).text(f"没有支持{label}的{word}源站")
                return
            note = f"{'、'.join(skipped)} 没有{label}" if skipped else ""
            title = f"{word}聚合排行：{label}" + (f"（{note}）" if note else "")
            if note and len(keys) == 1:  # 只剩一个站时按单站列表展示，说明单独发送
                await self._delivery(event).safe_text(note)
        page = max(1, args.query.page)
        session = SearchSession(sources=keys, mode=MODE_RANKING, title=title, period=period, page=page)
        await self._show_results(event, session, await self.service.ranking_many(keys, period, page))

    # ── 获取与详情 ──────────────────────────────────────

    def _resolve_target(self, event: AstrMessageEvent, tokens: list[str], key: str) -> Optional[WorkInfo | WorkRef]:
        """通用指令解析“序号 / 链接 / JM 编号”，站点指令解析“该站 ID / 链接”。"""
        token = tokens[0]
        if not key and len(tokens) == 1 and token.isdigit() and len(token) <= MAX_SESSION_INDEX_DIGITS:
            picked = self.sessions.pick(self._session_key(event), int(token))
            if picked is not None:
                return picked
        return self.sources.resolve_reference(tokens, key)

    async def _get_or_detail(self, event: AstrMessageEvent, with_files: bool, key: str = "") -> None:
        tokens = split_command(event.message_str)
        target = self._resolve_target(event, tokens, key) if tokens else None
        if target is None:
            if key:
                command = f"{ACTION_GET if with_files else ACTION_DETAIL}{SITE_TAGS[key][0]}"
                usage = f"用法：{command} 作品ID|链接"
            else:
                command = CMD_GET if with_files else CMD_DETAIL
                usage = (
                    f"用法：{command} 序号|链接\n例：{command} 3、{command} JM350234、"
                    f"{command} https://nhentai.net/g/123456/\n"
                    f"按 ID 获取请用站点指令，如「{ACTION_GET if with_files else ACTION_DETAIL}jm 350234」"
                )
            await self._delivery(event).text(usage)
            return
        work = await self._load_work(target)
        if with_files:
            await self._send_and_fetch(event, work)
        else:
            await self.presenter.send_card(self._delivery(event), work)

    @filter.command(CMD_GET, alias=COMMAND_ALIASES[CMD_GET])
    async def cmd_get(self, event: AstrMessageEvent):
        """下载并发送作品文件：看本 序号|链接。"""
        await self._guarded(event, lambda: self._get_or_detail(event, True))

    @filter.command(CMD_DETAIL, alias=COMMAND_ALIASES[CMD_DETAIL])
    async def cmd_detail(self, event: AstrMessageEvent):
        """查看作品信息：本子详情 序号|链接。"""
        await self._guarded(event, lambda: self._get_or_detail(event, False))

    # ── 随机 ────────────────────────────────────────────

    async def _send_random(self, event: AstrMessageEvent, works: list[WorkInfo]) -> None:
        delivery = self._delivery(event)
        if len(works) == 1:
            await self.presenter.send_card(delivery, works[0])
        else:
            await self.presenter.send_cards(delivery, works, f"随机作品 {len(works)} 部")
        await self._auto_fetch(event, works)

    async def _auto_fetch(self, event: AstrMessageEvent, works: list[WorkInfo]) -> None:
        if self.cfg.random.auto_fetch:
            for work in works:
                await self._send_and_fetch(event, work)

    @staticmethod
    def _random_count(count: int) -> int:
        return max(1, min(MAX_RANDOM_COUNT, count or 1))

    async def _site_random(self, event: AstrMessageEvent, key: str) -> None:
        args = parse_args(split_command(event.message_str))
        works = await self.service.random_works(key, args.query, self._random_count(args.count))
        await self._send_random(event, works)

    async def _aggregate_random(self, event: AstrMessageEvent, media: MediaType) -> None:
        args = parse_args(split_command(event.message_str))
        count = self._random_count(args.count)
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
        tokens = split_command(event.message_str)
        if len(tokens) >= 2 and tokens[0] in ("执行", "run") and tokens[1].isdigit():
            index = int(tokens[1])
            if not 1 <= index <= len(self.push_manager.tasks):
                await delivery.text("任务序号无效")
                return
            await delivery.text("正在执行推送任务…")
            await delivery.text(await self.push_manager.run_task(self.push_manager.tasks[index - 1]))
            return
        await delivery.text(self.push_manager.describe() + f"\n手动执行：{CMD_PUSH} 执行 序号")

    @filter.command(CMD_PUSH)
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

    @filter.command(CMD_CLEAN)
    async def cmd_clean(self, event: AstrMessageEvent):
        """清理全部已生成的缓存文件（管理员）。"""
        await self._guarded(event, lambda: self._clean(event))


# ── 生成站点与聚合指令 ──────────────────────────────────


def _register_command(
    command: str,
    handler_name: str,
    doc: str,
    run: Callable[[HentaiCollectorPlugin, AstrMessageEvent], Awaitable[None]],
) -> None:
    """生成指令处理函数并注册到 AstrBot。

    AstrBot 按“模块名_函数名”登记处理函数，加载插件时再绑定实例，
    因此每个生成的函数必须有唯一的 ``__name__``；``__doc__`` 会显示在 WebUI 指令管理中。
    """

    async def handler(self: HentaiCollectorPlugin, event: AstrMessageEvent):
        await self._guarded(event, lambda: run(self, event))

    handler.__name__ = handler_name
    handler.__qualname__ = f"{HentaiCollectorPlugin.__name__}.{handler_name}"
    handler.__doc__ = doc
    decorated = filter.command(command, alias=COMMAND_ALIASES[command])(handler)
    setattr(HentaiCollectorPlugin, handler_name, decorated)


def _register_generated_commands() -> None:
    for key, (tag, _) in SITE_TAGS.items():
        for action in SITE_ACTIONS:
            name, label, usage = ACTION_INFO[action]
            _register_command(
                f"{action}{tag}",
                f"cmd_{name}_{key}",
                f"{SOURCE_DISPLAY_NAMES[key]} {label}：{action}{tag} {usage}",
                lambda self, event, action=action, key=key: self._site_action(event, action, key),
            )
    for media, word in MEDIA_WORDS.items():
        for action in AGGREGATE_ACTIONS:
            name, label, usage = ACTION_INFO[action]
            _register_command(
                f"{action}{word}",
                f"cmd_{name}_{media.value}_all",
                f"全部{word}源站聚合{label}：{action}{word} {usage}",
                lambda self, event, action=action, media=media: self._aggregate_action(event, action, media),
            )


_register_generated_commands()
