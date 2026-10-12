"""异步任务收尾：取消后等待真实工作完成再释放其资源。"""

import asyncio
from typing import Any, Callable, TypeVar

from .logger import logger


Result = TypeVar('Result')


async def run_sync(func: Callable[..., Result], *args: Any, **kwargs: Any) -> Result:
    """在线程中执行同步函数；调用方取消时等待线程退出后再传播取消。

    Args:
        func: 需要在线程中执行的同步函数
        args: 函数的位置参数
        kwargs: 函数的关键字参数

    Returns:
        同步函数的返回结果
    """
    worker = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    completion = asyncio.gather(worker, return_exceptions=True)
    cancellation = None
    while not completion.done():
        try:
            await asyncio.shield(completion)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    if cancellation is not None:
        if not worker.cancelled() and worker.exception() is not None:
            logger.warning(f'取消后同步工作收尾失败：{worker.exception()}')
        raise cancellation
    return worker.result()


async def cancel_and_wait(tasks: list[asyncio.Task]) -> None:
    """取消剩余子任务并等全部收尾，等待期间再次取消仍先保证子任务结束。

    Args:
        tasks: 当前批次创建的子任务
    """
    for task in tasks:
        if not task.done():
            task.cancel()
    if not tasks:
        return
    completion = asyncio.gather(*tasks, return_exceptions=True)
    cancellation = None
    while not completion.done():
        try:
            await asyncio.shield(completion)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    if cancellation is not None:
        raise cancellation
