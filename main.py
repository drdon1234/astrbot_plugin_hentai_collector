"""AstrBot 本子与里番聚合插件入口：注册指令与自动解析，把请求委托给业务层与展示层。"""

import asyncio
from collections import OrderedDict
from typing import Awaitable, Callable, Optional

from .core.logger import logger

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.event_message_type import EventMessageType

from .core.config_manager import ConfigManager, resolve_plugin_data_dir
from .core.constants import Config
from .core.downloader import ImageDownloader, VideoDownloader
from .core.errors import CollectorError, SourceUnavailable
from .core.message_adapter import Delivery, PresenterOptions, WorkPresenter
from .core.packager import Packager
from .core.push import PushManager
from .core.service import WorkService, parse_args, split_command
from .core.storage import MODE_RANKING, MODE_SEARCH, JobStore, PushHistory, SearchSession, SearchSessionStore
from .core.types import PERIOD_LABELS, MediaType, Period, WorkInfo, WorkRef


CMD_HELP = "本子帮助"
CMD_SEARCH = "搜本"
CMD_PAGE = "本子翻页"
CMD_GET = "看本"
CMD_DETAIL = "本子详情"
CMD_RANKING = "本子排行"
CMD_RANDOM = "随机本子"
CMD_SEARCH_VIDEO = "搜里番"
CMD_RANKING_VIDEO = "里番排行"
CMD_RANDOM_VIDEO = "随机里番"
CMD_SESSION = "本子会话"
CMD_PUSH = "本子推送"
CMD_CLEAN = "本子清理缓存"

