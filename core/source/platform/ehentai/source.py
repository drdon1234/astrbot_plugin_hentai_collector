"""E-Hentai / ExHentai 源站：画廊链接解析、搜索、排行榜、随机与逐页下载。"""

import math
import random
import re
from collections import OrderedDict
from typing import Optional

from ....logger import logger

from ....errors import WorkNotFound
from ....net import HttpClient
from ....types import ImageTask, Period, SearchQuery, SortKey, WorkInfo, WorkPage
from ..base import BaseSource
from .api import (
    ALL_CATEGORIES,
    CATEGORY_BITS,
    EX_REQUIRED_COOKIES,
    SEARCH_PAGE_SIZE,
    EHentaiApi,
    meta_fields,
    resolve_category,
)


MAX_CURSOR_WALK = 20
CURSOR_CACHE_SIZE = 256


def resolve_site(site: str, cookies: dict[str, str]) -> str:
    """ExHentai 缺少必需 Cookie 时回退表站。"""
    if site != "exhentai":
        return "e-hentai"
    missing = [key for key in EX_REQUIRED_COOKIES if not cookies.get(key)]
    if missing:
        logger.warning(f"使用 ExHentai 需要 Cookie {', '.join(missing)}，已改用 E-Hentai")
        return "e-hentai"
    return "exhentai"


