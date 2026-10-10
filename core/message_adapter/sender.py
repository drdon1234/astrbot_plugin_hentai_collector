"""消息投递：统一被动回复与主动推送两种发送通道，封装文本、图片、合并转发、文件与视频。"""

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import File, Image, Node, Nodes, Plain, Video

from ..logger import logger

from ..constants import Config
from .formatter import chunk_text


SendFunction = Callable[[list], Awaitable[Any]]
FORWARD_PLATFORMS = frozenset({"aiocqhttp"})


class Delivery:
    """一次投递的目标。

    指令回复使用 ``event.send``，定时推送使用 ``context.send_message``；
    上层只依赖本类，不关心消息来自哪个通道。
    """

    def __init__(self, send: SendFunction, *, supports_forward: bool, self_id: str = "0"):
        self._send = send
        self.supports_forward = supports_forward
        self.self_id = str(self_id or "0")

    @classmethod
    def for_event(cls, event: AstrMessageEvent, use_forward: bool) -> "Delivery":
        """为消息事件创建投递对象。"""

        async def send(chain: list) -> None:
            await event.send(event.chain_result(chain))

        platform = str(event.get_platform_name() or "")
        return cls(
            send,
            supports_forward=use_forward and platform in FORWARD_PLATFORMS,
            self_id=str(event.get_self_id() or "0"),
        )

    @classmethod
    def for_session(cls, context: Any, umo: str, use_forward: bool) -> "Delivery":
        """为会话 ID（unified_msg_origin）创建主动推送投递对象。"""

        async def send(chain: list) -> None:
            found = await context.send_message(umo, MessageChain(chain=chain))
            if found is False:
                raise RuntimeError(f"没有找到会话 {umo} 对应的平台")

        platform_id = umo.split(":", 1)[0]
        platform_name = ""
        for inst in getattr(context.platform_manager, "platform_insts", []):
            meta = inst.meta()
            if meta.id == platform_id:
                platform_name = meta.name
                break
        return cls(send, supports_forward=use_forward and platform_name in FORWARD_PLATFORMS)

    # ── 发送原语 ──────────────────────────────────────

    async def chain(self, components: list) -> None:
        """发送一条由组件组成的消息。"""
        await self._send(components)

    async def text(self, text: str) -> None:
        """发送文本，超长时分片。"""
        for part in chunk_text(text):
            await self._send([Plain(part)])

    async def image(self, data: bytes, caption: str = "") -> None:
        """发送图片（可附带文字）。"""
        components = [Image.fromBytes(data)]
        if caption:
            components.append(Plain(caption))
        await self._send(components)

    async def forward(self, contents: list[list], name: str = Config.BOT_DISPLAY_NAME) -> None:
        """以合并转发发送多段内容；平台不支持时逐条发送。

        Args:
            contents: 每个元素是一段消息的组件列表
            name: 转发节点显示的昵称
        """
        contents = [content for content in contents if content]
        if not contents:
            return
        if not self.supports_forward:
            for content in contents:
                await self._send(content)
                await asyncio.sleep(Config.SEND_INTERVAL)
            return
        for start in range(0, len(contents), Config.FORWARD_MAX_NODES):
            batch = contents[start:start + Config.FORWARD_MAX_NODES]
            nodes = [Node(content=content, name=name, uin=self.self_id) for content in batch]
            await self._send([Nodes(nodes)])

    async def file(self, path: Path) -> None:
        """发送本地文件。"""
        resolved = Path(path).resolve()
        await self._send([File(name=resolved.name, file=str(resolved))])

    async def video(self, path: Path) -> None:
        """发送本地视频。"""
        await self._send([Video.fromFileSystem(str(Path(path).resolve()))])

    async def safe_text(self, text: str) -> None:
        """发送提示文本，失败只记日志（用于进度提示等非关键消息）。"""
        try:
            await self.text(text)
        except Exception as exc:  # 协议端异常类型不统一，提示消息失败不应中断主流程
            logger.warning(f"发送提示消息失败：{exc}")
