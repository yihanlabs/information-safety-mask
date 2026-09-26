"""Real, offline memory acceptance. Synthetic images only; never close user sessions."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


def run(mode, policy, cases):
    from safety_mask.network import install_local_only_guard

    install_local_only_guard()
    import psutil
    from conftest import MemoryRepository
    from fastapi.testclient import TestClient
    from PIL import Image, ImageDraw, ImageFont
    from verify_large_images import verify_output
    from verify_modes import dense_fixture
    from verify_offline import fixture

    from safety_mask.api import create_app
    from safety_mask.cache import SessionCache
    from safety_mask.memory import GIB, snapshot
    from safety_mask.models import CustomField, EditState, ManualMask, Rect, RuleSet
    from safety_mask.state import SessionStore
    from safety_mask.worker import ProcessEngine

    out = ROOT / "test-results"
    out.mkdir(exist_ok=True)
    report_path = out / f"memory-{mode}-{policy}.json"
    initial = snapshot()
    report = {
        "cpu_mode": mode,
        "memory_policy": policy,
        "offline": True,
        "initial_memory": initial.view(),
        "cases": [],
        "completed": False,
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    engine = ProcessEngine(memory_saving=None if policy == "auto" else policy == "saving")
    store = SessionStore(engine, MemoryRepository(), cache=SessionCache(out / "memory-cache"))
    store.set_performance(mode)
    store.update_rules(RuleSet(custom_fields=[CustomField(id="test", value="ALPHA-42")]))
    headers = {"X-Session-Token": "memory-acceptance-only"}
    process = psutil.Process()
    roots = store.cache.root
    try:
        with TestClient(create_app("memory-acceptance-only", store), base_url="http://127.0.0.1") as client:
            for case in cases:
                start_memory = snapshot()
                if case == "ordinary":
                    original, named = fixture()
                    targets = [box for _, box in named]
                elif case == "dense_320":
                    original, targets = dense_fixture()
                else:
                    original = Image.new("RGB", (2400, 12501), "white")
                    draw = ImageDraw.Draw(original)
                    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 80)
                    values = [
                        ("姓名：", "张三"),
                        ("电话：", "13800138000"),
                        ("身份证号：", "110105199001011234"),
                        ("地址：", "北京市海淀区示例路18号302室"),
                    ]
                    targets = []
                    for (label, value), y in zip(values, [1000, 4000, 7000, 10000]):
                        draw.text((30, y), label, font=font, fill=(70, 70, 70))
                        draw.text((480, y), value, font=font, fill=(40, 40, 40))
                        targets.append(draw.textbbox((480, y), value, font=font))
                size = original.size
                encoded = io.BytesIO()
                original.save(encoded, format="PNG")
                original.close()
                started = time.monotonic()
                response = client.post("/api/images", headers=headers, content=encoded.getvalue())
                assert response.status_code == 201, response.status_code
                key = response.json()["id"]
                encoded.close()
                peak_rss = peak_private = 0
                minimum = start_memory.available
                last_log, previous_stage = 0, None
                stages = []
                while True:
                    rss = private = 0
                    for child in [process, *process.children(recursive=True)]:
                        try:
                            info = child.memory_info()
                            rss += info.rss
                            private += getattr(info, "private", info.vms)
                        except psutil.Error:
                            pass
                    peak_rss, peak_private = max(peak_rss, rss), max(peak_private, private)
                    minimum = min(minimum, snapshot().available)
                    views = client.get("/api/images", headers=headers).json()
                    view = next(item for item in views if item["id"] == key)
                    progress = view.get("progress") or {}
                    stage = progress.get("stage")
                    elapsed = time.monotonic() - started
                    if stage != previous_stage:
                        stages.append({"stage": stage, "seconds": round(elapsed, 3)})
                        previous_stage = stage
                    if elapsed - last_log >= 20 or stage == "done":
                        print(
                            json.dumps(
                                {
                                    "case": case,
                                    "stage": stage,
                                    "seconds": round(elapsed, 1),
                                    "completed": progress.get("completed"),
                                    "total": progress.get("total"),
                                    "available_gib": round(snapshot().available / GIB, 2),
                                },
                                ensure_ascii=True,
                            ),
                            flush=True,
                        )
                        last_log = elapsed
                    if view["status"] not in {"queued", "processing"}:
                        break
                    if elapsed > 1800:
                        raise TimeoutError("memory acceptance case exceeded 30 minutes")
                    time.sleep(0.25)
                result = {
                    "case": case,
                    "initial_memory": start_memory.view(),
                    "status": view["status"],
                    "recognition_seconds": round(elapsed, 3),
                    "lines": view["line_count"],
                    "peak_tree_working_set_gib": round(peak_rss / GIB, 3),
                    "peak_tree_private_gib": round(peak_private / GIB, 3),
                    "minimum_available_gib": round(minimum / GIB, 3),
                    "stages": stages,
                    "runtime": store.runtime_status(),
                    "issue": view.get("resource_issue"),
                }
                report["cases"].append(result)
                report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
                assert view["status"] == "review", view["message"]
                if case == "dense_320":
                    assert view["line_count"] >= 300, view["line_count"]
                source = store.get(key).source
                preview = client.get(f"/api/images/{key}/preview?size=2048", headers=headers)
                assert preview.status_code == 200
                with Image.open(io.BytesIO(preview.content)) as image:
                    assert max(image.size) <= 2048
                manual = ManualMask(
                    id="manual-ground-truth", box=Rect(x=12, y=size[1] - 45, width=50, height=25)
                )
                updated = store.edit(key, view["revision"], EditState(manual=[manual]))
                download_request = {"images": [{"id": key, "revision": updated["revision"]}], "format": "png"}
                assert client.post("/api/exports", headers=headers, json=download_request).status_code == 409
                confirmation = client.post(
                    f"/api/images/{key}/confirm",
                    headers=headers,
                    json={"revision": updated["revision"], "reviewed": True},
                )
                assert confirmation.status_code == 200
                job_response = client.post("/api/exports", headers=headers, json=download_request)
                assert job_response.status_code == 202
                job_id = job_response.json()["id"]
                store.export_jobs[job_id]["future"].result(timeout=240)
                assert store.export_status(job_id)["status"] == "ready"
                blob = store.export_jobs[job_id]["blob"]
                coverage = verify_output(blob, source, targets, [])
                result["coverage"] = coverage
                result["mask_geometry"] = [mask.box.model_dump() for mask in store.get(key).masks()]
                result["target_geometry"] = targets
                # Synthetic fixture geometry only; no user inputs or OCR text.
                result["target_lines"] = [
                    {"box": line.box.model_dump(), "words": [word.box.model_dump() for word in line.words]}
                    for line in store.get(key).lines
                    if any(a[1] - 30 <= line.box.y <= a[3] + 30 for a in targets)
                ]
                report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
                assert all(value == 1 for value in coverage), coverage
                ticket = client.post(f"/api/exports/{job_id}/ticket", headers=headers).json()["url"]
                download = client.get(ticket)
                assert download.status_code == 200
                assert "sanitized_" in download.headers["content-disposition"]
                assert download.headers["cache-control"].startswith("no-store")
                with Image.open(io.BytesIO(download.content)) as image:
                    assert image.size == size and image.mode == "RGB" and not image.info
                    assert image.getpixel((20, size[1] - 30)) == (0, 0, 0)
                assert client.get(ticket).status_code == 404
                # A valid ticket also becomes unusable after an edit invalidates confirmation.
                stale = client.post(f"/api/exports/{job_id}/ticket", headers=headers).json()["url"]
                store.edit(key, updated["revision"], EditState())
                assert client.get(stale).status_code == 409
                result.update(
                    coverage=coverage,
                    total_seconds=round(time.monotonic() - started, 3),
                    independent_png=True,
                    metadata_clean=True,
                    original_size_preserved=True,
                    png_sha256=hashlib.sha256(download.content).hexdigest(),
                )
                with store.lock:
                    signature = [(line.text, line.box.model_dump()) for line in store.get(key).lines]
                result["ocr_signature"] = hashlib.sha256(
                    json.dumps(signature, sort_keys=True).encode()
                ).hexdigest()
                report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
                print(
                    json.dumps(
                        {
                            "case": case,
                            "passed": True,
                            "coverage": coverage,
                            "peak_tree_working_set_gib": result["peak_tree_working_set_gib"],
                        }
                    ),
                    flush=True,
                )
                store.clear(key)
            report["completed"] = True
    finally:
        store.close()
        report["cache_removed"] = not roots.exists()
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(str(report_path), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["low_impact", "high_performance"], default="low_impact")
    parser.add_argument("--memory", choices=["auto", "normal", "saving"], default="auto")
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=["ordinary", "dense_320", "large_30mp"],
        default=["ordinary", "dense_320"],
    )
    args = parser.parse_args()
    run(args.mode, args.memory, args.cases)
