"""统一 HTTP 客户端：封装 aiohttp 与 curl_cffi 两种后端、代理、Cookie、重试与流式下载。"""

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import aiohttp
from yarl import URL

from ..logger import logger

from ..constants import Config
from ..errors import DownloadError, HttpError

try:  # curl_cffi 用于模拟浏览器 TLS 指纹，绕过 Cloudflare 校验
    from curl_cffi.requests import AsyncSession as _CurlSession
    from curl_cffi.requests.exceptions import RequestException as _CurlError
except ImportError:  # pragma: no cover - 依赖缺失时回退 aiohttp
    _CurlSession = None
    _CurlError = None


BACKEND_AIOHTTP = "aiohttp"
BACKEND_CURL = "curl"
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524})

_NETWORK_ERRORS: tuple = (aiohttp.ClientError, asyncio.TimeoutError, OSError)
if _CurlError is not None:
    _NETWORK_ERRORS = _NETWORK_ERRORS + (_CurlError,)


def curl_available() -> bool:
    """curl_cffi 是否可用。"""
    return _CurlSession is not None


@dataclass
class HttpResponse:
    """已完整读取的 HTTP 响应。"""

    status: int
    url: str
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    def text(self) -> str:
        """按 UTF-8 解码响应体，非法字节替换为占位符。"""
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """解析 JSON 响应体。

        Raises:
            HttpError: 响应体不是合法 JSON
        """
        try:
            return json.loads(self.content.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise HttpError(
                f"接口返回的不是 JSON（HTTP {self.status}）", self.status
            ) from exc


class HttpClient:
    """单个源站使用的 HTTP 客户端。

    每个源站持有独立实例，以隔离代理、Cookie、默认请求头与后端选择。
    后端为 ``curl`` 时使用 curl_cffi 模拟浏览器指纹，未安装时自动回退 aiohttp。
    """

    def __init__(
        self,
        name: str,
        *,
        proxy: str = "",
        timeout: int = Config.DEFAULT_TIMEOUT,
        retries: int = Config.DEFAULT_RETRIES,
        headers: Optional[dict[str, str]] = None,
        cookies: Optional[dict[str, str]] = None,
        backend: str = BACKEND_AIOHTTP,
        impersonate: str = Config.DEFAULT_IMPERSONATE,
    ):
        self.name = name
        self.proxy = str(proxy or "").strip()
        self.timeout = max(5, int(timeout))
        self.retries = max(0, int(retries))
        self.impersonate = impersonate or Config.DEFAULT_IMPERSONATE
        self.default_headers = {"User-Agent": Config.DEFAULT_USER_AGENT}
        self.default_headers.update(headers or {})
        self.cookies = {k: v for k, v in (cookies or {}).items() if k and v}
        self.backend = self._select_backend(backend)
        self._aiohttp_session: Optional[aiohttp.ClientSession] = None
        self._curl_session = None

    # ── 后端选择 ──────────────────────────────────────

    def _select_backend(self, backend: str) -> str:
        wants_curl = backend == BACKEND_CURL or self.proxy.lower().startswith("socks")
        if wants_curl and curl_available():
            return BACKEND_CURL
        if wants_curl:
            logger.warning(
                f"{self.name}: 未安装 curl_cffi，已回退 aiohttp；"
                "Cloudflare 保护的站点和 SOCKS 代理可能无法访问"
            )
        return BACKEND_AIOHTTP

    def _aiohttp(self) -> aiohttp.ClientSession:
        if self._aiohttp_session is None or self._aiohttp_session.closed:
            self._aiohttp_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
        return self._aiohttp_session

    def _curl(self):
        if self._curl_session is None:
            self._curl_session = _CurlSession(
                impersonate=self.impersonate,
                timeout=self.timeout,
                max_clients=16,
            )
        return self._curl_session

    def _merge_headers(self, headers: Optional[dict[str, str]]) -> dict[str, str]:
        merged = dict(self.default_headers)
        merged.update(headers or {})
        if self.cookies and "Cookie" not in merged:
            merged["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if self.backend == BACKEND_CURL:
            # curl_cffi 按模拟的浏览器补齐 UA，手写 UA 会与 TLS 指纹不一致
            merged.pop("User-Agent", None)
        return merged

    # ── 请求 ──────────────────────────────────────────

    async def _fetch_once(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        headers: Optional[dict[str, str]] = None,
        data: Any = None,
        raw_url: bool = False,
        timeout: Optional[int] = None,
    ) -> HttpResponse:
        merged = self._merge_headers(headers)
        if self.backend == BACKEND_CURL:
            resp = await self._curl().request(
                method,
                url,
                params=params,
                data=data,
                headers=merged,
                proxy=self.proxy or None,
                timeout=timeout or self.timeout,
                allow_redirects=True,
                quote=False if raw_url else "",
            )
            return HttpResponse(
                status=resp.status_code,
                url=str(resp.url),
                content=resp.content,
                headers={k.lower(): v for k, v in resp.headers.items()},
            )

        target = URL(url, encoded=True) if raw_url else url
        async with self._aiohttp().request(
            method,
            target,
            params=params,
            data=data,
            headers=merged,
            proxy=self.proxy or None,
            timeout=aiohttp.ClientTimeout(total=timeout or self.timeout),
        ) as resp:
            content = await resp.read()
            return HttpResponse(
                status=resp.status,
                url=str(resp.url),
                content=content,
                headers={k.lower(): v for k, v in resp.headers.items()},
            )

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        headers: Optional[dict[str, str]] = None,
        data: Any = None,
        allow_status: tuple[int, ...] = (),
        raw_url: bool = False,
        retries: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> HttpResponse:
        """发送请求并按配置重试网络错误与可重试状态码。

        Args:
            method: HTTP 方法
            url: 请求地址
            params: 查询参数
            headers: 额外请求头，覆盖默认请求头
            data: 请求体
            allow_status: 视为正常返回的非 2xx 状态码
            raw_url: 为 True 时不对 URL 重新编码（签名接口需要）
            retries: 覆盖默认重试次数
            timeout: 覆盖默认超时秒数

        Returns:
            完整读取的响应

        Raises:
            HttpError: 重试耗尽或返回不可接受的状态码
        """
        attempts = 1 + (self.retries if retries is None else max(0, retries))
        last_error: Optional[BaseException] = None
        for attempt in range(attempts):
            try:
                resp = await self._fetch_once(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    data=data,
                    raw_url=raw_url,
                    timeout=timeout,
                )
            except asyncio.CancelledError:
                raise
            except _NETWORK_ERRORS as exc:
                last_error = exc
                logger.debug(f"{self.name}: 请求失败 {attempt + 1}/{attempts} {url}: {exc!r}")
            else:
                if resp.status in RETRY_STATUS and attempt < attempts - 1:
                    last_error = HttpError(f"HTTP {resp.status}", resp.status)
                    logger.debug(f"{self.name}: HTTP {resp.status}，准备重试 {url}")
                elif resp.status >= 400 and resp.status not in allow_status:
                    raise HttpError(
                        f"{self.name} 请求失败：HTTP {resp.status}", resp.status
                    )
                else:
                    return resp
            if attempt < attempts - 1:
                await asyncio.sleep(Config.RETRY_BASE_DELAY * (2**attempt))
        if isinstance(last_error, HttpError):
            raise HttpError(
                f"{self.name} 请求失败：{last_error}", last_error.status
            ) from last_error
        raise HttpError(f"{self.name} 网络请求失败：{last_error!r}") from last_error

    async def get_text(self, url: str, **kwargs: Any) -> str:
        """GET 并返回文本。"""
        return (await self.request("GET", url, **kwargs)).text()

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        """GET 并解析 JSON。"""
        return (await self.request("GET", url, **kwargs)).json()

    async def post_json(self, url: str, payload: Any, **kwargs: Any) -> Any:
        """以 JSON 请求体 POST 并解析 JSON 响应。"""
        headers = {"Content-Type": "application/json"}
        headers.update(kwargs.pop("headers", None) or {})
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return (
            await self.request("POST", url, data=body, headers=headers, **kwargs)
        ).json()

    # ── 流式下载 ──────────────────────────────────────

    async def download(
        self,
        url: str,
        dest: Path,
        *,
        headers: Optional[dict[str, str]] = None,
        max_bytes: int = 0,
        timeout: Optional[int] = None,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> int:
        """流式下载到临时文件，完成后原子替换为目标文件。

        Args:
            url: 下载地址
            dest: 目标文件路径
            headers: 额外请求头
            max_bytes: 大小上限，0 表示不限制
            timeout: 总超时秒数
            progress: 进度回调 ``(已下载字节, 总字节或 0)``

        Returns:
            写入的字节数

        Raises:
            DownloadError: 状态码异常、超过大小上限或网络中断
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".part")
        written = 0
        try:
            if self.backend == BACKEND_CURL:
                written = await self._download_curl(
                    url, partial, headers, max_bytes, timeout, progress
                )
            else:
                written = await self._download_aiohttp(
                    url, partial, headers, max_bytes, timeout, progress
                )
            os.replace(partial, dest)
            return written
        except asyncio.CancelledError:
            partial.unlink(missing_ok=True)
            raise
        except _NETWORK_ERRORS as exc:
            partial.unlink(missing_ok=True)
            raise DownloadError(f"下载中断：{exc!r}") from exc
        except DownloadError:
            partial.unlink(missing_ok=True)
            raise

    @staticmethod
    def _check_size(total: int, max_bytes: int) -> None:
        if max_bytes and total > max_bytes:
            raise DownloadError(
                f"文件大小 {total / 1024 / 1024:.1f} MB 超过上限 "
                f"{max_bytes / 1024 / 1024:.0f} MB"
            )

    async def _download_aiohttp(self, url, partial, headers, max_bytes, timeout, progress) -> int:
        written = 0
        async with self._aiohttp().get(
            url,
            headers=self._merge_headers(headers),
            proxy=self.proxy or None,
            timeout=aiohttp.ClientTimeout(total=timeout or Config.VIDEO_DOWNLOAD_TIMEOUT),
        ) as resp:
            if resp.status >= 400:
                raise DownloadError(f"下载失败：HTTP {resp.status}")
            total = int(resp.headers.get("Content-Length") or 0)
            self._check_size(total, max_bytes)
            with partial.open("wb") as stream:
                async for chunk in resp.content.iter_chunked(Config.STREAM_CHUNK_SIZE):
                    written += len(chunk)
                    self._check_size(written, max_bytes)
                    stream.write(chunk)
                    if progress:
                        progress(written, total)
        return written

    async def _download_curl(self, url, partial, headers, max_bytes, timeout, progress) -> int:
        written = 0
        resp = await self._curl().request(
            "GET",
            url,
            headers=self._merge_headers(headers),
            proxy=self.proxy or None,
            timeout=timeout or Config.VIDEO_DOWNLOAD_TIMEOUT,
            allow_redirects=True,
            stream=True,
        )
        try:
            if resp.status_code >= 400:
                raise DownloadError(f"下载失败：HTTP {resp.status_code}")
            total = int(resp.headers.get("Content-Length") or 0)
            self._check_size(total, max_bytes)
            with partial.open("wb") as stream:
                async for chunk in resp.aiter_content():
                    written += len(chunk)
                    self._check_size(written, max_bytes)
                    stream.write(chunk)
                    if progress:
                        progress(written, total)
        finally:
            await resp.aclose()
        return written

    async def close(self) -> None:
        """关闭底层会话。"""
        if self._aiohttp_session is not None and not self._aiohttp_session.closed:
            await self._aiohttp_session.close()
        self._aiohttp_session = None
        if self._curl_session is not None:
            try:
                await self._curl_session.close()
            except _NETWORK_ERRORS as exc:
                logger.debug(f"{self.name}: 关闭 curl 会话失败: {exc!r}")
            self._curl_session = None
