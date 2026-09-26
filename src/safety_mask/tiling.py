"""Bounded detector calls; recognition continues to crop the original pixels."""

from __future__ import annotations

from .progress import ProgressEvent, detection_megapixels, detection_size

TILE_LENGTH = 1536
OVERLAP = 256
# Enabled only after the measured quality, memory and latency release gate passes.
TILING_ENABLED = False


def tile_starts(height):
    if height <= TILE_LENGTH:
        return [0]
    starts = list(range(0, height - TILE_LENGTH + 1, TILE_LENGTH - OVERLAP))
    if starts[-1] != height - TILE_LENGTH:
        starts.append(height - TILE_LENGTH)
    return starts


def bounds(poly):
    return min(p[0] for p in poly), min(p[1] for p in poly), max(p[0] for p in poly), max(p[1] for p in poly)


def area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def overlap_fraction(a, b):
    intersection = area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))
    return intersection / max(1, min(area(a), area(b)))


def merge_detections(candidates):
    """A clipped box must have an intact peer, otherwise require a full-image retry."""
    intact = [item for item in candidates if not item[2]]
    for poly, tile, clipped in candidates:
        if clipped and not any(
            other_tile != tile
            and area(bounds(other)) >= area(bounds(poly))
            and overlap_fraction(bounds(poly), bounds(other)) >= 0.85
            for other, other_tile, _ in intact
        ):
            return [], True
    kept = []
    for item in sorted(intact, key=lambda item: area(bounds(item[0])), reverse=True):
        poly, tile, _ = item
        if not any(
            tile != other_tile and overlap_fraction(bounds(poly), bounds(other)) >= 0.85
            for other, other_tile, _ in kept
        ):
            kept.append(item)
    return [item[0] for item in kept], False


class DetectionAdapter:
    def __init__(self, predictor, report, tiled):
        self.predictor, self.report, self.tiled = predictor, report, tiled

    def __getattr__(self, name):
        return getattr(self.predictor, name)

    def emit(self, event):
        if self.report:
            self.report(event)

    def whole(self, pixels, options, boundary_review=False):
        height, width = pixels.shape[:2]
        units = detection_megapixels(width, height)
        self.emit(ProgressEvent("detecting", total=1, units=units, boundary_review=boundary_review))
        result = next(iter(self.predictor([pixels], **options)))
        self.emit(ProgressEvent("detecting", 1, 1, units, boundary_review=boundary_review))
        return result

    def __call__(self, images, **options):
        import cv2
        import numpy as np

        for pixels in images:
            height, width = pixels.shape[:2]
            canvas_width, canvas_height = detection_size(width, height)
            if not self.tiled or height < 3 * width or canvas_height <= 2048:
                yield self.whole(pixels, options)
                continue
            # Exactly the existing detector scale, including its independent 32px rounding.
            canvas = cv2.resize(pixels, (canvas_width, canvas_height), interpolation=cv2.INTER_LINEAR)
            starts = tile_starts(canvas_height)
            units = canvas_width * TILE_LENGTH * len(starts) / 1_000_000
            candidates = []
            self.emit(ProgressEvent("detecting", total=len(starts), units=units, unit="tile"))
            for index, top in enumerate(starts):
                tile = canvas[top : top + TILE_LENGTH]
                prediction = next(iter(self.predictor([tile], **options)))
                for polygon in prediction["dt_polys"]:
                    poly = np.asarray(polygon, dtype=float).copy()
                    box = bounds(poly)
                    clipped = (top > 0 and box[1] <= 3) or (
                        top + TILE_LENGTH < canvas_height and box[3] >= TILE_LENGTH - 4
                    )
                    poly[:, 1] += top
                    poly[:, 0] *= width / canvas_width
                    poly[:, 1] *= height / canvas_height
                    candidates.append((poly, index, clipped))
                del prediction, tile
                self.emit(ProgressEvent("detecting", index + 1, len(starts), units, unit="tile"))
            del canvas
            polygons, needs_review = merge_detections(candidates)
            if needs_review:
                yield self.whole(pixels, options, boundary_review=True)
            else:
                yield {"dt_polys": np.asarray(polygons, dtype=np.float32).reshape((-1, 4, 2))}
