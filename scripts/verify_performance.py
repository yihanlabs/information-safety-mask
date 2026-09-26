"""Synthetic offline benchmarks; isolate each backend to compare peak memory fairly."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import time
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "test-results"


def long_fixture():
    from PIL import Image, ImageDraw, ImageFont
    from verify_offline import FONT

    image = Image.new("RGB", (2400, 11347), "white")
    draw = ImageDraw.Draw(image)
    body = ImageFont.truetype(FONT, 52)
    font = ImageFont.truetype(FONT, 64)
    small = ImageFont.truetype(FONT, 40)
    fields = [
        ("姓名", "张三", 500, font),
        ("电话", "13800138000", 1900, font),
        ("身份证号", "110105199001011234", 4310, font),
        ("地址", "北京市海淀区示例路18号302室", 7940, font),
        ("电话", "13900139000", 9600, small),
        ("项目代号", "ALPHA-42", 11000, font),
    ]
    for index, y in enumerate(range(100, 11200, 220)):
        if any(abs(y - field[2]) < 160 for field in fields):
            continue
        draw.text(
            (100, y),
            f"记录项目 {index:03d}：本地数据处理与材料核对，内容仅作合成测试。",
            font=body,
            fill="#303838",
        )
    targets = []
    for label, value, y, current_font in fields:
        draw.text((100, y), label + "：", font=current_font, fill="#43574e")
        draw.text((520, y), value, font=current_font, fill="#172c20")
        targets.append((value, draw.textbbox((520, y), value, font=current_font)))
    return image, targets


def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


def child(mode):
    from safety_mask.resources import apply_worker_limits

    limits = apply_worker_limits()
    from safety_mask.network import install_local_only_guard

    install_local_only_guard()
    import importlib.metadata

    import numpy as np
    import psutil
    from PIL import Image
    from verify_offline import fixture

    from safety_mask.detection import detect
    from safety_mask.imaging import canonical_image, render_redacted
    from safety_mask.inference import LocalEngine
    from safety_mask.models import CustomField, RuleSet

    emit(
        {
            "kind": "configuration",
            "mode": mode,
            "pid": os.getpid(),
            "limits": limits.view(),
            "versions": {
                name: importlib.metadata.version(name) for name in ("paddlepaddle", "paddleocr", "paddlex")
            },
        }
    )
    engine = LocalEngine(accelerated=mode != "plain", threads=limits.threads, tiled=mode == "tiled")
    rules = RuleSet(custom_fields=[CustomField(id="sample", value="ALPHA-42")])
    ordinary, ordinary_targets = fixture()
    dense, dense_targets = long_fixture()
    for name, image, targets, rotated in [
        ("ordinary_cold", ordinary, ordinary_targets, False),
        ("ordinary_warm", ordinary, ordinary_targets, False),
        ("dense_long_first", dense, dense_targets, False),
        ("dense_long_warm", dense, dense_targets, False),
        ("rotated", ordinary, ordinary_targets, True),
        ("blank", Image.new("RGB", (600, 400), "white"), [], False),
    ]:
        buffer = io.BytesIO()
        if rotated:
            photo = image.transpose(Image.Transpose.ROTATE_90)
            exif = photo.getexif()
            exif[274] = 6
            exif[270] = "synthetic-private-metadata"
            photo.save(buffer, format="JPEG", quality=97, exif=exif)
        else:
            image.save(buffer, format="PNG")
        clean, width, height = canonical_image(buffer.getvalue())
        emit({"kind": "start", "case": name})
        started = time.perf_counter()
        events = []
        stage_peaks = {}
        current_process = psutil.Process()

        def report(
            event, events=events, started=started, stage_peaks=stage_peaks, current_process=current_process
        ):
            events.append((event, time.perf_counter() - started))
            if event.total and event.completed >= event.total:
                info = current_process.memory_info()
                stage_peaks[event.stage] = round(getattr(info, "peak_wset", info.rss) / 1024**2, 1)

        lines, entities = engine.analyze(clean, on_progress=report)
        seconds = time.perf_counter() - started
        stages = {}
        for (event, beginning), (_, end) in pairwise(events):
            stages[event.stage] = stages.get(event.stage, 0) + end - beginning
        emit({"kind": "end", "case": name})
        masks = detect(lines, entities, rules, width, height)
        redacted = Image.open(io.BytesIO(render_redacted(clean, masks)))
        original = Image.open(io.BytesIO(clean))
        coverage = []
        for _, box in targets:
            ink = np.max(np.asarray(original.crop(box)), axis=2) < 180
            black = np.all(np.asarray(redacted.crop(box)) == 0, axis=2)
            coverage.append(round(float((black & ink).sum() / max(1, ink.sum())), 5))
        emit(
            {
                "kind": "result",
                "case": name,
                "seconds": round(seconds, 3),
                "stages": {key: round(value, 3) for key, value in stages.items()},
                "process_peak_mb_after_stage": stage_peaks,
                "lines": len(lines),
                "mask_count": len(masks),
                "coverage": coverage,
                "metadata_clean": not redacted.info and redacted.mode == "RGB",
                "tile_completed": max((e.completed for e, _ in events if e.unit == "tile"), default=0),
                "boundary_review": any(e.boundary_review for e, _ in events),
            }
        )
    limits.close()


def parent(modes):
    import queue
    import threading

    import psutil

    OUT.mkdir(exist_ok=True)
    combined = {}
    for mode in modes:
        process = subprocess.Popen(
            [sys.executable, "-B", __file__, "--child", mode],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        messages = queue.Queue()

        def reader(output=process.stdout, target=messages):
            for line in output:
                target.put(line)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        monitored = psutil.Process(process.pid)
        monitored.cpu_percent()
        report = {"cases": [], "peak_rss_mb": 0, "cpu_samples_percent": []}
        active, peak = None, 0
        diagnostics = []
        while process.poll() is None or thread.is_alive() or not messages.empty():
            try:
                memory = monitored.memory_info().rss / 1024**2
                if active:
                    peak = max(peak, memory)
                    report["cpu_samples_percent"].append(
                        round(monitored.cpu_percent() / (os.cpu_count() or 1), 2)
                    )
                else:
                    monitored.cpu_percent()
                report["peak_rss_mb"] = max(report["peak_rss_mb"], round(memory, 1))
            except psutil.Error:
                pass
            try:
                line = messages.get(timeout=0.2)
            except queue.Empty:
                continue
            if not line.startswith("{"):
                diagnostics.append(line.strip())
                diagnostics = diagnostics[-12:]
                continue
            item = json.loads(line)
            if item["kind"] == "start":
                active, peak = item["case"], 0
                print(f"{mode}: {active}", flush=True)
            elif item["kind"] == "end":
                active = None
            elif item["kind"] == "configuration":
                report.update(item)
                # Windows venv python.exe is a launcher; sample the actual interpreter.
                monitored = psutil.Process(item["pid"])
                monitored.cpu_percent()
            elif item["kind"] == "result":
                item["peak_rss_mb"] = round(peak, 1)
                report["cases"].append(item)
                emit({"mode": mode, **item})
        report["exit_code"] = process.wait()
        if process.returncode:
            report["diagnostics"] = diagnostics
        combined[mode] = report
        (OUT / f"performance-{mode}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if process.returncode:
            raise SystemExit(f"Benchmark {mode} failed. See the process output.")
    return combined


def summarize():
    reports = {
        mode: json.loads((OUT / f"performance-{mode}.json").read_text(encoding="utf-8"))
        for mode in ("plain", "accelerated", "tiled")
    }
    cases = {mode: {item["case"]: item for item in report["cases"]} for mode, report in reports.items()}

    def valid_case(item):
        return (
            item["metadata_clean"]
            and all(value >= 0.995 for value in item["coverage"])
            and (item["case"] != "blank" or item["lines"] == item["mask_count"] == 0)
        )

    accelerated = cases["accelerated"]["dense_long_warm"]
    tiled = cases["tiled"]["dense_long_warm"]
    gate = {
        "coverage_preserved": all(
            len(item["coverage"]) == len(cases["accelerated"][name]["coverage"])
            and all(
                a >= b for a, b in zip(item["coverage"], cases["accelerated"][name]["coverage"], strict=True)
            )
            for name, item in cases["tiled"].items()
        ),
        "warm_time_not_worse": tiled["seconds"] <= accelerated["seconds"],
        "long_peak_memory_lower": max(
            item["peak_rss_mb"] for name, item in cases["tiled"].items() if name.startswith("dense_long")
        )
        < max(
            item["peak_rss_mb"]
            for name, item in cases["accelerated"].items()
            if name.startswith("dense_long")
        ),
    }
    summary = {
        "network": "outbound sockets blocked during all model work",
        "versions": reports["accelerated"]["versions"],
        "limits": reports["accelerated"]["limits"],
        "all_regression_cases_passed": all(
            valid_case(item) for group in cases.values() for item in group.values()
        ),
        "acceleration_improves_warm_time": all(
            cases["accelerated"][name]["seconds"] < cases["plain"][name]["seconds"]
            for name in ("ordinary_warm", "dense_long_warm")
        ),
        "tiling_gate": gate,
        "enable_tiling": all(gate.values()),
        "warm_seconds": {
            mode: {name: cases[mode][name]["seconds"] for name in ("ordinary_warm", "dense_long_warm")}
            for mode in reports
        },
        "note": "Synthetic coverage and sampled memory/CPU on this device; not a guarantee for other images or hardware.",
    }
    (OUT / "performance-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    emit(summary)
    assert summary["all_regression_cases_passed"] and summary["acceleration_improves_warm_time"]
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=["plain", "accelerated", "tiled"])
    parser.add_argument("--modes", nargs="+", default=["plain", "accelerated", "tiled"])
    parser.add_argument("--summarize", action="store_true")
    args = parser.parse_args()
    if args.summarize:
        summarize()
    elif args.child:
        child(args.child)
    else:
        parent(args.modes)
        if set(args.modes) == {"plain", "accelerated", "tiled"}:
            summarize()
