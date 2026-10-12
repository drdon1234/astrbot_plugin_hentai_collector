"""打包门面：校验图片并按配置生成 PDF / ZIP。"""

from dataclasses import dataclass
from pathlib import Path

from ..logger import logger

from ..errors import PackageError
from .pdf import build_pdf_files
from .zip import build_zip_file


FORMAT_PDF = "PDF"
FORMAT_ZIP = "ZIP"
FORMAT_BOTH = "PDF+ZIP"
OUTPUT_FORMATS = (FORMAT_PDF, FORMAT_ZIP, FORMAT_BOTH)


@dataclass(frozen=True)
class PackageOptions:
    """打包参数；``signature`` 用于判断缓存文件是否由当前参数生成。"""

    output_format: str = FORMAT_PDF
    max_pages_per_pdf: int = 200
    pdf_quality: int = 85
    pdf_max_width: int = 1600
    pdf_dpi: int = 150

    @property
    def signature(self) -> str:
        """打包参数指纹。"""
        return (
            f"{self.output_format}|{self.max_pages_per_pdf}|{self.pdf_quality}"
            f"|{self.pdf_max_width}|{self.pdf_dpi}"
        )


def validate_images(image_paths: list[Path]) -> tuple[list[Path], int]:
    """逐张校验图片可解码，返回 (可用图片, 损坏张数)。

    Raises:
        PackageError: 没有任何可用图片
    """
    from PIL import Image

    valid: list[Path] = []
    broken = 0
    for path in image_paths:
        try:
            with Image.open(path) as image:
                image.verify()
            # verify 不完整解码 JPEG；逐页加载实际使用的首帧，随上下文退出释放像素。
            with Image.open(path) as image:
                image.load()
            valid.append(path)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
            broken += 1
            logger.warning(f"图片损坏已跳过：{path.name}：{exc}")
    if not valid:
        raise PackageError("下载结果中没有可读取的图片")
    return valid, broken


class Packager:
    """按 ``PackageOptions`` 把图片序列打包为可发送文件。"""

    def __init__(self, options: PackageOptions):
        self.options = options

    def package(
        self,
        image_paths: list[Path],
        output_dir: Path,
        stem: str,
        manifest: dict,
    ) -> list[Path]:
        """同步打包（应在线程中调用）。

        Args:
            image_paths: 已校验的图片
            output_dir: 输出目录
            stem: 安全文件名
            manifest: 写入 ZIP 的作品信息

        Returns:
            生成的文件路径列表

        Raises:
            PackageError: 打包失败
        """
        options = self.options
        outputs: list[Path] = []
        if options.output_format in (FORMAT_PDF, FORMAT_BOTH):
            outputs.extend(
                build_pdf_files(
                    image_paths,
                    output_dir,
                    stem,
                    max_pages=options.max_pages_per_pdf,
                    quality=options.pdf_quality,
                    max_width=options.pdf_max_width,
                    dpi=options.pdf_dpi,
                )
            )
        if options.output_format in (FORMAT_ZIP, FORMAT_BOTH):
            outputs.append(build_zip_file(image_paths, output_dir, stem, manifest=manifest))
        return outputs
