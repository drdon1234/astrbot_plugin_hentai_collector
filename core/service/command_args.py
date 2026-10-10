"""指令参数解析：把用户输入拆分为源站、关键词、页码、排序、排行周期与筛选条件。"""

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..types import Period, SearchQuery, SortKey


PERIOD_WORDS = {
    Period.DAY: ("日榜", "日", "今日", "今天", "昨日", "day", "daily", "24h"),
    Period.WEEK: ("周榜", "周", "本周", "週", "week", "weekly", "7d"),
    Period.MONTH: ("月榜", "月", "本月", "month", "monthly", "30d"),
    Period.YEAR: ("年榜", "年", "今年", "year", "yearly"),
    Period.ALL: ("总榜", "总", "全部时间", "all", "alltime", "历史"),
}
SORT_WORDS = {
    SortKey.LATEST: ("最新", "新", "latest", "new", "date", "时间"),
    SortKey.POPULAR: ("热门", "热", "popular", "hot", "人气"),
    SortKey.VIEWS: ("观看", "播放", "浏览", "views", "view"),
    SortKey.LIKES: ("点赞", "喜欢", "收藏", "likes", "like", "fav"),
    SortKey.RATING: ("评分", "rating", "score"),
    SortKey.DEFAULT: ("默认", "default"),
}
KEY_ALIASES = {
    "page": ("页码", "第", "page", "p"),
    "sort": ("排序", "sort", "按"),
    "rating": ("评分", "rating", "星"),
    "pages": ("页数", "pages", "最少页数"),
    "category": ("分类", "类型", "类别", "category", "cat", "genre"),
    "count": ("数量", "count", "n"),
}
_KV_RE = re.compile(r"^([^=:：<>≥]+?)\s*(?:>=|≥|=|:|：|>)\s*(.+)$")
_PAGE_RE = re.compile(r"^(?:第(\d+)页|p(\d+)|(\d+)页)$", re.I)
_COUNT_RE = re.compile(r"^(\d+)\s*(?:本|部|个|条|篇)$")


def _lookup(table: dict, word: str):
    lowered = word.strip().lower()
    for key, words in table.items():
        if lowered in words:
            return key
    return None


def parse_period(word: str) -> Optional[Period]:
    """把中文 / 英文周期词解析为 Period。"""
    return _lookup(PERIOD_WORDS, word)


def parse_sort(word: str) -> Optional[SortKey]:
    """把排序词解析为 SortKey。"""
    return _lookup(SORT_WORDS, word)


@dataclass
class ParsedArgs:
    """解析后的指令参数。"""

    source: Optional[str] = None
    all_sources: bool = False
    query: SearchQuery = field(default_factory=SearchQuery)
    period: Optional[Period] = None
    page_given: bool = False
    count: int = 0
    rest: list[str] = field(default_factory=list)


def split_command(message: str) -> list[str]:
    """去掉 @ 提及与指令词，返回参数列表。"""
    cleaned = re.sub(r"\[CQ:at,[^\]]*\]|@\S+", " ", str(message or "")).strip()
    parts = cleaned.split()
    return parts[1:] if parts else []


def parse_args(
    tokens: list[str],
    resolve_source: Callable[[str], Optional[str]],
    is_all_word: Callable[[str], bool],
    *,
    allow_period: bool = False,
) -> ParsedArgs:
    """解析通用参数。

    规则：
        - 第一个参数是源站别名时作为源站；``全部`` 表示所有源站
        - ``p2`` / ``第2页`` / ``页码=2`` 为页码；``5本`` / ``数量=5`` 为数量
        - ``排序=最新``、``评分>=4``、``页数>=20``、``分类=同人志``
        - ``-词`` 为排除；``allow_period`` 时识别 ``日榜/周榜/月榜/年榜/总榜``
        - 其余参数按原顺序拼成关键词

    Args:
        tokens: 指令参数
        resolve_source: 源站别名解析函数
        is_all_word: 判断“全部源站”的函数
        allow_period: 是否解析排行周期

    Returns:
        解析结果
    """
    args = ParsedArgs()
    keywords: list[str] = []
    remaining = list(tokens)
    if remaining:
        if is_all_word(remaining[0]):
            args.all_sources = True
            remaining.pop(0)
        else:
            key = resolve_source(remaining[0])
            if key:
                args.source = key
                remaining.pop(0)

    for token in remaining:
        if allow_period and parse_period(token) and args.period is None:
            args.period = parse_period(token)
            continue
        page_match = _PAGE_RE.match(token)
        if page_match:
            args.query.page = int(next(g for g in page_match.groups() if g))
            args.page_given = True
            continue
        count_match = _COUNT_RE.match(token)
        if count_match:
            args.count = int(count_match.group(1))
            continue
        if token.isdigit() and allow_period and not args.page_given and not keywords:
            args.query.page = int(token)
            args.page_given = True
            continue
        if token.startswith("-") and len(token) > 1 and not token[1:].isdigit():
            args.query.exclude.append(token[1:])
            continue
        kv = _KV_RE.match(token)
        if kv and _apply_kv(args, kv.group(1), kv.group(2)):
            continue
        if token.startswith("按") and parse_sort(token[1:]):
            args.query.sort = parse_sort(token[1:])
            continue
        keywords.append(token)

    args.query.keyword = " ".join(keywords).strip()
    args.rest = keywords
    return args


def _apply_kv(args: ParsedArgs, key: str, value: str) -> bool:
    """应用 ``键=值`` 参数，未知键返回 False 交给关键词处理。"""
    name = _lookup(KEY_ALIASES, key)
    value = value.strip()
    digits = re.match(r"\d+", value)
    if name == "page" and digits:
        args.query.page = max(1, int(digits.group(0)))
        args.page_given = True
    elif name == "sort" and parse_sort(value):
        args.query.sort = parse_sort(value)
    elif name == "rating" and digits:
        args.query.min_rating = min(5, int(digits.group(0)))
    elif name == "pages" and digits:
        args.query.min_pages = int(digits.group(0))
    elif name == "category" and value:
        args.query.category = value
    elif name == "count" and digits:
        args.count = int(digits.group(0))
    else:
        return False
    return True
