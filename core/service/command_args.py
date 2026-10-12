"""指令参数解析：把用户输入拆分为关键词、页码、排序、排行周期、数量与筛选条件。"""

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..errors import CollectorError
from ..types import SORT_LABELS, Period, SearchQuery, SortKey


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
_KV_RE = re.compile(r"^([^=:：<>≥]+?)\s*(?:>=|≥|=|:|：|>)\s*(.*)$")
_PAGE_RE = re.compile(r"^(?:第(-?\d+)页|p(-?\d+)|(-?\d+)页)$", re.I)
_COUNT_RE = re.compile(r"^(-?\d+)\s*(?:本|部|个|条|篇)$")
# 只合并已知分类名，普通关键词必须保留在分类参数之外。
_MULTIWORD_CATEGORIES = {"motion anime", "artist cg", "game cg", "image set", "asian porn"}
MAX_RANDOM_COUNT = 5


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

    query: SearchQuery = field(default_factory=SearchQuery)
    period: Optional[Period] = None
    page_given: bool = False
    count: int = 0
    rest: list[str] = field(default_factory=list)


def split_command(message: str, *, command_words: Optional[Iterable[str]] = None) -> list[str]:
    """先去掉完整触发词，再按成对引号拆分参数并保留源站标签语法。

    Args:
        message: 消息正文，运行时由 AstrBot 处理唤醒前缀与机器人提及。
        command_words: 当前操作实际生效的触发词；未提供时按普通单词指令拆分。

    Raises:
        CollectorError: 触发词不匹配，或参数中的引号没有闭合。
    """
    cleaned = str(message or "").strip()
    if command_words is None:
        # 未指定操作的纯文本工具调用只移除开头提及，保留参数中的邮箱和 @ 字符。
        cleaned = re.sub(r"^(?:(?:\[CQ:at,[^\]]*\]|@\S+)\s+)+", "", cleaned)
        command_length = 1
    else:
        normalized = " ".join(cleaned.split())
        matched = [word for word in command_words if normalized == word or normalized.startswith(word + " ")]
        if not matched:
            raise CollectorError("指令触发词已变化，请按当前帮助中的写法重新发送")
        command_length = len(max(matched, key=len).split())
    fragments = cleaned.split(maxsplit=command_length)
    arguments = fragments[command_length] if len(fragments) > command_length else ""
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    for char in arguments:
        if quote:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in ("\"", "'") and (not current or current[-1] in "=:：->≥"):
            quote = char
            current.append(char)
        elif char.isspace():
            if current:
                parts.append("".join(current))
                current = []
        else:
            current.append(char)
    if quote:
        raise CollectorError('参数中的引号没有闭合；带空格的值请写成 分类="Motion Anime"')
    if current:
        parts.append("".join(current))
    return [_unquote(part) for part in parts]


def _unquote(value: str) -> str:
    """移除参数值外层引号，保留未知标签内部的引号。"""
    if len(value) >= 2 and value[0] in ("\"", "'") and value[-1] == value[0]:
        return value[1:-1].replace("\\" + value[0], value[0]).replace("\\\\", "\\")
    return value


def _category_tokens(tokens: list[str]) -> list[str]:
    """识别文档中的已知多词分类写法，不合并任意后续关键词。"""
    merged: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        match = _KV_RE.fullmatch(token)
        if match and _lookup(KEY_ALIASES, match.group(1)) == "category" and index + 1 < len(tokens):
            if f"{match.group(2)} {tokens[index + 1]}".lower() in _MULTIWORD_CATEGORIES:
                token += " " + tokens[index + 1]
                index += 1
        merged.append(token)
        index += 1
    return merged


