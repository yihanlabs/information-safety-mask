import io
import json
import os
import threading

import pytest
from conftest import MemoryRepository, png, settled
from PIL import Image, PngImagePlugin

from safety_mask.imaging import canonical_image, render_redacted
from safety_mask.models import (
    CustomField,
    EditState,
    ExportItem,
    ExportRequest,
    ManualMask,
    Mask,
    Rect,
    RuleSet,
)
from safety_mask.settings import SettingsRepository
from safety_mask.state import Conflict, SessionStore


def test_confirmation_revision_and_export_snapshot(store):
    item = store.import_image(png(), "private-name-13800138000.png")
    item = settled(store, item["id"])
    request = ExportRequest(images=[ExportItem(id=item["id"], revision=item["revision"])])
    with pytest.raises(Conflict):
        store.export(request)
    store.confirm(item["id"], item["revision"])
    data, mime, name = store.export(request)
    assert mime == "image/png" and name == "sanitized_001.png"
    result = Image.open(io.BytesIO(data))
    assert result.mode == "RGB" and not result.info
    for mask in item["masks"]:
        box = mask["box"]
        assert result.getpixel((int(box["x"] + 2), int(box["y"] + 2))) == (0, 0, 0)
    edits = EditState(manual=[ManualMask(id="m", box=Rect(x=1, y=1, width=20, height=20))])
    updated = store.edit(item["id"], item["revision"], edits)
    assert not updated["confirmed"]
    with pytest.raises(Conflict):
        store.export(request)


def test_rules_invalidate_confirmation_and_preserve_manual_and_exclusions(store):
    item = settled(store, store.import_image(png(), "x.png")["id"])
    excluded = item["masks"][0]["id"]
    edits = EditState(
        excluded=[excluded], manual=[ManualMask(id="m", box=Rect(x=1, y=1, width=10, height=10))]
    )
    item = store.edit(item["id"], item["revision"], edits)
    store.confirm(item["id"], item["revision"])
    store.update_rules(RuleSet(categories=[]))
    item = store.get(item["id"]).view()
    assert not item["confirmed"] and len(item["masks"]) == 1
    store.update_rules(RuleSet())
    item = store.get(item["id"]).view()
    assert excluded in item["edits"]["excluded"]
    assert excluded not in [mask["id"] for mask in item["masks"]]
    assert any(mask["source"] == "manual" for mask in item["masks"])


def test_edit_cannot_escape_image_or_overwrite_newer_revision(store):
    item = settled(store, store.import_image(png(), "x.png")["id"])
    with pytest.raises(ValueError):
        store.edit(
            item["id"],
            item["revision"],
            EditState(manual=[ManualMask(id="x", box=Rect(x=790, y=0, width=30, height=30))]),
        )
    with pytest.raises(Conflict):
        store.edit(item["id"], item["revision"] - 1, EditState())


def test_export_strips_metadata_alpha_and_burns_pixels():
    original = Image.new("RGBA", (100, 80), (22, 44, 66, 0))
    meta = PngImagePlugin.PngInfo()
    meta.add_text("secret", "private_name_and_location")
    data = io.BytesIO()
    original.save(data, format="PNG", pnginfo=meta)
    canonical, width, height = canonical_image(data.getvalue())
    assert (width, height) == (100, 80)
    output = render_redacted(
        canonical, [Mask(id="x", source="manual", box=Rect(x=10.2, y=20.2, width=20.3, height=20.3))]
    )
    image = Image.open(io.BytesIO(output))
    assert image.mode == "RGB" and image.info == {}
    assert image.getpixel((10, 20)) == (0, 0, 0)
    assert image.getpixel((30, 40)) == (0, 0, 0)
    assert image.getpixel((70, 50)) == (255, 255, 255)
    assert b"private_name_and_location" not in output


def test_exif_orientation_is_applied_then_removed():
    image = Image.new("RGB", (120, 80), "white")
    exif = image.getexif()
    exif[274] = 6
    exif[270] = "private-description"
    output = io.BytesIO()
    image.save(output, format="JPEG", exif=exif)
    clean, width, height = canonical_image(output.getvalue())
    assert (width, height) == (80, 120)
    assert not Image.open(io.BytesIO(clean)).getexif()


