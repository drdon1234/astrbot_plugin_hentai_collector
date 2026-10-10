"""任务目录管理：归属标记、已生成文件复用、过期标记与过期清理。"""

import json
import shutil
import time
from pathlib import Path
from typing import Optional

from ..logger import logger

from ..constants import Config
from ..packager import safe_filename


MARKER_NAME = ".astrbot_hentai_collector"
MANIFEST_NAME = "manifest.json"


class JobStore:
    """以 ``{源站}-{作品ID}`` 为目录管理每部作品的下载与输出文件。

    目录写入归属标记，清理只处理带标记的目录；``manifest.json`` 记录输出文件、
    打包参数指纹与过期时间，用于在保留期内直接复用已生成的文件。
    """

    def __init__(self, cache_dir: str):
        self.root = Path(cache_dir).expanduser().resolve() / Config.JOB_DIR_NAME

    def job_dir(self, source: str, work_id: str) -> Path:
        """作品对应的任务目录（不创建）。"""
        return self.root / safe_filename(f"{source}-{work_id}", f"{source}-job", 80)

    def open_job(self, source: str, work_id: str) -> Path:
        """创建任务目录并写入归属标记。"""
        path = self.job_dir(source, work_id)
        path.mkdir(parents=True, exist_ok=True)
        (path / MARKER_NAME).touch(exist_ok=True)
        return path

    @staticmethod
    def _read_manifest(path: Path) -> Optional[dict]:
        try:
            data = json.loads((path / MANIFEST_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def find_outputs(self, source: str, work_id: str, signature: str) -> Optional[dict]:
        """查找保留期内、由相同打包参数生成的输出。

        Returns:
            manifest 字典（``files`` 已转换为存在的绝对路径），没有可复用输出时返回 None
        """
        path = self.job_dir(source, work_id)
        manifest = self._read_manifest(path)
        if not manifest or manifest.get("signature") != signature:
            return None
        if float(manifest.get("expires_at") or 0) <= time.time():
            return None
        files = [path / name for name in manifest.get("files") or []]
        if not files or not all(file.is_file() for file in files):
            return None
        manifest["files"] = files
        return manifest

    def save_outputs(
        self,
        job: Path,
        files: list[Path],
        signature: str,
        retention_seconds: int,
        extra: Optional[dict] = None,
    ) -> None:
        """记录输出文件与过期时间。"""
        manifest = {
            "files": [str(file.relative_to(job)) for file in files],
            "signature": signature,
            "created_at": time.time(),
            "expires_at": time.time() + max(Config.MIN_FILE_RETENTION_SECONDS, retention_seconds),
        }
        manifest.update(extra or {})
        try:
            (job / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            logger.warning(f"写入任务清单失败：{exc}")

    def mark_expire(self, job: Path, retention_seconds: int) -> None:
        """为没有输出清单的任务目录（失败或仅链接）写入过期时间。"""
        manifest = self._read_manifest(job) or {"files": [], "signature": ""}
        manifest["expires_at"] = time.time() + max(Config.MIN_FILE_RETENTION_SECONDS, retention_seconds)
        try:
            (job / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            logger.warning(f"写入过期标记失败：{exc}")

    @staticmethod
    def remove_dir(path: Path) -> None:
        """删除任务内的中间目录（如下载的原图）。"""
        shutil.rmtree(path, ignore_errors=True)

    def cleanup_expired(self, force: bool = False) -> int:
        """清理已过期的任务目录。

        Args:
            force: 为 True 时清理全部带标记的目录（管理员清缓存）

        Returns:
            清理的目录数
        """
        if not self.root.is_dir():
            return 0
        now = time.time()
        stale_before = now - Config.STALE_JOB_HOURS * 3600
        cleaned = 0
        for path in self.root.iterdir():
            try:
                if not path.is_dir() or not (path / MARKER_NAME).is_file():
                    continue
                manifest = self._read_manifest(path)
                if force:
                    expired = True
                elif manifest and manifest.get("expires_at"):
                    expired = float(manifest["expires_at"]) <= now
                else:
                    expired = path.stat().st_mtime < stale_before
                if expired:
                    shutil.rmtree(path)
                    cleaned += 1
            except OSError as exc:
                logger.debug(f"清理任务目录失败 {path}: {exc}")
        return cleaned