def _integer(value: str, label: str, minimum: int = 1, maximum: Optional[int] = None) -> int:
    """校验整数值，拒绝截取数字前缀或悄悄改变越界值。"""
    hint = f"{minimum} 到 {maximum} 的整数" if maximum is not None else f"不小于 {minimum} 的整数"
    try:
        number = int(value) if value.isdecimal() else minimum - 1
    except ValueError as exc:
        raise CollectorError(f"{label}必须是{hint}，收到「{value}」") from exc
    if number < minimum or (maximum is not None and number > maximum):
        raise CollectorError(f"{label}必须是{hint}，收到「{value}」")
    return number


def _set_page(args: ParsedArgs, value: str) -> None:
    """校验并设置唯一的页码参数。"""
    if args.page_given:
        raise CollectorError("页码只能填写一次，请删除重复的页码参数")
    args.query.page = _integer(value, "页码")
    args.page_given = True


def parse_args(tokens: list[str], *, allow_period: bool = False) -> ParsedArgs:
    """解析通用参数（源站由指令本身决定，不从参数中识别）。

    规则：
        - ``p2`` / ``第2页`` / ``页码=2`` 为页码；``5本`` / ``数量=5`` 为数量
        - ``排序=最新``、``评分>=4``、``页数>=20``、``分类=同人志``
        - ``-词`` 为排除；``allow_period`` 时识别 ``日榜/周榜/月榜/年榜/总榜``
        - 搜索的未知参数按原顺序拼成关键词；排行榜仅允许周期和页码

    Args:
        tokens: 指令参数
        allow_period: 是否解析排行周期

    Returns:
        解析结果
    """
    args = ParsedArgs()
    keywords: list[str] = []
    for token in _category_tokens(tokens):
        period = parse_period(token) if allow_period else None
        if period is not None:
            if args.period is not None:
                raise CollectorError("榜单周期只能填写一次，可选：日榜、周榜、月榜、年榜、总榜")
            args.period = period
            continue
        page_match = _PAGE_RE.match(token)
        if page_match:
            _set_page(args, next(g for g in page_match.groups() if g))
            continue
        kv = _KV_RE.match(token)
        if allow_period:
            if token.isdecimal():
                _set_page(args, token)
            elif kv and _lookup(KEY_ALIASES, kv.group(1)) == "page":
                _set_page(args, _unquote(kv.group(2)))
            else:
                raise CollectorError(f"排行榜不支持参数「{token}」，只可填写日榜、周榜、月榜、年榜、总榜和页码")
            continue
        count_match = _COUNT_RE.match(token)
        if count_match:
            args.count = _integer(count_match.group(1), "数量", maximum=MAX_RANDOM_COUNT)
            continue
        if token.startswith("-") and len(token) > 1 and not token[1:].isdigit():
            args.query.exclude.append(_unquote(token[1:]))
            continue
        if kv and _apply_kv(args, kv.group(1), kv.group(2)):
            continue
        if token.startswith("按") and parse_sort(token[1:]):
            args.query.sort = parse_sort(token[1:])
            continue
        keywords.append(_unquote(token))

    args.query.keyword = " ".join(keywords).strip()
    args.rest = keywords
    return args


def _apply_kv(args: ParsedArgs, key: str, value: str) -> bool:
    """应用 ``键=值`` 参数，未知键返回 False 交给关键词处理。"""
    name = _lookup(KEY_ALIASES, key)
    if name is None:
        return False
    value = _unquote(value.strip())
    if name == "page":
        _set_page(args, value)
    elif name == "sort":
        sort = parse_sort(value)
        if sort is None:
            raise CollectorError(f"无法识别排序「{value}」，可选：{'、'.join(SORT_LABELS.values())}")
        args.query.sort = sort
    elif name == "rating":
        args.query.min_rating = _integer(value, "评分", 2, 5)
    elif name == "pages":
        args.query.min_pages = _integer(value, "页数")
    elif name == "category":
        if not value:
            raise CollectorError('分类不能为空；带空格的值请写成 分类="Motion Anime"')
        args.query.category = value
    elif name == "count":
        args.count = _integer(value, "数量", maximum=MAX_RANDOM_COUNT)
    return True
