"""禁漫天堂（JMComic）源站：通过 jmcomic 库的 API 客户端搜索、排行、随机，并下载解码图片。"""

import asyncio
import random
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from ...logger import logger

from ...errors import SourceError, SourceUnavailable, WorkNotFound
from ...net import HttpClient
from ...packager import natural_key
from ...types import MediaBundle, Period, SearchQuery, SortKey, WorkInfo, WorkPage
from ..utils import to_int
from .base import BaseSource, FetchContext


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
SORT_PARAMS = {
    SortKey.DEFAULT: "mr",
    SortKey.LATEST: "mr",
    SortKey.VIEWS: "mv",
    SortKey.POPULAR: "mv",
    SortKey.LIKES: "tf",
}
# 分类名 → jmcomic 分类参数
CATEGORY_PARAMS = {
    "同人": "doujin",
    "单本": "single",
    "短篇": "short",
    "韩漫": "hanman",
    "美漫": "meiman",
    "cosplay": "doujin_cosplay",
    "3d": "3D",
    "其他": "another",
}
SITE_BASE = "https://18comic.vip"


def _load_jmcomic():
    try:
        import jmcomic
    except ImportError as exc:
        raise SourceUnavailable("缺少 jmcomic 依赖，请安装插件 requirements.txt 中的依赖") from exc
    disable_log = getattr(jmcomic, "disable_jm_log", None)
    if callable(disable_log):
        disable_log()
    return jmcomic


def _iter_images(root: Path) -> list[Path]:
    if not root.exists():
        return []
    paths = [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES]
    return sorted(paths, key=lambda path: natural_key(path.relative_to(root)))


def _get(info: Any, key: str, default: Any = None) -> Any:
    if isinstance(info, dict):
        return info.get(key, default)
    return getattr(info, key, default)


