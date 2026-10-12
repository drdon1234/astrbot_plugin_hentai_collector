"""文本格式化：作品信息卡片、单站与聚合列表、获取结果与长文本分片。"""

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


def format_work_card(
    work: WorkInfo,
    source_name: str,
    *,
    get_hint: str,
    max_tags: int = 12,
    show_description: bool = True,
) -> str:
    """生成作品信息卡片文本。

    Args:
        work: 作品
        source_name: 源站显示名
        get_hint: 可直接复制的获取指令（如 ``看jm 350234``）
        max_tags: 最多展示的标签数，0 表示不展示
        show_description: 是否展示简介

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
    if get_hint:
        lines.append(f"{action}：{get_hint}")
    else:
        lines.append("下载指令已停用，可通过作品链接查看。")
    return "\n".join(lines)


def format_list_entry(index: int, work: WorkInfo, short: str) -> str:
    """生成单条作品摘要，将标题、统计与描述字段分行展示。"""
    meta = [short]
    if work.pages:
        meta.append(f"{work.pages}页")
    if work.duration:
        meta.append(f"时长 {work.duration}")
    if work.rating is not None:
        meta.append(f"★{work.rating:.1f}")
    if work.likes is not None:
        meta.append(f"♥{format_count(work.likes)}")
    if work.views is not None:
        meta.append(f"▶{format_count(work.views)}")
    lines = [f"[{index}] {work.title}", " · ".join(meta)]
    if work.category:
        lines.append(f"分类：{work.category}")
    if work.author:
        lines.append(f"作者：{work.author}")
    if work.uploaded:
        lines.append(f"发布：{work.uploaded}")
    return "\n".join(lines)


def format_list_blocks(
    page: WorkPage,
    shorts: dict[str, str],
    get_command: str,
    page_command: str,
    *,
    start: int = 1,
    tips: bool = True,
    detail_command: str = "",
    session_minutes: int = 30,
) -> list[str]:
    """生成列表文本块（标题、各条作品与操作提示）。

    Args:
        page: 列表页
        shorts: 源站键 → 短名
        get_command: 按序号获取作品的指令名
        page_command: 翻页指令名
        start: 第一条的序号（聚合列表中各站序号连续）
        tips: 是否附带操作提示（聚合列表的提示放在总标题中）
        detail_command: 查看详情的有效指令名，空字符串表示停用
        session_minutes: 最近一次列表的有效分钟数

    Returns:
        文本块列表，合并转发时逐块发送，普通消息以空行连接
    """
    header = page.title or "作品列表"
    if page.total_pages:
        header += f"\n（第 {page.page}/{page.total_pages} 页）"
    else:
        header += f"\n（第 {page.page} 页）"
    blocks = [header]
    if page.notes:
        blocks.append("\n".join(page.notes))
    for index, work in enumerate(page.items, start):
        blocks.append(format_list_entry(index, work, shorts.get(work.source, work.source)))
    if tips:
        blocks.append(list_tips(
            get_command, page_command, page.page, page.has_next,
            detail_command=detail_command, session_minutes=session_minutes,
        ))
    return blocks


def list_tips(
    get_command: str, page_command: str, page: int, has_next: bool,
    *, detail_command: str = "", session_minutes: int = 30,
) -> str:
    """列表底部的操作提示。"""
    tips = []
    if detail_command:
        tips.append(f"先看详情：「{detail_command} 1」")
    if get_command:
        tips.append(f"下载文件：「{get_command} 1」（把 1 换成作品序号）")
    if page_command and page > 1:
        tips.append(f"「{page_command} {page - 1}」返回上一页")
    if page_command and has_next:
        tips.append(f"「{page_command} {page + 1}」查看下一页")
    if not has_next:
        tips.append("已经是最后一页。")
    tips.append(f"序号仅对应本人在当前聊天中的最近一次列表，{session_minutes} 分钟后失效；新列表会替换旧序号。")
    return "\n".join(tips)


def format_aggregate_header(title: str, page: int, summary: list[tuple[str, str]], tips: str) -> str:
    """生成聚合列表的总标题。

    Args:
        title: 指令标题（如 ``本子聚合搜索：关键词「原神」``）
        page: 当前页码，0 表示不显示页码（随机结果）
        summary: (源站名, 该站情况) 列表，如 ``("E-Hentai", "1-10")``
        tips: 操作提示，空字符串表示不显示

    Returns:
        多行文本
    """
    blocks = [f"{title}\n（第 {page} 页）" if page else title]
    if summary:
        blocks.append("\n".join(f"· {name}：{state}" for name, state in summary))
    if tips:
        blocks.append(tips)
    return "\n\n".join(blocks)


def format_fetch_summary(result: FetchResult, retry_hint: str = "") -> str:
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
    if result.truncated:
        parts.append(f"因页数上限跳过 {result.truncated} 页")
    if result.from_cache:
        parts.append("使用缓存")
    text = "，".join(parts) + "，正在发送…"
    if result.missing:
        text += "\n本次文件不完整，下次获取会重新下载。"
        if retry_hint:
            text += f"可重试：{retry_hint}"
    if result.truncated:
        text += "\n如需完整作品，请管理员调高“文件输出 → 单本最大页数”后重新获取。"
    return text
