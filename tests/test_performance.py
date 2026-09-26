import os
import threading

import pytest
from conftest import FakeEngine, MemoryRepository, png, settled
from test_worker import IncompatibleEngine

from safety_mask.image_worker import ImageWorker
from safety_mask.models import EditState, ManualMask, Rect, RuleSet
from safety_mask.resources import CpuBudget, resource_profile
from safety_mask.settings import PerformanceRepository
from safety_mask.state import SessionStore
from safety_mask.worker import ProcessEngine


def test_performance_preference_is_independent_and_validated(tmp_path):
    path = tmp_path / "performance.json"
    repository = PerformanceRepository(path)
    assert repository.load() == "low_impact"
    repository.save("high_performance")
    assert PerformanceRepository(path).load() == "high_performance"
    with pytest.raises(ValueError):
        repository.save("unknown")
    path.write_text("invalid", encoding="utf-8")
    with pytest.raises(RuntimeError, match="性能设置"):
        repository.load()


def test_runtime_does_not_report_a_failed_worker_join_as_a_success():
    class UnjoinedEngine(FakeEngine):
        def runtime_status(self):
            return {"cpu_cap_applied": False, "threads": 1, "warning": "后台预算未生效"}

    store = SessionStore(UnjoinedEngine(), MemoryRepository())
    try:
        assert not store.runtime_status()["cpu_cap_applied"]
        assert store.runtime_status()["threads"] == 1
    finally:
        store.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows CPU job verification")
@pytest.mark.parametrize("saving", [False, True])
def test_high_mode_joins_budget_without_preview_reset_and_keeps_compatibility(tmp_path, saving):
    budget = CpuBudget()
    budget.set_mode("high_performance")
    engine = ProcessEngine(IncompatibleEngine, memory_saving=saving)
    engine.job_name = budget.name
    engine.configure(resource_profile("high_performance"))
    worker = ImageWorker(tmp_path, budget.name)
    try:
        engine.analyze(b"ok")
        status = engine.runtime_status()
        assert status["cpu_budget_percent"] == 80 and status["cpu_cap_applied"]
        assert status["threads"] == resource_profile("high_performance").threads
        assert not status["ecoqos"] and status["power_applied"]
        assert status["below_normal"] and not status["warning"]
        assert status["backend"] == "compatible"
        assert status["memory"]["mode"] == ("saving" if saving else "normal")
        from safety_mask.cache import Blob

        data = png()
        worker.call("info", Blob(len(data), data=data))
        assert budget.view()["cpu_budget_percent"] == 80
        assert worker.runtime["threads"] == 2 and worker.runtime["ecoqos"]
        engine.configure(resource_profile())
        budget.set_mode("low_impact")
        engine.analyze(b"ok")
        assert engine.runtime_status()["cpu_budget_percent"] == 20
        assert engine.runtime_status()["ecoqos"]
        assert engine.runtime_status()["backend"] == "compatible"
        assert engine.runtime_status()["memory"]["mode"] == ("saving" if saving else "normal")
    finally:
        engine.close()
        worker.close()
        budget.close()


class ControlledEngine(FakeEngine):
    def __init__(self, fail=False):
        self.started, self.release = threading.Event(), threading.Event()
        self.configurations, self.seen = [], []
        self.fail = fail

    def configure(self, profile):
        self.configurations.append(profile.mode)

    def analyze(self, source, on_progress=None):
        self.seen.append(self.configurations[-1])
        self.started.set()
        assert self.release.wait(15)
        if self.fail:
            self.fail = False
            raise ValueError("private")
        return super().analyze(source, on_progress)

    def cancel(self):
        self.release.set()


@pytest.mark.skipif(os.name != "nt", reason="Windows CPU budget")
@pytest.mark.parametrize("ending", ["complete", "failure", "cancel"])
def test_switch_waits_for_whole_image_and_latest_choice_wins(ending):
    engine = ControlledEngine(ending == "failure")
    store = SessionStore(engine, MemoryRepository())
    try:
        first = store.import_image(png(), "first.png")["id"]
        assert engine.started.wait(8)
        second = store.import_image(png(), "second.png")["id"]
        assert store.set_performance("high_performance")["pending"]
        assert not store.set_performance("low_impact")["pending"]
        status = store.set_performance("high_performance")
        assert status["active_mode"] == "low_impact" and status["pending"]
        assert engine.configurations == ["low_impact"]
        if ending == "cancel":
            store.clear(first)
        else:
            engine.release.set()
        settled(store, second)
        assert engine.seen == ["low_impact", "high_performance"]
        assert store.runtime_status()["active_mode"] == "high_performance"
        assert not store.runtime_status()["pending"]
    finally:
        engine.release.set()
        store.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows CPU budget")
def test_idle_switch_preserves_edits_confirmation_and_rules():
    store = SessionStore(FakeEngine(), MemoryRepository())
    try:
        key = store.import_image(png(), "example.png")["id"]
        image = settled(store, key)
        edits = EditState(manual=[ManualMask(id="manual", box=Rect(x=4, y=5, width=6, height=7))])
        image = store.edit(key, image["revision"], edits)
        store.confirm(key, image["revision"])
        before = store.get(key).view()
        assert store.set_performance("high_performance")["active_mode"] == "high_performance"
        after = store.get(key).view()
        assert before["revision"] == after["revision"] and after["confirmed"]
        assert before["edits"] == after["edits"] and before["masks"] == after["masks"]
        store.update_rules(RuleSet(), reset=True)
        assert store.performance_repository.load() == "high_performance"
        # Failed budget update retains the existing low budget and an honest warning.
        store.set_performance("low_impact")
        original = store.budget._set
        store.budget._set = lambda percent: False if percent == 80 else original(percent)
        status = store.set_performance("high_performance")
        assert status["active_mode"] == "low_impact" and status["warning"]
        assert status["cpu_budget_percent"] == 20 and not status["pending"]
    finally:
        store.close()
