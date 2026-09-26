import io
import os
import time
from pathlib import Path

import numpy as np
import pytest
from conftest import png, settled
from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from safety_mask.api import create_app
from safety_mask.cache import CHUNK, Blob, ResourceUnavailable, SessionCache
from safety_mask.models import EditState, ExportItem, ExportRequest, ManualMask, Mask, Rect
from safety_mask.raster import TRANSPOSE, Raster, export_png, prepare_image, preview_png
from safety_mask.state import Conflict


def test_encrypted_seek_tampering_quota_and_session_cleanup(tmp_path):
    cache = SessionCache(tmp_path, reserve=0)
    root = cache.root
    data = b"PRIVATE-SYNTHETIC-MARKER" + os.urandom(CHUNK * 2 + 13)
    writer = cache.writer("image")
    for offset in range(0, len(data), 17003):
        writer.write(data[offset : offset + 17003])
    blob = writer.finish()
    assert b"PRIVATE-SYNTHETIC-MARKER" not in Path(blob.path).read_bytes()
    with blob.open() as stream:
        for offset, length in [(0, 27), (CHUNK - 11, 38), (CHUNK * 2, 90), (8, CHUNK + 99)]:
            stream.seek(offset)
            assert stream.read(length) == data[offset : offset + length]
    # A second live session must not delete the first owner's ciphertext.
    other = SessionCache(tmp_path, reserve=0)
    assert root.exists()
    other.close()
    with open(blob.path, "r+b") as stream:
        stream.seek(20)
        stream.write(b"!changed!")
    with blob.open() as stream, pytest.raises(InvalidTag):
        stream.read(30)
    cache.disk_budget = 1
    writer = cache.writer("quota")
    writer.write(b"a")
    with pytest.raises(ResourceUnavailable, match="缓存"):
        writer.finish()
    writer.abort()
    cache.close()
    assert not root.exists()
    stale = tmp_path / "session-abandoned"
    stale.mkdir()
    (stale / "owner.lock").write_bytes(b"0")
    cleaner = SessionCache(tmp_path, reserve=0)
    assert not stale.exists()
    cleaner.close()


@pytest.mark.parametrize("orientation", range(1, 9))
def test_region_and_streamed_masks_use_oriented_original_coordinates(tmp_path, orientation):
    cache = SessionCache(tmp_path, reserve=0)
    pixels = np.arange(240 * 180 * 3, dtype=np.uint8).reshape(180, 240, 3)
    raster = Raster(Blob(pixels.nbytes, data=pixels.tobytes()), 240, 180, orientation)
    original = Image.fromarray(pixels)
    expected = original.transpose(TRANSPOSE[orientation]) if orientation in TRANSPOSE else original.copy()
    try:
        with raster.read_rect(17, 29, 65, 47) as region:
            assert region.tobytes() == expected.crop((17, 29, 82, 76)).tobytes()
        masks = [Mask(id="m", source="manual", box=Rect(x=12.3, y=120.2, width=51.8, height=39.4))]
        output = export_png(raster, masks, cache, "out")
        with output.open() as stream, Image.open(stream) as result:
            result.load()
            assert result.size == expected.size and result.mode == "RGB" and not result.info
            assert result.getpixel((12, 120)) == (0, 0, 0)
            assert result.getpixel((64, 159)) == (0, 0, 0)
            assert result.getpixel((10, 110)) == expected.getpixel((10, 110))
        with Image.open(
            io.BytesIO(preview_png(raster, box=(17, 29, 65, 47), output_size=(65, 47)))
        ) as region:
            assert region.tobytes() == expected.crop((17, 29, 82, 76)).tobytes()
    finally:
        cache.close()


@pytest.mark.parametrize("fmt,mode", [("PNG", "RGBA"), ("JPEG", "RGB"), ("WEBP", "RGBA"), ("BMP", "RGB")])
def test_compatible_decoders_metadata_and_small_image_memory(tmp_path, fmt, mode):
    image = Image.new(mode, (240, 400), (100, 150, 200, 128) if mode == "RGBA" else (100, 150, 200))
    data = io.BytesIO()
    image.save(data, format=fmt)
    cache = SessionCache(tmp_path, reserve=0)
    try:
        raster = prepare_image(
            Blob(len(data.getvalue()), data=data.getvalue()), cache, "image", memory_limit=512 * 1024
        )
        assert raster.blob.data is not None and raster.blob.path is None
        with raster.read_rect(0, 0, 1, 1) as region:
            # Alpha is composited onto white before any preview or recognition.
            assert region.getpixel((0, 0))[0] >= (174 if mode == "RGBA" else 95)
    finally:
        cache.close()


