"""逐页流式写入 PDF：单页编码为 JPEG，按最大页数分片，峰值内存只与单页尺寸相关。"""

import os
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

from ..errors import PackageError
from .naming import natural_key


def prepare_rgb_image(image: Any, max_width: int, max_height: int) -> Any:
    """纠正 EXIF 方向、按上限缩放，并将透明背景铺白转为 RGB。"""
    from PIL import Image, ImageOps

    image = ImageOps.exif_transpose(image)
    width = max_width if max_width > 0 else 2**31 - 1
    height = max_height if max_height > 0 else 2**31 - 1
    if image.width > width or image.height > height:
        image.thumbnail((width, height), Image.Resampling.LANCZOS)
    if image.mode == "RGB":
        return image.copy()
    if image.mode == "L":
        return image.convert("RGB")
    background = Image.new("RGB", image.size, "white")
    rgba = image.convert("RGBA")
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def _encode_jpeg(path: Path, quality: int, max_width: int, max_height: int) -> tuple[int, int, bytes]:
    """以单页为单位编码 JPEG，避免同时持有整本漫画。"""
    from PIL import Image

    try:
        with Image.open(path) as source:
            source.seek(0)  # GIF / WebP 动图只取首帧
            image = prepare_rgb_image(source, max_width, max_height)
        try:
            stream = BytesIO()
            image.save(stream, format="JPEG", quality=quality, optimize=True)
            return image.width, image.height, stream.getvalue()
        finally:
            image.close()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise PackageError(f"读取图片失败：{path.name}：{exc}") from exc


def _write_object(stream: Any, offsets: list[int], number: int, payload: bytes) -> None:
    offsets[number] = stream.tell()
    stream.write(f"{number} 0 obj\n".encode("ascii"))
    stream.write(payload)
    stream.write(b"\nendobj\n")


def _write_single_pdf(
    pages: list[Path],
    output_path: Path,
    *,
    quality: int,
    max_width: int,
    max_height: int,
    dpi: int,
) -> None:
    """把一组图片写成一个 PDF 文件。"""
    partial_path = output_path.with_suffix(".pdf.part")
    max_object = 2 + len(pages) * 3
    offsets = [0] * (max_object + 1)
    try:
        with partial_path.open("wb") as stream:
            stream.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
            page_refs: list[str] = []
            for page_index, path in enumerate(pages):
                width, height, image_data = _encode_jpeg(path, quality, max_width, max_height)
                page_object = 3 + page_index * 3
                image_object = page_object + 1
                content_object = page_object + 2
                page_refs.append(f"{page_object} 0 R")
                image_payload = (
                    f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                    f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
                    f"/Length {len(image_data)} >>\nstream\n".encode("ascii")
                    + image_data
                    + b"\nendstream"
                )
                _write_object(stream, offsets, image_object, image_payload)
                page_width = width * 72 / dpi
                page_height = height * 72 / dpi
                content = f"q {page_width:.2f} 0 0 {page_height:.2f} 0 0 cm /Im0 Do Q".encode("ascii")
                content_payload = (
                    f"<< /Length {len(content)} >>\nstream\n".encode("ascii")
                    + content
                    + b"\nendstream"
                )
                _write_object(stream, offsets, content_object, content_payload)
                page_payload = (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width:.2f} {page_height:.2f}] "
                    f"/Resources << /XObject << /Im0 {image_object} 0 R >> >> "
                    f"/Contents {content_object} 0 R >>"
                ).encode("ascii")
                _write_object(stream, offsets, page_object, page_payload)
                if page_index % 20 == 19:
                    stream.flush()

            pages_payload = (
                f"<< /Type /Pages /Kids [{' '.join(page_refs)}] /Count {len(page_refs)} >>"
            ).encode("ascii")
            _write_object(stream, offsets, 2, pages_payload)
            _write_object(stream, offsets, 1, b"<< /Type /Catalog /Pages 2 0 R >>")
            xref_offset = stream.tell()
            stream.write(f"xref\n0 {max_object + 1}\n".encode("ascii"))
            stream.write(b"0000000000 65535 f \n")
            for offset in offsets[1:]:
                stream.write(f"{offset:010d} 00000 n \n".encode("ascii"))
            stream.write(
                f"trailer\n<< /Size {max_object + 1} /Root 1 0 R >>\n"
                f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
            )
        os.replace(partial_path, output_path)
    except (OSError, PackageError):
        partial_path.unlink(missing_ok=True)
        raise


def build_pdf_files(
    image_paths: Iterable[Path],
    output_dir: Path,
    stem: str,
    *,
    max_pages: int = 200,
    quality: int = 85,
    max_width: int = 0,
    max_height: int = 0,
    dpi: int = 150,
) -> list[Path]:
    """生成 PDF，超过最大页数时按 ``stem-1.pdf``、``stem-2.pdf`` 分片。

    Args:
        image_paths: 图片路径，按文件名自然排序
        output_dir: 输出目录
        stem: 不含扩展名的文件名（已做安全处理）
        max_pages: 单个 PDF 最大页数，0 表示不分片
        quality: JPEG 质量 1-100
        max_width: 单页最大宽度，0 表示不缩放
        max_height: 单页最大高度，0 表示不缩放
        dpi: 页面 DPI，决定 PDF 页面物理尺寸

    Returns:
        生成的 PDF 路径列表

    Raises:
        PackageError: 没有图片或写入失败
    """
    paths = sorted((Path(path) for path in image_paths), key=natural_key)
    if not paths:
        raise PackageError("没有可用于生成 PDF 的图片")
    page_limit = max_pages if max_pages > 0 else len(paths)
    quality = max(1, min(100, int(quality)))
    dpi = max(36, min(600, int(dpi)))
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks = [paths[start:start + page_limit] for start in range(0, len(paths), page_limit)]
    outputs: list[Path] = []
    try:
        for chunk_index, chunk in enumerate(chunks, 1):
            suffix = "" if len(chunks) == 1 else f"-{chunk_index}"
            output_path = output_dir / f"{stem}{suffix}.pdf"
            _write_single_pdf(
                chunk,
                output_path,
                quality=quality,
                max_width=max(0, int(max_width)),
                max_height=max(0, int(max_height)),
                dpi=dpi,
            )
            outputs.append(output_path)
    except (OSError, PackageError) as exc:
        for generated in outputs:
            generated.unlink(missing_ok=True)
        if isinstance(exc, PackageError):
            raise
        raise PackageError(f"生成 PDF 失败：{exc}") from exc
    return outputs
