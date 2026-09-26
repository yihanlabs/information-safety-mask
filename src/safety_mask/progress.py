"""Content-free, session-only progress and deliberately broad timing estimates."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

Stage = Literal["preparing", "loading", "detecting", "recognizing", "analyzing", "finishing"]
STAGES = {
    "preparing": (1, "准备图片"),
    "loading": (2, "准备识别模型"),
    "detecting": (3, "寻找文字位置"),
    "recognizing": (4, "识别图片文字"),
    "analyzing": (5, "分析敏感内容"),
    "finishing": (5, "生成遮盖区域"),
}


@dataclass(frozen=True)
class ProgressEvent:
    stage: Stage
    completed: int = 0
    total: int | None = None
    units: float = 1.0
    unit: str | None = None
    profile: str | None = None
    boundary_review: bool = False


ProgressCallback = Callable[[ProgressEvent], None]


def detection_size(width: int, height: int) -> tuple[int, int]:
    # Matches the pinned PaddleX OCR pipeline: min side 64, max side 4000,
    # followed by rounding both dimensions to multiples of 32. No image is resized here.
    ratio = max(1.0, 64 / min(width, height))
    width, height = int(width * ratio), int(height * ratio)
    ratio = min(1.0, 4000 / max(width, height))
    width, height = int(width * ratio), int(height * ratio)
    width, height = max(32, round(width / 32) * 32), max(32, round(height / 32) * 32)
    return width, height


def detection_megapixels(width: int, height: int) -> float:
    width, height = detection_size(width, height)
    return width * height / 1_000_000


class TimingEstimates:
    def __init__(self):
        self.profile = None
        # Broad initial estimates from the pinned CPU pipeline's synthetic benchmark.
        # These are NOT hardware guarantees; successful stages calibrate them in memory.
        self.rates = {
            "preparing": 0.5,
            "loading": 30.0,
            "detecting": 20.0,
            "recognizing": 0.55,
            "analyzing": 0.035,
            "finishing": 1.0,
        }
        self.observed: set[str] = set()

    def use_profile(self, profile):
        if profile is not None and profile != self.profile:
            self.__init__()
            self.profile = profile

    def expected(self, stage: str, units: float) -> float:
        return max(0.5, self.rates[stage] * max(0, units))

    def record(self, stage: str, seconds: float, units: float):
        if units <= 0 or seconds <= 0:
            return
        measured = seconds / units
        self.rates[stage] = (
            measured if stage not in self.observed else 0.5 * self.rates[stage] + 0.5 * measured
        )
        self.observed.add(stage)


class ProcessingProgress:
    def __init__(self, estimates: TimingEstimates, clock=time.monotonic):
        self.estimates, self.clock = estimates, clock
        self.queued_at = clock()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.stage_started = self.queued_at
        self.event: ProgressEvent | None = None
        self.expected = 0.0
        self.basis = "rough"
        self.learned = False
        self.failed = False

    def update(self, event: ProgressEvent):
        if self.finished_at is not None:
            return
        now = self.clock()
        self.estimates.use_profile(event.profile)
        if self.started_at is None:
            self.started_at = now
        if (
            self.event is None
            or self.event.stage != event.stage
            or event.completed < self.event.completed
            or self.event.profile != event.profile
            or self.event.boundary_review != event.boundary_review
        ):
            self.stage_started = now
            self.expected = self.estimates.expected(event.stage, event.units)
            self.basis = "session" if event.stage in self.estimates.observed else "rough"
            self.learned = False
        self.event = event
        elapsed = now - self.stage_started
        if (
            (event.stage in {"preparing", "recognizing", "analyzing"} or event.unit == "tile")
            and event.completed > 0
            and event.total
        ):
            self.expected = max(0.5, elapsed / event.completed * event.total)
            self.basis = "current"
        if event.total and event.completed >= event.total and not self.learned:
            self.estimates.record(event.stage, elapsed, event.units)
            self.learned = True

    def finish(self, failed=False):
        self.finished_at = self.clock()
        self.failed = failed

    def view(self) -> dict:
        now = self.finished_at if self.finished_at is not None else self.clock()
        elapsed = 0 if self.started_at is None else max(0, now - self.started_at)
        stage = self.event.stage if self.event else "queued"
        step, label = STAGES.get(stage, (0, "等待识别"))
        done = self.finished_at is not None and not self.failed
        total = self.event.total if self.event else None
        completed = self.event.completed if self.event else 0
        countable = (
            stage in {"preparing", "recognizing", "analyzing"} or (self.event and self.event.unit == "tile")
        ) and bool(total)
        percent = min(100, int(completed / total * 100)) if countable else None
        low = high = None
        overdue = False
        if self.event and self.finished_at is None:
            stage_elapsed = max(0, now - self.stage_started)
            lower, upper = (0.5, 2.5) if self.basis == "rough" else (0.65, 1.7)
            # The detector is one indivisible model call. Elapsed time is never
            # presented as its measured percentage; an overrun removes the ETA.
            overdue = stage_elapsed > self.expected * upper
            if not overdue:
                tail = 0.0
                if stage == "recognizing":
                    tail = self.estimates.expected("analyzing", total or 0) + 1
                elif stage == "analyzing":
                    tail = 1
                low = math.ceil(max(1, self.expected * lower - stage_elapsed + tail))
                high = math.ceil(max(low, self.expected * upper - stage_elapsed + tail))
        return {
            "stage": "done" if done else stage,
            "label": "识别完成，请检查"
            if done
            else ("复查分块边界" if self.event and self.event.boundary_review else label),
            "step": 5 if done else step,
            "steps": 5,
            "elapsed_seconds": round(elapsed, 1),
            "waiting_seconds": round(
                max(0, (self.started_at if self.started_at is not None else now) - self.queued_at), 1
            ),
            "completed": completed,
            "total": total,
            "unit": self.event.unit if self.event else None,
            "percent": 100 if done else percent,
            "eta_min_seconds": low,
            "eta_max_seconds": high,
            "eta_scope": "stage" if stage in {"preparing", "loading", "detecting"} else "image",
            "estimate_basis": self.basis,
            "overdue": overdue,
        }
