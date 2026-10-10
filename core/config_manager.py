"""配置管理模块，负责默认值处理、类型转换、配置兜底与源站工厂。"""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .logger import logger

from .constants import Config
from .net import BACKEND_AIOHTTP, BACKEND_CURL, HttpClient
from .packager import FORMAT_PDF, OUTPUT_FORMATS, PackageOptions
from .source import (
    BaseSource,
    EHentaiSource,
    Hanime1Source,
    JmComicSource,
    NHentaiSource,
    PicaSource,
    SourceManager,
)
from .source.platform.ehentai import resolve_site


# 源站统一顺序：文档、配置、工厂与帮助文本保持一致
SOURCE_KEYS = ("ehentai", "nhentai", "jmcomic", "pica", "hanime1")
SOURCE_DISPLAY_NAMES = {
    "ehentai": "E-Hentai",
    "nhentai": "nhentai",
    "jmcomic": "JMComic",
    "pica": "哔咔",
    "hanime1": "Hanime1",
}
SOURCE_DEFAULT_ENABLED = {"ehentai": True, "nhentai": True, "jmcomic": True, "pica": False, "hanime1": True}
RANDOM_POOL_DEFAULTS = {"ehentai": True, "nhentai": True, "jmcomic": True, "pica": False, "hanime1": False}

LINK_ACTION_FULL = "发送信息和文件"
LINK_ACTION_INFO = "仅发送信息"


# ── 类型转换兜底 ────────────────────────────────────────


def _section(raw: Any, key: str) -> dict:
    value = raw.get(key) if isinstance(raw, Mapping) else None
    return dict(value) if isinstance(value, Mapping) else {}


def as_bool(value: Any, default: bool) -> bool:
    """宽松转换布尔值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on", "是", "开启"}:
            return True
        if lowered in {"false", "0", "no", "off", "否", "关闭"}:
            return False
    return default


def as_int(value: Any, default: int, lower: Optional[int] = None, upper: Optional[int] = None) -> int:
    """宽松转换整数并限制范围。"""
    try:
        parsed = int(float(value))
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if lower is not None:
        parsed = max(lower, parsed)
    if upper is not None:
        parsed = min(upper, parsed)
    return parsed


def as_float(value: Any, default: float, lower: float = 0.0, upper: Optional[float] = None) -> float:
    """宽松转换浮点数并限制范围。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    parsed = max(lower, parsed)
    return min(upper, parsed) if upper is not None else parsed


def as_str(value: Any, default: str = "") -> str:
    """转换为去除首尾空白的字符串。"""
    if value is None:
        return default
    return str(value).strip()


def as_choice(value: Any, options: tuple, default: str) -> str:
    """限制为候选值之一。"""
    text = as_str(value, default)
    return text if text in options else default


def as_str_list(value: Any) -> list[str]:
    """转换为去重、去空的字符串列表。"""
    if isinstance(value, str):
        value = [part for part in value.replace("，", ",").split(",")]
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def source_key_from_name(value: Any, default: str = "") -> str:
    """把配置中的源站显示名或键转换为源站键。"""
    text = as_str(value).lower()
    for key, name in SOURCE_DISPLAY_NAMES.items():
        if text in (key, name.lower()):
            return key
    return default


# ── 运行环境路径 ────────────────────────────────────────


def is_docker() -> bool:
    """判断当前是否运行在 Docker 容器内。"""
    return os.path.exists("/.dockerenv")


def resolve_plugin_data_dir() -> Path:
    """获取插件持久化数据目录；非 AstrBot 运行时回退到工作目录下的 data。"""
    try:
        from astrbot.api.star import StarTools

        return Path(StarTools.get_data_dir(Config.PLUGIN_NAME))
    except (ImportError, RuntimeError, ValueError):
        return Path(os.getcwd()) / "data" / Config.PLUGIN_NAME


def resolve_cache_dir(configured: str, data_dir: Path) -> str:
    """解析缓存目录：配置值优先，Docker 默认使用共享挂载路径，其余使用插件数据目录。"""
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    if is_docker():
        return Config.DOCKER_CACHE_DIR
    return Config.build_cache_dir(str(data_dir))


