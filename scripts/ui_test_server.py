"""Deterministic browser fixture with ground-truth text geometry; not used by the launcher."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from conftest import MemoryRepository
from PIL import Image, ImageDraw, ImageFont

from safety_mask.api import create_app
from safety_mask.cache import SessionCache
from safety_mask.models import OcrLine, Rect, Word
from safety_mask.network import install_local_only_guard
from safety_mask.state import SessionStore


class FixtureEngine:
    def analyze(self, source, on_progress=None):
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 28)
        draw = ImageDraw.Draw(Image.new("RGB", (1100, 680)))
        lines = []
        values = [
            ("姓名", "张三"),
            ("电话", "13800138000"),
            ("身份证号", "110105199001011234"),
            ("地址", "北京市海淀区示例路18号302室"),
            ("项目代号", "ALPHA-42"),
        ]
        for index, (label, value) in enumerate(values):
            for part, (x, text) in enumerate(((70, label + "："), (260, value))):
                y = 160 + index * 80
                left, top, right, bottom = draw.textbbox((x, y), text, font=font)
                words = []
                for i, char in enumerate(text):
                    xx = x + font.getlength(text[:i])
                    a, b, c, d = draw.textbbox((xx, y), char, font=font)
                    words.append(
                        Word(
                            start=i, end=i + 1, box=Rect(x=a, y=b, width=max(1, c - a), height=max(1, d - b))
                        )
                    )
                lines.append(
                    OcrLine(
                        id=f"line-{index}-{part}",
                        text=text,
                        box=Rect(x=left, y=top, width=right - left, height=bottom - top),
                        words=words,
                    )
                )
        return lines, []


if __name__ == "__main__":
    import uvicorn

    install_local_only_guard()
    store = SessionStore(
        FixtureEngine(),
        MemoryRepository(),
        cache=SessionCache(Path(__file__).resolve().parents[1] / "test-results" / "ui-cache"),
    )
    uvicorn.run(
        create_app("e2e-local-only", store),
        host="127.0.0.1",
        port=8765,
        access_log=False,
        log_level="critical",
    )