def test_invalid_and_animated_images_rejected():
    with pytest.raises(ValueError):
        canonical_image(b"broken-image")
    out = io.BytesIO()
    frames = [Image.new("RGB", (20, 20), color) for color in ("red", "blue")]
    frames[0].save(out, format="PNG", save_all=True, append_images=frames[1:], duration=100)
    with pytest.raises(ValueError, match="动画"):
        canonical_image(out.getvalue())


def test_failure_requires_manual_review_and_never_auto_confirms():
    class Broken:
        def analyze(self, source, on_progress=None):
            raise ValueError("SECRET original input should not reach UI")

    store = SessionStore(Broken(), MemoryRepository())
    try:
        item = settled(store, store.import_image(png(), "x.png")["id"])
        assert item["status"] == "error" and not item["confirmed"]
        assert "SECRET" not in item["message"]
        with pytest.raises(Conflict):
            store.export(ExportRequest(images=[ExportItem(id=item["id"], revision=item["revision"])]))
        store.confirm(item["id"], item["revision"])
        assert store.get(item["id"]).view()["confirmed"]
    finally:
        store.close()


def test_clear_during_processing_does_not_resurrect_image():
    start, finish = threading.Event(), threading.Event()

    class Slow:
        def analyze(self, source, on_progress=None):
            start.set()
            finish.wait(3)
            return [], []

    store = SessionStore(Slow(), MemoryRepository())
    try:
        store.import_image(png(), "x.png")
        assert start.wait(8)  # Includes spawning the isolated image preparation process.
        store.clear()
        finish.set()
        store.executor.shutdown(wait=True)
        assert store.list() == []
    finally:
        finish.set()
        store.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_real_windows_encryption_roundtrip_and_clear(tmp_path):
    repo = SettingsRepository(tmp_path / "rules.json")
    rules = RuleSet(categories=["person"], custom_fields=[CustomField(id="x", value="隐私测试内容")])
    repo.save(rules)
    raw = repo.path.read_text(encoding="utf8")
    assert "隐私测试内容" not in raw and "custom_fields" not in raw
    assert json.loads(raw)["version"] == 1
    assert repo.load() == rules
    repo.clear()
    assert not repo.path.exists()


def test_corrupt_settings_not_silently_replaced(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text("invalid", encoding="utf8")
    with pytest.raises(RuntimeError):
        SettingsRepository(path).load()


def test_locked_model_hashes_detect_corruption(tmp_path):
    import hashlib

    from safety_mask.inference import verify_model_files

    model = tmp_path / "model.bin"
    model.write_bytes(b"expected")
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps({"files": {"model.bin": hashlib.sha256(b"expected").hexdigest()}}), encoding="utf8"
    )
    verify_model_files(tmp_path, lock)
    model.write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="校验"):
        verify_model_files(tmp_path, lock)


def test_missing_models_fail_locally_before_inference(monkeypatch):
    from safety_mask import inference

    monkeypatch.setattr(inference, "model_status", lambda: {"ready": False, "missing": ["missing"]})
    with pytest.raises(RuntimeError, match="本地模型未准备"):
        inference.LocalEngine().load()


def test_blank_word_box_compatibility_does_not_hide_other_errors():
    from safety_mask.inference import LocalEngine

    class EmptyOCR:
        def __init__(self):
            self.calls = []

        def predict(self, pixels, **options):
            self.calls.append(options)
            if options.get("return_word_box") is not False:
                raise KeyError("text_word_region")
            return [{"rec_texts": [], "rec_boxes": [], "rec_scores": []}]

    class EmptyNER:
        def pipe(self, texts, **options):
            return []

    engine = LocalEngine()
    engine.ocr = EmptyOCR()
    engine.nlp = EmptyNER()
    assert engine.analyze(png()) == ([], [])
    assert engine.ocr.calls == [{}, {"return_word_box": False}]


def test_changed_active_policy_invalidates_even_zero_new_matches(store):
    item = settled(store, store.import_image(png(), "x.png")["id"])
    store.confirm(item["id"], item["revision"])
    store.update_rules(RuleSet(custom_fields=[CustomField(id="new", value="未识别的敏感内容")]))
    assert not store.get(item["id"]).view()["confirmed"]
