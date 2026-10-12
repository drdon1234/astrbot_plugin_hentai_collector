"""展示编排：把业务结果（作品、单站与聚合列表、获取结果）渲染为消息并经 Delivery 发送，指令与推送共用。"""

import asyncio
from dataclasses import dataclass
from typing import Callable, Optional

from astrbot.api.message_components import Image, Plain

from ..logger import logger

from ..commands import COMMAND_SPECS
from ..errors import CollectorError
from ..service import SiteResult, WorkService
from ..source import SourceManager
from ..types import FetchResult, MediaType, WorkInfo, WorkPage
from .cover_grid import build_cover_grid, process_cover
from .formatter import (
    format_aggregate_header,
    format_fetch_summary,
    format_list_blocks,
    format_work_card,
    list_tips,
)
from .sender import Delivery


def _default_command(key: str) -> str:
    """独立展示器使用同一目录的默认词，插件运行时注入实时指令查询。"""
    return COMMAND_SPECS[key].default_words[0] if key in COMMAND_SPECS else ""


@dataclass(frozen=True)
class PresenterOptions:
    """展示相关配置。

    Attributes:
        nested_forward: 聚合结果是否使用嵌套合并转发
        command: 按操作标识读取当前可用指令，停用操作返回空字符串
        session_minutes: 列表序号有效期
    """

    show_cover: bool = True
    cover_blur: int = 0
    search_cover_grid: bool = True
    max_tags: int = 12
    show_description: bool = True
    video_as_file: bool = False
    send_progress: bool = False
    nested_forward: bool = True
    command: Callable[[str], str] = _default_command
    session_minutes: int = 30


