import os
import threading
import time

import pytest

from safety_mask.inference import AccelerationCompatibilityError
from safety_mask.models import OcrLine, Rect
from safety_mask.progress import ProgressEvent
from safety_mask.worker import ProcessEngine


class StubEngine:
    def __init__(self):
        self.calls = 0

    def analyze(self, source, on_progress=None):
        on_progress(ProgressEvent("detecting", total=1))
        if source == b"error":
            raise ValueError("SECRET OCR INPUT")
        if source == b"missing":
            raise RuntimeError("本地模型未准备完成，请运行安装与准备")
        if source == b"crash":
            os._exit(7)
        if source == b"memory":
            raise MemoryError("SECRET MEMORY INPUT")
        if source == b"slow":
            time.sleep(30)
        self.calls += 1
        on_progress(ProgressEvent("detecting", 1, 1))
        return [OcrLine(id=str(os.getpid()), text=str(self.calls), box=Rect(x=0, y=0, width=1, height=1))], []


def test_worker_is_separate_reused_and_emits_real_progress():
    engine = ProcessEngine(StubEngine)
    try:
        events = []
        first, _ = engine.analyze(b"ok", on_progress=events.append)
        second, _ = engine.analyze(b"ok")
        assert first[0].id == second[0].id and first[0].id != str(os.getpid())
        assert first[0].text == "1" and second[0].text == "2"
        assert [event.completed for event in events] == [0, 1]
        status = engine.runtime_status()
        assert status["backend"] == "accelerated"
        if os.name == "nt":
            assert status["cpu_cap_applied"] and status["below_normal"] and status["ecoqos"]
            assert status["threads"] == 2 and status["cpu_budget_percent"] == 20
    finally:
        engine.close()
    with pytest.raises(RuntimeError, match="会话已结束"):
        engine.analyze(b"ok")


def test_worker_sanitizes_errors_and_recovers_for_the_next_image():
    engine = ProcessEngine(StubEngine)
    try:
        with pytest.raises(RuntimeError) as failure:
            engine.analyze(b"error")
        assert "SECRET" not in str(failure.value)
        with pytest.raises(RuntimeError, match="本地模型未准备"):
            engine.analyze(b"missing")
        with pytest.raises((RuntimeError, EOFError, OSError)):
            engine.analyze(b"crash")
        result, _ = engine.analyze(b"ok")
        assert result[0].text == "1"
    finally:
        engine.close()


def test_closing_session_stops_an_inflight_worker_and_releases_waiting_thread():
    engine = ProcessEngine(StubEngine)
    started = threading.Event()
    errors = []

    def run():
        try:
            engine.analyze(b"slow", on_progress=lambda event: started.set())
        except (RuntimeError, EOFError, OSError, ValueError) as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert started.wait(8)
        process = engine._process
        engine.close()
        thread.join(5)
        assert not thread.is_alive() and errors
        assert engine._process is None
        with pytest.raises(ValueError):
            process.is_alive()  # The owned process handle has also been closed.
    finally:
        engine.close()
        thread.join(5)


class IncompatibleEngine(StubEngine):
    accelerated = True

    def analyze(self, source, on_progress=None):
        on_progress(ProgressEvent("loading", total=1, units=float(os.getpid())))
        if self.accelerated:
            # The message must never be exposed; only a sanitized reason may leave the worker.
            raise AccelerationCompatibilityError("SECRET MODEL INPUT")
        return super().analyze(source, on_progress)


def test_known_backend_failure_retries_in_a_fresh_process_and_stays_compatible():
    engine = ProcessEngine(IncompatibleEngine)
    events = []
    try:
        first, _ = engine.analyze(b"ok", events.append)
        second, _ = engine.analyze(b"ok")
        assert first[0].id == second[0].id
        assert first[0].text == "1" and second[0].text == "2"
        assert int(events[0].units) != int(first[0].id)
        status = engine.runtime_status()
        assert status["backend"] == "compatible" and "兼容模式" in status["fallback_reason"]
        if os.name == "nt":
            assert status["cpu_cap_applied"] and status["below_normal"] and status["ecoqos"]
        assert "SECRET" not in str(status)
        profiles = {event.profile for event in events}
        assert any(profile.startswith("accelerated:") for profile in profiles)
        assert any(profile.startswith("compatible:") for profile in profiles)
    finally:
        engine.close()


def test_memory_failure_is_actionable_sanitized_and_worker_can_retry():
    from safety_mask.cache import ResourceUnavailable

    engine = ProcessEngine(StubEngine)
    try:
        with pytest.raises(ResourceUnavailable, match="内存不足") as failure:
            engine.analyze(b"memory")
        assert "SECRET" not in str(failure.value)
        result, _ = engine.analyze(b"ok")
        assert result[0].text == "1"
    finally:
        engine.close()