COMMAND_ALIASES = {
    CMD_HELP: {"hc", "hc帮助", "里番帮助"},
    CMD_SEARCH: {"本子搜索"},
    CMD_PAGE: {"翻页"},
    CMD_GET: {"本子下载", "获取本子"},
    CMD_DETAIL: {"详情"},
    CMD_RANKING: {"排行榜", "本子排行榜"},
    CMD_RANDOM: {"随机本"},
    CMD_SEARCH_VIDEO: set(),
    CMD_RANKING_VIDEO: {"里番排行榜"},
    CMD_RANDOM_VIDEO: set(),
    CMD_SESSION: set(),
    CMD_PUSH: set(),
    CMD_CLEAN: set(),
}
COMMAND_WORDS = frozenset(
    word for name, aliases in COMMAND_ALIASES.items() for word in (name, *aliases)
)
MAX_LINKS_PER_MESSAGE = 3
MAX_RANDOM_COUNT = 5
HANIME1 = "hanime1"


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
                get_command=CMD_GET,
                page_command=CMD_PAGE,
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

    def _default_source(self, media: Optional[MediaType] = None) -> str:
        if media == MediaType.VIDEO:
            return HANIME1
        default = self.cfg.basic.default_source
        if self.sources.get(default):
            return default
        keys = self.sources.keys()
        if not keys:
            raise SourceUnavailable("没有启用任何源站，请在插件配置“源站开关”中开启")
        return keys[0]

    def _resolve_target(self, event: AstrMessageEvent, tokens: list[str]) -> Optional[WorkInfo | WorkRef]:
        """解析“序号 / 链接 / 源站 ID”，返回会话中的作品或作品引用。"""
        if len(tokens) == 1 and tokens[0].isdigit() and len(tokens[0]) <= 2:
            picked = self.sessions.pick(self._session_key(event), int(tokens[0]))
            if picked is not None:
                return picked
        return self.sources.resolve_reference(tokens, self._default_source())

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
        lines = ["本子 / 里番聚合插件指令："]
        lines += [
            f"[1] {CMD_SEARCH} [源站] 关键词 [条件]  搜索作品",
            f"[2] {CMD_PAGE} 页码|下一页|上一页  翻阅最近一次列表",
            f"[3] {CMD_GET} 序号|链接|源站 ID  下载并发送文件（里番为视频）",
            f"[4] {CMD_DETAIL} 序号|链接|源站 ID  只看作品信息",
            f"[5] {CMD_RANKING} [源站] [日榜|周榜|月榜|年榜|总榜] [页码]  排行榜",
            f"[6] {CMD_RANDOM} [源站|全部] [条件] [N本]  随机作品",
            f"[7] {CMD_SEARCH_VIDEO} / {CMD_RANKING_VIDEO} / {CMD_RANDOM_VIDEO}  Hanime1 快捷指令",
            f"[8] {CMD_SESSION}  查看当前会话 ID（填写推送目标用）",
            "",
            "条件写法：p2 或 第2页、排序=最新|热门|观看|点赞、评分=4、页数=20、分类=xxx、-排除词",
            "直接发送作品链接或 JM 编号会自动解析。",
            "",
            "已启用源站：",
        ]
        for source in self.sources:
            features = []
            if source.supports_search:
                features.append("搜索")
            if source.ranking_periods:
                features.append("排行(" + "/".join(PERIOD_LABELS[p] for p in source.ranking_periods) + ")")
            if source.supports_random:
                features.append("随机")
            status = f"（不可用：{source.unavailable_reason}）" if source.unavailable_reason else ""
            lines.append(f"· {source.name}{status}  别名：{'、'.join(source.aliases[:3])}  {' '.join(features)}")
        default = self.sources.get(self.cfg.basic.default_source)
        lines.append(f"默认源站：{default.name if default else '无'}")
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

    # ── 搜索、翻页、排行 ────────────────────────────────

    async def _show_list(self, event: AstrMessageEvent, session: SearchSession, page) -> None:
        page.items = page.items[: self.cfg.display.list_size]
        if not page.items:
            await self._delivery(event).text("没有找到符合条件的作品")
            return
        session.items = page.items
        session.page = page.page
        session.has_next = page.has_next
        self.sessions.save(self._session_key(event), session)
        await self.presenter.send_list(self._delivery(event), page)

    async def _search(self, event: AstrMessageEvent, media: Optional[MediaType] = None) -> None:
        tokens = split_command(event.message_str)
        args = parse_args(tokens, self.sources.resolve_alias, self.sources.is_all_word)
        if not tokens:
            await self._delivery(event).text(
                f"用法：{CMD_SEARCH} [源站] 关键词 [条件]\n例：{CMD_SEARCH} eh 中文 评分=4 页数=20\n"
                f"发送「{CMD_HELP}」查看全部说明"
            )
            return
        source_key = args.source or self._default_source(media)
        source = self.sources.require(source_key)
        if source.search_hint and not args.query.keyword and args.query.is_empty():
            await self._delivery(event).safe_text(f"未填写关键词，显示最新作品。{source.search_hint}")
        page = await self.service.search(source_key, args.query)
        session = SearchSession(source=source_key, mode=MODE_SEARCH, query=args.query)
        await self._show_list(event, session, page)

    @filter.command(CMD_SEARCH, alias=COMMAND_ALIASES[CMD_SEARCH])
    async def cmd_search(self, event: AstrMessageEvent):
        """搜索作品：搜本 [源站] 关键词 [条件]。"""
        await self._guarded(event, lambda: self._search(event))

    @filter.command(CMD_SEARCH_VIDEO)
    async def cmd_search_video(self, event: AstrMessageEvent):
        """搜索 Hanime1 里番。"""
        await self._guarded(event, lambda: self._search(event, MediaType.VIDEO))

    async def _turn_page(self, event: AstrMessageEvent) -> None:
        tokens = split_command(event.message_str)
        session = self.sessions.get(self._session_key(event))
        if session is None:
            await self._delivery(event).text(f"没有最近的列表，请先使用「{CMD_SEARCH}」或「{CMD_RANKING}」")
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
        if session.mode == MODE_RANKING and session.period is not None:
            page = await self.service.ranking(session.source, session.period, target)
        else:
            page = await self.service.search(session.source, session.query.with_page(target))
        await self._show_list(event, session, page)

    @filter.command(CMD_PAGE, alias=COMMAND_ALIASES[CMD_PAGE])
    async def cmd_page(self, event: AstrMessageEvent):
        """翻阅最近一次搜索或排行榜。"""
        await self._guarded(event, lambda: self._turn_page(event))

    async def _ranking(self, event: AstrMessageEvent, media: Optional[MediaType] = None) -> None:
        tokens = split_command(event.message_str)
        args = parse_args(tokens, self.sources.resolve_alias, self.sources.is_all_word, allow_period=True)
        source_key = args.source or self._default_source(media)
        source = self.sources.require(source_key)
        if not source.ranking_periods:
            options = "、".join(s.name for s in self.sources.with_ranking())
            await self._delivery(event).text(f"{source.name} 没有排行榜，支持排行榜的源站：{options}")
            return
        period = args.period or (Period.WEEK if Period.WEEK in source.ranking_periods else source.ranking_periods[0])
        page = await self.service.ranking(source_key, period, max(1, args.query.page))
        session = SearchSession(source=source_key, mode=MODE_RANKING, period=period)
        await self._show_list(event, session, page)

    @filter.command(CMD_RANKING, alias=COMMAND_ALIASES[CMD_RANKING])
    async def cmd_ranking(self, event: AstrMessageEvent):
        """查看排行榜：本子排行 [源站] [日榜|周榜|月榜|年榜|总榜] [页码]。"""
        await self._guarded(event, lambda: self._ranking(event))

    @filter.command(CMD_RANKING_VIDEO, alias=COMMAND_ALIASES[CMD_RANKING_VIDEO])
    async def cmd_ranking_video(self, event: AstrMessageEvent):
        """查看 Hanime1 排行榜。"""
        await self._guarded(event, lambda: self._ranking(event, MediaType.VIDEO))

    # ── 获取与详情 ──────────────────────────────────────

    async def _get_or_detail(self, event: AstrMessageEvent, with_files: bool) -> None:
        tokens = split_command(event.message_str)
        target = self._resolve_target(event, tokens) if tokens else None
        if target is None:
            command = CMD_GET if with_files else CMD_DETAIL
            await self._delivery(event).text(
                f"用法：{command} 序号|链接|源站 ID\n例：{command} 3、{command} jm 350234、"
                f"{command} https://nhentai.net/g/123456/"
            )
            return
        work = await self._load_work(target)
        await self.presenter.send_card(self._delivery(event), work)
        if with_files:
            await self._send_and_fetch(event, work)

    @filter.command(CMD_GET, alias=COMMAND_ALIASES[CMD_GET])
    async def cmd_get(self, event: AstrMessageEvent):
        """下载并发送作品文件：看本 序号|链接|源站 ID。"""
        await self._guarded(event, lambda: self._get_or_detail(event, True))

    @filter.command(CMD_DETAIL, alias=COMMAND_ALIASES[CMD_DETAIL])
    async def cmd_detail(self, event: AstrMessageEvent):
        """查看作品信息：本子详情 序号|链接|源站 ID。"""
        await self._guarded(event, lambda: self._get_or_detail(event, False))

    # ── 随机 ────────────────────────────────────────────

    async def _random(self, event: AstrMessageEvent, media: Optional[MediaType] = None) -> None:
        tokens = split_command(event.message_str)
        args = parse_args(tokens, self.sources.resolve_alias, self.sources.is_all_word)
        source_key = args.source
        if media == MediaType.VIDEO:
            source_key = HANIME1
            self.sources.require(HANIME1)
        count = max(1, min(MAX_RANDOM_COUNT, args.count or 1))
        delivery = self._delivery(event)
        works: list[WorkInfo] = []
        for _ in range(count * 2):
            if len(works) >= count:
                break
            try:
                work = await self.service.random_work(source_key, args.query)
            except CollectorError as exc:
                if not works:
                    raise
                logger.warning(f"随机作品失败：{exc}")
                continue
            if all(work.ref != existing.ref for existing in works):
                works.append(work)
        if len(works) == 1:
            await self.presenter.send_card(delivery, works[0])
        else:
            await self.presenter.send_cards(delivery, works, f"随机作品 {len(works)} 部")
        if self.cfg.random.auto_fetch:
            for work in works:
                await self._send_and_fetch(event, work)

    @filter.command(CMD_RANDOM, alias=COMMAND_ALIASES[CMD_RANDOM])
    async def cmd_random(self, event: AstrMessageEvent):
        """随机作品：随机本子 [源站|全部] [条件] [N本]。"""
        await self._guarded(event, lambda: self._random(event))

    @filter.command(CMD_RANDOM_VIDEO)
    async def cmd_random_video(self, event: AstrMessageEvent):
        """随机 Hanime1 里番。"""
        await self._guarded(event, lambda: self._random(event, MediaType.VIDEO))

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
