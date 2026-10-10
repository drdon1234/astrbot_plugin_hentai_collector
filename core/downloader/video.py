"""视频下载：按大小上限流式写入临时文件后原子落盘。"""

from pathlib import Path
from typing import Optional

from ..constants import Config
from ..net import HttpClient


class VideoDownloader:
    """单文件视频下载器，大小上限由配置决定。"""

    def __init__(self, max_size_mb: float = 0.0):
        self.max_bytes = int(max(0.0, float(max_size_mb)) * 1024 * 1024)

    async def download(
        self,
        url: str,
        dest: Path,
        http: HttpClient,
        headers: Optional[dict[str, str]] = None,
    ) -> Path:
        """下载视频到目标路径并返回该路径。

        Args:
            url: 视频直链
            dest: 目标文件
            http: 所属源站的 HTTP 客户端
            headers: 额外请求头（如 Referer）

        Returns:
            下载完成的文件路径

        Raises:
            DownloadError: 超过大小上限或下载中断
        """
        await http.download(
            url,
            dest,
            headers=headers,
            max_bytes=self.max_bytes,
            timeout=Config.VIDEO_DOWNLOAD_TIMEOUT,
        )
        return dest
