"""Compare both CPU modes with the production Raster path, synthetic data and no network."""

from __future__ import annotations

import argparse
import io
import json
import os
import statistics
import subprocess
import sys
import time
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "test-results"
sys.path.insert(0, str(ROOT / "tests"))


def dense_fixture():
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1800, 9000), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 30)
    targets = []
    fields = {
        12: ("姓名：", "张三"),
        99: ("电话：", "13800138000"),
        182: ("身份证号：", "110105199001011234"),
        281: ("地址：", "北京市海淀区示例路18号302室"),
    }
    for row in range(160):
        for column in range(2):
            index = row * 2 + column
            x, y = 40 + column * 900, 100 + row * 54
            if index in fields:
                label, value = fields[index]
                draw.text((x, y), label, font=font, fill="#303838")
                value_x = x + round(font.getlength(label)) + 20
                draw.text((value_x, y), value, font=font, fill="#172c20")
                targets.append(draw.textbbox((value_x, y), value, font=font))
            else:
                draw.text((x, y), f"材料项目 {index:03d} 本地测试记录与内容核对", font=font, fill="#303838")
    return image, sorted(targets, key=lambda b: b[1])


def run_mode(mode):
    from safety_mask.network import install_local_only_guard

    install_local_only_guard()
    import psutil
    from conftest import MemoryRepository
    from verify_large_images import verify_output
    from verify_offline import fixture

    from safety_mask.models import CustomField, ExportItem, ExportRequest, RuleSet
    from safety_mask.state import SessionStore
    from safety_mask.worker import ProcessEngine

    class MeasuredEngine(ProcessEngine):
        def analyze(self, source, on_progress=None):
            self.events = []

            def report(event):
                self.events.append((event, time.perf_counter()))
                if on_progress:
                    on_progress(event)

            return super().analyze(source, report)

    engine = MeasuredEngine()
    store = SessionStore(engine, MemoryRepository())
    store.set_performance(mode)
    store.update_rules(RuleSet(custom_fields=[CustomField(id="synthetic", value="ALPHA-42")]))
    ordinary, named = fixture()
    dense, targets = dense_fixture()
    inputs = []
    for name, image, boxes in (
        ("ordinary", ordinary, [box for _, box in named]),
        ("dense_320", dense, targets),
    ):
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        inputs.append((name, buffer.getvalue(), boxes))
        image.close()
    report = {"mode": mode, "offline": True, "cases": []}
    me = psutil.Process()
    monitored = {}
    try:
        for repeat in range(4):
            for name, data, boxes in inputs:
                print(f"{mode}: {name} {'first' if repeat == 0 else 'warm ' + str(repeat)}", flush=True)
                started = time.perf_counter()
                key = store.import_image(data, name + ".png")["id"]
                peak, samples = 0, []
                while True:
                    rss, cpu = 0, 0
                    for current in [me, *me.children(recursive=True)]:
                        try:
                            process = monitored.setdefault(current.pid, current)
                            rss += process.memory_info().rss
                            cpu += process.cpu_percent()
                        except psutil.Error:
                            pass
                    peak = max(peak, rss)
                    samples.append(cpu / (os.cpu_count() or 1))
                    with store.lock:
                        view = store.get(key).view()
                    if view["status"] not in {"queued", "processing"}:
                        break
                    if time.perf_counter() - started > 1200:
                        raise TimeoutError("mode benchmark")
                    time.sleep(0.25)
                assert view["status"] == "review", view["message"]
                stages = {}
                for (event, start), (_, end) in pairwise(engine.events):
                    stages[event.stage] = stages.get(event.stage, 0) + end - start
                store.confirm(key, view["revision"])
                blob, _, _ = store.export_file(
                    ExportRequest(images=[ExportItem(id=key, revision=view["revision"])])
                )
                coverage = verify_output(blob, store.get(key).source, boxes, [])
                runtime = store.runtime_status()
                assert runtime["active_mode"] == mode and runtime["cpu_cap_applied"], runtime
                result = {
                    "case": name,
                    "repeat": repeat,
                    "seconds": round(time.perf_counter() - started, 3),
                    "stages": {k: round(v, 3) for k, v in stages.items()},
                    "lines": view["line_count"],
                    "coverage": coverage,
                    "peak_tree_mib": round(peak / 1024**2, 1),
                    "cpu_median": round(statistics.median(samples), 2),
                    "cpu_p95": round(sorted(samples)[int((len(samples) - 1) * 0.95)], 2),
                    "runtime": runtime,
                }
                report["cases"].append(result)
                (OUT / f"modes-{mode}.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(json.dumps(result, ensure_ascii=True), flush=True)
                assert all(value == 1 for value in coverage), coverage
                if name == "dense_320":
                    assert view["line_count"] >= 300, view["line_count"]
                store.clear(key)
    finally:
        store.close()


def summarize():
    results = {}
    for mode in ("low_impact", "high_performance"):
        report = json.loads((OUT / f"modes-{mode}.json").read_text(encoding="utf-8"))
        results[mode] = {}
        for name in ("ordinary", "dense_320"):
            cases = [x for x in report["cases"] if x["case"] == name and x["repeat"] > 0]
            assert len(cases) == 3
            results[mode][name] = {
                stage: round(statistics.median(x["stages"].get(stage, 0) for x in cases), 3)
                for stage in ("detecting", "recognizing")
            }
            results[mode][name]["combined"] = round(
                statistics.median(
                    sum(x["stages"].get(s, 0) for s in ("detecting", "recognizing")) for x in cases
                ),
                3,
            )
    assert (
        results["high_performance"]["dense_320"]["combined"] < results["low_impact"]["dense_320"]["combined"]
    )
    (OUT / "modes-summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results), flush=True)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["low_impact", "high_performance"])
    parser.add_argument("--summarize", action="store_true")
    args = parser.parse_args()
    if args.mode:
        run_mode(args.mode)
    elif not args.summarize:
        for mode in ("low_impact", "high_performance"):
            subprocess.run(
                [sys.executable, __file__, "--mode", mode],
                check=True,
                cwd=ROOT,
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            )
        summarize()
    else:
        summarize()
