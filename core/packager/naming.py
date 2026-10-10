"""文件命名工具：安全文件名与自然排序。"""

import re
from pathlib import Path
from typing import Any

from ..constants import Config


_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(value: object, fallback: str, max_length: int = Config.MAX_FILENAME_LENGTH) -> str:
    """清理文件名中的非法字符并截断；结果为空时返回 ``fallback``。"""
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) > max_length:
        text = text[:max_length].rstrip(" .")
    if not text:
        return fallback
    if text.upper().split(".", 1)[0] in _WINDOWS_RESERVED:
        text = f"_{text}"
    return text


def natural_key(path: Path) -> tuple[Any, ...]:
    """按文件名中的数字自然排序。"""
    pieces: list[tuple[int, Any]] = []
    for part in Path(path).parts:
        for piece in re.split(r"(\d+)", part):
            if piece.isdigit():
                pieces.append((0, int(piece)))
            elif piece:
                pieces.append((1, piece.lower()))
    return tuple(pieces)