class JmComicSource(BaseSource):
    """JMComic 源站，作品 ID 为本子编号（album id）。"""

    key = "jmcomic"
    name = "JMComic"
    short = "JM"
    aliases = ("jm", "禁漫", "18comic", "jmcomic", "禁漫天堂")
    link_patterns = (
        re.compile(r"(?i)(?:18comic|jmcomic|jm-?comic|18-comic)[\w.-]*\.\w+/(?:album|photo)s?/(\d{3,9})"),
        re.compile(r"(?i)(?<![a-z0-9])jm\s*[-_:：#]?\s*(\d{3,9})(?!\d)"),
    )
    id_pattern = re.compile(r"\d{3,9}")
    ranking_periods = (Period.DAY, Period.WEEK, Period.MONTH, Period.ALL)
    sort_options = (SortKey.LATEST, SortKey.VIEWS, SortKey.LIKES)
    search_hint = "关键词可以是作品名、作者、标签；可用 排序=最新/观看/点赞、分类=同人/单本/韩漫 筛选"

    def __init__(
        self,
        http: HttpClient,
        proxy: str = "",
        domains: Iterable[str] = (),
        retries: int = 3,
        image_threads: int = 10,
        parse_code: bool = True,
    ):
        super().__init__(http)
        self.proxy = proxy
        self.domains = [str(domain).strip() for domain in domains if str(domain).strip()]
        self.retries = max(1, int(retries))
        self.image_threads = max(1, int(image_threads))
        self.parse_code = parse_code
        self._client = None
        self._client_lock = asyncio.Lock()

    @property
    def unavailable_reason(self) -> str:
        try:
            import jmcomic  # noqa: F401
        except ImportError:
            return "缺少 jmcomic 依赖"
        return ""

    def extract_ids(self, text: str) -> list[str]:
        patterns = self.link_patterns if self.parse_code else self.link_patterns[:1]
        found = []
        for pattern in patterns:
            found.extend((m.start(), m.group(1)) for m in pattern.finditer(text or ""))
        found.sort(key=lambda item: item[0])
        return list(dict.fromkeys(work_id for _, work_id in found))

    def normalize_id(self, raw: str) -> Optional[str]:
        text = str(raw or "").strip()
        for pattern in self.link_patterns:
            match = pattern.search(text)
            if match:
                return match.group(1)
        return text if self.id_pattern.fullmatch(text) else None

    def work_url(self, work_id: str) -> str:
        return f"{SITE_BASE}/album/{work_id}/"

    # ── jmcomic 客户端 ────────────────────────────────

    def _option(self, base_dir: Optional[Path] = None):
        jmcomic = _load_jmcomic()
        client: dict[str, Any] = {"impl": "api", "retry_times": self.retries}
        if self.domains:
            client["domain"] = self.domains
        if self.proxy:
            client["postman"] = {"meta_data": {"proxies": {"http": self.proxy, "https": self.proxy}}}
        data: dict[str, Any] = {
            "client": client,
            "download": {
                "cache": True,
                "image": {"decode": True, "suffix": ".jpg"},
                "threading": {"image": self.image_threads},
            },
            "log": False,
        }
        if base_dir is not None:
            data["dir_rule"] = {"rule": "Bd_Aid_Pindex", "base_dir": str(base_dir)}
        try:
            return jmcomic.JmOption.construct(data)
        except (AttributeError, TypeError, ValueError) as exc:
            raise SourceError(f"创建 JM 配置失败：{exc}") from exc

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """在线程中调用 jmcomic 客户端方法，统一转换异常。"""
        async with self._client_lock:
            if self._client is None:
                self._client = await asyncio.to_thread(lambda: self._option().new_jm_client())
        client = self._client
        jmcomic = _load_jmcomic()
        try:
            return await asyncio.to_thread(getattr(client, method), *args, **kwargs)
        except jmcomic.JmcomicException as exc:
            message = str(exc)
            if "不存在" in message or "404" in message or "not found" in message.lower():
                raise WorkNotFound(f"JM 作品不存在：{message[:120]}") from exc
            raise SourceError(f"JM 请求失败：{message[:200]}") from exc

    # ── 数据转换 ──────────────────────────────────────

    def _cover(self, album_id: str) -> str:
        try:
            return _load_jmcomic().JmcomicText.get_album_cover_url(album_id)
        except (AttributeError, SourceUnavailable):
            return ""

    def _summary(self, album_id: str, info: Any) -> WorkInfo:
        category = _get(info, "category") or {}
        tags = _get(info, "tags") or []
        return WorkInfo(
            source=self.key,
            work_id=str(album_id),
            title=str(_get(info, "name") or _get(info, "title") or f"JM{album_id}"),
            url=self.work_url(str(album_id)),
            cover_url=self._cover(str(album_id)),
            author=str(_get(info, "author") or ""),
            category=str(_get(category, "title") or ""),
            tags=[str(tag) for tag in tags] if isinstance(tags, list) else [],
            detailed=False,
        )

    def _page(self, page_obj: Any, page: int) -> WorkPage:
        items = [self._summary(aid, info) for aid, info in getattr(page_obj, "content", []) or []]
        try:
            total_pages = int(page_obj.page_count)
        except (AttributeError, TypeError, ValueError, ZeroDivisionError, NotImplementedError):
            total_pages = None
        return WorkPage(
            items=items,
            page=page,
            total_pages=total_pages,
            has_next=bool(total_pages and page < total_pages),
        )

    # ── 查询能力 ──────────────────────────────────────

    async def get_work(self, work_id: str) -> WorkInfo:
        album = await self._call("get_album_detail", work_id)
        authors = list(getattr(album, "authors", None) or [])
        tags = list(getattr(album, "tags", None) or [])
        works = list(getattr(album, "works", None) or [])
        actors = list(getattr(album, "actors", None) or [])
        episodes = list(getattr(album, "episode_list", None) or [])
        return WorkInfo(
            source=self.key,
            work_id=str(work_id),
            title=str(getattr(album, "name", "") or f"JM{work_id}"),
            url=self.work_url(str(work_id)),
            cover_url=self._cover(str(work_id)),
            author=", ".join(str(a) for a in authors[:3]),
            tags=[str(t) for t in tags] + [f"作品:{w}" for w in works[:3]] + [f"角色:{a}" for a in actors[:5]],
            pages=to_int(getattr(album, "page_count", 0), 0) or 0,
            likes=to_int(getattr(album, "likes", None)),
            views=to_int(getattr(album, "views", None)),
            uploaded=str(getattr(album, "pub_date", "") or ""),
            description=str(getattr(album, "description", "") or "").strip(),
            detailed=True,
            extra={"episodes": len(episodes)},
        )

    async def search(self, query: SearchQuery) -> WorkPage:
        keyword = " ".join([query.keyword.strip()] + [f"-{word}" for word in query.exclude]).strip()
        page = max(1, query.page)
        result = await self._call(
            "search_site",
            keyword,
            page=page,
            order_by=SORT_PARAMS.get(query.sort, "mr"),
            category=CATEGORY_PARAMS.get(query.category.strip().lower(), "0"),
        )
        return self._page(result, page)

    async def ranking(self, period: Period, page: int = 1) -> WorkPage:
        page = max(1, page)
        if period == Period.DAY:
            result = await self._call("day_ranking", page)
        elif period == Period.WEEK:
            result = await self._call("week_ranking", page)
        elif period == Period.MONTH:
            result = await self._call("month_ranking", page)
        else:
            result = await self._call("categories_filter", page=page, time="a", category="0", order_by="mv")
        return self._page(result, page)

    async def random_work(self, query: Optional[SearchQuery] = None) -> WorkInfo:
        if query is not None and not query.is_empty():
            return await super().random_work(query)
        first = await self._call("categories_filter", page=1, time="a", category="0", order_by="mr")
        listing = self._page(first, 1)
        total = max(1, min(listing.total_pages or 1, 200))
        page = random.randint(1, total)
        if page > 1:
            listing = self._page(
                await self._call("categories_filter", page=page, time="a", category="0", order_by="mr"),
                page,
            )
        if not listing.items:
            raise WorkNotFound("JM 没有可随机的作品")
        return random.choice(listing.items)

    # ── 内容获取 ──────────────────────────────────────

    async def fetch(self, work: WorkInfo, ctx: FetchContext) -> MediaBundle:
        """使用 jmcomic 下载并解码整本图片（jmcomic 自带多线程与图片还原）。"""
        jmcomic = _load_jmcomic()
        base_dir = ctx.work_dir / "jm"
        option = self._option(base_dir)
        await ctx.progress("JM 图片下载与解码中，大本子可能需要几分钟")
        try:
            await asyncio.to_thread(jmcomic.download_album, work.work_id, option)
        except jmcomic.JmcomicException as exc:
            raise SourceError(f"JM{work.work_id} 下载失败：{str(exc)[:200]}") from exc
        images = _iter_images(base_dir)
        if not images:
            raise WorkNotFound(f"JM{work.work_id} 下载完成但没有找到图片")
        truncated = 0
        if ctx.max_pages > 0 and len(images) > ctx.max_pages:
            truncated = len(images) - ctx.max_pages
            logger.info(f"JM{work.work_id} 共 {len(images)} 页，按配置截取前 {ctx.max_pages} 页")
            images = images[: ctx.max_pages]
        return MediaBundle(work=work, images=images, truncated=truncated)
