"""nhentai 源站：基于官方 v2 匿名接口的详情、搜索、热门排行、随机与图片下载。"""

import re
from typing import Optional

from ...logger import logger

from ...errors import HttpError, SourceError, WorkNotFound
from ...net import HttpClient
from ...types import ImageTask, Period, SearchQuery, SortKey, WorkInfo, WorkPage
from ..utils import format_timestamp, to_int
from .base import BaseSource


API_BASE = "https://nhentai.net/api/v2"
SITE_BASE = "https://nhentai.net"
DEFAULT_IMAGE_SERVERS = ("https://i1.nhentai.net", "https://i2.nhentai.net", "https://i3.nhentai.net")
DEFAULT_THUMB_SERVERS = ("https://t1.nhentai.net", "https://t2.nhentai.net", "https://t3.nhentai.net")
SORT_PARAMS = {
    SortKey.DEFAULT: "date",
    SortKey.LATEST: "date",
    SortKey.POPULAR: "popular",
    SortKey.LIKES: "popular",
}
PERIOD_SORTS = {
    Period.DAY: "popular-today",
    Period.WEEK: "popular-week",
    Period.MONTH: "popular-month",
    Period.ALL: "popular",
}
LANGUAGE_FILTERS = {"不限": "", "中文": "chinese", "英文": "english", "日文": "japanese"}
AUTHOR_TAG_TYPES = ("artist", "group")


