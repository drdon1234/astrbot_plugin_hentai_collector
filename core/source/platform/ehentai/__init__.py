"""E-Hentai 源站子包，只导出源站类与站点选择函数。"""

from .source import EHentaiSource, resolve_site

__all__ = ["EHentaiSource", "resolve_site"]
