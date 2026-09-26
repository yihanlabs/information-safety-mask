"""Bounded pixel access in original coordinates and metadata-free streaming PNG."""

from __future__ import annotations

import io
import math
import struct
import zlib
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw

from .cache import Blob
from .progress import ProgressEvent

# Admission is based on resources and decoder requirements, not compressed-pixel ratios.
Image.MAX_IMAGE_PIXELS = None
TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def inspect_image(blob: Blob):
    try:
        with blob.open() as stream, Image.open(stream) as image:
            if image.format not in {"PNG", "JPEG", "WEBP", "BMP"}:
                raise ValueError("只支持 PNG、JPEG、静态 WebP 和 BMP 图片")
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("不支持动画图片，请先导出为静态图片")
            width, height = image.size
            if width <= 0 or height <= 0:
                raise ValueError("图片尺寸无效")
            orientation = image.getexif().get(274, 1)
            if orientation not in range(1, 9):
                orientation = 1
            return (
                width,
                height,
                orientation,
                image.format,
                bool(image.info.get("interlace") or image.info.get("progressive")),
            )
    except (OSError, SyntaxError) as exc:
        raise ValueError("无法读取图片，文件可能损坏或格式不支持") from exc


def require_memory(needed):
    from .memory import require_memory as check

    return check(needed, "preparing")


@dataclass
class Raster:
    blob: Blob
    raw_width: int
    raw_height: int
    orientation: int = 1

    @property
    def width(self):
        return self.raw_height if self.orientation >= 5 else self.raw_width

    @property
    def height(self):
        return self.raw_width if self.orientation >= 5 else self.raw_height

    def _raw_point(self, x, y):
        w, h, o = self.raw_width, self.raw_height, self.orientation
        return {
            1: (x, y),
            2: (w - x, y),
            3: (w - x, h - y),
            4: (x, h - y),
            5: (y, x),
            6: (y, h - x),
            7: (w - y, h - x),
            8: (w - y, x),
        }[o]

    def read_rect(self, x, y, width, height, reader=None):
        if min(x, y) < 0 or min(width, height) <= 0 or x + width > self.width or y + height > self.height:
            raise ValueError("读取区域超出图片边界")
        a, b = self._raw_point(x, y), self._raw_point(x + width, y + height)
        left, top = min(a[0], b[0]), min(a[1], b[1])
        rw, rh = abs(a[0] - b[0]), abs(a[1] - b[1])
        own = reader is None
        reader = reader or self.blob.open()
        try:
            rows = bytearray(rw * rh * 3)
            for row in range(rh):
                reader.seek(((top + row) * self.raw_width + left) * 3)
                part = reader.read(rw * 3)
                if len(part) != rw * 3:
                    raise ValueError("图片缓存不完整，请重新导入")
                rows[row * rw * 3 : (row + 1) * rw * 3] = part
            image = Image.frombytes("RGB", (rw, rh), bytes(rows))
            if self.orientation in TRANSPOSE:
                transformed = image.transpose(TRANSPOSE[self.orientation])
                image.close()
                image = transformed
            return image
        finally:
            if own:
                reader.close()

    def resized(self, width, height, box=None):
        """Bilinear sampling; at most two source scanlines are decoded at once."""
        import cv2

        x, y, sw, sh = box or (0, 0, self.width, self.height)
        if min(width, height) <= 0 or width * height > 16_000_000:
            raise ValueError("预览尺寸无效")
        result = np.empty((height, width, 3), dtype=np.uint8)
        with self.blob.open() as reader:
            for row in range(height):
                sy = min(sh - 1, max(0, (row + 0.5) * sh / height - 0.5))
                iy, fraction = int(sy), sy - int(sy)
                count = 2 if iy < sh - 1 else 1
                with self.read_rect(x, y + iy, sw, count, reader) as strip:
                    horizontal = cv2.resize(np.asarray(strip), (width, count), interpolation=cv2.INTER_LINEAR)
                if count == 1:
                    result[row] = horizontal[0]
                else:
                    result[row] = np.rint(
                        horizontal[0].astype(np.float32) * (1 - fraction) + horizontal[1] * fraction
                    ).astype(np.uint8)
        return result


