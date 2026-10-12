"""封面图片处理：单张封面的缩放与模糊、搜索结果的带序号拼图。"""

from io import BytesIO
from typing import Optional


TARGET_HEIGHT = 420
GRID_COLUMNS = 5
GRID_PADDING = 8
JPEG_QUALITY = 85
BLANK_CHANNEL_RANGE = 2
FONT_CANDIDATES = ("msyh.ttc", "arial.ttf", "DejaVuSans-Bold.ttf", "NotoSansCJK-Regular.ttc")


def _load_font(size: int):
    from PIL import ImageFont

    for name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _open_cover(data: bytes):
    """完整解码封面，按可见像素剔除纯色及近乎纯色的空白图片。"""
    from PIL import Image

    with Image.open(BytesIO(data)) as source:
        source.seek(0)
        source.load()
        if "A" in source.getbands() or "transparency" in source.info:
            rgba = source.convert("RGBA")
            background = Image.new("RGBA", source.size, (255, 255, 255, 255))
            image = Image.alpha_composite(background, rgba).convert("RGB")
        else:
            image = source.convert("RGB")
    # 检查完整图像，避免缩略图或模糊抹去细小文字后误判正常封面。
    if all(high - low <= BLANK_CHANNEL_RANGE for low, high in image.getextrema()):
        return None
    return image


def _blur(image, radius: int):
    from PIL import ImageFilter

    return image.filter(ImageFilter.GaussianBlur(radius)) if radius > 0 else image


def process_cover(data: bytes, blur_radius: int = 0, max_height: int = 800) -> Optional[bytes]:
    """缩放并按需模糊单张封面，返回 JPEG 字节；损坏或空白时返回 None。"""
    from PIL import Image

    try:
        image = _open_cover(data)
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    if image is None:
        return None
    if image.height > max_height:
        image.thumbnail((max_height * 4, max_height), Image.Resampling.LANCZOS)
    image = _blur(image, blur_radius)
    output = BytesIO()
    image.save(output, format="JPEG", quality=JPEG_QUALITY)
    return output.getvalue()


def _draw_number(image, number: int):
    from PIL import Image, ImageDraw

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _load_font(max(24, image.height // 9))
    text = str(number)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    width, height = right - left, bottom - top
    band = height + 16
    draw.rectangle((0, image.height - band, image.width, image.height), fill=(0, 0, 0, 160))
    draw.text(((image.width - width) / 2, image.height - band + 8 - top), text, font=font, fill=(255, 255, 255, 255))
    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def build_cover_grid(covers: list[Optional[bytes]], blur_radius: int = 0, start: int = 1) -> Optional[bytes]:
    """把封面按 5 列拼接为一张带序号的图片，序号与列表序号一致。

    Args:
        covers: 与列表同序的封面字节；缺失、损坏或空白封面跳过，保留原作品序号
        blur_radius: 高斯模糊半径，0 表示不模糊
        start: 第一张封面的序号（聚合列表中各站序号连续）

    Returns:
        JPEG 字节；没有任何可用封面时返回 None
    """
    from PIL import Image

    tile_width = int(TARGET_HEIGHT * 0.7)
    prepared = []
    for number, data in enumerate(covers, start):
        if not data:
            continue
        try:
            image = _open_cover(data)
        except (OSError, ValueError, Image.DecompressionBombError):
            continue
        if image is None:
            continue
        # 每张完整解码、判空后立即缩小，整批只保留固定尺寸的小图。
        full_image = image
        try:
            image = full_image.resize(
                (max(1, int(full_image.width * TARGET_HEIGHT / full_image.height)), TARGET_HEIGHT),
                Image.Resampling.LANCZOS,
            )
        finally:
            full_image.close()
        image = image.crop((0, 0, min(image.width, tile_width), TARGET_HEIGHT))
        if image.width < tile_width:
            canvas = Image.new("RGB", (tile_width, TARGET_HEIGHT), (40, 40, 40))
            canvas.paste(image, ((tile_width - image.width) // 2, 0))
            image = canvas
        image = _blur(image, blur_radius)
        prepared.append(_draw_number(image, number))
    if not prepared:
        return None

    rows = (len(prepared) + GRID_COLUMNS - 1) // GRID_COLUMNS
    columns = min(GRID_COLUMNS, len(prepared))
    grid = Image.new(
        "RGB",
        (columns * tile_width + (columns - 1) * GRID_PADDING, rows * TARGET_HEIGHT + (rows - 1) * GRID_PADDING),
        (255, 255, 255),
    )
    for index, tile in enumerate(prepared):
        row, column = divmod(index, GRID_COLUMNS)
        grid.paste(tile, (column * (tile_width + GRID_PADDING), row * (TARGET_HEIGHT + GRID_PADDING)))
    output = BytesIO()
    grid.save(output, format="JPEG", quality=JPEG_QUALITY)
    return output.getvalue()
