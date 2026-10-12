"""漫画图片并发下载：候选地址轮换、延迟解析、重试与图片签名校验。"""

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..logger import logger

from ..async_utils import cancel_and_wait
from ..constants import Config
from ..errors import BlockedError, DownloadError, SourceError
from ..net import HttpClient
from ..types import ImageTask


ProgressCallback = Callable[[int, int], Awaitable[None]]

_SIGNATURES = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)


def detect_image_suffix(head: bytes) -> Optional[str]:
    """根据文件头判断图片格式，返回扩展名；不是图片时返回 None。"""
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    for signature, suffix in _SIGNATURES:
        if head.startswith(signature):
            return suffix
    return None


class ImageDownloader:
    """按任务列表并发下载图片到同一目录，文件名为四位页码。"""

    def __init__(
        self,
        concurrency: int = Config.DEFAULT_IMAGE_CONCURRENCY,
        retries: int = Config.DEFAULT_RETRIES,
    ):
        self.concurrency = max(1, int(concurrency))
        self.retries = max(0, int(retries))

    async def download_all(
        self,
        tasks: list[ImageTask],
        dest_dir: Path,
        http: HttpClient,
        progress: Optional[ProgressCallback] = None,
    ) -> tuple[list[Path], int]:
        """下载全部图片。

        Args:
            tasks: 图片任务，``index`` 决定文件名与顺序
            dest_dir: 保存目录
            http: 所属源站的 HTTP 客户端（携带代理与请求头）
            progress: 进度回调 ``(已完成数, 总数)``，每完成约 25% 调用一次

        Returns:
            (按页码排序的成功图片路径, 失败张数)

        Raises:
            BlockedError: 源站封禁或额度耗尽时立即中止
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        semaphore = asyncio.Semaphore(self.concurrency)
        total = len(tasks)
        done = 0
        next_report = max(1, total // 4)
        results: dict[int, Path] = {}

        async def worker(task: ImageTask) -> None:
            nonlocal done, next_report
            async with semaphore:
                path = await self._download_one(task, dest_dir, http)
            if path is not None:
                results[task.index] = path
            done += 1
            if progress and total >= 8 and done >= next_report and done < total:
                next_report += max(1, total // 4)
                await progress(done, total)

        jobs = [asyncio.create_task(worker(task)) for task in tasks]
        try:
            if jobs:
                completed, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_EXCEPTION)
                for job in completed:
                    job.result()
        finally:
            await cancel_and_wait(jobs)
        paths = [results[index] for index in sorted(results)]
        return paths, total - len(paths)

    async def _download_one(
        self, task: ImageTask, dest_dir: Path, http: HttpClient
    ) -> Optional[Path]:
        """依次尝试候选地址，失败后按重试次数重新解析。"""
        last_error = ""
        for attempt in range(1 + self.retries):
            try:
                urls = list(task.urls)
                if not urls and task.resolve is not None:
                    urls = await task.resolve()
            except BlockedError:
                raise
            except (SourceError, DownloadError) as exc:
                last_error = str(exc)
                urls = []
            for url in urls:
                try:
                    return await self._save(url, task, dest_dir, http)
                except asyncio.CancelledError:
                    raise
                except BlockedError:
                    raise
                except (SourceError, DownloadError) as exc:
                    last_error = str(exc)
            if attempt < self.retries:
                await asyncio.sleep(Config.RETRY_BASE_DELAY * (attempt + 1))
        logger.warning(f"第 {task.index} 页下载失败：{last_error or '没有可用地址'}")
        return None

    @staticmethod
    async def _save(url: str, task: ImageTask, dest_dir: Path, http: HttpClient) -> Path:
        temp = dest_dir / f"{task.index:04d}.download"
        size = await http.download(url, temp, headers=task.headers, timeout=120)
        try:
            with temp.open("rb") as stream:
                head = stream.read(16)
            suffix = detect_image_suffix(head)
            if size < Config.MIN_IMAGE_BYTES or suffix is None:
                raise DownloadError(f"返回内容不是有效图片（{size} 字节）")
            target = dest_dir / f"{task.index:04d}{suffix}"
            temp.replace(target)
            return target
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise DownloadError(f"保存图片失败：{exc}") from exc
        except DownloadError:
            temp.unlink(missing_ok=True)
            raise