def test_above_old_pixel_limit_has_bounded_preview_and_complete_export(tmp_path):
    image = Image.new("RGB", (2400, 12501), (120, 160, 200))
    original = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private", "SYNTHETIC-PRIVATE-METADATA")
    image.save(original, format="PNG", pnginfo=metadata)
    image.close()
    cache = SessionCache(tmp_path, reserve=0)
    events = []
    try:
        data = original.getvalue()
        raster = prepare_image(Blob(len(data), data=data), cache, "large", events.append)
        assert raster.width * raster.height > 30_000_000 and raster.blob.path
        assert events[-1].completed == events[-1].total
        with Image.open(io.BytesIO(preview_png(raster))) as preview:
            assert max(preview.size) == 2048
        mask = Mask(id="bottom", source="manual", box=Rect(x=100, y=12400, width=500, height=101))
        result = export_png(raster, [mask], cache, "large")
        with result.open() as stream, Image.open(stream) as output:
            assert output.size == (2400, 12501) and output.info == {}
            assert output.getpixel((200, 12500)) == (0, 0, 0)
            assert output.getpixel((800, 12500)) == (120, 160, 200)
    finally:
        cache.close()


def test_download_ticket_cannot_bypass_confirmation_or_replay(store):
    image = settled(store, store.import_image(png(), "private.png")["id"])
    request = ExportRequest(images=[ExportItem(id=image["id"], revision=image["revision"])])
    with pytest.raises(Conflict):
        store.start_export(request)
    store.confirm(image["id"], image["revision"])
    job = store.start_export(request)
    store.export_jobs[job["id"]]["future"].result(timeout=15)
    blob = store.export_jobs[job["id"]]["blob"]
    assert store.pin_export(blob) == job["id"]
    assert store.export_jobs[job["id"]]["expires"] is None
    store.finish_download(job["id"])
    ticket = store.issue_ticket(job["id"]).split("/")[-1]
    edits = EditState(manual=[ManualMask(id="new", box=Rect(x=1, y=1, width=10, height=10))])
    updated = store.edit(image["id"], image["revision"], edits)
    with pytest.raises(Conflict):
        store.take_download(ticket)
    store.confirm(image["id"], updated["revision"])
    request.images[0].revision = updated["revision"]
    job = store.start_export(request)
    store.export_jobs[job["id"]]["future"].result(timeout=15)
    ticket = store.issue_ticket(job["id"]).split("/")[-1]
    app = create_app("secret", store)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert (
            client.get("/download/" + ticket, headers={"Origin": "https://external.invalid"}).status_code
            == 403
        )
        result = client.get("/download/" + ticket)
        assert result.status_code == 200 and result.content.startswith(b"\x89PNG")
        assert result.headers["cache-control"].startswith("no-store")
        assert client.get("/download/" + ticket).status_code == 404


def test_removing_image_cancels_preparation_and_releases_ciphertext(store):
    image = store.import_image(png(), "cancel.png")
    store.clear(image["id"])
    store.executor.shutdown(wait=True)
    assert not store.list()
    assert not (store.cache.root / image["id"]).exists()


def test_file_over_25_mib_is_accepted_without_reducing_resolution(store):
    pixels = np.random.default_rng(42).integers(0, 256, (1200, 9000, 3), dtype=np.uint8)
    output = io.BytesIO()
    with Image.fromarray(pixels) as image:
        image.save(output, format="PNG", compress_level=0)
    data = output.getvalue()
    assert len(data) > 25 * 1024 * 1024
    item = store.import_image(data, "large-file.png")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with store.lock:
            item = store.get(item["id"]).view()
        if item["status"] not in {"queued", "processing"}:
            break
        time.sleep(0.05)
    assert item["status"] == "review"
    assert (item["width"], item["height"]) == (9000, 1200)


def test_preparation_reads_exif_without_rotating_twice(tmp_path):
    original = Image.new("RGB", (120, 80), "white")
    original.paste((255, 0, 0), (0, 0, 30, 20))
    exif = original.getexif()
    exif[274] = 6
    exif[270] = "SYNTHETIC-PRIVATE-EXIF"
    encoded = io.BytesIO()
    original.save(encoded, format="PNG", exif=exif)
    cache = SessionCache(tmp_path, reserve=0)
    try:
        data = encoded.getvalue()
        raster = prepare_image(Blob(len(data), data=data), cache, "rotated")
        assert (raster.width, raster.height) == (80, 120)
        with raster.read_rect(0, 0, 80, 120) as result:
            assert result.tobytes() == original.transpose(Image.Transpose.ROTATE_270).tobytes()
        with raster.blob.open() as stream:
            assert b"SYNTHETIC-PRIVATE-EXIF" not in stream.read()
    finally:
        cache.close()