def check_dir_writable(path: str) -> tuple[bool, str]:
    """创建并检查目录可写。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".write_probe_{os.getpid()}")
        with open(probe, "wb") as stream:
            stream.write(b"ok")
        os.unlink(probe)
        return True, ""
    except OSError as exc:
        return False, f"缓存目录不可写：{path}：{exc}"


# ── 配置分组 dataclass ──────────────────────────────────


@dataclass
class BasicConfig:
    auto_parse: bool = True
    parse_jm_code: bool = True
    link_action: str = LINK_ACTION_FULL
    default_source: str = "ehentai"
    send_progress: bool = True

    @property
    def send_files_on_link(self) -> bool:
        """自动解析链接时是否继续下载并发送文件。"""
        return self.link_action == LINK_ACTION_FULL


@dataclass
class NetworkConfig:
    proxy: str = ""
    timeout: int = Config.DEFAULT_TIMEOUT
    retries: int = Config.DEFAULT_RETRIES
    image_concurrency: int = Config.DEFAULT_IMAGE_CONCURRENCY

    def proxy_for(self, enabled: bool) -> str:
        """源站开启代理且全局代理非空时返回代理地址。"""
        return self.proxy if enabled else ""


@dataclass
class EHentaiConfig:
    site: str = "e-hentai"
    cookies: dict[str, str] = field(default_factory=dict)
    use_proxy: bool = True
    request_interval: float = 0.5


@dataclass
class NHentaiConfig:
    language: str = "不限"
    use_proxy: bool = True


@dataclass
class JmConfig:
    use_proxy: bool = False
    domains: list[str] = field(default_factory=list)
    image_threads: int = 10


@dataclass
class PicaConfig:
    email: str = ""
    password: str = ""
    image_quality: str = "original"
    use_proxy: bool = True


@dataclass
class Hanime1Config:
    default_genre: str = "裏番"
    quality: int = 1080
    send_mode: str = "视频"
    max_size_mb: float = 500.0
    cookie: str = ""
    use_proxy: bool = True


@dataclass
class OutputConfig:
    max_pages: int = 0
    package: PackageOptions = field(default_factory=PackageOptions)


@dataclass
class DisplayConfig:
    show_cover: bool = True
    cover_blur: int = 0
    search_cover_grid: bool = True
    use_forward: bool = True
    list_size: int = 20
    max_tags: int = 12
    show_description: bool = True


@dataclass
class RandomConfig:
    auto_fetch: bool = False
    pool: list[str] = field(default_factory=list)


@dataclass
class PushConfig:
    enable: bool = False
    platform_id: str = ""
    tasks: list[dict] = field(default_factory=list)


@dataclass
class PermissionConfig:
    configuration_valid: bool = True
    admin_id: str = ""
    whitelist_enable: bool = False
    whitelist_user: list[str] = field(default_factory=list)
    whitelist_group: list[str] = field(default_factory=list)
    blacklist_enable: bool = False
    blacklist_user: list[str] = field(default_factory=list)
    blacklist_group: list[str] = field(default_factory=list)

    def is_admin(self, sender_id: Any) -> bool:
        """是否为插件管理员。"""
        return bool(self.admin_id) and str(sender_id or "").strip() == self.admin_id

    def check(self, is_private: bool, sender_id: Any, group_id: Any) -> bool:
        """检查用户或群组是否有权限使用插件。

        优先级：管理员 > 个人白名单 > 个人黑名单 > 群白名单 > 群黑名单；
        均未命中时，白名单开启则拒绝，否则放行。
        """
        if not self.configuration_valid:
            return False
        sender = str(sender_id or "").strip()
        group = "" if is_private else str(group_id or "").strip()
        if self.is_admin(sender):
            return True
        allowed = None
        if self.whitelist_enable and sender in self.whitelist_user:
            allowed = True
        elif self.blacklist_enable and sender in self.blacklist_user:
            allowed = False
        elif self.whitelist_enable and group and group in self.whitelist_group:
            allowed = True
        elif self.blacklist_enable and group and group in self.blacklist_group:
            allowed = False
        return (not self.whitelist_enable) if allowed is None else allowed


@dataclass
class CacheConfig:
    cache_dir: str = ""
    cache_dir_available: bool = False
    cache_dir_error: str = ""
    keep_hours: float = 24.0

    @property
    def retention_seconds(self) -> int:
        """已生成文件的保留秒数（至少保留到协议端拉取完成）。"""
        return max(Config.MIN_FILE_RETENTION_SECONDS, int(self.keep_hours * 3600))


@dataclass
class LimitConfig:
    max_concurrent_jobs: int = Config.DEFAULT_MAX_CONCURRENT_JOBS
    user_cooldown_seconds: int = 0
    session_minutes: int = 30


# ── 配置管理器 ──────────────────────────────────────────


class ConfigManager:
    """把 AstrBot 配置字典归一为分组 dataclass，并负责创建源站实例。"""

    def __init__(self, raw: Optional[Mapping[str, Any]], data_dir: Path):
        raw = raw if isinstance(raw, Mapping) else {}
        self.data_dir = data_dir
        self.basic = self._parse_basic(_section(raw, "basic"))
        self.enabled_sources = self._parse_enabled(_section(raw, "sources"))
        self.network = self._parse_network(_section(raw, "network"))
        self.ehentai = self._parse_ehentai(_section(raw, "ehentai"))
        self.nhentai = NHentaiConfig(
            language=as_choice(_section(raw, "nhentai").get("language"), ("不限", "中文", "英文", "日文"), "不限"),
            use_proxy=as_bool(_section(raw, "nhentai").get("use_proxy"), True),
        )
        self.jmcomic = self._parse_jm(_section(raw, "jmcomic"))
        self.pica = self._parse_pica(_section(raw, "pica"))
        self.hanime1 = self._parse_hanime1(_section(raw, "hanime1"))
        self.output = self._parse_output(_section(raw, "output"))
        self.display = self._parse_display(_section(raw, "display"))
        self.random = self._parse_random(_section(raw, "random"))
        self.push = self._parse_push(_section(raw, "push"))
        self.permission = self._parse_permission(raw.get("permission"))
        self.cache = self._parse_cache(_section(raw, "cache"))
        self.limits = self._parse_limits(_section(raw, "limits"))

    # ── 分组解析 ──────────────────────────────────────

    @staticmethod
    def _parse_basic(section: dict) -> BasicConfig:
        return BasicConfig(
            auto_parse=as_bool(section.get("auto_parse"), True),
            parse_jm_code=as_bool(section.get("parse_jm_code"), True),
            link_action=as_choice(section.get("link_action"), (LINK_ACTION_FULL, LINK_ACTION_INFO), LINK_ACTION_FULL),
            default_source=source_key_from_name(section.get("default_source"), "ehentai"),
            send_progress=as_bool(section.get("send_progress"), True),
        )

    @staticmethod
    def _parse_enabled(section: dict) -> dict[str, bool]:
        return {key: as_bool(section.get(key), SOURCE_DEFAULT_ENABLED[key]) for key in SOURCE_KEYS}

    @staticmethod
    def _parse_network(section: dict) -> NetworkConfig:
        return NetworkConfig(
            proxy=as_str(section.get("proxy")),
            timeout=as_int(section.get("timeout"), Config.DEFAULT_TIMEOUT, 5, 300),
            retries=as_int(section.get("retries"), Config.DEFAULT_RETRIES, 0, 10),
            image_concurrency=as_int(section.get("image_concurrency"), Config.DEFAULT_IMAGE_CONCURRENCY, 1, 32),
        )

    @staticmethod
    def _parse_ehentai(section: dict) -> EHentaiConfig:
        cookies = {
            key: as_str(section.get(key))
            for key in ("ipb_member_id", "ipb_pass_hash", "igneous")
            if as_str(section.get(key))
        }
        site = resolve_site(as_choice(section.get("site"), ("e-hentai", "exhentai"), "e-hentai"), cookies)
        return EHentaiConfig(
            site=site,
            cookies=cookies,
            use_proxy=as_bool(section.get("use_proxy"), True),
            request_interval=as_float(section.get("request_interval"), 0.5, 0.0, 10.0),
        )

    @staticmethod
    def _parse_jm(section: dict) -> JmConfig:
        return JmConfig(
            use_proxy=as_bool(section.get("use_proxy"), False),
            domains=as_str_list(section.get("domains")),
            image_threads=as_int(section.get("image_threads"), 10, 1, 32),
        )

    @staticmethod
    def _parse_pica(section: dict) -> PicaConfig:
        return PicaConfig(
            email=as_str(section.get("email")),
            password=str(section.get("password") or ""),
            image_quality=as_choice(section.get("image_quality"), ("original", "high", "medium", "low"), "original"),
            use_proxy=as_bool(section.get("use_proxy"), True),
        )

    @staticmethod
    def _parse_hanime1(section: dict) -> Hanime1Config:
        return Hanime1Config(
            default_genre=as_str(section.get("default_genre"), "裏番") or "裏番",
            quality=as_int(str(section.get("quality") or "1080").rstrip("pP"), 1080, 240, 2160),
            send_mode=as_choice(section.get("send_mode"), ("视频", "文件", "仅链接"), "视频"),
            max_size_mb=as_float(section.get("max_size_mb"), 500.0, 0.0, 4096.0),
            cookie=as_str(section.get("cookie")),
            use_proxy=as_bool(section.get("use_proxy"), True),
        )

    @staticmethod
    def _parse_output(section: dict) -> OutputConfig:
        return OutputConfig(
            max_pages=as_int(section.get("max_pages"), 0, 0, 5000),
            package=PackageOptions(
                output_format=as_choice(section.get("format"), OUTPUT_FORMATS, FORMAT_PDF),
                max_pages_per_pdf=as_int(section.get("max_pages_per_pdf"), 200, 0, 5000),
                pdf_quality=as_int(section.get("pdf_quality"), 85, 1, 100),
                pdf_max_width=as_int(section.get("pdf_max_width"), 1600, 0, 10000),
            ),
        )

    @staticmethod
    def _parse_display(section: dict) -> DisplayConfig:
        return DisplayConfig(
            show_cover=as_bool(section.get("show_cover"), True),
            cover_blur=as_int(section.get("cover_blur"), 0, 0, 50),
            search_cover_grid=as_bool(section.get("search_cover_grid"), True),
            use_forward=as_bool(section.get("use_forward"), True),
            list_size=as_int(section.get("list_size"), 20, 1, 50),
            max_tags=as_int(section.get("max_tags"), 12, 0, 100),
            show_description=as_bool(section.get("show_description"), True),
        )

    @staticmethod
    def _parse_random(section: dict) -> RandomConfig:
        pool_section = section.get("pool") if isinstance(section.get("pool"), Mapping) else {}
        pool = [key for key in SOURCE_KEYS if as_bool(pool_section.get(key), RANDOM_POOL_DEFAULTS[key])]
        return RandomConfig(auto_fetch=as_bool(section.get("auto_fetch"), False), pool=pool)

    @staticmethod
    def _parse_push(section: dict) -> PushConfig:
        tasks = section.get("tasks")
        return PushConfig(
            enable=as_bool(section.get("enable"), False),
            platform_id=as_str(section.get("platform_id")),
            tasks=[task for task in tasks if isinstance(task, Mapping)] if isinstance(tasks, list) else [],
        )

    @staticmethod
    def _parse_permission(raw: Any) -> PermissionConfig:
        if raw is None:
            return PermissionConfig()
        if not isinstance(raw, Mapping):
            logger.warning("权限配置类型无效，已拒绝所有消息")
            return PermissionConfig(configuration_valid=False)
        whitelist = raw.get("whitelist") or {}
        blacklist = raw.get("blacklist") or {}
        if not isinstance(whitelist, Mapping) or not isinstance(blacklist, Mapping):
            logger.warning("黑白名单配置类型无效，已拒绝所有消息")
            return PermissionConfig(configuration_valid=False)
        admin_id = as_str(raw.get("admin_id"))
        whitelist_user = as_str_list(whitelist.get("user"))
        if admin_id and admin_id not in whitelist_user:
            whitelist_user.append(admin_id)
        return PermissionConfig(
            admin_id=admin_id,
            whitelist_enable=as_bool(whitelist.get("enable"), False),
            whitelist_user=whitelist_user,
            whitelist_group=as_str_list(whitelist.get("group")),
            blacklist_enable=as_bool(blacklist.get("enable"), False),
            blacklist_user=as_str_list(blacklist.get("user")),
            blacklist_group=as_str_list(blacklist.get("group")),
        )

    def _parse_cache(self, section: dict) -> CacheConfig:
        configured = as_str(section.get("cache_dir"))
        cache_dir = resolve_cache_dir(configured, self.data_dir)
        available, error = check_dir_writable(cache_dir)
        if available and is_docker() and not configured:
            logger.info(f"Docker 环境未配置缓存目录，使用默认共享路径：{cache_dir}")
        return CacheConfig(
            cache_dir=cache_dir,
            cache_dir_available=available,
            cache_dir_error=error,
            keep_hours=as_float(section.get("keep_hours"), 24.0, 0.0, 24 * 30),
        )

    @staticmethod
    def _parse_limits(section: dict) -> LimitConfig:
        return LimitConfig(
            max_concurrent_jobs=as_int(section.get("max_concurrent_jobs"), Config.DEFAULT_MAX_CONCURRENT_JOBS, 1, 8),
            user_cooldown_seconds=as_int(section.get("user_cooldown_seconds"), 0, 0, 86400),
            session_minutes=as_int(section.get("session_minutes"), 30, 1, 1440),
        )

    # ── 源站工厂 ──────────────────────────────────────

    def _http(self, name: str, use_proxy: bool, backend: str = BACKEND_AIOHTTP, **kwargs: Any) -> HttpClient:
        return HttpClient(
            name,
            proxy=self.network.proxy_for(use_proxy),
            timeout=self.network.timeout,
            retries=self.network.retries,
            backend=backend,
            **kwargs,
        )

    def _create_source(self, key: str) -> BaseSource:
        if key == "ehentai":
            cfg = self.ehentai
            cookies = {"nw": "1", **(cfg.cookies if cfg.site == "exhentai" else {})}
            return EHentaiSource(
                self._http("E-Hentai", cfg.use_proxy, cookies=cookies),
                site=cfg.site,
                request_interval=cfg.request_interval,
            )
        if key == "nhentai":
            return NHentaiSource(
                self._http("nhentai", self.nhentai.use_proxy, BACKEND_CURL),
                language=self.nhentai.language,
            )
        if key == "jmcomic":
            cfg = self.jmcomic
            return JmComicSource(
                self._http("JMComic", cfg.use_proxy),
                proxy=self.network.proxy_for(cfg.use_proxy),
                domains=cfg.domains,
                retries=max(1, self.network.retries),
                image_threads=cfg.image_threads,
                parse_code=self.basic.parse_jm_code,
            )
        if key == "pica":
            cfg = self.pica
            return PicaSource(
                self._http("哔咔", cfg.use_proxy),
                email=cfg.email,
                password=cfg.password,
                image_quality=cfg.image_quality,
                state_file=Path(Config.build_runtime_dir(str(self.data_dir), "pica_token.json")),
            )
        if key == "hanime1":
            cfg = self.hanime1
            headers = {"Cookie": cfg.cookie} if cfg.cookie else None
            return Hanime1Source(
                self._http("Hanime1", cfg.use_proxy, BACKEND_CURL, headers=headers),
                default_genre=cfg.default_genre,
                quality=cfg.quality,
                send_mode=cfg.send_mode,
            )
        raise ValueError(f"未知源站：{key}")

    def create_source_manager(self) -> SourceManager:
        """按统一顺序创建已启用的源站。"""
        sources = [self._create_source(key) for key in SOURCE_KEYS if self.enabled_sources.get(key)]
        manager = SourceManager(sources, self.random.pool)
        for source in manager:
            reason = source.unavailable_reason
            if reason:
                logger.warning(f"{source.name} 已启用但暂不可用：{reason}")
        return manager
