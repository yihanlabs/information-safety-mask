from __future__ import annotations

import io
import math

from PIL import Image, ImageDraw, ImageOps

from .models import Mask, Rect

Image.MAX_IMAGE_PIXELS = None


def canonical_image(data: bytes) -> tuple[bytes, int, int]:
    if not data:
        raise ValueError("图片为空")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP", "BMP"}:
                raise ValueError("只支持 PNG、JPEG、静态 WebP 和 BMP 图片")
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("不支持动画图片，请先导出为静态图片")
            from .raster import require_memory

            require_memory(image.width * image.height * 20)
            image.load()
            oriented = ImageOps.exif_transpose(image)
            rgba = oriented.convert("RGBA")
            clean = Image.new("RGB", rgba.size, "white")
            clean.paste(rgba, mask=rgba.getchannel("A"))
            output = io.BytesIO()
            clean.save(output, format="PNG")
            return output.getvalue(), clean.width, clean.height
    except (OSError, SyntaxError) as exc:
        raise ValueError("无法读取图片，文件可能损坏或格式不支持") from exc


def validate_rect(rect: Rect, width: int, height: int) -> Rect:
    if (
        rect.x >= width
        or rect.y >= height
        or rect.x + rect.width > width + 0.01
        or rect.y + rect.height > height + 0.01
    ):
        raise ValueError("遮盖区域超出图片边界")
    return rect


def render_redacted(source: bytes, masks: list[Mask]) -> bytes:
    with Image.open(io.BytesIO(source)) as original:
        clean = Image.new("RGB", original.size, "white")
        clean.paste(original.convert("RGB"))
    draw = ImageDraw.Draw(clean)
    for mask in masks:
        r = validate_rect(mask.box, clean.width, clean.height)
        draw.rectangle(
            (
                math.floor(r.x),
                math.floor(r.y),
                min(clean.width - 1, math.ceil(r.x + r.width) - 1),
                min(clean.height - 1, math.ceil(r.y + r.height) - 1),
            ),
            fill=(0, 0, 0),
        )
    output = io.BytesIO()
    clean.save(output, format="PNG")
    return output.getvalue()
