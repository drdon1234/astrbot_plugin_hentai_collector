"""E-Hentai / ExHentai 访问层：列表游标、排行榜、gdata 元数据接口与单页图片解析。"""

import random
import re
from dataclasses import dataclass
from html import unescape
from typing import Optional

from ....errors import BlockedError, HttpError, SourceError
from ....net import HttpClient
from ...utils import RateLimiter, format_timestamp, to_float, to_int


SITES = {
    "e-hentai": ("https://e-hentai.org", "https://api.e-hentai.org/api.php"),
    "exhentai": ("https://exhentai.org", "https://exhentai.org/api.php"),
}
EX_REQUIRED_COOKIES = ("ipb_member_id", "ipb_pass_hash", "igneous")

# f_cats 是“排除”位掩码：位为 1 表示不显示该分类
CATEGORY_BITS = {
    "Misc": 1,
    "Doujinshi": 2,
    "Manga": 4,
    "Artist CG": 8,
    "Game CG": 16,
    "Image Set": 32,
    "Cosplay": 64,
    "Asian Porn": 128,
    "Non-H": 256,
    "Western": 512,
}
CATEGORY_ALIASES = {
    "杂项": "Misc",
    "同人": "Doujinshi",
    "同人志": "Doujinshi",
    "漫画": "Manga",
    "画师cg": "Artist CG",
    "画师": "Artist CG",
    "游戏cg": "Game CG",
    "游戏": "Game CG",
    "图集": "Image Set",
    "cos": "Cosplay",
    "cosplay": "Cosplay",
    "亚洲": "Asian Porn",
    "非h": "Non-H",
    "西方": "Western",
    "欧美": "Western",
}
ALL_CATEGORIES = 1023

# 排行榜 tl 参数：15=昨日、13=近一月、12=近一年、11=总榜（站点没有周榜）
TOPLIST_IDS = {"day": 15, "month": 13, "year": 12, "all": 11}

GALLERY_RE = re.compile(r"https?://(?:e-hentai|exhentai)\.org/g/(\d+)/([0-9a-f]{10})/?")
NEXT_RE = re.compile(r'id="unext"\s+href="[^"]*?(?:[?&]|&amp;)next=(\d+)')
FOUND_RE = re.compile(r"Found(?: about)? ([\d,]+) results?")
IMAGE_RE = re.compile(r'<img id="img" src="([^"]+)"')
# 单页“Reload broken image”的换节点参数，附加 ?nl= 重新请求单页会分配另一个 H@H 节点
RELOAD_RE = re.compile(r"nl\('([^']+)'\)")
BANNED_MARK = "Your IP address has been temporarily banned"
GDATA_BATCH = 25
SEARCH_PAGE_SIZE = 25


@dataclass
class Listing:
    """一页列表的解析结果。"""

    pairs: list[tuple[int, str]]
    next_cursor: Optional[int]
    total: Optional[int]


def resolve_category(name: str) -> Optional[str]:
    """把中英文分类名解析为站点分类名，无法识别时返回 None。"""
    text = str(name or "").strip()
    if not text:
        return None
    for official in CATEGORY_BITS:
        if official.lower() == text.lower():
            return official
    return CATEGORY_ALIASES.get(text.lower())


def parse_listing(html: str) -> Listing:
    """解析搜索页或排行榜页，返回 (gid, token) 序列、下一页游标与结果总数。"""
    seen: set[int] = set()
    pairs: list[tuple[int, str]] = []
    for gid_text, token in GALLERY_RE.findall(html):
        gid = int(gid_text)
        if gid not in seen:
            seen.add(gid)
            pairs.append((gid, token))
    next_match = NEXT_RE.search(html)
    found = FOUND_RE.search(html)
    return Listing(
        pairs=pairs,
        next_cursor=int(next_match.group(1)) if next_match else None,
        total=to_int(found.group(1)) if found else None,
    )


def parse_page_links(html: str, gid: int) -> dict[int, str]:
    """解析画廊缩略图页，返回 ``{页码: 单页 URL}``。"""
    pattern = re.compile(rf"https?://[^/\"'\s]+/s/[0-9a-f]{{10}}/{gid}-(\d+)")
    return {int(m.group(1)): m.group(0) for m in pattern.finditer(html)}


def parse_image_page(html: str) -> tuple[Optional[str], Optional[str]]:
    """解析单页，返回 (图片地址, 换节点参数)，缺失项为 None。"""
    image = IMAGE_RE.search(html)
    reload_key = RELOAD_RE.search(html)
    return (
        unescape(image.group(1)) if image else None,
        reload_key.group(1) if reload_key else None,
    )


def split_tags(tags: list[str]) -> dict[str, list[str]]:
    """把 ``namespace:tag`` 形式的标签按命名空间分组。"""
    groups: dict[str, list[str]] = {}
    for tag in tags:
        namespace, _, value = str(tag).partition(":")
        if not value:
            namespace, value = "misc", namespace
        groups.setdefault(namespace, []).append(value)
    return groups


