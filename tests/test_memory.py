import time

import pytest
from conftest import FakeEngine, MemoryRepository, png, settled

from safety_mask import memory
from safety_mask.cache import ResourceUnavailable
from safety_mask.models import EditState, ManualMask, Rect
from safety_mask.state import SessionStore
from safety_mask.worker import ProcessEngine


def test_admission_uses_incremental_physical_and_commit_budgets(monkeypatch):
    # This 32 GiB computer would fail the former 9.2 GiB gate.
    current = memory.MemorySnapshot(7 * memory.GIB, 12 * memory.GIB, 32 * memory.GIB)
    monkeypatch.setattr(memory, "snapshot", lambda: current)
    memory.require_memory(2 * memory.GIB, "loading")
    assert current.saving
    current = memory.MemorySnapshot(7 * memory.GIB, 2 * memory.GIB, 32 * memory.GIB)
    with pytest.raises(ResourceUnavailable) as failed:
        memory.require_memory(2 * memory.GIB, "loading")
    assert failed.value.issue["code"] == "memory_preflight"
    assert failed.value.issue["release_bytes"] == 1.5 * memory.GIB
    assert "系统剩余可分配额度" in str(failed.value)
    current = memory.MemorySnapshot(2 * memory.GIB, 30 * memory.GIB, 32 * memory.GIB)
    with pytest.raises(ResourceUnavailable):
        memory.require_memory(2 * memory.GIB, "loading")
    memory.require_memory(64 * 1024**2, "recognizing")


class RecoverableEngine:
    def analyze(self, source, on_progress=None):
        if not self.memory_saving:
            raise MemoryError("PRIVATE TEST INPUT")
        return [], []


class AlwaysOutOfMemory:
    def analyze(self, source, on_progress=None):
        raise MemoryError("PRIVATE TEST INPUT")


def test_worker_retries_memory_once_in_a_fresh_saving_process():
    engine = ProcessEngine(RecoverableEngine, memory_saving=False)
    try:
        assert engine.analyze(b"synthetic") == ([], [])
        assert engine.runtime_status()["memory"]["mode"] == "saving"
        first = engine._process.pid
        engine.analyze(b"synthetic")
        assert engine._process.pid == first
    finally:
        engine.close()
    engine = ProcessEngine(AlwaysOutOfMemory, memory_saving=False)
    try:
        with pytest.raises(ResourceUnavailable) as failed:
            engine.analyze(b"synthetic")
        assert failed.value.issue["code"] == "memory_allocation"
        assert "PRIVATE" not in str(failed.value)
        assert engine._process is None and engine._saving
    finally:
        engine.close()


class OnceUnavailable(FakeEngine):
    def __init__(self):
        self.fail = False
        self.calls = 0

    def analyze(self, source, on_progress=None):
        self.calls += 1
        if self.fail:
            self.fail = False
            raise memory.memory_error("detecting", code="memory_preflight", additional=2 * memory.GIB)
        return super().analyze(source, on_progress)


@pytest.mark.parametrize("resume", ["retry", "remove", "manual"])
def test_waiting_memory_pauses_queue_and_preserves_edits_and_other_confirmations(resume):
    engine = OnceUnavailable()
    store = SessionStore(engine, MemoryRepository())
    try:
        completed = settled(store, store.import_image(png(), "done.png")["id"])
        store.confirm(completed["id"], completed["revision"])
        engine.fail = True
        waiting = settled(store, store.import_image(png(), "waiting.png")["id"])
        assert waiting["status"] == "waiting_memory"
        assert waiting["revision"] == 1 and waiting["resource_issue"]["stage"] == "detecting"
        edits = EditState(manual=[ManualMask(id="hand", box=Rect(x=1, y=1, width=12, height=12))])
        waiting = store.edit(waiting["id"], waiting["revision"], edits)
        queued = store.import_image(png(), "next.png")
        time.sleep(0.1)
        assert store.get(queued["id"]).status == "queued" and engine.calls == 2
        assert store.get(completed["id"]).view()["confirmed"]
        if resume == "retry":
            store.retry(waiting["id"])
            recovered = settled(store, waiting["id"])
            assert recovered["status"] == "review" and recovered["edits"] == edits.model_dump()
        elif resume == "manual":
            store.confirm(waiting["id"], waiting["revision"])
            assert store.get(waiting["id"]).view()["confirmed"]
        else:
            store.clear(waiting["id"])
        assert settled(store, queued["id"])["status"] == "review"
        assert store.get(completed["id"]).view()["confirmed"]
        assert store.memory_paused_id is None
    finally:
        store.close()


def test_retry_refreshes_memory_without_spawning_when_still_insufficient(monkeypatch):
    engine = OnceUnavailable()
    engine.fail = True
    store = SessionStore(engine, MemoryRepository())
    try:
        waiting = settled(store, store.import_image(png(), "waiting.png")["id"])
        low = memory.MemorySnapshot(2 * memory.GIB, 20 * memory.GIB, 32 * memory.GIB)
        monkeypatch.setattr(memory, "snapshot", lambda: low)
        retried = store.retry(waiting["id"])
        assert retried["status"] == "waiting_memory" and engine.calls == 1
        assert retried["revision"] == waiting["revision"]
        assert retried["resource_issue"]["available_bytes"] == 2 * memory.GIB
    finally:
        store.close()
