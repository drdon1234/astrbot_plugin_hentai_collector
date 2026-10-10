"""作品业务编排：详情、单站与聚合的搜索 / 排行 / 随机、“下载→打包→缓存复用”，不依赖 AstrBot 运行时。"""

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from ..logger import logger

from ..constants import Config
from ..downloader import ImageDownloader, VideoDownloader, detect_image_suffix
from ..errors import CollectorError, SourceUnavailable, WorkNotFound
from ..packager import Packager, safe_filename, validate_images
from ..source import FetchContext, SourceManager
from ..storage import JobStore
from ..types import PERIOD_LABELS, FetchResult, MediaType, Period, SearchQuery, WorkInfo, WorkPage, WorkRef


ProgressCallback = Callable[[str], Awaitable[None]]
IN_PROGRESS_RETENTION = 6 * 3600


async def _silent(_: str) -> None:
    return None


@dataclass
class SiteResult:
    """聚合操作中单个源站的结果：成功时有 ``page``，失败时有 ``error``。"""

    source: str
    page: Optional[WorkPage] = None
    error: str = ""

    @property
    def items(self) -> list[WorkInfo]:
        """该源站返回的作品（失败时为空）。"""
        return self.page.items if self.page else []


def default_period(source) -> Period:
    """源站默认榜单：支持周榜时用周榜，否则用其第一个榜单。"""
    return Period.WEEK if Period.WEEK in source.ranking_periods else source.ranking_periods[0]


