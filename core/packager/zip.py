"""将漫画图片原样写入 ZIP 压缩包（不二次压缩图片）。"""

import json
import os
import zipfile
from pathlib import Path
from typing import Iterable

from ..errors import PackageError
from .naming import natural_key


def build_zip_file(
    image_paths: Iterable[Path],
    output_dir: Path,
    stem: str,
    *,
    manifest: dict | None = None,
) -> Path:
    """把图片按页码重命名后写入 ``stem.zip``。

    图片本身已是压缩格式，使用 ``ZIP_STORED`` 避免无效压缩；
    ``manifest`` 非空时附带 ``info.json``。

    Raises:
        PackageError: 没有图片或写入失败
    """
    paths = sorted((Path(path) for path in image_paths), key=natural_key)
    if not paths:
        raise PackageError("没有可用于生成压缩包的图片")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{stem}.zip"
    partial_path = output_path.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(partial_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for index, path in enumerate(paths, 1):
                archive.write(path, f"{stem}/{index:04d}{path.suffix.lower()}")
            if manifest:
                archive.writestr(
                    f"{stem}/info.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
        os.replace(partial_path, output_path)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        partial_path.unlink(missing_ok=True)
        raise PackageError(f"生成压缩包失败：{exc}") from exc
    return output_path
