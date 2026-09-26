from itertools import pairwise

import numpy as np

from safety_mask.tiling import DetectionAdapter, merge_detections, tile_starts


def polygon(x, y, width=100, height=24):
    return np.array([[x, y], [x + width, y], [x + width, y + height], [x, y + height]], dtype=float)


def test_overlapping_tiles_cover_whole_canvas_and_end_on_original_boundary():
    starts = tile_starts(4000)
    assert starts == [0, 1280, 2464]
    assert starts[-1] + 1536 == 4000
    assert all(b - a <= 1280 for a, b in pairwise(starts))


def test_merging_retains_intact_boundary_text_and_distinct_adjacent_lines():
    intact = polygon(10, 1495)
    partial = polygon(10, 1495, height=12)
    below = polygon(10, 1530)
    result, fallback = merge_detections([(partial, 0, True), (intact, 1, False), (below, 1, False)])
    assert not fallback and len(result) == 2
    assert sorted(p[:, 1].max() for p in result) == [1519, 1554]
    assert merge_detections([(partial, 0, True)])[1]


def test_detector_maps_both_axes_back_to_original_pixels_and_reports_real_blocks():
    events, calls = [], []

    def predict(images, **options):
        calls.append(images[0].shape)
        yield {"dt_polys": [polygon(80, 200)]}

    result = next(
        DetectionAdapter(predict, events.append, True)([np.zeros((11347, 2400, 3), dtype=np.uint8)])
    )
    assert calls == [(1536, 832, 3)] * 3
    assert len(result["dt_polys"]) == 3
    first_points = sorted((poly[0] for poly in result["dt_polys"]), key=lambda point: point[1])
    np.testing.assert_allclose(
        first_points,
        [[80 * 2400 / 832, (200 + top) * 11347 / 4000] for top in [0, 1280, 2464]],
        rtol=1e-6,
    )
    assert [event.completed for event in events] == [0, 1, 2, 3]
    assert all(event.unit == "tile" for event in events)


def test_unresolved_cut_retries_the_original_image_and_keeps_whole_result():
    calls, events = [], []
    original = np.zeros((4000, 1000, 3), dtype=np.uint8)
    final = {"dt_polys": [polygon(10, 1500, height=400)]}

    def predict(images, **options):
        calls.append(images[0])
        if images[0] is original:
            yield final
        else:
            yield {"dt_polys": [polygon(10, 0)]}

    assert next(DetectionAdapter(predict, events.append, True)([original])) is final
    assert len(calls) == 4 and calls[-1] is original
    assert events[-1].boundary_review and events[-1].unit is None


def test_small_and_horizontal_images_keep_existing_detection_path():
    original = np.zeros((500, 2200, 3), dtype=np.uint8)
    seen = []

    def predict(images, **options):
        seen.extend(images)
        yield {"dt_polys": []}

    assert list(DetectionAdapter(predict, None, True)([original])) == [{"dt_polys": []}]
    assert seen[0] is original