class WorkService:
    """面向指令与推送的统一业务入口。"""

    def __init__(
        self,
        sources: SourceManager,
        job_store: JobStore,
        packager: Packager,
        image_downloader: ImageDownloader,
        video_downloader: VideoDownloader,
        *,
        max_pages: int,
        max_concurrent_jobs: int,
        retention_seconds: int,
        cache_available: bool,
        cache_error: str = "",
    ):
        self.sources = sources
        self.job_store = job_store
        self.packager = packager
        self.image_downloader = image_downloader
        self.video_downloader = video_downloader
        self.max_pages = max_pages
        self.retention_seconds = retention_seconds
        self.cache_available = cache_available
        self.cache_error = cache_error
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent_jobs))
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_users: dict[str, int] = {}

    # ── 查询 ──────────────────────────────────────────

    async def get_work(self, ref: WorkRef) -> WorkInfo:
        """获取作品完整信息。"""
        return await self.sources.require(ref.source).get_work(ref.work_id)

    async def ensure_detail(self, work: WorkInfo) -> WorkInfo:
        """列表条目字段不全时补全详情；补全失败保留原条目。"""
        if work.detailed:
            return work
        try:
            return await self.get_work(work.ref)
        except CollectorError as exc:
            logger.debug(f"补全作品详情失败 {work.ref}: {exc}")
            return work

    async def search(self, source_key: str, query: SearchQuery) -> WorkPage:
        """搜索作品。"""
        source = self.sources.require(source_key)
        if not source.supports_search:
            raise SourceUnavailable(f"{source.name} 不支持搜索")
        page = await source.search(query)
        page.title = f"{source.name} 搜索：{query.describe()}"
        return page

    async def ranking(self, source_key: str, period: Period, page: int = 1) -> WorkPage:
        """获取排行榜。"""
        source = self.sources.require(source_key)
        self.sources.check_period(source, period)
        result = await source.ranking(period, page)
        result.title = f"{source.name} {PERIOD_LABELS[period]}"
        return result

    async def random_works(
        self,
        source_key: Optional[str],
        query: Optional[SearchQuery],
        count: int,
        media_type: Optional[MediaType] = None,
    ) -> list[WorkInfo]:
        """随机若干部互不重复的作品；部分失败时返回已取得的作品。

        Raises:
            CollectorError: 一部都没有取得时抛出最后一次的失败
        """
        works: list[WorkInfo] = []
        for _ in range(count * 2):
            if len(works) >= count:
                break
            try:
                work = await self.random_work(source_key, query, media_type)
            except CollectorError as exc:
                if not works:
                    raise
                logger.warning(f"随机作品失败：{exc}")
                continue
            if all(work.ref != existing.ref for existing in works):
                works.append(work)
        return works

    async def random_work(
        self,
        source_key: Optional[str],
        query: Optional[SearchQuery] = None,
        media_type: Optional[MediaType] = None,
    ) -> WorkInfo:
        """随机作品；``source_key`` 为空时从全随机候选池挑选源站。"""
        if source_key:
            source = self.sources.require(source_key)
        else:
            source = self.sources.pick_random_source(media_type)
        work = await source.random_work(query if query and not query.is_empty() else None)
        return await self.ensure_detail(work)

    # ── 聚合 ──────────────────────────────────────────

    async def _gather(
        self, keys: list[str], call: Callable[[str], Awaitable[WorkPage]]
    ) -> list[SiteResult]:
        """并发执行各源站请求，单站失败或超时只记录在该站结果中。"""

        async def one(key: str) -> SiteResult:
            try:
                page = await asyncio.wait_for(call(key), Config.AGGREGATE_TIMEOUT)
                return SiteResult(key, page=page)
            except asyncio.TimeoutError:
                logger.warning(f"聚合请求 {key} 超时")
                return SiteResult(key, error="请求超时")
            except CollectorError as exc:
                logger.debug(f"聚合请求 {key} 失败：{exc}")
                return SiteResult(key, error=str(exc))

        return list(await asyncio.gather(*(one(key) for key in keys)))

    async def search_many(self, keys: list[str], query: SearchQuery) -> list[SiteResult]:
        """在多个源站并发搜索。"""
        return await self._gather(keys, lambda key: self.search(key, query))

    async def ranking_many(self, keys: list[str], period: Optional[Period], page: int = 1) -> list[SiteResult]:
        """并发获取多个源站的排行榜；``period`` 为空时各站使用默认榜单。"""

        async def call(key: str) -> WorkPage:
            return await self.ranking(key, period or default_period(self.sources.require(key)), page)

        return await self._gather(keys, call)

    async def random_many(self, keys: list[str], query: Optional[SearchQuery], count: int) -> list[SiteResult]:
        """每个源站各随机 ``count`` 部作品。"""

        async def call(key: str) -> WorkPage:
            works = await self.random_works(key, query, count)
            return WorkPage(items=works, title=f"{self.sources.require(key).name} 随机")

        return await self._gather(keys, call)

    # ── 获取作品内容 ──────────────────────────────────

    def _acquire_lock(self, key: str) -> asyncio.Lock:
        self._lock_users[key] = self._lock_users.get(key, 0) + 1
        return self._locks.setdefault(key, asyncio.Lock())

    def _release_lock(self, key: str) -> None:
        users = self._lock_users.get(key, 1) - 1
        if users <= 0:
            self._lock_users.pop(key, None)
            self._locks.pop(key, None)
        else:
            self._lock_users[key] = users

    async def fetch(self, work: WorkInfo, progress: Optional[ProgressCallback] = None) -> FetchResult:
        """下载作品并生成可发送的文件；保留期内重复请求直接复用已生成文件。

        同一作品串行处理，全局并发受“同时处理作品数”限制。

        Args:
            work: 作品
            progress: 进度提示回调

        Returns:
            获取结果

        Raises:
            CollectorError: 缓存目录不可用、下载或打包失败
        """
        if not self.cache_available:
            raise SourceUnavailable(
                "缓存目录不可用，无法下载文件。请检查“缓存设置”，"
                f"Docker 部署需填写 AstrBot 与消息平台共同挂载的路径。{self.cache_error}"
            )
        progress = progress or _silent
        source = self.sources.require(work.source)
        key = str(work.ref)
        lock = self._acquire_lock(key)
        try:
            async with lock:
                cached = self._cached_result(work)
                if cached is not None:
                    return cached
                async with self._semaphore:
                    return await self._fetch_fresh(source, work, progress)
        finally:
            self._release_lock(key)

    def _cached_result(self, work: WorkInfo) -> Optional[FetchResult]:
        signature = self._signature(work)
        manifest = self.job_store.find_outputs(work.source, work.work_id, signature)
        if manifest is None:
            return None
        files = manifest["files"]
        if work.media_type == MediaType.VIDEO:
            return FetchResult(work=work, video=files[0], from_cache=True)
        return FetchResult(
            work=work,
            files=files,
            missing=int(manifest.get("missing") or 0),
            page_count=int(manifest.get("page_count") or 0),
            from_cache=True,
        )

    def _signature(self, work: WorkInfo) -> str:
        if work.media_type == MediaType.VIDEO:
            source = self.sources.get(work.source)
            return f"video|{getattr(source, 'quality', '')}"
        return f"{self.packager.options.signature}|{self.max_pages}"

    async def _fetch_fresh(self, source, work: WorkInfo, progress: ProgressCallback) -> FetchResult:
        job = self.job_store.open_job(work.source, work.work_id)
        self.job_store.mark_expire(job, IN_PROGRESS_RETENTION)
        if not work.detailed:
            work = await self.ensure_detail(work)
        succeeded = False
        try:
            ctx = FetchContext(
                work_dir=job,
                image_downloader=self.image_downloader,
                video_downloader=self.video_downloader,
                max_pages=self.max_pages,
                progress=progress,
            )
            bundle = await source.fetch(work, ctx)
            if bundle.link:
                return FetchResult(work=work, link=bundle.link)
            if bundle.video is not None:
                self.job_store.save_outputs(job, [bundle.video], self._signature(work), self.retention_seconds)
                succeeded = True
                return FetchResult(work=work, video=bundle.video)
            if not bundle.images:
                raise WorkNotFound("没有下载到任何图片")
            await progress(f"已下载 {len(bundle.images)} 页，正在生成文件…")
            files, broken = await self._package(work, bundle.images, job)
            missing = bundle.missing + broken
            self.job_store.save_outputs(
                job,
                files,
                self._signature(work),
                self.retention_seconds,
                {"missing": missing, "page_count": len(bundle.images) - broken},
            )
            succeeded = True
            return FetchResult(work=work, files=files, missing=missing, page_count=len(bundle.images) - broken)
        finally:
            if not succeeded:
                self.job_store.mark_expire(job, Config.MIN_FILE_RETENTION_SECONDS)
            self.job_store.remove_dir(job / "images")
            self.job_store.remove_dir(job / "jm")

    async def _package(self, work: WorkInfo, images: list, job) -> tuple[list, int]:
        source = self.sources.get(work.source)
        short = getattr(source, "short", work.source.upper())
        suffix = f" [{short}{work.work_id}]"
        title = safe_filename(work.title, "", Config.MAX_FILENAME_LENGTH - len(suffix))
        stem = safe_filename(f"{title}{suffix}", f"{short}{work.work_id}")
        manifest = {
            "source": work.source,
            "id": work.work_id,
            "title": work.title,
            "url": work.url,
            "author": work.author,
            "tags": work.tags,
        }

        def run() -> tuple[list, int]:
            valid, broken = validate_images(images)
            return self.packager.package(valid, job / "output", stem, manifest), broken

        return await asyncio.to_thread(run)

    # ── 封面 ──────────────────────────────────────────

    async def download_covers(self, works: list[WorkInfo]) -> list[Optional[bytes]]:
        """并发下载封面（每个源站独立限流，聚合列表中各站并行），失败的位置返回 None。

        网络错误、5xx 与返回内容不是图片时重试，明确的 4xx 不重试；
        失败汇总记录 warning，便于排查拼图中的灰色占位。
        """
        semaphores = {work.source: asyncio.Semaphore(Config.COVER_DOWNLOAD_CONCURRENCY) for work in works}
        failures: list[str] = []

        async def fetch(source, url: str) -> bytes:
            last_error = ""
            for attempt in range(1 + Config.COVER_RETRIES):
                try:
                    resp = await source.http.request(
                        "GET", url, headers=source.resource_headers(), retries=0, timeout=Config.COVER_TIMEOUT
                    )
                    if detect_image_suffix(resp.content[:16]):
                        return resp.content
                    last_error = f"返回内容不是图片（{len(resp.content)} 字节）"
                except CollectorError as exc:
                    last_error = str(exc)
                    if getattr(exc, "status", None) in (400, 401, 403, 404, 410, 422):
                        break
                if attempt < Config.COVER_RETRIES:
                    await asyncio.sleep(Config.RETRY_BASE_DELAY * (attempt + 1))
            raise CollectorError(last_error)

        async def one(work: WorkInfo) -> Optional[bytes]:
            source = self.sources.get(work.source)
            if not work.cover_url or source is None:
                return None
            async with semaphores[work.source]:
                try:
                    return await fetch(source, work.cover_url)
                except CollectorError as exc:
                    failures.append(f"{work.source}:{work.work_id}（{exc}）")
                    return None

        covers = list(await asyncio.gather(*(one(work) for work in works)))
        if failures:
            logger.warning(f"{len(failures)}/{len(works)} 张封面下载失败，以灰色占位：{'；'.join(failures[:5])}")
        return covers
