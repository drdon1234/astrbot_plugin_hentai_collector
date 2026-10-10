"""Hanime1 里番源站：播放页解析、搜索、排行榜、随机与视频下载。"""

import re
from typing import Optional

from ....errors import HttpError, WorkNotFound
from ....net import HttpClient
from ....types import MediaBundle, MediaType, Period, SearchQuery, SortKey, WorkInfo, WorkPage
from ..base import BaseSource, FetchContext
from .page import VideoCard, parse_search, parse_sources, parse_watch


SITE_BASE = "https://hanime1.me"
SORT_PARAMS = {
    SortKey.DEFAULT: "",
    SortKey.LATEST: "最新上市",
    SortKey.POPULAR: "本週排行",
    SortKey.VIEWS: "觀看次數",
    SortKey.LIKES: "讚好比例",
}
PERIOD_SORTS = {
    Period.DAY: "本日排行",
    Period.WEEK: "本週排行",
    Period.MONTH: "本月排行",
    Period.ALL: "觀看次數",
}
GENRES = ("全部", "裏番", "泡麵番", "Motion Anime", "3DCG", "2.5D", "2D動畫", "AI生成", "MMD", "Cosplay")
GENRE_ALIASES = {"里番": "裏番", "泡面番": "泡麵番", "2d动画": "2D動畫", "ai": "AI生成", "cos": "Cosplay"}
SEND_MODE_LINK = "仅链接"


def resolve_genre(name: str, default: str) -> str:
    """把用户输入的类型名解析为站点 genre 参数，“全部”返回空字符串。"""
    text = str(name or "").strip() or default
    text = GENRE_ALIASES.get(text.lower(), text)
    for genre in GENRES:
        if genre.lower() == text.lower():
            return "" if genre == "全部" else genre
    return text


class Hanime1Source(BaseSource):
    """Hanime1 源站，作品 ID 为播放页 ``v`` 参数。"""

    key = "hanime1"
    name = "Hanime1"
    short = "H1"
    aliases = ("hanime1", "hanime", "h1", "里番", "裏番", "hamine1", "hanime1.me")
    media_type = MediaType.VIDEO
    link_patterns = (re.compile(r"hanime1\.(?:me|com)/watch\?v=(\d+)"),)
    id_pattern = re.compile(r"\d{1,8}")
    ranking_periods = (Period.DAY, Period.WEEK, Period.MONTH, Period.ALL)
    sort_options = (SortKey.LATEST, SortKey.POPULAR, SortKey.VIEWS, SortKey.LIKES)
    search_hint = "关键词为作品名或标签；可用 分类=裏番/泡麵番/3DCG/Motion Anime、排序=最新/热门/观看 筛选"

    def __init__(
        self,
        http: HttpClient,
        default_genre: str = "裏番",
        quality: int = 1080,
        send_mode: str = "视频",
    ):
        super().__init__(http)
        self.default_genre = default_genre or "裏番"
        self.quality = int(quality)
        self.send_mode = send_mode

    def work_url(self, work_id: str) -> str:
        return f"{SITE_BASE}/watch?v={work_id}"

    def resource_headers(self) -> dict[str, str]:
        return {"Referer": SITE_BASE + "/"}

    def _card_to_work(self, card: VideoCard) -> WorkInfo:
        return WorkInfo(
            source=self.key,
            work_id=card.video_id,
            title=card.title,
            url=self.work_url(card.video_id),
            media_type=MediaType.VIDEO,
            cover_url=card.cover,
            duration=card.duration,
            detailed=False,
        )

    async def _search_page(self, params: dict, page: int) -> WorkPage:
        query = {key: value for key, value in params.items() if value}
        if page > 1:
            query["page"] = str(page)
        html = await self.http.get_text(f"{SITE_BASE}/search", params=query)
        cards, max_page = parse_search(html)
        total_pages = max(max_page or 1, page) if cards else max_page
        return WorkPage(
            items=[self._card_to_work(card) for card in cards],
            page=page,
            total_pages=total_pages,
            has_next=bool(total_pages and page < total_pages),
        )

    # ── 查询能力 ──────────────────────────────────────

    async def get_work(self, work_id: str) -> WorkInfo:
        try:
            html = await self.http.get_text(self.work_url(work_id))
        except HttpError as exc:
            if exc.status == 404:
                raise WorkNotFound(f"Hanime1 影片 {work_id} 不存在") from exc
            raise
        page = parse_watch(html)
        if not page.title:
            raise WorkNotFound(f"Hanime1 影片 {work_id} 不存在或需要登录")
        return WorkInfo(
            source=self.key,
            work_id=str(work_id),
            title=page.title,
            url=self.work_url(work_id),
            media_type=MediaType.VIDEO,
            cover_url=page.cover,
            author=page.artist,
            tags=page.tags,
            views=page.views,
            uploaded=page.date,
            description=page.description,
            detailed=True,
            extra={"sources": page.sources},
        )

    async def search(self, query: SearchQuery) -> WorkPage:
        keyword = " ".join([query.keyword.strip()] + [f"-{word}" for word in query.exclude]).strip()
        params = {
            "query": keyword,
            "genre": resolve_genre(query.category, self.default_genre),
            "sort": SORT_PARAMS.get(query.sort, ""),
        }
        return await self._search_page(params, max(1, query.page))

    async def ranking(self, period: Period, page: int = 1) -> WorkPage:
        params = {"genre": resolve_genre("", self.default_genre), "sort": PERIOD_SORTS[period]}
        return await self._search_page(params, max(1, page))

    # ── 内容获取 ──────────────────────────────────────

    async def _sources(self, work: WorkInfo) -> list[tuple[int, str]]:
        sources = list(work.extra.get("sources") or [])
        if not sources or max(q for q, _ in sources) < self.quality:
            try:
                html = await self.http.get_text(f"{SITE_BASE}/download", params={"v": work.work_id})
                merged = dict(sources)
                for quality, url in parse_sources(html):
                    merged.setdefault(quality, url)
                sources = sorted(merged.items(), reverse=True)
            except HttpError:
                pass
        return sources

    def _pick_source(self, sources: list[tuple[int, str]]) -> Optional[tuple[int, str]]:
        """选择不超过目标清晰度的最高清晰度；都高于目标时选最低的一个。"""
        within = [item for item in sources if item[0] <= self.quality]
        if within:
            return max(within)
        return min(sources) if sources else None

    async def fetch(self, work: WorkInfo, ctx: FetchContext) -> MediaBundle:
        if not work.detailed:
            work = await self.get_work(work.work_id)
        picked = self._pick_source(await self._sources(work))
        if picked is None:
            raise WorkNotFound("没有找到可用的视频源（可能需要在配置中填写 Cookie）")
        quality, url = picked
        if self.send_mode == SEND_MODE_LINK:
            return MediaBundle(work=work, link=url)
        await ctx.progress(f"开始下载 {quality}P 视频，文件较大请耐心等待")
        path = await ctx.video_downloader.download(
            url, ctx.work_dir / f"{work.work_id}-{quality}p.mp4", self.http, self.resource_headers()
        )
        return MediaBundle(work=work, video=path)
