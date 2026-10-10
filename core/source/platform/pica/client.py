"""哔咔（PicACG）App 接口客户端：HMAC 签名、账号登录与 Token 持久化。"""

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from typing import Optional

from ....logger import logger

from ....errors import BlockedError, SourceError, SourceUnavailable, WorkNotFound
from ....net import HttpClient


API_BASE = "https://picaapi.picacomic.com/"
API_KEY = "C69BAF41DA5ABD1FFEDC6D2FEA56B"
SECRET = r"~d}$Q7$eIni=V)9\RK/P.RM4;9[7|@/CA}b~OW!3?EV`:<>M7pddUBL5n|0/*Cn"
# 请求头必须与 App 一致，否则登录会被误判为 1023 too many requests
APP_HEADERS = {
    "api-key": API_KEY,
    "accept": "application/vnd.picacomic.com.v1+json",
    "app-channel": "1",
    "app-version": "2.2.1.2.3.3",
    "app-uuid": "defaultUuid",
    "app-platform": "android",
    "app-build-version": "44",
    "User-Agent": "okhttp/3.8.1",
}
LOGIN_BACKOFF = 600


def sign(path: str, timestamp: str, nonce: str, method: str) -> str:
    """计算哔咔接口签名。"""
    raw = (path + timestamp + nonce + method + API_KEY).lower()
    return hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()


class PicaClient:
    """带登录态的哔咔接口客户端；Token 有效期约 7 天，过期返回 401 时自动重新登录一次。"""

    def __init__(
        self,
        http: HttpClient,
        email: str,
        password: str,
        image_quality: str = "original",
        state_file: Optional[Path] = None,
    ):
        self.http = http
        self.email = str(email or "").strip()
        self.password = str(password or "")
        self.image_quality = image_quality
        self.state_file = state_file
        self._token: Optional[str] = None
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()
        self._load_state()

    @property
    def configured(self) -> bool:
        """是否填写了账号和密码。"""
        return bool(self.email and self.password)

    def _credentials_key(self) -> str:
        return hashlib.sha256(f"{self.email}\0{self.password}".encode()).hexdigest()

    def _load_state(self) -> None:
        if not self.state_file or not self.configured:
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and data.get("credentials") == self._credentials_key():
            self._token = data.get("token") or None

    def _save_state(self) -> None:
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps({"credentials": self._credentials_key(), "token": self._token}),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(f"保存哔咔登录状态失败：{exc}")

    async def _send(self, method: str, path: str, body: Optional[dict]) -> tuple[int, dict]:
        timestamp, nonce = str(int(time.time())), uuid.uuid4().hex
        headers = dict(APP_HEADERS)
        headers.update(
            {
                "time": timestamp,
                "nonce": nonce,
                "signature": sign(path, timestamp, nonce, method),
                "image-quality": self.image_quality,
            }
        )
        if self._token:
            headers["authorization"] = self._token
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json; charset=UTF-8"
            data = json.dumps(body).encode()
        resp = await self.http.request(
            method,
            API_BASE + path,
            headers=headers,
            data=data,
            raw_url=True,
            allow_status=(400, 401, 403, 404),
        )
        try:
            payload = resp.json()
        except SourceError:
            payload = {"message": resp.text()[:200]}
        return resp.status, payload if isinstance(payload, dict) else {}

    async def _login(self, stale: Optional[str]) -> None:
        async with self._lock:
            if self._token and self._token != stale:
                return
            if not self.configured:
                raise SourceUnavailable("未填写哔咔账号和密码")
            if time.time() < self._blocked_until:
                raise BlockedError("哔咔登录失败，冷却中，请稍后再试")
            self._token = None
            status, data = await self._send(
                "POST", "auth/sign-in", {"email": self.email, "password": self.password}
            )
            token = (data.get("data") or {}).get("token")
            if status != 200 or not token:
                self._blocked_until = time.time() + LOGIN_BACKOFF
                raise BlockedError(f"哔咔登录失败：{data.get('message') or status}")
            self._token = token
            self._save_state()
            logger.info("哔咔登录成功")

    async def request(self, path: str, body: Optional[dict] = None) -> dict:
        """发送带登录态的请求，返回响应中的 ``data`` 字段。

        Args:
            path: 不含域名的接口路径（含查询参数，参与签名）
            body: 请求体；非空时使用 POST

        Raises:
            WorkNotFound: 作品不存在
            SourceError: 请求失败
        """
        if self._token is None:
            await self._login(None)
        method = "GET" if body is None else "POST"
        for attempt in range(2):
            token = self._token
            status, data = await self._send(method, path, body)
            if status == 401 and attempt == 0:
                await self._login(token)
                continue
            if status in (400, 404):
                raise WorkNotFound(f"哔咔作品不存在：{data.get('message', '')}")
            if status != 200:
                raise SourceError(f"哔咔请求失败：HTTP {status} {data.get('message', '')}")
            return data.get("data") or {}
        raise SourceError("哔咔登录状态失效")
