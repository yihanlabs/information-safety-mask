"""Exercise real models using generated fixtures, with outbound sockets disabled."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from safety_mask.detection import detect
from safety_mask.imaging import canonical_image, render_redacted
from safety_mask.inference import LocalEngine
from safety_mask.models import CustomField, RuleSet
from safety_mask.network import install_local_only_guard

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "test-results"
FONT = "C:/Windows/Fonts/msyh.ttc"


def fixture():
    image = Image.new("RGB", (1100, 680), "#ffffff")
    draw = ImageDraw.Draw(image)
    heading = ImageFont.truetype(FONT, 36)
    font = ImageFont.truetype(FONT, 28)
    caption = ImageFont.truetype(FONT, 18)
    draw.rectangle((0, 0, 1100, 8), fill="#176d57")
    draw.text((70, 45), "信息登记表", font=heading, fill="#234637")
    draw.text((70, 100), "合成测试图片 · 不对应真实个人资料", font=caption, fill="#7f9587")
    values = [
        ("姓名", "张三"),
        ("电话", "13800138000"),
        ("身份证号", "110105199001011234"),
        ("地址", "北京市海淀区示例路18号302室"),
        ("项目代号", "ALPHA-42"),
    ]
    targets = []
    for index, (label, value) in enumerate(values):
        y = 160 + index * 80
        draw.text((70, y), label + "：", font=font, fill="#68836f")
        draw.text((260, y), value, font=font, fill="#172c20")
        targets.append((value, draw.textbbox((260, y), value, font=font)))
    draw.text((70, 605), "仅用于验证本地识别、编辑与导出流程", font=caption, fill="#8ca091")
    return image, targets


def run():
    OUT.mkdir(exist_ok=True)
    source, targets = fixture()
    source.save(OUT / "synthetic.png")
    engine = LocalEngine()
    rules = RuleSet(custom_fields=[CustomField(id="sample", value="ALPHA-42")])
    install_local_only_guard()
    reports = []
    for name, kind in [
        ("document", "png"),
        ("exif_rotated", "jpeg"),
        ("long_screenshot", "long"),
        ("blank", "blank"),
    ]:
        image = source.copy()
        if kind == "long":
            image = Image.new("RGB", (1100, 3000), "white")
            image.paste(source, (0, 0))
        if kind == "blank":
            image = Image.new("RGB", (600, 400), "white")
        buffer = io.BytesIO()
        if kind == "jpeg":
            image = image.transpose(Image.Transpose.ROTATE_90)
            exif = image.getexif()
            exif[274] = 6
            exif[270] = "private test metadata"
            image.save(buffer, format="JPEG", quality=97, exif=exif)
        else:
            image.save(buffer, format="PNG")
        clean, width, height = canonical_image(buffer.getvalue())
        started = time.perf_counter()
        lines, entities = engine.analyze(clean)
        masks = detect(lines, entities, rules, width, height)
        output = render_redacted(clean, masks)
        (OUT / f"{name}_redacted.png").write_bytes(output)
        redacted = Image.open(io.BytesIO(output))
        original = Image.open(io.BytesIO(clean))
        coverage = []
        for value, bounds in [] if kind == "blank" else targets:
            ink, total = 0, 0
            for y in range(bounds[1], bounds[3]):
                for x in range(bounds[0], bounds[2]):
                    if max(original.getpixel((x, y))) < 180:
                        total += 1
                        if redacted.getpixel((x, y)) == (0, 0, 0):
                            ink += 1
            score = ink / total if total else 1
            coverage.append({"field": value, "covered_ink_fraction": round(score, 5)})
            assert score > 0.995, f"{name}: sensitive field not fully covered: {value} ({score:.3%})"
        assert not redacted.info and redacted.mode == "RGB"
        if kind == "blank":
            assert not lines and not masks
        reports.append(
            {
                "case": name,
                "seconds": round(time.perf_counter() - started, 2),
                "ocr_lines": len(lines),
                "mask_count": len(masks),
                "coverage": coverage,
            }
        )
        print(json.dumps(reports[-1], ensure_ascii=False), flush=True)
    report = {
        "outbound_network": "blocked",
        "models": "PP-OCRv5 + zh_core_web_trf 3.8.0",
        "cases": reports,
        "note": "Synthetic regression coverage, not a general accuracy guarantee.",
    }
    (OUT / "offline-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8"
    )


if __name__ == "__main__":
    run()
