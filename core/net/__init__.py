"""网络访问层导出面。"""

from .http_client import (
    BACKEND_AIOHTTP,
    BACKEND_CURL,
    HttpClient,
    HttpResponse,
    curl_available,
)

__all__ = [
    "BACKEND_AIOHTTP",
    "BACKEND_CURL",
    "HttpClient",
    "HttpResponse",
    "curl_available",
]
