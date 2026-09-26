import io
import time

import pytest
from PIL import Image

from safety_mask.models import OcrLine, Rect, RuleSet, Word
from safety_mask.state import SessionStore


@pytest.fixture(autouse=True)
def isolated_appdata(tmp_path, monkeypatch):
    # Regression runs must never scan or clean users' live session directories.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-appdata"))


def line(text, key="a", y=20, x=20, words=True):
    return OcrLine(
        id=key,
        text=text,
        box=Rect(x=x, y=y, width=max(1, len(text) * 12), height=24),
        words=[
            Word(start=i, end=i + 1, box=Rect(x=x + i * 12, y=y, width=12, height=24))
            for i in range(len(text))
        ]
        if words
        else [],
    )


def png():
    output = io.BytesIO()
    Image.new("RGB", (800, 500), "white").save(output, format="PNG")
    return output.getvalue()


class MemoryRepository:
    def __init__(self):
        self.rules = RuleSet()

    def load(self):
        return self.rules.model_copy(deep=True)

    def save(self, rules):
        self.rules = rules.model_copy(deep=True)

    def clear(self):
        self.rules = RuleSet()


class FakeEngine:
    def analyze(self, source, on_progress=None):
        return [line("姓名：张三"), line("电话：13800138000", "b", 70)], []


def settled(store, key):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with store.lock:
            view = store.get(key).view()
        if view["status"] not in {"queued", "processing"}:
            return view
        time.sleep(0.01)
    raise AssertionError("测试识别任务未完成")


@pytest.fixture
def store():
    value = SessionStore(FakeEngine(), MemoryRepository())
    yield value
    value.close()
