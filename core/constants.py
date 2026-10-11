"""常量与默认配置定义。"""

import os


class Config:
    """全局常量，集中管理路径、超时、并发与消息长度等默认值。"""

    PLUGIN_NAME = "astrbot_plugin_hentai_collector"
    PLUGIN_VERSION = "1.1.1"
    BOT_DISPLAY_NAME = "本子聚合bot"

    # ── 路径 ──────────────────────────────────────────
    CACHE_DIR_NAME = "cache"
    JOB_DIR_NAME = "jobs"
    RUNTIME_DIR_NAME = "runtime"
    DOCKER_CACHE_DIR = "/app/sharedFolder/astrbot_plugin_hentai_collector/cache"

    # ── 网络 ──────────────────────────────────────────
    DEFAULT_TIMEOUT = 30
    DEFAULT_RETRIES = 3
    RETRY_BASE_DELAY = 1.0
    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    )
    DEFAULT_IMPERSONATE = "chrome"
    STREAM_CHUNK_SIZE = 1024 * 1024
    VIDEO_DOWNLOAD_TIMEOUT = 1800

    # ── 下载与打包 ────────────────────────────────────
    DEFAULT_IMAGE_CONCURRENCY = 8
    MIN_IMAGE_BYTES = 256
    DEFAULT_MAX_CONCURRENT_JOBS = 2
    COVER_DOWNLOAD_CONCURRENCY = 6
    COVER_TIMEOUT = 20
    COVER_RETRIES = 1
    MAX_FILENAME_LENGTH = 120

    # ── 会话与清理 ────────────────────────────────────
    MAX_SEARCH_SESSIONS = 1000
    MIN_FILE_RETENTION_SECONDS = 300
    EXPIRED_CLEANUP_INTERVAL = 1800
    STALE_JOB_HOURS = 72
    MESSAGE_DEDUPE_SIZE = 512

    # ── 消息 ──────────────────────────────────────────
    TEXT_CHUNK_LIMIT = 3500
    FORWARD_MAX_NODES = 90
    SEND_INTERVAL = 0.4
    RANDOM_PAGE_LIMIT = 50
    AGGREGATE_TIMEOUT = 90

    @staticmethod
    def build_cache_dir(prefix: str) -> str:
        """基于插件数据目录前缀生成缓存目录。"""
        return os.path.abspath(os.path.join(prefix, Config.CACHE_DIR_NAME))

    @staticmethod
    def build_runtime_dir(prefix: str, *parts: str) -> str:
        """基于插件数据目录前缀生成运行时状态目录（账号 Token、推送记录等）。"""
        return os.path.abspath(
            os.path.join(prefix, Config.RUNTIME_DIR_NAME, *parts)
        )
