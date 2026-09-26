"""Keep native model calls out of the HTTP server's Python interpreter."""

from __future__ import annotations

import multiprocessing
import threading
import time
from dataclasses import replace

from .cache import ResourceUnavailable
from .inference import AccelerationCompatibilityError, LocalEngine, is_acceleration_error
from .memory import GIB, RESERVE, is_memory_failure, memory_error, snapshot
from .network import install_local_only_guard
from .resources import apply_worker_limits, resource_profile
from .tiling import TILING_ENABLED


def _worker_main(connection, engine_factory, accelerated, tiled, job_name=None, profile=None, saving=False):
    limits = apply_worker_limits(job_name, profile)
    install_local_only_guard()
    try:
        connection.send(("runtime", {**limits.view(), "backend": "starting"}))
        if engine_factory is LocalEngine:
            engine = engine_factory(
                accelerated=accelerated, threads=limits.threads, tiled=tiled, memory_saving=saving
            )
        else:
            engine = engine_factory()
            if hasattr(engine, "accelerated"):
                engine.accelerated = accelerated
            engine.memory_saving = saving
        engine.memory_report = lambda value: connection.send(("memory", value))
        profile_key = f"{'accelerated' if accelerated else 'compatible'}:{limits.profile.key}:{limits.threads}:{tiled}:{saving}"
        stage = "loading"

        def progress(event):
            nonlocal stage
            stage = event.stage
            connection.send(("progress", replace(event, profile=profile_key)))

        def memory_report(value):
            nonlocal stage
            stage = value["stage"]
            connection.send(("memory", value))

        engine.memory_report = memory_report
        while True:
            source = connection.recv()
            lines = entities = None
            try:
                lines, entities = engine.analyze(
                    source,
                    on_progress=progress,
                )
                connection.send(
                    (
                        "runtime",
                        {
                            **limits.view(),
                            "backend": "accelerated" if accelerated else "compatible",
                        },
                    )
                )
                connection.send(("result", (lines, entities)))
            except Exception as exc:  # noqa: BLE001 - sanitize all third-party model errors at the boundary
                if accelerated and (
                    isinstance(exc, AccelerationCompatibilityError) or is_acceleration_error(exc)
                ):
                    connection.send(("compatibility_error", None))
                    return
                if isinstance(exc, ResourceUnavailable) or is_memory_failure(exc):
                    error = exc if isinstance(exc, ResourceUnavailable) else memory_error(stage)
                    connection.send(("resource_error", {"message": str(error), "issue": error.issue}))
                    return
                # Never send arbitrary third-party exceptions or OCR text as an error.
                message = (
                    str(exc)
                    if isinstance(exc, RuntimeError) and str(exc).startswith("本地模型未准备")
                    else "自动识别未完成，请检查本地模型；可手动遮盖并确认"
                )
                connection.send(("error", message))
            finally:
                source = lines = entities = None
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()
        limits.close()


