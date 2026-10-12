"""统一指令目录：固定操作标识、默认触发词与用户配置校验。"""

from dataclasses import dataclass
from typing import Any, Mapping

from .logger import logger


@dataclass(frozen=True)
class CommandSpec:
    """描述一个固定操作及其默认指令写法。

    Attributes:
        key: 配置、展示与绑定共用的操作标识。
        handler_name: AstrBot 中稳定的处理函数名称。
        label: 面向用户的操作说明。
        default_words: 默认触发词，第一项为提示中的首选词。
        action: 单站或聚合操作的业务动作。
        source: 单站操作对应的源站键。
        media: 聚合操作对应的媒体类型。
        usage: 指令参数写法。
        admin_only: 是否仅供管理员使用。
    """

    key: str
    handler_name: str
    label: str
    default_words: tuple[str, ...]
    action: str = ""
    source: str = ""
    media: str = ""
    usage: str = ""
    admin_only: bool = False


def _build_specs() -> dict[str, CommandSpec]:
    """按通用、聚合、单站顺序建立唯一指令目录。"""
    common = (
        ("help", "查看帮助", ("本子帮助", "hc", "hc帮助", "里番帮助"), "", False),
        ("page", "翻阅最近列表", ("翻页", "本子翻页"), "页码|下一页|上一页", False),
        ("get", "下载并发送文件", ("看本", "本子下载", "获取本子", "看里番"), "序号|链接", False),
        ("detail", "查看作品信息", ("本子详情", "详情"), "序号|链接", False),
        ("session", "查看当前会话 ID", ("本子会话",), "", False),
        ("push", "查看或执行推送任务", ("本子推送",), "[执行 序号]", True),
        ("clean", "清理全部缓存", ("本子清理缓存",), "", True),
    )
    specs = {
        key: CommandSpec(key, f"cmd_{key}", label, words, usage=usage, admin_only=admin)
        for key, label, words, usage, admin in common
    }
    actions = {
        "search": ("搜", "搜索", "[关键词] [条件]"),
        "ranking": ("排行", "排行榜", "[日榜|周榜|月榜|年榜|总榜] [页码]"),
        "random": ("随机", "随机", "[条件] [N本]"),
        "get": ("看", "下载并发送文件", "ID|链接"),
        "detail": ("详情", "查看作品信息", "ID|链接"),
    }
    aggregate_aliases = {
        "search_comic": ("搜本",),
        "ranking_comic": ("本子排行", "本子排行榜", "排行榜"),
        "random_comic": ("随机本",),
        "ranking_video": ("里番排行", "里番排行榜"),
    }
    for media, word in (("comic", "本子"), ("video", "里番")):
        for action in ("search", "ranking", "random"):
            verb, label, usage = actions[action]
            key = f"{action}_{media}"
            words = (f"{verb}{word}", *aggregate_aliases.get(key, ()))
            specs[key] = CommandSpec(key, f"cmd_{action}_{media}_all", f"全部{word}{label}", words,
                                     action=action, media=media, usage=usage)
    sites = (
        ("ehentai", "E-Hentai", ("eh", "ex")),
        ("nhentai", "nhentai", ("nh",)),
        ("jmcomic", "JMComic", ("jm", "禁漫")),
        ("pica", "哔咔", ("哔咔", "pica", "bika")),
        ("hanime1", "Hanime1", ("anime", "hanime", "h1")),
    )
    for source, name, tags in sites:
        for action, (verb, label, usage) in actions.items():
            key = f"{action}_{source}"
            specs[key] = CommandSpec(key, f"cmd_{action}_{source}", f"{name} {label}",
                                     tuple(f"{verb}{tag}" for tag in tags),
                                     action=action, source=source, usage=usage)
    return specs


COMMAND_SPECS = _build_specs()


class CommandCatalog:
    """校验触发词配置并提供稳定操作到多个触发词的映射。"""

    def __init__(self, raw: Any = None):
        if raw is not None and not isinstance(raw, Mapping):
            logger.warning("指令触发词配置必须为对象，已使用默认触发词")
        section = raw if isinstance(raw, Mapping) else {}
        self._words = {key: self._normalize(spec, section.get(key)) for key, spec in COMMAND_SPECS.items()}
        self._resolve_conflicts()

    @staticmethod
    def _normalize(spec: CommandSpec, raw: Any) -> tuple[str, ...]:
        """保留有效词的输入顺序，空配置或错误类型回退默认。"""
        if raw is None:
            return spec.default_words
        if not isinstance(raw, list):
            logger.warning(f"{spec.label}触发词必须填写列表，已使用默认触发词")
            return spec.default_words
        words = []
        for item in raw:
            word = item.strip() if isinstance(item, str) else ""
            if not word or any(char.isspace() for char in word) or word[0] in "/／!！#":
                logger.warning(f"{spec.label}包含无效触发词，已忽略；触发词不能留空、含空格或以唤醒前缀开头")
            elif word in words:
                logger.warning(f"{spec.label}触发词“{word}”重复，已保留第一项")
            else:
                words.append(word)
        if not words:
            logger.warning(f"{spec.label}没有有效触发词，已使用默认触发词；停用请使用 AstrBot 指令管理")
        return tuple(words) or spec.default_words

    def _resolve_conflicts(self) -> None:
        """反复回退冲突项，避免回退后的默认词再次占用其他操作。"""
        while True:
            owners: dict[str, list[str]] = {}
            for key, words in self._words.items():
                for word in words:
                    owners.setdefault(word, []).append(key)
            conflicts = {word: keys for word, keys in owners.items() if len(keys) > 1}
            if not conflicts:
                return
            for word, keys in conflicts.items():
                labels = "、".join(COMMAND_SPECS[key].label for key in keys)
                logger.warning(f"触发词“{word}”同时用于{labels}，相关自定义配置已回退默认触发词")
                for key in keys:
                    self._words[key] = COMMAND_SPECS[key].default_words

    def spec(self, key: str) -> CommandSpec:
        """返回操作的固定定义。"""
        return COMMAND_SPECS[key]

    def words(self, key: str) -> tuple[str, ...]:
        """返回该操作的全部有效配置词，首选词排在第一项。"""
        return self._words[key]

    def preferred(self, key: str) -> str:
        """返回用于帮助和操作提示的首选触发词。"""
        return self._words[key][0]