def prepare_image(blob, cache, group, report=None, memory_limit=0):
    width, height, orientation, fmt, full_decoder = inspect_image(blob)
    # Refuse only resources that cannot fit the configured cache, not a pixel count.
    if width * height * 3 > memory_limit:
        with cache.disk_allocation(width * height * 3 + ((width * height * 3) // (1024 * 1024) + 1) * 28):
            pass
    rows = max(1, min(256, (8 * 1024 * 1024) // (width * 3)))
    if fmt in {"WEBP", "BMP"} or full_decoder:
        require_memory(width * height * 12)
    else:
        require_memory(max(width * 3 * rows * 4, 64 * 1024 * 1024))
    writer = cache.writer(group, memory_limit=memory_limit)
    total = math.ceil(height / rows)
    if report:
        report(ProgressEvent("preparing", total=total, units=width * height / 1e6, unit="strip"))
    try:
        if fmt in {"PNG", "JPEG"}:
            import pyvips

            # No libvips disk fallback: only our authenticated encrypted writer may persist pixels.
            pyvips.cache_set_max(0)
            with blob.open() as reader:
                source = pyvips.SourceCustom()
                source.on_read(reader.read)
                source.on_seek(reader.seek)
                image = pyvips.Image.new_from_source(
                    source, "", access="sequential", memory=True, fail_on="error"
                )
                if image.interpretation not in {"srgb", "rgb"}:
                    image = image.colourspace("srgb")
                if image.hasalpha():
                    image = image.flatten(background=[255, 255, 255])
                image = image.cast("uchar")
                region = pyvips.Region.new(image)
                for index, top in enumerate(range(0, height, rows), 1):
                    writer.write(region.fetch(0, top, width, min(rows, height - top)))
                    if report:
                        report(ProgressEvent("preparing", index, total, width * height / 1e6, unit="strip"))
                del region, image, source
        else:
            with blob.open() as reader, Image.open(reader) as image:
                image.load()
                for index, top in enumerate(range(0, height, rows), 1):
                    with image.crop((0, top, width, min(height, top + rows))) as strip:
                        if strip.mode in {"RGBA", "LA", "P"}:
                            rgba = strip.convert("RGBA")
                            clean = Image.new("RGB", strip.size, "white")
                            clean.paste(rgba, mask=rgba.getchannel("A"))
                            rgba.close()
                        else:
                            clean = strip.convert("RGB")
                        writer.write(clean.tobytes())
                        clean.close()
                    if report:
                        report(ProgressEvent("preparing", index, total, width * height / 1e6, unit="strip"))
        return Raster(writer.finish(), width, height, orientation)
    except Exception:
        writer.abort()
        raise


def preview_png(raster, max_side=2048, box=None, output_size=None):
    sw, sh = (box[2], box[3]) if box else (raster.width, raster.height)
    ratio = min(1, max_side / max(sw, sh))
    width, height = output_size or (max(1, round(sw * ratio)), max(1, round(sh * ratio)))
    pixels = raster.resized(width, height, box)
    with Image.fromarray(pixels) as image:
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()


def png_chunk(writer, kind, content):
    writer.write(
        struct.pack(">I", len(content))
        + kind
        + content
        + struct.pack(">I", zlib.crc32(kind + content) & 0xFFFFFFFF)
    )


def export_png(raster, masks, cache, group, report=None):
    from .imaging import validate_rect

    rectangles = []
    for mask in masks:
        r = validate_rect(mask.box, raster.width, raster.height)
        rectangles.append(
            (math.floor(r.x), math.floor(r.y), math.ceil(r.x + r.width) - 1, math.ceil(r.y + r.height) - 1)
        )
    writer = cache.writer(group)
    rows = max(1, min(128, (8 * 1024 * 1024) // (raster.width * 3)))
    total = math.ceil(raster.height / rows)
    compressor = zlib.compressobj(6)
    try:
        writer.write(b"\x89PNG\r\n\x1a\n")
        png_chunk(writer, b"IHDR", struct.pack(">IIBBBBB", raster.width, raster.height, 8, 2, 0, 0, 0))
        with raster.blob.open() as reader:
            for index, top in enumerate(range(0, raster.height, rows), 1):
                count = min(rows, raster.height - top)
                with raster.read_rect(0, top, raster.width, count, reader) as strip:
                    draw = ImageDraw.Draw(strip)
                    for x1, y1, x2, y2 in rectangles:
                        if y2 >= top and y1 < top + count:
                            draw.rectangle((x1, y1 - top, x2, y2 - top), fill=(0, 0, 0))
                    pixels = np.asarray(strip)
                    scanlines = np.zeros((count, raster.width * 3 + 1), dtype=np.uint8)
                    scanlines[:, 1:] = pixels.reshape(count, -1)
                    compressed = compressor.compress(scanlines.tobytes())
                if compressed:
                    png_chunk(writer, b"IDAT", compressed)
                if report:
                    report({"completed": index, "total": total, "label": "正在生成 PNG"})
        png_chunk(writer, b"IDAT", compressor.flush())
        png_chunk(writer, b"IEND", b"")
        return writer.finish()
    except Exception:
        writer.abort()
        raise
