import threading
from types import SimpleNamespace

import pytest
from conftest import MemoryRepository, png, settled

from safety_mask.inference import observe_ocr
from safety_mask.progress import ProcessingProgress, ProgressEvent, TimingEstimates, detection_megapixels
from safety_mask.state import Conflict, SessionStore


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


def test_indivisible_detection_never_claims_a_percentage_or_zero_eta():
    clock = Clock()
    progress = ProcessingProgress(TimingEstimates(), clock)
    clock.value = 20
    assert progress.view()["waiting_seconds"] == 20
    progress.update(ProgressEvent("detecting", total=1, units=3.3))
    clock.value += 40
    view = progress.view()
    assert view["percent"] is None and view["eta_scope"] == "stage"
    assert view["eta_min_seconds"] > 0 and view["eta_max_seconds"] > 0
    assert view["elapsed_seconds"] == 40 and view["waiting_seconds"] == 20
    clock.value += 180
    view = progress.view()
    assert view["overdue"] and view["eta_max_seconds"] is None
    assert view["percent"] is None and view["stage"] == "detecting"


def test_real_counts_calibrate_eta_and_only_terminal_success_completes_image():
    clock = Clock()
    progress = ProcessingProgress(TimingEstimates(), clock)
    progress.update(ProgressEvent("recognizing", total=100, units=100))
    clock.value = 5
    progress.update(ProgressEvent("recognizing", 10, 100, 100))
    view = progress.view()
    assert view["percent"] == 10 and view["estimate_basis"] == "current"
    assert view["eta_scope"] == "image" and view["eta_min_seconds"] > 0
    assert view["waiting_seconds"] == 0
    clock.value = 50
    progress.update(ProgressEvent("recognizing", 100, 100, 100))
    assert progress.view()["stage"] == "recognizing"
    progress.update(ProgressEvent("finishing", total=1))
    assert progress.view()["percent"] is None
    progress.finish()
    clock.value += 50
    assert progress.view()["stage"] == "done" and progress.view()["elapsed_seconds"] == 50
    assert progress.view()["percent"] == 100 and progress.view()["eta_max_seconds"] is None


def test_failed_task_stops_elapsed_time_and_never_reports_success():
    clock = Clock()
    progress = ProcessingProgress(TimingEstimates(), clock)
    progress.update(ProgressEvent("recognizing", total=100, units=100))
    clock.value = 3
    progress.update(ProgressEvent("recognizing", 6, 100, 100))
    progress.finish(failed=True)
    clock.value = 100
    progress.update(ProgressEvent("recognizing", 100, 100, 100))
    view = progress.view()
    assert view["elapsed_seconds"] == 3 and view["percent"] == 6
    assert view["stage"] != "done" and view["eta_max_seconds"] is None


def test_detection_estimate_uses_actual_capped_geometry_and_session_measurements():
    assert detection_megapixels(2400, 11347) == 3.328
    assert detection_megapixels(1100, 3000) == 3.272704
    clock = Clock()
    timings = TimingEstimates()
    first = ProcessingProgress(timings, clock)
    first.update(ProgressEvent("detecting", total=1, units=2))
    clock.value = 100
    first.update(ProgressEvent("detecting", 1, 1, 2))
    next_image = ProcessingProgress(timings, clock)
    next_image.update(ProgressEvent("detecting", total=1, units=1))
    assert next_image.view()["estimate_basis"] == "session"
    assert next_image.expected == 50


def test_real_tile_progress_boundary_retry_and_backend_recalibration():
    clock = Clock()
    timings = TimingEstimates()
    progress = ProcessingProgress(timings, clock)
    progress.update(ProgressEvent("detecting", 0, 3, 3.8, unit="tile", profile="accelerated"))
    clock.value = 20
    progress.update(ProgressEvent("detecting", 1, 3, 3.8, unit="tile", profile="accelerated"))
    assert progress.view()["percent"] == 33 and progress.view()["unit"] == "tile"
    progress.update(ProgressEvent("detecting", total=1, profile="accelerated", boundary_review=True))
    assert progress.view()["percent"] is None and progress.view()["label"] == "复查分块边界"
    timings.record("recognizing", 100, 1)
    progress.update(ProgressEvent("recognizing", total=10, units=10, profile="compatible"))
    assert progress.view()["estimate_basis"] == "rough"
    assert not timings.observed


def test_predictor_observation_preserves_results_options_and_restores_after_error():
    results = [object(), object()]
    inputs = [object(), object()]
    calls, events = [], []

    def predict(images, **options):
        calls.append((images, options))
        yield from results

    pipeline = SimpleNamespace(text_det_model=predict, text_rec_model=predict)
    ocr = SimpleNamespace(paddlex_pipeline=SimpleNamespace(_pipeline=pipeline))
    with pytest.raises(RuntimeError), observe_ocr(ocr, events.append, 3.3):
        assert list(pipeline.text_rec_model(inputs, return_word_box=True)) == results
        raise RuntimeError("test failure")
    assert pipeline.text_det_model is predict and pipeline.text_rec_model is predict
    assert calls == [(inputs, {"return_word_box": True})]
    assert [(event.completed, event.total) for event in events] == [(0, 2), (1, 2), (2, 2)]


def test_queue_progress_is_visible_during_work_and_does_not_confirm_or_change_revision():
    started, release = threading.Event(), threading.Event()

    class Engine:
        def analyze(self, source, on_progress=None):
            on_progress(ProgressEvent("recognizing", 0, 20, 20))
            on_progress(ProgressEvent("recognizing", 6, 20, 20))
            started.set()
            assert release.wait(5)
            return [], []

    store = SessionStore(Engine(), MemoryRepository())
    try:
        first = store.import_image(png(), "first.png")
        assert started.wait(2)
        second = store.import_image(png(), "second.png")
        views = store.list()
        assert views[0]["progress"]["completed"] == 6 and views[0]["revision"] == 1
        assert not views[0]["confirmed"]
        assert views[1]["queue_position"] == 1 and views[1]["status"] == "queued"
        with pytest.raises(Conflict):
            store.confirm(first["id"], 1)
        release.set()
        done = settled(store, second["id"])
        assert done["progress"]["stage"] == "done" and not done["confirmed"]
    finally:
        release.set()
        store.executor.shutdown(wait=True)
        store.close()
