"""插件异常定义；异常消息直接面向用户展示，必须是可读的中文说明。"""


class CollectorError(Exception):
    """插件内所有可预期失败的基类。"""


class SourceError(CollectorError):
    """源站请求或解析失败。"""


class SourceUnavailable(SourceError):
    """源站未启用、缺少账号或缺少依赖，暂时无法使用。"""


class UnsupportedOperation(SourceError):
    """源站不支持该功能（例如没有排行榜）。"""


class WorkNotFound(SourceError):
    """作品不存在或没有符合条件的结果。"""


class BlockedError(SourceError):
    """IP 被封禁、额度用尽或 Cookie 失效，继续请求没有意义。"""


class HttpError(SourceError):
    """HTTP 状态码异常。"""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class DownloadError(CollectorError):
    """作品内容下载失败。"""


class PackageError(CollectorError):
    """PDF / ZIP 打包失败。"""
