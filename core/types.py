"""核心数据契约：作品、查询、列表页与下载结果，贯穿源站→业务→消息全流程。"""

from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional


class MediaType(str, Enum):
    """作品媒体类型。"""

    COMIC = "comic"
    VIDEO = "video"


class Period(str, Enum):
    """排行榜统计周期。"""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"
    ALL = "all"


PERIOD_LABELS = {
    Period.DAY: "日榜",
    Period.WEEK: "周榜",
    Period.MONTH: "月榜",
    Period.YEAR: "年榜",
    Period.ALL: "总榜",
}


class SortKey(str, Enum):
    """统一的搜索排序键；各源站在自身实现中映射为站点参数。"""

    DEFAULT = "default"
    LATEST = "latest"
    POPULAR = "popular"
    VIEWS = "views"
    LIKES = "likes"
    RATING = "rating"


SORT_LABELS = {
    SortKey.DEFAULT: "默认",
    SortKey.LATEST: "最新",
    SortKey.POPULAR: "热门",
    SortKey.VIEWS: "观看",
    SortKey.LIKES: "点赞",
    SortKey.RATING: "评分",
}


@dataclass(frozen=True)
class WorkRef:
    """作品的最小引用：源站键 + 作品 ID，用于会话缓存与推送去重。"""

    source: str
    work_id: str

    def __str__(self) -> str:
        return f"{self.source}:{self.work_id}"


@dataclass
class WorkInfo:
    """一部作品（本子或里番）的统一描述。

    列表接口返回的条目通常字段不全，``detailed`` 为 False；
    业务层需要完整信息时调用源站 ``get_work`` 补全。
    """

    source: str
    work_id: str
    title: str
    url: str = ""
    media_type: MediaType = MediaType.COMIC
    subtitle: str = ""
    cover_url: str = ""
    author: str = ""
    category: str = ""
    language: str = ""
    tags: list[str] = field(default_factory=list)
    pages: int = 0
    rating: Optional[float] = None
    likes: Optional[int] = None
    views: Optional[int] = None
    uploaded: str = ""
    duration: str = ""
    description: str = ""
    detailed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> WorkRef:
        """返回作品引用。"""
        return WorkRef(self.source, self.work_id)


@dataclass
class SearchQuery:
    """统一的搜索 / 随机筛选条件。

    Attributes:
        keyword: 关键词，可包含源站自身的标签语法
        page: 页码，从 1 开始
        sort: 排序键，源站不支持时回退默认排序
        min_rating: 最低评分（仅 E-Hentai 生效）
        min_pages: 最少页数（E-Hentai / nhentai 生效）
        category: 分类 / 类型，按源站说明填写
        exclude: 需要排除的关键词或标签
    """

    keyword: str = ""
    page: int = 1
    sort: SortKey = SortKey.DEFAULT
    min_rating: int = 0
    min_pages: int = 0
    category: str = ""
    exclude: list[str] = field(default_factory=list)

    def with_page(self, page: int) -> "SearchQuery":
        """返回替换页码后的新查询。"""
        return replace(self, page=max(1, int(page)))

    def is_empty(self) -> bool:
        """没有任何筛选条件时返回 True。"""
        return not (
            self.keyword.strip()
            or self.min_rating
            or self.min_pages
            or self.category.strip()
            or self.exclude
        )

    def describe(self) -> str:
        """生成面向用户的条件摘要。"""
        parts = []
        if self.keyword.strip():
            parts.append(f"关键词「{self.keyword.strip()}」")
        if self.category.strip():
            parts.append(f"分类「{self.category.strip()}」")
        if self.sort != SortKey.DEFAULT:
            parts.append(f"排序：{SORT_LABELS[self.sort]}")
        if self.min_rating:
            parts.append(f"评分≥{self.min_rating}")
        if self.min_pages:
            parts.append(f"页数≥{self.min_pages}")
        if self.exclude:
            parts.append("排除：" + "、".join(self.exclude))
        return "，".join(parts) if parts else "无条件"


@dataclass
class WorkPage:
    """一页作品列表（搜索结果或排行榜）。"""

    items: list[WorkInfo] = field(default_factory=list)
    page: int = 1
    total_pages: Optional[int] = None
    has_next: bool = False
    title: str = ""


@dataclass
class ImageTask:
    """单张图片的下载任务。

    ``urls`` 为候选地址，按顺序尝试；为空时由 ``resolve`` 延迟解析
    （例如 E-Hentai 需先请求单页 HTML 才能拿到图片地址），每次重试都会重新解析。
    """

    index: int
    urls: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    resolve: Optional[Callable[[], Awaitable[list[str]]]] = None


@dataclass
class MediaBundle:
    """源站下载阶段的产物：图片序列或视频文件。

    Attributes:
        work: 对应作品
        images: 按页码排序的本地图片
        video: 本地视频文件
        link: 不下载时直接发送的播放 / 下载链接
        missing: 下载失败的页数
        truncated: 因页数上限跳过的页数
    """

    work: WorkInfo
    images: list[Path] = field(default_factory=list)
    video: Optional[Path] = None
    link: str = ""
    missing: int = 0
    truncated: int = 0


@dataclass
class FetchResult:
    """业务层“获取作品”的最终结果：可直接发送的文件或链接。

    Attributes:
        truncated: 因页数上限跳过的页数，与下载失败页数分开统计
    """

    work: WorkInfo
    files: list[Path] = field(default_factory=list)
    video: Optional[Path] = None
    link: str = ""
    missing: int = 0
    truncated: int = 0
    page_count: int = 0
    from_cache: bool = False
