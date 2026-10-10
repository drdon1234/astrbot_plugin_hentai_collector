"""推送去重记录：按任务保存已推送的作品引用，JSON 原子写入插件数据目录。"""

import json
import os
from pathlib import Path

from ..logger import logger

from ..types import WorkRef


MAX_RECORDS_PER_TASK = 3000


class PushHistory:
    """记录每个推送任务已推送过的作品，用于“只推送新作品”。"""

    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, list[str]] = self._load()

    def _load(self) -> dict[str, list[str]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): [str(i) for i in v] for k, v in data.items() if isinstance(v, list)}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            partial = self.path.with_suffix(".json.part")
            partial.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            os.replace(partial, self.path)
        except OSError as exc:
            logger.warning(f"保存推送记录失败：{exc}")

    def seen(self, task_id: str, ref: WorkRef) -> bool:
        """作品是否已被该任务推送过。"""
        return str(ref) in self._data.get(task_id, [])

    def add(self, task_id: str, refs: list[WorkRef]) -> None:
        """追加推送记录并裁剪到上限。"""
        if not refs:
            return
        records = self._data.setdefault(task_id, [])
        known = set(records)
        records.extend(str(ref) for ref in refs if str(ref) not in known)
        if len(records) > MAX_RECORDS_PER_TASK:
            del records[: len(records) - MAX_RECORDS_PER_TASK]
        self._save()