class EHentaiSource(BaseSource):
    """E-Hentai 画廊源站。作品 ID 形如 ``{gid}_{token}``。"""

    key = "ehentai"
    name = "E-Hentai"
    short = "EH"
    aliases = ("eh", "e站", "ehentai", "e-hentai", "ex", "exhentai", "e绅士")
    link_patterns = (re.compile(r"https?://(?:e-hentai|exhentai)\.org/g/(\d+)/([0-9a-f]{10})"),)
    ranking_periods = (Period.DAY, Period.MONTH, Period.YEAR, Period.ALL)
    sort_options = (SortKey.LATEST,)
    search_hint = "支持 E-Hentai 标签语法，如 female:\"big breasts\"$；可用 评分=4、页数=20、分类=同人志 筛选"

    def __init__(self, http: HttpClient, site: str = "e-hentai", request_interval: float = 0.5):
        super().__init__(http)
        self.api = EHentaiApi(http, site, request_interval)
        # (搜索参数, 页码) → 该页的 next 游标；E-Hentai 列表只能按游标翻页
        self._cursors: OrderedDict = OrderedDict()

    def _id_from_match(self, match: re.Match) -> Optional[str]:
        return f"{match.group(1)}_{match.group(2)}"

    def normalize_id(self, raw: str) -> Optional[str]:
        text = str(raw or "").strip()
        ids = self.extract_ids(text)
        if ids:
            return ids[0]
        match = re.fullmatch(r"(\d+)[_/ ]([0-9a-f]{10})", text)
        return f"{match.group(1)}_{match.group(2)}" if match else None

    @staticmethod
    def _split_id(work_id: str) -> tuple[int, str]:
        gid, _, token = work_id.partition("_")
        return int(gid), token

    def work_url(self, work_id: str) -> str:
        gid, token = self._split_id(work_id)
        return self.api.gallery_url(gid, token)

    def resource_headers(self) -> dict[str, str]:
        return {"Referer": self.api.base + "/"}

    def _to_work(self, meta: dict) -> WorkInfo:
        fields = meta_fields(meta)
        work_id = f"{fields['extra']['gid']}_{fields['extra']['token']}"
        return WorkInfo(
            source=self.key,
            work_id=work_id,
            url=self.work_url(work_id),
            detailed=True,
            **fields,
        )

    async def _works(self, pairs: list[tuple[int, str]]) -> list[WorkInfo]:
        return [self._to_work(meta) for meta in await self.api.gdata(pairs)]

    # ── 查询能力 ──────────────────────────────────────

    async def get_work(self, work_id: str) -> WorkInfo:
        works = await self._works([self._split_id(work_id)])
        if not works:
            raise WorkNotFound(f"E-Hentai 画廊 {work_id} 不存在或已被删除")
        return works[0]

    @staticmethod
    def _search_params(query: SearchQuery) -> dict[str, str]:
        words = [query.keyword.strip()]
        words.extend(f'-"{word}"' if " " in word else f"-{word}" for word in query.exclude)
        params: dict[str, str] = {}
        search = " ".join(word for word in words if word)
        if search:
            params["f_search"] = search
        category = resolve_category(query.category)
        if category:
            params["f_cats"] = str(ALL_CATEGORIES & ~CATEGORY_BITS[category])
        if query.min_rating >= 2:
            params.update(advsearch="1", f_sr="on", f_srdd=str(min(query.min_rating, 5)))
        if query.min_pages > 1:
            params.update(advsearch="1", f_spf=str(query.min_pages))
        return params

    def _remember_cursor(self, key: tuple, page: int, cursor: Optional[int]) -> None:
        if cursor is None:
            return
        self._cursors[(key, page)] = cursor
        self._cursors.move_to_end((key, page))
        while len(self._cursors) > CURSOR_CACHE_SIZE:
            self._cursors.popitem(last=False)

    async def search(self, query: SearchQuery) -> WorkPage:
        params = self._search_params(query)
        key = tuple(sorted(params.items()))
        target = max(1, query.page)
        known = max(
            [page for (cached_key, page) in self._cursors if cached_key == key and page < target] or [0]
        )
        if target - known > MAX_CURSOR_WALK:
            raise WorkNotFound(f"E-Hentai 只能逐页翻页，一次最多向后跳 {MAX_CURSOR_WALK} 页")
        page, listing = known, None
        while page < target:
            page += 1
            cursor = self._cursors.get((key, page - 1)) if page > 1 else None
            if page > 1 and cursor is None:
                listing = None
                break
            request = dict(params)
            if cursor is not None:
                request["next"] = str(cursor)
            listing = await self.api.listing(request)
            self._remember_cursor(key, page, listing.next_cursor)
            if listing.next_cursor is None and page < target:
                listing = None
                break
        if listing is None or not listing.pairs:
            return WorkPage(items=[], page=target)
        total_pages = math.ceil(listing.total / SEARCH_PAGE_SIZE) if listing.total else None
        return WorkPage(
            items=await self._works(listing.pairs),
            page=target,
            total_pages=total_pages,
            has_next=listing.next_cursor is not None,
        )

    async def ranking(self, period: Period, page: int = 1) -> WorkPage:
        listing = await self.api.toplist(period.value, page)
        return WorkPage(
            items=await self._works(listing.pairs),
            page=page,
            total_pages=None,
            has_next=bool(listing.pairs),
        )

    async def random_work(self, query: Optional[SearchQuery] = None) -> WorkInfo:
        params = self._search_params(query or SearchQuery())
        pairs = await self.api.random_listing(params)
        if not pairs:
            raise WorkNotFound("E-Hentai 没有符合条件的画廊")
        random.shuffle(pairs)
        works = await self._works(pairs[:5])
        candidates = [work for work in works if not work.extra.get("expunged")] or works
        if not candidates:
            raise WorkNotFound("E-Hentai 没有符合条件的画廊")
        return candidates[0]

    # ── 内容获取 ──────────────────────────────────────

    async def list_images(self, work: WorkInfo) -> list[ImageTask]:
        gid, token = self._split_id(work.work_id)
        page_urls = await self.api.page_links(gid, token, work.pages or 1)
        headers = self.resource_headers()

        def resolver(url: str):
            async def resolve() -> list[str]:
                return await self.api.image_url(url)

            return resolve

        return [
            ImageTask(index=index, headers=headers, resolve=resolver(url))
            for index, url in enumerate(page_urls, 1)
        ]
