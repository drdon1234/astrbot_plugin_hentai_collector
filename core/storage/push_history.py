"""推送去重记录：按任务和目标保存完成的作品引用，JSON 原子写入插件数据目录。"""

import json
import os
from pathlib import Path
from typing import Any

from ..logger import logger

from ..types import WorkRef


MAX_RECORDS_PER_TARGET = 3000


class PushHistory:
    """记录每个推送目标已完成的作品，并保存迁移前记录以便追溯。"""

    def __init__(self, path: Path):
        self.path = path
        self._pending_migration: dict[str, Any] | None = None
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        empty = {"tasks": {}, "migrated_records": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return empty
        if not isinstance(data, dict):
            return empty
        if not isinstance(data.get("tasks"), dict):
            self._pending_migration = data
            return empty
        tasks = {}
        for task_id, targets in data["tasks"].items():
            if isinstance(targets, dict):
                tasks[str(task_id)] = {
                    str(target): [str(ref) for ref in records]
                    for target, records in targets.items() if isinstance(records, list)
                }
        return {"tasks": tasks, "migrated_records": data.get("migrated_records", {})}

    def _save(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            partial = self.path.with_suffix(".json.part")
            partial.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            os.replace(partial, self.path)
        except OSError as exc:
            logger.warning(f"保存推送记录失败：{exc}")
            return False
        return True

    def migrate(self) -> None:
        """一次性归档旧任务级记录，原数据完整保存在迁移存档中。

        旧记录只证明至少一个目标曾成功，不能推断某个目标已完成。
        未知完成状态不建立目标基线，允许升级后重新推送。
        """
        original = self._pending_migration
        if original is None:
            return
        self._data["migrated_records"] = original
        if not self._save():
            raise OSError("推送历史迁移无法保存，请检查插件数据目录权限")
        self._pending_migration = None
        logger.info("旧推送历史已完整归档；缺少目标成功证据的作品将重新推送")

    def seen(self, task_id: str, target: str, ref: WorkRef) -> bool:
        """作品是否已由该任务在指定目标完成推送。"""
        return str(ref) in self._data["tasks"].get(task_id, {}).get(target, [])

    def add(self, task_id: str, target: str, refs: list[WorkRef]) -> None:
        """追加指定目标的完成记录并裁剪到上限。"""
        if self._pending_migration is not None:
            raise RuntimeError("旧推送历史尚未归档，请先完成迁移")
        if not refs:
            return
        records = self._data["tasks"].setdefault(task_id, {}).setdefault(target, [])
        known = set(records)
        for ref in refs:
            value = str(ref)
            if value not in known:
                records.append(value)
                known.add(value)
        if len(records) > MAX_RECORDS_PER_TARGET:
            del records[: len(records) - MAX_RECORDS_PER_TARGET]
        self._save()