class EHentaiApi:
    """E-Hentai 请求封装；所有请求经过限速器，避免触发临时封禁。"""

    def __init__(self, http: HttpClient, site: str, interval: float):
        self.http = http
        self.site = site if site in SITES else "e-hentai"
        self.base, self.api_url = SITES[self.site]
        self.limiter = RateLimiter(interval)

    def gallery_url(self, gid: int, token: str) -> str:
        """画廊网页地址。"""
        return f"{self.base}/g/{gid}/{token}/"

    async def get_html(self, url: str, params: Optional[dict] = None) -> str:
        """请求页面并识别封禁、空页面等异常状态。

        Raises:
            BlockedError: IP 被封禁或 ExHentai Cookie 无效
        """
        await self.limiter.wait()
        try:
            html = await self.http.get_text(url, params=params)
        except HttpError as exc:
            if exc.status == 404:
                raise SourceError("画廊不存在或已被删除") from exc
            raise
        if BANNED_MARK in html:
            raise BlockedError("IP 被 E-Hentai 临时封禁，请稍后再试或更换代理")
        if not html.strip():
            raise BlockedError("站点返回空页面：ExHentai Cookie 无效或已过期")
        return html

    async def listing(self, params: dict) -> Listing:
        """请求首页搜索列表。"""
        return parse_listing(await self.get_html(self.base + "/", params))

    async def toplist(self, period: str, page: int) -> Listing:
        """请求排行榜页（排行榜只在表站提供）。"""
        params = {"tl": str(TOPLIST_IDS[period])}
        if page > 1:
            params["p"] = str(page - 1)
        return parse_listing(await self.get_html("https://e-hentai.org/toplist.php", params))

    async def random_listing(self, params: dict) -> list[tuple[int, str]]:
        """随机跳转到搜索结果中的某一页。

        列表只能按 gid 游标翻页，先取最新与最旧画廊的 gid，
        再在两者之间随机取一个游标跳过去；gid 随时间递增，属于按时间近似均匀的抽样。
        """
        newest = await self.listing(params)
        if not newest.pairs:
            return []
        if newest.next_cursor is None:
            return newest.pairs
        oldest = await self.listing({**params, "prev": "1"})
        low = min(gid for gid, _ in (oldest.pairs or newest.pairs))
        high = max(gid for gid, _ in newest.pairs)
        if high <= low:
            return newest.pairs
        jumped = await self.listing({**params, "next": str(random.randint(low + 1, high + 1))})
        return jumped.pairs or newest.pairs

    async def gdata(self, pairs: list[tuple[int, str]]) -> list[dict]:
        """批量查询画廊元数据，按输入顺序返回成功的条目。"""
        results: dict[int, dict] = {}
        for start in range(0, len(pairs), GDATA_BATCH):
            batch = pairs[start:start + GDATA_BATCH]
            await self.limiter.wait()
            data = await self.http.post_json(
                self.api_url,
                {"method": "gdata", "gidlist": [[gid, token] for gid, token in batch], "namespace": 1},
            )
            for meta in data.get("gmetadata", []) if isinstance(data, dict) else []:
                if isinstance(meta, dict) and "error" not in meta:
                    results[int(meta["gid"])] = meta
        return [results[gid] for gid, _ in pairs if gid in results]

    async def page_links(self, gid: int, token: str, filecount: int) -> list[str]:
        """按页码顺序返回画廊全部单页地址。"""
        url = self.gallery_url(gid, token)
        links: dict[int, str] = {}
        page = 0
        while len(links) < filecount:
            html = await self.get_html(url, {"p": str(page)} if page else None)
            found = {k: v for k, v in parse_page_links(html, gid).items() if k not in links}
            if not found:
                break
            links.update(found)
            page += 1
        return [links[number] for number in sorted(links)]

    async def image_url(self, page_url: str, reload_key: Optional[str] = None) -> tuple[str, Optional[str]]:
        """解析单页上的图片地址。

        Args:
            page_url: 单页地址
            reload_key: 上次解析得到的换节点参数，传入时请求另一个 H@H 节点

        Returns:
            (图片地址, 本次的换节点参数)

        Raises:
            BlockedError: 图片额度耗尽（509）
            SourceError: 单页中没有图片
        """
        image, next_key = parse_image_page(
            await self.get_html(page_url, {"nl": reload_key} if reload_key else None)
        )
        if not image:
            raise SourceError("单页中没有图片")
        if image.endswith("/509.gif"):
            raise BlockedError("E-Hentai 图片额度已用尽（509），请稍后再试")
        return image, next_key


def meta_fields(meta: dict) -> dict:
    """把 gdata 元数据转换为 WorkInfo 的字段字典。"""
    tags = [str(tag) for tag in meta.get("tags", [])]
    groups = split_tags(tags)
    authors = groups.get("artist") or groups.get("group") or []
    language = [lang for lang in groups.get("language", []) if lang not in ("translated", "rewrite")]
    return {
        "title": unescape(meta.get("title") or ""),
        "subtitle": unescape(meta.get("title_jpn") or ""),
        "cover_url": str(meta.get("thumb") or ""),
        "author": ", ".join(authors[:3]),
        "category": str(meta.get("category") or ""),
        "language": ", ".join(language),
        "tags": tags,
        "pages": to_int(meta.get("filecount"), 0) or 0,
        "rating": to_float(meta.get("rating")),
        "uploaded": format_timestamp(meta.get("posted")),
        "extra": {
            "gid": int(meta["gid"]),
            "token": str(meta["token"]),
            "uploader": unescape(meta.get("uploader") or ""),
            "expunged": bool(meta.get("expunged")),
        },
    }