class WorkPresenter:
    """渲染作品卡片、作品列表与文件发送流程。"""

    def __init__(self, service: WorkService, sources: SourceManager, options: PresenterOptions):
        self.service = service
        self.sources = sources
        self.options = options

    # ── 辅助 ──────────────────────────────────────────

    def _source_name(self, key: str) -> str:
        source = self.sources.get(key)
        return source.name if source else key

    def _alias(self, key: str) -> str:
        source = self.sources.get(key)
        return source.aliases[0] if source and source.aliases else key

    def get_hint(self, work: WorkInfo) -> str:
        """可直接复制的获取指令：优先使用该站专属指令。"""
        command = self.options.command(f"get_{work.source}")
        if command:
            return f"{command} {work.work_id}"
        command = self.options.command("get")
        if command:
            return f"{command} {work.url or (self._alias(work.source) + ' ' + work.work_id)}"
        return ""

    def retry_hint(self, work: WorkInfo) -> str:
        """提供失败后的恢复操作，避免提示已停用或空的指令。"""
        command = self.get_hint(work)
        return f"重新获取：{command}" if command else "获取指令已停用，请联系管理员启用后重试。"

    def card_text(self, work: WorkInfo) -> str:
        """作品信息卡片文本。"""
        return format_work_card(
            work,
            self._source_name(work.source),
            get_hint=self.get_hint(work),
            max_tags=self.options.max_tags,
            show_description=self.options.show_description,
        )

    async def _cover_components(self, works: list[WorkInfo]) -> list[Optional[Image]]:
        if not self.options.show_cover:
            return [None] * len(works)
        raw = await self.service.download_covers(works)
        processed = await asyncio.to_thread(
            lambda: [process_cover(data, self.options.cover_blur) if data else None for data in raw]
        )
        return [Image.fromBytes(data) if data else None for data in processed]

    # ── 作品卡片 ──────────────────────────────────────

    async def send_card(self, delivery: Delivery, work: WorkInfo) -> None:
        """发送单个作品的封面与信息。"""
        cover = (await self._cover_components([work]))[0]
        components = ([cover] if cover else []) + [Plain(self.card_text(work))]
        try:
            await delivery.chain(components)
        except Exception as exc:  # 封面被平台拒发时退回纯文本
            if cover is None:
                raise
            logger.warning(f"带封面的作品信息发送失败，改为纯文本：{exc}")
            await delivery.text(self.card_text(work))

    def _card_contents(self, works: list[WorkInfo], covers: list[Optional[Image]]) -> list[list]:
        return [([cover] if cover else []) + [Plain(self.card_text(work))] for work, cover in zip(works, covers, strict=True)]

    async def send_cards(self, delivery: Delivery, works: list[WorkInfo], header: str = "") -> None:
        """以合并转发发送多部作品的信息卡片。"""
        covers = await self._cover_components(works)
        contents: list[list] = [[Plain(header)]] if header else []
        contents += self._card_contents(works, covers)
        await delivery.forward(contents)

    async def send_grouped_cards(self, delivery: Delivery, title: str, results: list[SiteResult]) -> None:
        """按源站分组发送多部作品的信息卡片（每站随机）。"""
        summary = [
            (self._source_name(r.source), f"失败（{r.error}）" if r.error else
             f"{len(r.items)} 部" + (f"；{'；'.join(r.page.notes)}" if r.page and r.page.notes else ""))
            for r in results
        ]
        works = [work for r in results for work in r.items]
        covers = iter(await self._cover_components(works))
        sections = []
        for r in results:
            if r.items:
                site_covers = [next(covers) for _ in r.items]
                sections.append((self._source_name(r.source), self._card_contents(r.items, site_covers)))
        header = format_aggregate_header(title, 0, summary, "")
        await delivery.forward_sections([Plain(header)], sections, nested=self.options.nested_forward)

    # ── 列表 ──────────────────────────────────────────

    async def _grids(self, pages: list[tuple[list[WorkInfo], int]]) -> list[Optional[bytes]]:
        """为若干列表生成带序号的封面拼图；未开启拼图时全部为 None。"""
        if not (self.options.show_cover and self.options.search_cover_grid):
            return [None] * len(pages)
        covers = iter(await self.service.download_covers([work for items, _ in pages for work in items]))
        grouped = [([next(covers) for _ in items], start) for items, start in pages]

        def build() -> list[Optional[bytes]]:
            return [
                build_cover_grid(site_covers, self.options.cover_blur, start) if site_covers else None
                for site_covers, start in grouped
            ]

        return await asyncio.to_thread(build)

    def _shorts(self) -> dict[str, str]:
        return {source.key: source.short for source in self.sources}

    def _list_contents(
        self,
        page: WorkPage,
        grid: Optional[bytes],
        *,
        separate_entries: bool,
        start: int = 1,
        tips: bool = True,
    ) -> list[list]:
        """统一构造单站与聚合列表：转发逐条展示，普通消息按块留白。"""
        blocks = format_list_blocks(
            page, self._shorts(), self.options.command("get"), self.options.command("page"), start=start, tips=tips,
            detail_command=self.options.command("detail"), session_minutes=self.options.session_minutes,
        )
        covers = [[Image.fromBytes(grid)]] if grid else []
        if separate_entries:
            return [[Plain(blocks[0])]] + covers + [[Plain(block)] for block in blocks[1:]]
        return covers + [[Plain("\n\n".join(blocks))]]

    async def send_list(self, delivery: Delivery, page: WorkPage) -> None:
        """发送单站作品列表：标题、封面拼图、分条摘要与操作提示。"""
        grid = (await self._grids([(page.items, 1)]))[0]
        contents = self._list_contents(page, grid, separate_entries=delivery.supports_forward)
        if delivery.supports_forward:
            await delivery.forward(contents)
            return
        await delivery.send_contents(contents)

    async def send_aggregate_list(self, delivery: Delivery, title: str, page: int, results: list[SiteResult]) -> None:
        """发送聚合列表：总标题 + 每站一组（封面拼图 + 文本列表），序号在各站之间连续。

        Args:
            delivery: 投递目标
            title: 总标题
            page: 当前页码
            results: 各源站结果，顺序即序号顺序
        """
        starts, index = [], 1
        for r in results:
            starts.append(index)
            index += len(r.items)
        summary = []
        for r, start in zip(results, starts, strict=True):
            if r.error:
                state = f"失败（{r.error}）"
            elif not r.items:
                state = "无结果"
            else:
                state = f"{start}-{start + len(r.items) - 1}"
            summary.append((self._source_name(r.source), state))
        has_next = any(r.error or (r.page is not None and r.page.has_next) for r in results)
        tips = list_tips(
            self.options.command("get"), self.options.command("page"), page, has_next,
            detail_command=self.options.command("detail"), session_minutes=self.options.session_minutes,
        )
        header = format_aggregate_header(title, page, summary, tips)

        listed = [(r, start) for r, start in zip(results, starts, strict=True) if r.items]
        grids = await self._grids([(r.items, start) for r, start in listed])
        sections = []
        for (r, start), grid in zip(listed, grids, strict=True):
            contents = self._list_contents(
                r.page, grid, separate_entries=delivery.supports_forward, start=start, tips=False
            )
            sections.append((self._source_name(r.source), contents))
        await delivery.forward_sections([Plain(header)], sections, nested=self.options.nested_forward)

    # ── 获取作品 ──────────────────────────────────────

    async def send_fetch(self, delivery: Delivery, work: WorkInfo, *, quiet: bool = False) -> FetchResult:
        """下载作品并发送文件 / 视频 / 链接。

        Args:
            delivery: 投递目标
            work: 作品
            quiet: 为 True 时不发送进度提示（定时推送使用）

        Returns:
            获取结果

        Raises:
            CollectorError: 下载或打包失败
        """
        send_progress = self.options.send_progress and not quiet

        async def progress(text: str) -> None:
            if send_progress:
                await delivery.safe_text(text)

        if send_progress:
            kind = "视频" if work.media_type == MediaType.VIDEO else "本子"
            await delivery.safe_text(f"开始获取{kind}《{work.title}》，请稍候…")
        result = await self.service.fetch(work, progress)
        if result.link:
            await delivery.text(format_fetch_summary(result, self.get_hint(work)))
        elif send_progress or result.missing or result.truncated:
            await delivery.safe_text(format_fetch_summary(result, self.get_hint(work)))
        if result.video is not None:
            if self.options.video_as_file:
                await delivery.file(result.video)
            else:
                await delivery.video(result.video)
        for path in result.files:
            await delivery.file(path)
        return result

    @staticmethod
    def describe_error(exc: BaseException) -> str:
        """把异常转换为面向用户的说明。"""
        if isinstance(exc, CollectorError):
            return str(exc)
        return "处理失败，请稍后重新发送本次指令；仍失败时请联系管理员查看日志。"