class ProcessEngine:
    """One reusable local worker, one image at a time, no image or OCR files."""

    def __init__(
        self, engine_factory=LocalEngine, *, accelerated=True, tiled=TILING_ENABLED, memory_saving=None
    ):
        self._factory = engine_factory
        self._lifecycle = threading.Lock()
        self._serial = threading.Lock()
        self._process = self._connection = None
        self._closed = False
        self._accelerated, self._tiled = accelerated, tiled
        self._runtime = {"mode": "low_impact", "backend": "pending", "warning": None}
        self._fallback_reason = None
        self.job_name = None
        self.profile = resource_profile()
        self._memory_preference = memory_saving
        self._saving = bool(memory_saving)
        self._forced_saving = False
        self._memory = {}

    def configure(self, profile):
        # SessionStore calls this only between images; direct callers are serialized too.
        with self._serial:
            if self.profile == profile:
                return
            self.cancel()
            with self._lifecycle:
                self.profile = profile
                self._runtime = {"mode": profile.mode, "backend": "pending", "warning": None}

    def runtime_status(self):
        with self._lifecycle:
            return {
                **self._runtime,
                "fallback_reason": self._fallback_reason,
                "memory": {**self._memory, "mode": "saving" if self._saving else "normal"},
            }

    @staticmethod
    def _stop(process, connection):
        if connection is not None:
            connection.close()
        if process is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join(timeout=10)
            if process.is_alive():
                # Windows can finish asynchronous native teardown after the
                # termination request. Keep its handle until exit, rather than
                # raising from close() and skipping all session cleanup.
                def reap():
                    process.join()
                    process.close()

                threading.Thread(target=reap, name="local-worker-exit", daemon=True).start()
            else:
                process.close()

    def _worker(self):
        with self._lifecycle:
            if self._closed:
                raise RuntimeError("本地识别会话已结束")
            if self._process is None or not self._process.is_alive():
                old_process, old_connection = self._process, self._connection
                self._process = self._connection = None
                self._stop(old_process, old_connection)
                context = multiprocessing.get_context("spawn")
                parent, child = context.Pipe(duplex=True)
                process = context.Process(
                    target=_worker_main,
                    args=(
                        child,
                        self._factory,
                        self._accelerated,
                        self._tiled,
                        self.job_name,
                        self.profile,
                        self._saving,
                    ),
                    name="local-ocr",
                    daemon=True,
                )
                try:
                    process.start()
                except Exception:
                    parent.close()
                    process.close()
                    raise
                finally:
                    child.close()
                self._connection, self._process = parent, process
            return self._process, self._connection

    def _discard(self, process, connection):
        with self._lifecycle:
            if self._process is not process:
                return
            self._process = self._connection = None
            self._runtime = {**self._runtime, "backend": "pending"}
        self._stop(process, connection)

    def analyze(self, source, on_progress=None):
        with self._serial:
            current = snapshot()
            saving = self._saving
            if self._memory_preference is None:
                if current.saving:
                    saving = True
                elif current.available >= 10 * GIB and not self._forced_saving:
                    saving = False
            if saving != self._saving:
                self.cancel()
                self._saving = saving
            memory_retried = False
            while True:
                try:
                    return self._analyze_once(source, on_progress)
                except AccelerationCompatibilityError:
                    if not self._accelerated:
                        raise
                    # Compatibility is not a memory optimization. The fresh
                    # process retains memory mode, CPU budget and monitoring.
                    self._accelerated = False
                    with self._lifecycle:
                        self._fallback_reason = "CPU 加速不兼容，已切换兼容模式"
                        self._runtime = {**self._runtime, "backend": "compatible"}
                except ResourceUnavailable as exc:
                    if not is_memory_failure(exc) or self._saving or memory_retried:
                        raise
                    memory_retried = True
                    self._saving = self._forced_saving = True

    def _analyze_once(self, source, on_progress):
        process, connection = self._worker()
        stage, checked_at = "loading", 0.0
        try:
            connection.send(source)
            while True:
                if time.monotonic() - checked_at >= 0.5:
                    checked_at = time.monotonic()
                    current = snapshot()
                    if current.available < RESERVE or (
                        current.commit_available is not None and current.commit_available < RESERVE
                    ):
                        raise memory_error(stage, code="memory_pressure", measured=current)
                if not connection.poll(0.5):
                    if not process.is_alive():
                        raise RuntimeError("本地识别进程已退出，请重新尝试")
                    continue
                kind, payload = connection.recv()
                if kind == "progress":
                    stage = payload.stage
                    if on_progress:
                        on_progress(payload)
                elif kind == "result":
                    return payload
                elif kind == "runtime":
                    with self._lifecycle:
                        self._runtime = payload
                elif kind == "memory":
                    stage = payload["stage"]
                    with self._lifecycle:
                        self._memory = payload
                elif kind == "compatibility_error":
                    raise AccelerationCompatibilityError("本地加速兼容性错误")
                elif kind == "error":
                    raise RuntimeError(payload)
                elif kind == "resource_error":
                    raise ResourceUnavailable(payload["message"], issue=payload["issue"])
                else:
                    raise RuntimeError("本地识别进程返回了无效状态")
        except Exception:
            self._discard(process, connection)
            raise

    def close(self):
        with self._lifecycle:
            self._closed = True
            process, connection = self._process, self._connection
            self._process = self._connection = None
        self._stop(process, connection)

    def cancel(self):
        with self._lifecycle:
            process, connection = self._process, self._connection
            self._process = self._connection = None
        self._stop(process, connection)
