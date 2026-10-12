"""AstrBot 指令绑定：配置触发词与原生过滤器、WebUI 状态共用。"""

from typing import Any

from ..logger import logger

from ..commands import COMMAND_SPECS, CommandCatalog, CommandSpec


class CommandBindings:
    """将指令目录绑定到原生处理器，并实时读取改名与禁用状态。"""

    def __init__(self, catalog: CommandCatalog, module_name: str):
        self.catalog = catalog
        self.module_name = module_name
        self._handlers: dict[str, tuple[Any, Any]] = {}
        self._snapshots: list[tuple[Any, str, set[str], str, set[str]]] = []

    async def initialize(self) -> None:
        """应用配置触发词，然后载入 AstrBot 已保存的改名和禁用设置。"""
        from astrbot.core.star.command_management import list_commands
        from astrbot.core.star.filter.command import CommandFilter
        from astrbot.core.star.filter.command_group import CommandGroupFilter
        from astrbot.core.star.star_handler import star_handlers_registry

        self.close()
        # 先应用其他插件已保存的改名，冲突检查必须基于其实际生效词。
        await list_commands()
        words = {key: list(self.catalog.words(key)) for key in COMMAND_SPECS}
        external_words = {
            word
            for handler in star_handlers_registry
            if handler.handler_module_path != self.module_name
            for command_filter in handler.event_filters
            if isinstance(command_filter, (CommandFilter, CommandGroupFilter))
            for word in command_filter.get_complete_command_names()
        }
        for key, spec in COMMAND_SPECS.items():
            added = set(words[key]) - set(spec.default_words)
            conflicts = added & external_words
            if conflicts:
                logger.warning(f"{spec.label}自定义触发词与其他插件冲突：{'、'.join(sorted(conflicts))}；已使用默认触发词")
                words[key] = list(spec.default_words)
        effective = CommandCatalog(words)
        initialized = False
        try:
            for key, spec in COMMAND_SPECS.items():
                handler = star_handlers_registry.get_handler_by_full_name(f"{self.module_name}_{spec.handler_name}")
                if handler is None:
                    raise RuntimeError(f"无法绑定指令：{spec.label}的处理函数未注册")
                command_filter = next((item for item in handler.event_filters if isinstance(item, CommandFilter)), None)
                if command_filter is None:
                    raise RuntimeError(f"无法绑定指令：{spec.label}缺少 AstrBot 指令过滤器")
                configured = effective.words(key)
                original_name, original_aliases = command_filter.command_name, set(command_filter.alias)
                self._snapshots.append((command_filter, original_name, original_aliases,
                                        configured[0], set(configured[1:])))
                command_filter.command_name = configured[0]
                command_filter.alias = set(configured[1:])
                command_filter._cmpl_cmd_names = None
                self._handlers[key] = (handler, command_filter)
            # 官方入口重新应用本插件的 WebUI 覆盖，不删除任何配置。
            await list_commands()
            initialized = True
        finally:
            if not initialized:
                # 初始化异常或取消时归还已改动的过滤器，异常继续由 AstrBot 处理。
                self.close()

    def close(self) -> None:
        """恢复本实例仍持有的改动，保留初始化后 WebUI 更新的触发词。"""
        for command_filter, name, aliases, applied_name, applied_aliases in self._snapshots:
            if command_filter.command_name == applied_name and command_filter.alias == applied_aliases:
                command_filter.command_name = name
                command_filter.alias = aliases
                command_filter._cmpl_cmd_names = None
        self._snapshots.clear()
        self._handlers.clear()

    def spec(self, key: str) -> CommandSpec:
        """返回操作的固定定义。"""
        return self.catalog.spec(key)

    def words(self, key: str, *, include_disabled: bool = False) -> tuple[str, ...]:
        """读取运行时触发词；默认不返回已禁用指令。"""
        binding = self._handlers.get(key)
        if binding is None:
            return self.catalog.words(key)
        handler, command_filter = binding
        if not handler.enabled and not include_disabled:
            return ()
        primary = command_filter.command_name
        aliases = sorted(word for word in command_filter.alias if word != primary)
        return (primary, *aliases)

    def preferred(self, key: str) -> str:
        """返回当前可用首选词，禁用时返回空字符串以便隐藏操作提示。"""
        words = self.words(key)
        return words[0] if words else ""

    def is_command(self, text: str) -> bool:
        """识别本插件指令消息，包括已禁用指令，防止自动解析绕过禁用。"""
        # AstrBot 已去掉真实唤醒前缀；再次猜测前缀会破坏 WebUI 允许的特殊符号词。
        message = " ".join(str(text or "").split())
        return any(
            message == word or message.startswith(word + " ")
            for key in COMMAND_SPECS
            for word in self.words(key, include_disabled=True)
        )
