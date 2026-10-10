"""Hanime1 页面解析：搜索结果卡片、分页、播放页元数据与视频源。"""

import re
from dataclasses import dataclass, field
from html import unescape
from typing import Optional

from ...utils import parse_count, strip_html


WATCH_ID_RE = re.compile(r'href="(?:https?://hanime1\.(?:me|com))?/watch\?v=(\d+)"')
IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"')
TITLE_RES = (
    re.compile(r'class="home-rows-videos-title"[^>]*>(.*?)</div>', re.S),
    re.compile(r'class="card-mobile-title"[^>]*>(.*?)</div>', re.S),
    re.compile(r'title="([^"]+)"'),
    re.compile(r'alt="([^"]+)"'),
)
DURATION_RE = re.compile(r'class="card-mobile-duration"[^>]*>\s*([\d:]+)\s*<')
PAGE_LINK_RE = re.compile(r'class="page-link"[^>]*href="[^"]*?(?:[?&]|&amp;)page=(\d+)')
SOURCE_RE = re.compile(r'<source[^>]+src="([^"]+)"[^>]*size="(\d+)"')
DOWNLOAD_LINK_RE = re.compile(r'href="(https?://[^"]+?-(\d{3,4})p\.mp4[^"]*)"')
OG_RE = re.compile(r'<meta property="og:(\w+)" content="([^"]*)"')
SHARE_TITLE_RE = re.compile(r'id="shareBtn-title"[^>]*>(.*?)</', re.S)
ARTIST_RE = re.compile(r'id="video-artist-name"[^>]*>(.*?)</a>', re.S)
VIEWS_DATE_RE = re.compile(r"觀看次數：([\d.,]+[萬万億亿]?)次(?:&nbsp;|\s)+(\d{4}-\d{2}-\d{2})")
CAPTION_RE = re.compile(r'class="video-caption-text[^"]*"[^>]*>(.*?)</div>', re.S)
TAG_RE = re.compile(r'class="single-video-tag"[^>]*>\s*<a[^>]*>(.*?)(?:&nbsp;|<)', re.S)
POSTER_RE = re.compile(r'poster="([^"]+)"')


@dataclass
class VideoCard:
    """搜索结果中的一个视频。"""

    video_id: str
    title: str
    cover: str = ""
    duration: str = ""


@dataclass
class WatchPage:
    """播放页解析结果。"""

    title: str
    cover: str = ""
    description: str = ""
    artist: str = ""
    views: Optional[int] = None
    date: str = ""
    tags: list[str] = field(default_factory=list)
    sources: list[tuple[int, str]] = field(default_factory=list)


def parse_search(html: str) -> tuple[list[VideoCard], Optional[int]]:
    """解析搜索页，返回 (视频卡片, 最大页码)。广告卡片没有 watch 链接，会被自然跳过。"""
    matches = list(WATCH_ID_RE.finditer(html))
    cards: list[VideoCard] = []
    seen: set[str] = set()
    for index, match in enumerate(matches):
        video_id = match.group(1)
        end = matches[index + 1].start() if index + 1 < len(matches) else min(len(html), match.end() + 3000)
        block = html[match.end():end]
        if video_id in seen:
            continue
        title = ""
        for pattern in TITLE_RES:
            found = pattern.search(block)
            if found:
                title = strip_html(found.group(1))
                if title:
                    break
        if not title:
            continue
        seen.add(video_id)
        cover = IMG_RE.search(block)
        duration = DURATION_RE.search(block)
        cards.append(
            VideoCard(
                video_id=video_id,
                title=title,
                cover=unescape(cover.group(1)) if cover else "",
                duration=duration.group(1) if duration else "",
            )
        )
    pages = [int(page) for page in PAGE_LINK_RE.findall(html)]
    return cards, (max(pages) if pages else None)


def parse_sources(html: str) -> list[tuple[int, str]]:
    """解析视频源，返回按清晰度降序的 ``[(清晰度, 地址)]``。"""
    found: dict[int, str] = {}
    for url, size in SOURCE_RE.findall(html):
        found.setdefault(int(size), unescape(url))
    for url, size in DOWNLOAD_LINK_RE.findall(html):
        found.setdefault(int(size), unescape(url))
    return sorted(found.items(), reverse=True)


def parse_watch(html: str) -> WatchPage:
    """解析播放页元数据与视频源。"""
    og = {key: unescape(value) for key, value in OG_RE.findall(html)}
    share_title = SHARE_TITLE_RE.search(html)
    title = strip_html(share_title.group(1)) if share_title else ""
    if not title:
        title = re.sub(r"\s*-\s*Hanime1\.me\s*$", "", og.get("title", "")).strip()
    artist = ARTIST_RE.search(html)
    views_date = VIEWS_DATE_RE.search(html)
    caption = CAPTION_RE.search(html)
    poster = POSTER_RE.search(html)
    tags = list(dict.fromkeys(strip_html(tag) for tag in TAG_RE.findall(html) if strip_html(tag)))
    return WatchPage(
        title=title.splitlines()[0].strip() if title else "",
        cover=unescape(poster.group(1)) if poster else og.get("image", ""),
        description=strip_html(caption.group(1)) if caption else og.get("description", ""),
        artist=strip_html(artist.group(1)) if artist else "",
        views=parse_count(views_date.group(1)) if views_date else None,
        date=views_date.group(2) if views_date else "",
        tags=tags,
        sources=parse_sources(html),
    )
