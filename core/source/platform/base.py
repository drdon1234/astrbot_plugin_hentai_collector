"""源站抽象基类：定义链接识别、详情、搜索、排行、随机与内容获取接口。"""

import random
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, ClassVar, Optional

from ...constants import Config
from ...downloader import ImageDownloader, VideoDownloader
from ...errors import UnsupportedOperation, WorkNotFound
from ...net import HttpClient
from ...types import (
    ImageTask,
    MediaBundle,
    MediaType,
    Period,
    SearchQuery,
    SortKey,
    WorkInfo,
    WorkPage,
)


ProgressCallback = Callable[[str], Awaitable[None]]


@dataclass
class FetchContext:
    """内容获取阶段由业务层注入的运行参数。

    Attributes:
        work_dir: 本次任务的工作目录（源站可在其中自由创建子目录）
        image_downloader: 共享的图片下载器
        video_downloader: 共享的视频下载器
        max_pages: 单本最多下载页数，0 表示不限制
        progress: 进度提示回调，发送面向用户的简短文本
    """

    work_dir: Path
    image_downloader: ImageDownloader
    video_downloader: VideoDownloader
    max_pages: int
    progress: ProgressCallback


class BaseSource(ABC):
    """所有源站的基类。

    子类必须声明 ``key`` / ``name`` / ``aliases`` 并实现 ``get_work``；
    其余能力按需覆盖，未覆盖的能力抛出 ``UnsupportedOperation``。
    """

    key: ClassVar[str] = ""
    name: ClassVar[str] = ""
    short: ClassVar[str] = ""
    aliases: ClassVar[tuple[str, ...]] = ()
    media_type: ClassVar[MediaType] = MediaType.COMIC
    link_patterns: ClassVar[tuple[re.Pattern, ...]] = ()
    id_pattern: ClassVar[Optional[re.Pattern]] = None
    ranking_periods: ClassVar[tuple[Period, ...]] = ()
    sort_options: ClassVar[tuple[SortKey, ...]] = ()
    supports_search: ClassVar[bool] = True
    supports_random: ClassVar[bool] = True
    search_hint: ClassVar[str] = ""

    def __init__(self, http: HttpClient):
        self.http = http

    # ── 状态与识别 ────────────────────────────────────

    @property
    def unavailable_reason(self) -> str:
        """非空时表示源站当前不可用（缺账号、缺依赖等），内容为原因说明。"""
        return ""

    def extract_ids(self, text: str) -> list[str]:
        """从任意消息文本中提取本源站的作品 ID（按出现顺序去重）。

        Args:
            text: 消息文本

        Returns:
            作品 ID 列表
        """
        found: list[tuple[int, str]] = []
        for pattern in self.link_patterns:
            for match in pattern.finditer(text or ""):
                work_id = self._id_from_match(match)
                if work_id:
                    found.append((match.start(), work_id))
        found.sort(key=lambda item: item[0])
        return list(dict.fromkeys(work_id for _, work_id in found))

    def _id_from_match(self, match: re.Match) -> Optional[str]:
        """把链接正则的匹配结果转换为作品 ID，默认取第一个分组。"""
        return match.group(1)

    def normalize_id(self, raw: str) -> Optional[str]:
        """校验用户显式输入的作品 ID（如 ``jm 123456`` 中的 ``123456``）。

        Args:
            raw: 用户输入

        Returns:
            规范化后的 ID，无法识别时返回 None
        """
        text = str(raw or "").strip()
        ids = self.extract_ids(text)
        if ids:
            return ids[0]
        if self.id_pattern is not None and self.id_pattern.fullmatch(text):
            return text
        return None

    @abstractmethod
    def work_url(self, work_id: str) -> str:
        """返回作品的网页地址；没有公开网页时返回空字符串。"""

    def resource_headers(self) -> dict[str, str]:
        """下载封面等站点资源时需要的请求头（如 Referer）。"""
        return {}

    # ── 查询能力 ──────────────────────────────────────

    @abstractmethod
    async def get_work(self, work_id: str) -> WorkInfo:
        """获取作品完整信息。

        Raises:
            WorkNotFound: 作品不存在
            SourceError: 请求或解析失败
        """

    async def search(self, query: SearchQuery) -> WorkPage:
        """按条件搜索作品。"""
        raise UnsupportedOperation(f"{self.name} 不支持搜索")

    async def ranking(self, period: Period, page: int = 1) -> WorkPage:
        """获取排行榜。"""
        raise UnsupportedOperation(f"{self.name} 没有排行榜")

    async def random_work(self, query: Optional[SearchQuery] = None) -> WorkInfo:
        """随机返回一部作品。

        默认实现：按条件搜索得到总页数，随机翻到某一页再随机挑一部；
        有专用随机接口的源站应覆盖本方法。

        Raises:
            WorkNotFound: 没有符合条件的作品
        """
        if not self.supports_search:
            raise UnsupportedOperation(f"{self.name} 不支持随机")
        query = query or SearchQuery()
        first = await self.search(query.with_page(1))
        if not first.items:
            raise WorkNotFound(f"{self.name} 没有符合条件的作品")
        total = max(1, min(first.total_pages or 1, Config.RANDOM_PAGE_LIMIT))
        page = random.randint(1, total)
        listing = first if page == 1 else await self.search(query.with_page(page))
        return random.choice(listing.items or first.items)

    # ── 内容获取 ──────────────────────────────────────

    async def list_images(self, work: WorkInfo) -> list[ImageTask]:
        """列出作品全部图片的下载任务（漫画类源站实现）。"""
        raise UnsupportedOperation(f"{self.name} 不支持图片下载")

    async def fetch(self, work: WorkInfo, ctx: FetchContext) -> MediaBundle:
        """下载作品内容。

        默认实现面向漫画：``list_images`` 后交给共享图片下载器；
        需要特殊下载流程的源站（JM 解码、视频）覆盖本方法。
        """
        tasks = await self.list_images(work)
        if not tasks:
            raise WorkNotFound(f"{self.name} 作品没有可下载的图片")
        truncated = 0
        if ctx.max_pages > 0 and len(tasks) > ctx.max_pages:
            truncated = len(tasks) - ctx.max_pages
            tasks = tasks[: ctx.max_pages]

        async def report(done: int, total: int) -> None:
            await ctx.progress(f"下载进度 {done}/{total}")

        images, missing = await ctx.image_downloader.download_all(
            tasks, ctx.work_dir / "images", self.http, report
        )
        return MediaBundle(work=work, images=images, missing=missing, truncated=truncated)

    async def close(self) -> None:
        """释放网络资源。"""
        await self.http.close()
