"""哔咔（PicACG）源站：作品详情、搜索、排行榜、随机与分话图片下载。"""

import asyncio
import random
import re
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from ....errors import WorkNotFound
from ....net import HttpClient
from ....types import ImageTask, Period, SearchQuery, SortKey, WorkInfo, WorkPage
from ...utils import iso_date, to_int
from ..base import BaseSource
from .client import PicaClient


SORT_PARAMS = {
    SortKey.DEFAULT: "dd",
    SortKey.LATEST: "dd",
    SortKey.LIKES: "ld",
    SortKey.POPULAR: "ld",
    SortKey.VIEWS: "vd",
}
LEADERBOARD_PARAMS = {Period.DAY: "H24", Period.WEEK: "D7", Period.MONTH: "D30"}


def media_url(media: dict) -> str:
    """拼接哔咔图片地址。"""
    server = str(media.get("fileServer") or "").rstrip("/")
    path = str(media.get("path") or "")
    if not server or not path:
        return ""
    return f"{server}/static/{path}"


class PicaSource(BaseSource):
    """哔咔源站，作品 ID 为 24 位十六进制字符串；哔咔没有公开网页，只能用 ID 引用。"""

    key = "pica"
    name = "哔咔"
    short = "PICA"
    aliases = ("pica", "哔咔", "bika", "picacg", "哔咔漫画", "picacomic")
    id_pattern = re.compile(r"[0-9a-f]{24}")
    ranking_periods = (Period.DAY, Period.WEEK, Period.MONTH)
    sort_options = (SortKey.LATEST, SortKey.LIKES, SortKey.VIEWS)
    search_hint = "关键词为普通文字；可用 排序=最新/点赞/观看、分类=哔咔分类名（如 全彩、长篇、同人）筛选"

    def __init__(
        self,
        http: HttpClient,
        email: str = "",
        password: str = "",
        image_quality: str = "original",
        state_file: Optional[Path] = None,
    ):
        super().__init__(http)
        self.client = PicaClient(http, email, password, image_quality, state_file)

    @property
    def unavailable_reason(self) -> str:
        return "" if self.client.configured else "未填写哔咔账号和密码"

    def work_url(self, work_id: str) -> str:
        return ""

    def _to_work(self, comic: dict, detailed: bool) -> WorkInfo:
        tags = [str(tag) for tag in comic.get("tags") or []]
        categories = [str(cat) for cat in comic.get("categories") or []]
        return WorkInfo(
            source=self.key,
            work_id=str(comic.get("_id")),
            title=str(comic.get("title") or "").strip(),
            cover_url=media_url(comic.get("thumb") or {}),
            author=str(comic.get("author") or "").strip(),
            category=", ".join(categories[:4]),
            tags=tags,
            pages=to_int(comic.get("pagesCount"), 0) or 0,
            likes=to_int(comic.get("totalLikes") if "totalLikes" in comic else comic.get("likesCount")),
            views=to_int(comic.get("totalViews") if "totalViews" in comic else comic.get("viewsCount")),
            uploaded=iso_date(comic.get("updated_at") or comic.get("created_at")),
            description=str(comic.get("description") or "").strip(),
            detailed=detailed,
            extra={"eps": max(1, to_int(comic.get("epsCount"), 1) or 1)},
        )

    def _page(self, comics: dict, page: int) -> WorkPage:
        docs = comics.get("docs") or []
        total_pages = to_int(comics.get("pages"))
        return WorkPage(
            items=[self._to_work(doc, False) for doc in docs if isinstance(doc, dict) and doc.get("_id")],
            page=page,
            total_pages=total_pages,
            has_next=bool(total_pages and page < total_pages),
        )

    # ── 查询能力 ──────────────────────────────────────

    async def get_work(self, work_id: str) -> WorkInfo:
        comic = (await self.client.request(f"comics/{work_id}")).get("comic")
        if not isinstance(comic, dict) or not comic.get("_id"):
            raise WorkNotFound(f"哔咔作品 {work_id} 不存在")
        return self._to_work(comic, True)

    async def search(self, query: SearchQuery) -> WorkPage:
        page = max(1, query.page)
        body: dict = {"keyword": query.keyword.strip(), "sort": SORT_PARAMS.get(query.sort, "dd")}
        if query.category.strip():
            body["categories"] = [query.category.strip()]
        if not body["keyword"] and query.category.strip():
            path = f"comics?page={page}&c={quote(query.category.strip())}&s={body['sort']}"
            data = await self.client.request(path)
        else:
            data = await self.client.request(f"comics/advanced-search?page={page}", body)
        result = self._page(data.get("comics") or {}, page)
        if query.exclude:
            lowered = [word.lower() for word in query.exclude]
            result.items = [
                work for work in result.items
                if not any(word in (work.title + " " + " ".join(work.tags)).lower() for word in lowered)
            ]
        return result

    async def ranking(self, period: Period, page: int = 1) -> WorkPage:
        data = await self.client.request(f"comics/leaderboard?tt={LEADERBOARD_PARAMS[period]}&ct=VC")
        comics = [c for c in data.get("comics") or [] if isinstance(c, dict) and c.get("_id")]
        return WorkPage(items=[self._to_work(c, False) for c in comics], page=1, total_pages=1)

    async def random_work(self, query: Optional[SearchQuery] = None) -> WorkInfo:
        if query is not None and not query.is_empty():
            return await super().random_work(query)
        data = await self.client.request("comics/random")
        comics = [c for c in data.get("comics") or [] if isinstance(c, dict) and c.get("_id")]
        if not comics:
            raise WorkNotFound("哔咔随机接口没有返回作品")
        return self._to_work(random.choice(comics), False)

    # ── 内容获取 ──────────────────────────────────────

    async def _episode_urls(self, work_id: str, order: int, max_pages: int = 0) -> list[str]:
        """读取一话图片列表，有上限时只请求仍需发现的分页。"""
        first = (await self.client.request(f"comics/{work_id}/order/{order}/pages?page=1")).get("pages") or {}
        total_pages = to_int(first.get("pages"), 1) or 1
        docs = list(first.get("docs") or [])
        if max_pages > 0:
            for page in range(2, total_pages + 1):
                if len(docs) >= max_pages:
                    break
                data = await self.client.request(f"comics/{work_id}/order/{order}/pages?page={page}")
                docs.extend((data.get("pages") or {}).get("docs") or [])
            docs = docs[:max_pages]
        else:
            rest = await asyncio.gather(
                *(
                    self.client.request(f"comics/{work_id}/order/{order}/pages?page={page}")
                    for page in range(2, total_pages + 1)
                )
            )
            for data in rest:
                docs.extend((data.get("pages") or {}).get("docs") or [])
        return [media_url(doc.get("media") or {}) if isinstance(doc, dict) else "" for doc in docs]

    async def list_images(self, work: WorkInfo, max_pages: int = 0) -> list[ImageTask]:
        if not work.detailed:
            work = await self.get_work(work.work_id)
        urls: list[str] = []
        for order in range(1, int(work.extra.get("eps", 1)) + 1):
            remaining = max_pages - len(urls) if max_pages > 0 else 0
            if max_pages > 0 and remaining <= 0:
                break
            urls.extend(await self._episode_urls(work.work_id, order, remaining))
        return [ImageTask(index=index, urls=[url]) for index, url in enumerate(urls, 1) if url]
