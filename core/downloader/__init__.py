"""下载层导出面。"""

from .image import ImageDownloader, detect_image_suffix
from .video import VideoDownloader

__all__ = ["ImageDownloader", "VideoDownloader", "detect_image_suffix"]
