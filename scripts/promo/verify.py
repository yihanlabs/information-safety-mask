"""Check the actual demo PNG downloads and final media, without rerunning OCR."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "test-results" / "promo-v0.1.1"


def coverage(original, sanitized, bounds):
    ink = covered = 0
    for y in range(int(bounds[1]), int(bounds[3])):
        for x in range(int(bounds[0]), int(bounds[2])):
            if max(original.getpixel((x, y))) < 180:
                ink += 1
                covered += sanitized.getpixel((x, y)) == (0, 0, 0)
    assert ink and covered == ink, f"Sensitive ink left uncovered: {ink - covered} pixels"
    return covered / ink


def main():
    saved = json.loads((OUT / "verification.json").read_text(encoding="utf8"))
    records = saved["records"]
    assert len(records) == 2 and saved["downloaded"] == ["sanitized_001.png", "sanitized_002.png"]
    for item, filename in zip(records, saved["downloaded"]):
        with Image.open(OUT / filename) as image:
            assert image.mode == "RGB" and not image.info
            assert image.size == (item["width"], item["height"]) and item["confirmed"]
            for mask in item["masks"]:
                b = mask["box"]
                assert image.getpixel((int(b["x"] + b["width"] / 2), int(b["y"] + b["height"] / 2))) == (
                    0,
                    0,
                    0,
                )
    with Image.open(OUT / "form.png") as original, Image.open(OUT / "sanitized_001.png") as sanitized:
        form_coverage = [
            coverage(original, sanitized, bounds)
            for _, bounds in json.loads((OUT / "targets.json").read_text(encoding="utf8"))
        ]
    with Image.open(OUT / "long.png") as original, Image.open(OUT / "sanitized_002.png") as sanitized:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 28)
        draw = ImageDraw.Draw(original)
        values = [
            ("姓名：", "张三", 940),
            ("电话：", "13800138000", 1040),
            ("地址：", "北京市海淀区示例路18号302室", 1140),
            ("内部编号：", "DEMO-NOTE-42", 1730),
        ]
        long_coverage = [
            coverage(original, sanitized, draw.textbbox((85 + font.getlength(prefix), y), value, font=font))
            for prefix, value, y in values
        ]
    assert records[1]["edits"]["overrides"], "Expected the recorded phone-mask adjustment"
    assert len(records[1]["edits"]["manual"]) == 1
    assert not (OUT / "session.json").exists(), "Demo session did not shut down"
    cover = ROOT / "docs/images/social-preview.png"
    with Image.open(cover) as image:
        assert image.size == (1280, 640) and image.mode == "RGB" and not image.info
    assert cover.stat().st_size < 1000000
    video = OUT / "yinqu-demo-v0.1.1.mp4"
    probe = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,size:stream=codec_name,width,height,r_frame_rate,sample_rate",
                "-of",
                "json",
                str(video),
            ],
            text=True,
        )
    )
    assert 30 <= float(probe["format"]["duration"]) <= 35 and video.stat().st_size < 20 * 1024**2
    picture = next(s for s in probe["streams"] if s["codec_name"] == "h264")
    assert (picture["width"], picture["height"], picture["r_frame_rate"]) == (1920, 1080, "30/1")
    assert any(s["codec_name"] == "aac" for s in probe["streams"])
    result = {
        "passed": True,
        "real_ocr_lines": [r["line_count"] for r in records],
        "confirmed_pngs": 2,
        "form_ink_coverage": form_coverage,
        "long_ink_coverage": long_coverage,
        "phone_mask_adjusted": True,
        "manual_mask_verified": True,
        "demo_session_closed": True,
        "video": probe,
        "cover_bytes": cover.stat().st_size,
        "social_preview_uploaded": False,
        "social_preview_reason": "Browser connection unavailable; settings upload pending.",
        "audio_review": "Huihui synthesis, decoded audio, volume and local recognition checked; no human listening review.",
    }
    (OUT / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