class NHentaiSource(BaseSource):
    """nhentai 源站，作品 ID 为数字编号。"""

    key = "nhentai"
    name = "nhentai"
    short = "NH"
    aliases = ("nh", "n站", "nhentai")
    link_patterns = (re.compile(r"nhentai\.(?:net|to|xxx|website)/g/(\d+)"),)
    id_pattern = re.compile(r"\d{1,7}")
    ranking_periods = (Period.DAY, Period.WEEK, Period.MONTH, Period.ALL)
    sort_options = (SortKey.LATEST, SortKey.POPULAR)
    supports_min_pages = True
    search_hint = "支持 nhentai 搜索语法，如 tag:\"big breasts\"、-yaoi；可用 页数=20 筛选"

    def __init__(self, http: HttpClient, language: str = ""):
        super().__init__(http)
        self.language = LANGUAGE_FILTERS.get(language, language or "")
        self._image_servers: tuple[str, ...] = ()
        self._thumb_servers: tuple[str, ...] = ()

    def work_url(self, work_id: str) -> str:
        return f"{SITE_BASE}/g/{work_id}/"

    def resource_headers(self) -> dict[str, str]:
        return {"Referer": SITE_BASE + "/"}

    async def _servers(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """读取图片 / 缩略图服务器列表，失败时使用内置默认值。"""
        if not self._image_servers:
            try:
                data = await self.http.get_json(f"{API_BASE}/cdn", retries=1)
                self._image_servers = tuple(data.get("image_servers") or ()) or DEFAULT_IMAGE_SERVERS
                self._thumb_servers = tuple(data.get("thumb_servers") or ()) or DEFAULT_THUMB_SERVERS
            except (HttpError, AttributeError) as exc:
                logger.debug(f"nhentai CDN 配置读取失败，使用默认服务器：{exc}")
                self._image_servers, self._thumb_servers = DEFAULT_IMAGE_SERVERS, DEFAULT_THUMB_SERVERS
        return self._image_servers, self._thumb_servers

    async def _api(self, path: str, params: Optional[dict] = None):
        try:
            return await self.http.get_json(f"{API_BASE}{path}", params=params)
        except HttpError as exc:
            if exc.status == 404:
                raise WorkNotFound("nhentai 作品不存在或已被删除") from exc
            raise

    # ── 数据转换 ──────────────────────────────────────

    async def _detail_to_work(self, data: dict) -> WorkInfo:
        _, thumbs = await self._servers()
        title = data.get("title") or {}
        tags = data.get("tags") or []
        by_type: dict[str, list[str]] = {}
        for tag in tags:
            if isinstance(tag, dict):
                by_type.setdefault(str(tag.get("type")), []).append(str(tag.get("name")))
        authors = [name for kind in AUTHOR_TAG_TYPES for name in by_type.get(kind, [])]
        languages = [lang for lang in by_type.get("language", []) if lang != "translated"]
        cover = (data.get("cover") or data.get("thumbnail") or {}).get("path", "")
        work_id = str(data.get("id"))
        return WorkInfo(
            source=self.key,
            work_id=work_id,
            title=title.get("english") or title.get("pretty") or title.get("japanese") or work_id,
            subtitle=title.get("japanese") or "",
            url=self.work_url(work_id),
            cover_url=f"{thumbs[0]}/{cover}" if cover else "",
            author=", ".join(authors[:3]),
            category=", ".join(by_type.get("category", [])),
            language=", ".join(languages),
            tags=[f"{kind}:{name}" for kind in ("parody", "character", "tag") for name in by_type.get(kind, [])],
            pages=to_int(data.get("num_pages"), 0) or 0,
            likes=to_int(data.get("num_favorites")),
            uploaded=format_timestamp(data.get("upload_date")),
            detailed=True,
            extra={
                "media_id": str(data.get("media_id") or ""),
                "pages": [page.get("path") for page in data.get("pages") or [] if isinstance(page, dict)],
            },
        )

    async def _summary_to_work(self, item: dict) -> WorkInfo:
        _, thumbs = await self._servers()
        work_id = str(item.get("id"))
        thumb = item.get("thumbnail") or ""
        return WorkInfo(
            source=self.key,
            work_id=work_id,
            title=item.get("english_title") or item.get("japanese_title") or work_id,
            subtitle=item.get("japanese_title") or "",
            url=self.work_url(work_id),
            cover_url=f"{thumbs[0]}/{thumb}" if thumb else "",
            pages=to_int(item.get("num_pages"), 0) or 0,
            likes=to_int(item.get("num_favorites")),
            detailed=False,
        )

    async def _page_from_result(self, data: dict, page: int) -> WorkPage:
        items = [await self._summary_to_work(item) for item in data.get("result") or [] if isinstance(item, dict)]
        total_pages = to_int(data.get("num_pages"))
        return WorkPage(
            items=items,
            page=page,
            total_pages=total_pages,
            has_next=bool(total_pages and page < total_pages),
        )

    def _build_query(self, query: SearchQuery) -> str:
        words = [query.keyword.strip()]
        words.extend(f'-"{word}"' if " " in word else f"-{word}" for word in query.exclude)
        if query.min_pages > 0:
            words.append(f"pages:>={query.min_pages}")
        if query.category.strip():
            words.append(f"category:{query.category.strip()}")
        if self.language and "language:" not in query.keyword:
            words.append(f"language:{self.language}")
        text = " ".join(word for word in words if word)
        return text or "*"

    # ── 查询能力 ──────────────────────────────────────

    async def get_work(self, work_id: str) -> WorkInfo:
        data = await self._api(f"/galleries/{work_id}")
        if not isinstance(data, dict) or not data.get("id"):
            raise WorkNotFound(f"nhentai 作品 {work_id} 不存在")
        return await self._detail_to_work(data)

    async def search(self, query: SearchQuery) -> WorkPage:
        params = {
            "query": self._build_query(query),
            "sort": SORT_PARAMS.get(query.sort, "date"),
            "page": max(1, query.page),
        }
        return await self._page_from_result(await self._api("/search", params), params["page"])

    async def ranking(self, period: Period, page: int = 1) -> WorkPage:
        params = {
            "query": f"language:{self.language}" if self.language else "*",
            "sort": PERIOD_SORTS[period],
            "page": max(1, page),
        }
        return await self._page_from_result(await self._api("/search", params), params["page"])

    async def random_work(self, query: Optional[SearchQuery] = None) -> WorkInfo:
        if query is None or query.is_empty():
            if not self.language:
                data = await self._api("/galleries/random")
                work_id = str((data or {}).get("id") or "")
                if not work_id:
                    raise SourceError("nhentai 随机接口没有返回作品")
                return await self.get_work(work_id)
            query = SearchQuery()
        return await super().random_work(query)

    # ── 内容获取 ──────────────────────────────────────

    async def list_images(self, work: WorkInfo, max_pages: int = 0) -> list[ImageTask]:
        if not work.extra.get("pages"):
            work = await self.get_work(work.work_id)
        servers, _ = await self._servers()
        headers = self.resource_headers()
        tasks = []
        paths = work.extra.get("pages") or []
        if max_pages > 0:
            paths = paths[:max_pages]
        for index, path in enumerate(paths, 1):
            if not path:
                continue
            rotated = servers[index % len(servers):] + servers[: index % len(servers)]
            tasks.append(ImageTask(index=index, urls=[f"{server}/{path}" for server in rotated], headers=headers))
        return tasks
