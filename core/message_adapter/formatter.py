"""文本格式化：作品信息卡片、列表、获取结果与长文本分片。"""

from typing import Optional

from ..constants import Config
from ..types import FetchResult, MediaType, WorkInfo, WorkPage


DESCRIPTION_LIMIT = 200


def format_count(value: Optional[int]) -> str:
    """把大数格式化为 ``1.2万`` 形式。"""
    if value is None:
        return ""
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f}万"
    return str(value)


def chunk_text(text: str, limit: int = Config.TEXT_CHUNK_LIMIT) -> list[str]:
    """按行切分超长文本，单行超长时硬切。"""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


def reference_hint(work: WorkInfo, alias: str) -> str:
    """生成可直接复制的获取指令参数。"""
    return f"{alias} {work.work_id}"


def format_work_card(
    work: WorkInfo,
    source_name: str,
    *,
    alias: str,
    max_tags: int = 12,
    show_description: bool = True,
    get_command: str = "看本",
) -> str:
    """生成作品信息卡片文本。

    Args:
        work: 作品
        source_name: 源站显示名
        alias: 源站短别名，用于生成获取指令
        max_tags: 最多展示的标签数，0 表示不展示
        show_description: 是否展示简介
        get_command: 获取作品的指令名

    Returns:
        多行文本
    """
    lines = [f"【{source_name}】{work.title}"]
    if work.subtitle and work.subtitle != work.title:
        lines.append(f"原名：{work.subtitle}")

    meta = []
    if work.author:
        meta.append(f"作者：{work.author}")
    if work.category:
        meta.append(f"分类：{work.category}")
    if work.language:
        meta.append(f"语言：{work.language}")
    if meta:
        lines.append(" ｜ ".join(meta))

    stats = []
    if work.pages:
        stats.append(f"页数：{work.pages}")
    if work.duration:
        stats.append(f"时长：{work.duration}")
    if work.rating is not None:
        stats.append(f"评分：{work.rating:.1f}")
    if work.likes is not None:
        stats.append(f"点赞：{format_count(work.likes)}")
    if work.views is not None:
        stats.append(f"观看：{format_count(work.views)}")
    if stats:
        lines.append(" ｜ ".join(stats))
    if work.uploaded:
        lines.append(f"发布：{work.uploaded}")

    if max_tags and work.tags:
        shown = work.tags[:max_tags]
        suffix = f"（共 {len(work.tags)} 个）" if len(work.tags) > max_tags else ""
        lines.append("标签：" + "、".join(shown) + suffix)
    if show_description and work.description:
        text = work.description.replace("\n", " ").strip()
        if len(text) > DESCRIPTION_LIMIT:
            text = text[:DESCRIPTION_LIMIT] + "…"
        lines.append(f"简介：{text}")
    if work.url:
        lines.append(f"链接：{work.url}")
    action = "获取视频" if work.media_type == MediaType.VIDEO else "获取文件"
    lines.append(f"{action}：{get_command} {reference_hint(work, alias)}")
    return "\n".join(lines)


def format_list_entry(index: int, work: WorkInfo, short: str) -> str:
    """生成列表中的单条作品摘要。"""
    meta = [short]
    if work.pages:
        meta.append(f"{work.pages}页")
    if work.duration:
        meta.append(work.duration)
    if work.rating is not None:
        meta.append(f"★{work.rating:.1f}")
    if work.likes is not None:
        meta.append(f"♥{format_count(work.likes)}")
    if work.views is not None:
        meta.append(f"▶{format_count(work.views)}")
    if work.category:
        meta.append(work.category)
    if work.author:
        meta.append(work.author)
    if work.uploaded:
        meta.append(work.uploaded)
    return f"[{index}] {work.title}\n    {' · '.join(meta)}"


def format_work_list(page: WorkPage, shorts: dict[str, str], get_command: str, page_command: str) -> str:
    """生成列表文本（标题、条目与操作提示）。"""
    header = page.title or "作品列表"
    if page.total_pages:
        header += f"（第 {page.page}/{page.total_pages} 页）"
    else:
        header += f"（第 {page.page} 页）"
    lines = [header]
    for index, work in enumerate(page.items, 1):
        lines.append(format_list_entry(index, work, shorts.get(work.source, work.source)))
    tips = [f"发送「{get_command} 序号」获取作品"]
    if page.has_next:
        tips.append(f"「{page_command} {page.page + 1}」查看下一页")
    lines.append("—— " + "，".join(tips))
    return "\n".join(lines)


def format_fetch_summary(result: FetchResult) -> str:
    """生成文件发送前的说明文字。"""
    work = result.work
    if result.link:
        return f"《{work.title}》\n视频地址（有时效）：{result.link}"
    if result.video is not None:
        return f"《{work.title}》视频准备完成，正在发送…"
    parts = [f"《{work.title}》"]
    if result.page_count:
        parts.append(f"共 {result.page_count} 页")
    if len(result.files) > 1:
        parts.append(f"{len(result.files)} 个文件")
    if result.missing:
        parts.append(f"{result.missing} 页下载失败已跳过")
    if result.from_cache:
        parts.append("使用缓存")
    return "，".join(parts) + "，正在发送…"
