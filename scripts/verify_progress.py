"""Verify live API progress and redaction with real, offline models and synthetic images."""

from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from safety_mask.api import create_app
from safety_mask.models import CustomField, ExportItem, ExportRequest, RuleSet
from safety_mask.network import install_local_only_guard
from safety_mask.state import SessionStore
from safety_mask.worker import ProcessEngine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from conftest import MemoryRepository
from verify_offline import fixture

ROOT = Path(__file__).resolve().parents[1]


class ObservedEngine(ProcessEngine):
    def __init__(self):
        super().__init__()
        self.events = []

    def analyze(self, source, on_progress=None):
        def report(event):
            self.events.append(event)
            if on_progress:
                on_progress(event)

        return super().analyze(source, report)


def run():
    install_local_only_guard()
    engine = ObservedEngine()
    store = SessionStore(engine, MemoryRepository())
    store.update_rules(RuleSet(custom_fields=[CustomField(id="test", value="ALPHA-42")]))
    base, targets = fixture()
    long_image = Image.new("RGB", (1100, 3000), "white")
    long_image.paste(base, (0, 0))
    reports = []
    headers = {"X-Session-Token": "progress-test"}
    with TestClient(create_app("progress-test", store), base_url="http://127.0.0.1") as client:
        for name, image in [("long", long_image), ("blank", Image.new("RGB", (600, 400), "white"))]:
            engine.events.clear()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            started = time.monotonic()
            item = client.post("/api/images", headers=headers, content=buffer.getvalue()).json()
            key, samples, last_stage = item["id"], [], None
            latencies = []
            while time.monotonic() - started < 300:
                request_started = time.perf_counter()
                response = client.get("/api/images", headers=headers)
                latencies.append(time.perf_counter() - request_started)
                assert response.status_code == 200
                item = next(value for value in response.json() if value["id"] == key)
                progress = item["progress"]
                samples.append(progress)
                assert not item["confirmed"]
                if progress["stage"] != last_stage:
                    print(
                        json.dumps(
                            {
                                "case": name,
                                "stage": progress["stage"],
                                "elapsed_seconds": progress["elapsed_seconds"],
                            }
                        ),
                        flush=True,
                    )
                    last_stage = progress["stage"]
                if item["status"] not in {"queued", "processing"}:
                    break
                if (
                    progress["stage"] in {"loading", "detecting", "finishing"}
                    and progress.get("unit") != "tile"
                ):
                    assert progress["percent"] is None
                time.sleep(0.5)
            assert item["status"] == "review", item["message"]
            assert progress["stage"] == "done" and progress["percent"] == 100
            runtime = client.get("/api/health", headers=headers).json()["runtime"]
            assert runtime["backend"] == "accelerated", runtime
            assert runtime["cpu_cap_applied"] and runtime["below_normal"] and runtime["ecoqos"]
            p95 = sorted(latencies)[int((len(latencies) - 1) * 0.95)]
            assert p95 < 0.5, f"API response was too slow: p95={p95:.3f}s"
            request = ExportRequest(images=[ExportItem(id=key, revision=item["revision"])], format="png")
            assert client.post("/api/export", headers=headers, json=request.model_dump()).status_code == 409
            store.confirm(key, item["revision"])
            data, _, _ = store.export(request)
            redacted = Image.open(io.BytesIO(data))
            assert redacted.mode == "RGB" and not redacted.info
            if name == "long":
                detection_samples = [sample for sample in samples if sample["stage"] == "detecting"]
                assert len(detection_samples) >= 2, "API must remain responsive during detection"
                assert detection_samples[-1]["elapsed_seconds"] > detection_samples[0]["elapsed_seconds"]
                for _, bounds in targets:
                    pixels = [
                        (x, y)
                        for y in range(bounds[1], bounds[3])
                        for x in range(bounds[0], bounds[2])
                        if max(image.getpixel((x, y))) < 180
                    ]
                    assert (
                        sum(redacted.getpixel(point) == (0, 0, 0) for point in pixels) / len(pixels) > 0.995
                    )
                assert any(
                    e.stage == "recognizing" and e.completed == e.total and e.total for e in engine.events
                )
            else:
                assert item["line_count"] == 0 and not item["masks"]
            reports.append(
                {
                    "case": name,
                    "seconds": round(time.monotonic() - started, 2),
                    "api_samples": len(samples),
                    "api_latency_p95_ms": round(p95 * 1000, 1),
                    "runtime": runtime,
                    "stages": list(dict.fromkeys(e.stage for e in engine.events)),
                    "ocr_lines": item["line_count"],
                    "masks": len(item["masks"]),
                    "passed": True,
                }
            )
            print(json.dumps(reports[-1]), flush=True)
    output = ROOT / "test-results" / "progress-report.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "outbound_network": "blocked",
                "native_trace_enabled": os.environ.get("ONEDNN_VERBOSE") == "1",
                "cases": reports,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    run()
