"""展示编排：把业务结果（作品、列表、获取结果）渲染为消息并经 Delivery 发送，指令与推送共用。"""

import asyncio
from dataclasses import dataclass
from typing import Optional

from astrbot.api.message_components import Image, Plain

from ..logger import logger

from ..errors import CollectorError
from ..service import WorkService
from ..source import SourceManager
from ..types import FetchResult, MediaType, WorkInfo, WorkPage
from .cover_grid import build_cover_grid, process_cover
from .formatter import format_fetch_summary, format_work_card, format_work_list
from .sender import Delivery


@dataclass(frozen=True)
class PresenterOptions:
    """展示相关配置。"""

    show_cover: bool = True
    cover_blur: int = 0
    search_cover_grid: bool = True
    max_tags: int = 12
    show_description: bool = True
    video_as_file: bool = False
    send_progress: bool = True
    get_command: str = "看本"
    page_command: str = "本子翻页"


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

    def card_text(self, work: WorkInfo) -> str:
        """作品信息卡片文本。"""
        return format_work_card(
            work,
            self._source_name(work.source),
            alias=self._alias(work.source),
            max_tags=self.options.max_tags,
            show_description=self.options.show_description,
            get_command=self.options.get_command,
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

    async def send_cards(self, delivery: Delivery, works: list[WorkInfo], header: str = "") -> None:
        """以合并转发发送多部作品的信息卡片。"""
        covers = await self._cover_components(works)
        contents: list[list] = [[Plain(header)]] if header else []
        for work, cover in zip(works, covers, strict=True):
            contents.append(([cover] if cover else []) + [Plain(self.card_text(work))])
        await delivery.forward(contents)

    # ── 列表 ──────────────────────────────────────────

    async def send_list(self, delivery: Delivery, page: WorkPage) -> None:
        """发送作品列表：带序号的封面拼图 + 文本列表。"""
        shorts = {source.key: source.short for source in self.sources}
        text = format_work_list(page, shorts, self.options.get_command, self.options.page_command)
        grid = None
        if self.options.show_cover and self.options.search_cover_grid and page.items:
            covers = await self.service.download_covers(page.items)
            grid = await asyncio.to_thread(build_cover_grid, covers, self.options.cover_blur)
        contents: list[list] = []
        if grid:
            contents.append([Image.fromBytes(grid)])
        contents.append([Plain(text)])
        if delivery.supports_forward:
            await delivery.forward(contents)
            return
        for content in contents:
            await delivery.chain(content)

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
        if send_progress or result.link:
            await delivery.safe_text(format_fetch_summary(result))
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
        return f"处理失败，请查看日志：{exc!r}"[:300]
