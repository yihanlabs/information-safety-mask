"""Offline, bounded-memory large-image acceptance run; synthetic inputs only."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


def fixture(store, width, height, group):
    from PIL import Image, ImageDraw, ImageFont

    from safety_mask.raster import Raster

    font = ImageFont.truetype(
        "C:/Windows/Fonts/msyh.ttc", max(64, min(width // 30, round(height / 4000 * 36)))
    )
    fields = [
        ("姓名：", "张三", 0.08),
        ("电话：", "13800138000", 0.32),
        ("身份证号：", "110105199001011234", 0.54),
        ("地址：", "北京市海淀区示例路18号302室", 0.77),
    ]
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    targets = []
    x = width // 5
    for label, value, fraction in fields:
        y = round(height * fraction)
        targets.append((label, value, y, draw.textbbox((x, y), value, font=font)))
        assert targets[-1][3][2] <= width
        assert draw.textbbox((30, y), label, font=font)[2] < x
    writer = store.cache.writer(group)
    for top in range(0, height, 256):
        with Image.new("RGB", (width, min(256, height - top)), "white") as strip:
            draw = ImageDraw.Draw(strip)
            for label, value, y, box in targets:
                if box[3] >= top and y < top + 256:
                    draw.text((30, y - top), label, font=font, fill=(70, 70, 70))
                    draw.text((x, y - top), value, font=font, fill=(40, 40, 40))
            writer.write(strip.tobytes())
    return Raster(writer.finish(), width, height), [box for _, _, _, box in targets]


def verify_output(blob, source, targets, masks):
    import numpy as np
    import pyvips

    with blob.open() as stream:
        assert stream.read(8) == b"\x89PNG\r\n\x1a\n"
        chunks = []
        while header := stream.read(8):
            count = int.from_bytes(header[:4], "big")
            chunks.append(header[4:8].decode("ascii"))
            stream.seek(count + 4, 1)
        assert set(chunks) == {"IHDR", "IDAT", "IEND"}
    with blob.open() as reader:
        src = pyvips.SourceCustom()
        src.on_read(reader.read)
        src.on_seek(reader.seek)
        output = pyvips.Image.new_from_source(src, "", access="sequential", memory=True)
        assert (output.width, output.height, output.bands) == (source.width, source.height, 3)
        region = pyvips.Region.new(output)
        coverage = []
        for box in targets:
            x1, y1, x2, y2 = map(int, box)
            pixels = np.frombuffer(region.fetch(x1, y1, x2 - x1, y2 - y1), dtype=np.uint8).reshape(
                y2 - y1, x2 - x1, 3
            )
            with source.read_rect(x1, y1, x2 - x1, y2 - y1) as original:
                ink = np.max(np.asarray(original), axis=2) < 180
            covered = np.all(pixels == 0, axis=2)
            coverage.append(round(float((covered & ink).sum() / max(1, ink.sum())), 5))
        del region, output, src
    return coverage


def main(sizes):
    from safety_mask.network import install_local_only_guard

    install_local_only_guard()
    import psutil
    from conftest import MemoryRepository

    from safety_mask.models import EditState, ExportItem, ExportRequest, ManualMask, Rect
    from safety_mask.state import SessionStore
    from safety_mask.worker import ProcessEngine

    store = SessionStore(ProcessEngine(), MemoryRepository())
    dimensions = {30: (2400, 12501), 60: (3000, 20000), 100: (4000, 25000), 300: (6000, 50000)}
    report = {"offline": True, "cases": []}
    process = psutil.Process()
    try:
        # Extremely tall browser fixture exercises fit below 1% and region requests.
        from PIL import Image

        from safety_mask.raster import Raster

        writer = store.cache.writer("uifixture")
        for top in range(0, 80000, 256):
            with Image.new("RGB", (400, min(256, 80000 - top)), (245, 248, 250)) as strip:
                writer.write(strip.tobytes())
        tall = Raster(writer.finish(), 400, 80000)
        output = store.export_worker.call("export", tall, [], "uifixture")
        with output.open() as reader, (ROOT / "test-results" / "synthetic-tall.png").open("wb") as target:
            while chunk := reader.read(1024 * 1024):
                target.write(chunk)
        tall.blob.remove()
        output.remove()
        for size in sizes:
            width, height = dimensions[size]
            print(f"Preparing synthetic {width} x {height}", flush=True)
            raw, targets = fixture(store, width, height, "fixture")
            encoded = store.export_worker.call("export", raw, [], "fixture")
            raw.blob.remove()
            group = f"case{size}"
            started = time.perf_counter()
            image = store.import_blob(encoded, f"synthetic-{size}.png", group)
            peak = 0
            stages = []
            previous = None
            while True:
                with store.lock:
                    state = store.get(image["id"]).view()
                stage = state.get("progress", {}).get("stage")
                if stage != previous:
                    stages.append({"stage": stage, "seconds": round(time.perf_counter() - started, 2)})
                    previous = stage
                    print(f"{size} MP: {stage}", flush=True)
                processes = [process, *process.children(recursive=True)]
                rss = 0
                for child in processes:
                    try:
                        rss += child.memory_info().rss
                    except psutil.Error:
                        pass
                peak = max(peak, rss)
                if state["status"] not in {"queued", "processing"}:
                    break
                if time.perf_counter() - started > 1200:
                    raise RuntimeError("acceptance timeout")
                time.sleep(0.25)
            assert state["status"] == "review", state["message"]
            assert state["cache"] == "encrypted"
            source = store.get(group).source
            # Test bounded overview and a high-resolution region near the bottom.
            store.preview(group, 2048)
            store.preview(group, 2048, (100, height - 1000, 500, 600), (500, 600))
            mask = ManualMask(id="bottom", box=Rect(x=100, y=height - 128, width=500, height=128))
            updated = store.edit(group, state["revision"], EditState(manual=[mask]))
            store.confirm(group, updated["revision"])
            request = ExportRequest(images=[ExportItem(id=group, revision=updated["revision"])])
            result, mime, name = store.export_file(request)
            output_coverage = verify_output(result, source, targets, store.get(group).masks())
            assert all(value == 1 for value in output_coverage), output_coverage
            assert name.startswith("sanitized_") and mime == "image/png"
            report["cases"].append(
                {
                    "pixels": width * height,
                    "width": width,
                    "height": height,
                    "seconds": round(time.perf_counter() - started, 2),
                    "peak_process_tree_mib": round(peak / 1024**2, 1),
                    "lines": state["line_count"],
                    "coverage": output_coverage,
                    "stages": stages,
                    "runtime": store.engine.runtime_status(),
                    "metadata_clean": True,
                    "original_dimensions": True,
                }
            )
            print(json.dumps(report["cases"][-1], ensure_ascii=True), flush=True)
            store.clear(group)
            (ROOT / "test-results" / "large-image-report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    finally:
        session_root = store.cache.root
        store.close()
        report["session_cache_removed"] = not session_root.exists()
        (ROOT / "test-results" / "large-image-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="+", default=[30, 60, 100, 300])
    main(parser.parse_args().sizes)
