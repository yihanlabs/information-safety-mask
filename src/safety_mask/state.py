from __future__ import annotations

import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .cache import MEMORY_BUDGET, Blob, ResourceUnavailable, SessionCache
from .detection import detect, normalized
from .image_worker import ImageWorker
from .imaging import validate_rect
from .memory import GIB, is_memory_failure, memory_error, require_memory, snapshot
from .models import EditState, Entity, ExportRequest, Mask, OcrLine, RuleSet
from .progress import ProcessingProgress, ProgressEvent, TimingEstimates
from .raster import Raster
from .resources import CpuBudget, resource_profile
from .settings import PerformanceRepository, SettingsRepository


class Conflict(ValueError):
    pass


@dataclass
class ImageRecord:
    id: str
    name: str
    number: int
    source: Raster | None
    width: int
    height: int
    upload: Blob | None = None
    status: str = "queued"
    message: str = "等待识别"
    revision: int = 1
    confirmed_revision: int | None = None
    lines: list[OcrLine] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    automatic: list[Mask] = field(default_factory=list)
    edits: EditState = field(default_factory=EditState)
    progress: ProcessingProgress | None = None
    resource_issue: dict | None = None

    def masks(self) -> list[Mask]:
        result = []
        for automatic in self.automatic:
            if automatic.id in self.edits.excluded:
                continue
            mask = automatic.model_copy(deep=True)
            if mask.id in self.edits.overrides:
                mask.box = self.edits.overrides[mask.id]
            result.append(mask)
        for manual in self.edits.manual:
            result.append(Mask(id=manual.id, box=manual.box, source="manual", reasons=["手动遮盖"]))
        return result

    def view(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "number": self.number,
            "width": self.width,
            "height": self.height,
            "status": self.status,
            "message": self.message,
            "revision": self.revision,
            "confirmed": self.confirmed_revision == self.revision,
            "masks": [mask.model_dump() for mask in self.masks()],
            "automatic_count": len(self.automatic),
            "excluded_count": len(self.edits.excluded),
            "edits": self.edits.model_dump(),
            "line_count": len(self.lines),
            "progress": self.progress.view() if self.progress else None,
            "preview_ready": self.source is not None,
            "cache": "encrypted" if self.source and self.source.blob.path else "memory",
            "resource_issue": self.resource_issue,
        }

    def change(self):
        self.revision += 1
        self.confirmed_revision = None


class SessionStore:
    def __init__(self, engine, repository: SettingsRepository, performance_repository=None, *, cache=None):
        self.engine = engine
        self.repository = repository
        self.settings_error = None
        try:
            self.rules = repository.load()
        except RuntimeError as exc:
            self.rules = RuleSet()
            self.settings_error = str(exc)
        self.images: dict[str, ImageRecord] = {}
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-ocr")
        self.sequence = 0
        self.timings = TimingEstimates()
        self.cache = cache or SessionCache()
        preference_path = (
            repository.path.with_name("performance.json") if hasattr(repository, "path") else None
        )
        self.performance_repository = performance_repository or PerformanceRepository(preference_path)
        self.performance_error = None
        try:
            self.requested_mode = self.performance_repository.load()
        except RuntimeError as exc:
            self.requested_mode, self.performance_error = "low_impact", str(exc)
        self.budget = CpuBudget()
        self.active_mode = self.budget.set_mode(self.requested_mode)
        self.performance_pending = False
        job_name = self.budget.name
        if hasattr(engine, "job_name"):
            engine.job_name = job_name
        if hasattr(engine, "configure"):
            engine.configure(resource_profile(self.active_mode))
        self.image_worker = ImageWorker(self.cache.root, job_name)
        self.preview_worker = ImageWorker(self.cache.root, job_name)
        self.export_worker = ImageWorker(self.cache.root, job_name)
        self.export_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-export")
        self.export_jobs = {}
        self.tickets = {}
        self.processing_id = self.exporting_id = None
        self.closed = False
        self.memory_reserved = 0
        self.memory_paused_id = None
        self._draining = False

    def memory_status(self):
        with self.lock:
            runtime = self.engine.runtime_status() if hasattr(self.engine, "runtime_status") else {}
            current = snapshot()
            mode = runtime.get("memory", {}).get("mode", "saving" if current.saving else "normal")
            return {
                **runtime.get("memory", {}),
                **current.view(),
                "mode": mode,
                "waiting_image_id": self.memory_paused_id,
                "processing": self.processing_id is not None,
                "message": "已启用省内存处理，可能需要更长时间" if mode == "saving" else None,
            }

    def _schedule(self):
        # Called under lock. A single drain avoids blocked futures preventing a
        # paused image from being retried ahead of the remaining queue.
        if not self.closed and not self._draining and not self.memory_paused_id:
            self._draining = True
            self.executor.submit(self._drain)

    def _drain(self):
        while True:
            with self.lock:
                key = next((image.id for image in self.images.values() if image.status == "queued"), None)
                if self.closed or self.memory_paused_id or key is None:
                    self._draining = False
                    return
            self._process(key)

    def get(self, image_id: str) -> ImageRecord:
        if image_id not in self.images:
            raise KeyError("图片已移除，请刷新列表")
        return self.images[image_id]

    def runtime_status(self):
        with self.lock:
            model = self.engine.runtime_status() if hasattr(self.engine, "runtime_status") else {}
            budget_status = self.budget.view()
            if model.get("cpu_cap_applied") is False:
                budget_status["cpu_cap_applied"] = False
            warnings = [self.performance_error, self.budget.warning, model.get("warning")]
            return {
                "backend": "pending",
                **model,
                **budget_status,
                "mode": self.active_mode,
                "requested_mode": self.requested_mode,
                "active_mode": self.active_mode,
                "pending": self.performance_pending,
                "target_threads": resource_profile(self.active_mode).threads,
                "warning": "；".join(dict.fromkeys(x for x in warnings if x)) or None,
            }

    def set_performance(self, mode):
        resource_profile(mode)  # Validate even for non-HTTP callers.
        with self.lock:
            self.performance_repository.save(mode)
            self.requested_mode, self.performance_error = mode, None
            self.performance_pending = mode != self.active_mode
            if self.processing_id is None:
                self._apply_performance()
            return self.runtime_status()

    def _apply_performance(self):
        # Called under the session lock, between whole images (including preparation).
        if not self.performance_pending or self.closed:
            return
        self.active_mode = self.budget.set_mode(self.requested_mode)
        if hasattr(self.engine, "configure"):
            self.engine.configure(resource_profile(self.active_mode))
        self.performance_pending = False

    def list(self) -> list[dict]:
        with self.lock:
            views, position = [], 0
            for image in self.images.values():
                view = image.view()
                if image.status == "queued":
                    position += 1
                    view["queue_position"] = position
                views.append(view)
            return views

    def import_image(self, data: bytes, name: str) -> dict:
        group = uuid.uuid4().hex
        writer = self.cache.writer(group, memory_limit=min(8 * 1024 * 1024, self.memory_available()))
        try:
            writer.write(data)
            return self.import_blob(writer.finish(), name, group)
        except Exception:
            writer.abort()
            self.cache.clear_group(group)
            raise

    def memory_available(self):
        with self.lock:
            if snapshot().saving:
                return 0
            used = sum(
                blob.size
                for image in self.images.values()
                for blob in (image.upload, image.source.blob if image.source else None)
                if blob and blob.data is not None
            )
            return max(0, MEMORY_BUDGET - used - self.memory_reserved)

    def import_blob(self, upload: Blob, name: str, group: str):
        width, height, orientation, _, _ = self.preview_worker.call("info", upload)
        if orientation >= 5:
            width, height = height, width
        with self.lock:
            if self.closed:
                raise ValueError("图片会话已结束")
            if len(self.images) >= 100:
                raise ValueError("当前批次已达到 100 张，请导出并移除部分图片后重试")
            self.sequence += 1
            image = ImageRecord(
                id=group,
                name=name[:240],
                number=self.sequence,
                source=None,
                upload=upload,
                width=width,
                height=height,
                progress=ProcessingProgress(self.timings),
            )
            self.images[image.id] = image
            self._schedule()
            return image.view()

    def _process(self, image_id):
        with self.lock:
            if image_id not in self.images:
                return
            image = self.images[image_id]
            self.processing_id = image_id
            image.status, image.message = "processing", "正在本地识别文字与敏感内容"
            image.resource_issue = None
            image.confirmed_revision = None
            source = image.source

        def report(event):
            with self.lock:
                if image_id in self.images:
                    image.progress.update(event)
                    image.message = image.progress.view()["label"]

        try:
            if source is None:
                with self.lock:
                    needed = image.width * image.height * 3
                    allowance = needed if needed <= min(90_000_000, self.memory_available()) else 0
                    self.memory_reserved = allowance
                try:
                    source = self.image_worker.call(
                        "prepare", image.upload, image_id, allowance, report=report
                    )
                finally:
                    with self.lock:
                        self.memory_reserved = 0
                with self.lock:
                    if image_id not in self.images:
                        source.blob.remove()
                        return
                    image.source = source
                    image.upload.remove()
                    image.upload = None
            report(ProgressEvent("loading", total=1))
            lines, entities = self.engine.analyze(source, on_progress=report)
            report(ProgressEvent("finishing", total=1))
            with self.lock:
                if image_id not in self.images:
                    return
                image.lines, image.entities = lines, entities
                image.automatic = detect(lines, entities, self.rules, image.width, image.height)
                image.status = "review"
                uncertain = any(line.confidence < 0.8 for line in lines)
                fallback = any(mask.fallback for mask in image.automatic)
                if not lines:
                    image.message = "未识别到文字，请对照原图检查并按需手动补框"
                elif uncertain or fallback:
                    image.message = "含不确定识别或整行遮盖，请检查并调整"
                else:
                    image.message = "识别完成，请检查遮盖后确认"
                image.change()
                image.progress.finish()
        except Exception as exc:  # noqa: BLE001 - isolate a failed worker without exposing input
            if is_memory_failure(exc) and not isinstance(exc, ResourceUnavailable):
                exc = memory_error(image.progress.event.stage if image.progress.event else "loading")
            # Never return third-party exception details: they may include OCR input.
            safe = (
                str(exc)
                if isinstance(exc, ResourceUnavailable)
                or (isinstance(exc, RuntimeError) and str(exc).startswith("本地模型未准备"))
                else "自动识别未完成，请检查本地模型；可手动遮盖并确认"
            )
            with self.lock:
                if image_id in self.images:
                    waiting = is_memory_failure(exc)
                    image.status, image.message = "waiting_memory" if waiting else "error", safe
                    image.resource_issue = exc.issue if isinstance(exc, ResourceUnavailable) else None
                    if waiting:
                        self.memory_paused_id = image_id
                    image.progress.finish(failed=True)
                    if not waiting:
                        image.change()
        finally:
            with self.lock:
                if self.processing_id == image_id:
                    self.processing_id = None
                    self._apply_performance()
                removed = image_id not in self.images
            if removed:
                try:
                    self.cache.clear_group(image_id)
                except OSError:
                    pass

    def retry(self, image_id):
        with self.lock:
            image = self.get(image_id)
            if image.status not in {"error", "waiting_memory"}:
                raise Conflict("当前图片无需重试")
            if image.status == "waiting_memory":
                try:
                    require_memory(2 * GIB if image.source else 0, "loading" if image.source else "preparing")
                except ResourceUnavailable as exc:
                    image.message, image.resource_issue = str(exc), exc.issue
                    return image.view()
                self.memory_paused_id = None
            image.status, image.message = "queued", "等待重新处理"
            image.resource_issue = None
            image.progress = ProcessingProgress(self.timings)
            image.confirmed_revision = None
            self._schedule()
            return image.view()

    def preview(self, image_id, max_side=2048, box=None, output_size=None):
        with self.lock:
            image = self.get(image_id)
            if image.source is None:
                raise Conflict("正在准备图片，请稍候")
            source = image.source
        return self.preview_worker.call("preview", source, max_side, box, output_size)

    def update_rules(self, rules: RuleSet, reset=False):
        with self.lock:
            if reset:
                self.repository.clear()
            self.repository.save(rules)
            self.settings_error = None

            def active_policy(value):
                return (
                    sorted(value.categories),
                    sorted({normalized(field.value)[0] for field in value.custom_fields if field.enabled}),
                )

            policy_changed = active_policy(self.rules) != active_policy(rules)
            self.rules = rules.model_copy(deep=True)
            for image in self.images.values():
                previous = [m.model_dump() for m in image.masks()]
                image.automatic = detect(image.lines, image.entities, self.rules, image.width, image.height)
                current = [m.model_dump() for m in image.masks()]
                if policy_changed or previous != current:
                    # A changed privacy policy requires review even when OCR found no new match.
                    image.change()
            return self.rules.model_dump()

    def edit(self, image_id: str, revision: int, edits: EditState):
        with self.lock:
            image = self.get(image_id)
            if revision != image.revision:
                raise Conflict("图片已更新，请重新检查后操作")
            if image.source is None or image.status in {"queued", "processing"}:
                raise Conflict("请等待本地识别完成后编辑")
            for item in edits.manual:
                validate_rect(item.box, image.width, image.height)
            for rect in edits.overrides.values():
                validate_rect(rect, image.width, image.height)
            if edits != image.edits:
                image.edits = edits.model_copy(deep=True)
                image.change()
            return image.view()

    def confirm(self, image_id: str, revision: int):
        with self.lock:
            image = self.get(image_id)
            if self.settings_error:
                raise Conflict("请先处理规则读取错误")
            if image.source is None or revision != image.revision or image.status in {"queued", "processing"}:
                raise Conflict("图片已更新或尚未处理完成，请重新检查")
            image.confirmed_revision = image.revision
            if image.status == "waiting_memory":
                image.status, image.message = "review", "已手动检查并确认"
                image.resource_issue = None
                self.memory_paused_id = None
                self._schedule()
            return image.view()

    def _snapshot(self, request: ExportRequest):
        with self.lock:
            if len({item.id for item in request.images}) != len(request.images):
                raise ValueError("导出图片重复")
            if request.format == "png" and len(request.images) != 1:
                raise ValueError("单张 PNG 导出只能选择一张图片")
            snapshots = []
            for item in request.images:
                image = self.get(item.id)
                if (
                    image.source is None
                    or item.revision != image.revision
                    or image.confirmed_revision != image.revision
                ):
                    raise Conflict("存在尚未确认或确认已失效的图片，请重新检查")
                snapshots.append(
                    (image.number, image.source, [m.model_copy(deep=True) for m in image.masks()])
                )
        return snapshots[0]

    def start_export(self, request):
        number, source, masks = self._snapshot(request)
        with self.lock:
            self._expire_exports()
            if len(self.export_jobs) >= 100:
                raise Conflict("下载任务较多，请稍候再试")
            job_id = secrets.token_hex(16)
            job = {
                "id": job_id,
                "request": request.model_copy(deep=True),
                "status": "queued",
                "completed": 0,
                "total": None,
                "label": "等待生成 PNG",
                "name": f"sanitized_{number:03d}.png",
                "blob": None,
                "expires": None,
                "error": None,
            }
            self.export_jobs[job_id] = job
            future = self.export_executor.submit(self._generate_export, job, source, masks)
            job["future"] = future
            return self.export_status(job_id)

    def _generate_export(self, job, source, masks):
        image_id = job["request"].images[0].id

        def report(progress):
            with self.lock:
                job.update(progress)

        try:
            with self.lock:
                self._snapshot(job["request"])
                self.exporting_id = image_id
                job["status"] = "generating"
            result = self.export_worker.call("export", source, masks, image_id, report=report)
            with self.lock:
                try:
                    self._snapshot(job["request"])
                except (KeyError, Conflict):
                    result.remove()
                    raise Conflict("图片已修改或移除，请重新确认后下载") from None
                job.update(status="ready", blob=result, label="PNG 已生成", expires=time.monotonic() + 300)
        except Exception as exc:  # noqa: BLE001 - sanitize export errors without exposing image data
            with self.lock:
                job.update(
                    status="failed",
                    error=str(exc)
                    if isinstance(exc, (Conflict, ResourceUnavailable))
                    else "PNG 生成未完成，请重试",
                    expires=time.monotonic() + 300,
                )
        finally:
            with self.lock:
                self.exporting_id = None

    def export_status(self, job_id):
        with self.lock:
            self._expire_exports()
            job = self.export_jobs[job_id]
            return {k: job[k] for k in ("id", "status", "completed", "total", "label", "name", "error")}

    def _expire_exports(self):
        now = time.monotonic()
        for key, job in list(self.export_jobs.items()):
            if job["expires"] and job["expires"] < now:
                if job["blob"]:
                    job["blob"].remove()
                del self.export_jobs[key]
        for ticket, (_, expires) in list(self.tickets.items()):
            if expires < now:
                del self.tickets[ticket]

    def issue_ticket(self, job_id):
        with self.lock:
            self._expire_exports()
            job = self.export_jobs[job_id]
            self._snapshot(job["request"])
            if job["status"] != "ready":
                raise Conflict("图片尚未生成，请稍候")
            if job.get("downloading"):
                raise Conflict("此文件正在提交浏览器，请稍候")
            ticket = secrets.token_urlsafe(32)
            self.tickets[ticket] = (job_id, time.monotonic() + 120)
            return "/download/" + ticket

    def take_download(self, ticket):
        with self.lock:
            self._expire_exports()
            job_id, _ = self.tickets.pop(ticket)
            job = self.export_jobs[job_id]
            self._snapshot(job["request"])
            if job.get("downloading"):
                raise Conflict("此文件正在提交浏览器，请稍候")
            job["downloading"] = True
            job["expires"] = None
            return job_id, job["blob"], job["name"]

    def pin_export(self, blob):
        # Legacy streaming downloads also need a lease: large transfers can
        # outlive the ready-result TTL and must not lose their ciphertext midway.
        with self.lock:
            for job_id, job in self.export_jobs.items():
                if job["blob"] is blob:
                    self._snapshot(job["request"])
                    job.update(downloading=True, expires=None)
                    return job_id
            raise Conflict("下载任务已过期，请重新生成")

    def finish_download(self, job_id):
        with self.lock:
            job = self.export_jobs.get(job_id)
            if job:
                job["downloading"] = False
                job["expires"] = time.monotonic() + 30
                image_id = job["request"].images[0].id
                if image_id not in self.images:
                    job["blob"].remove()
                    try:
                        self.cache.clear_group(image_id)
                    except OSError:
                        pass

    def export_file(self, request):
        job_id = self.start_export(request)["id"]
        with self.lock:
            job = self.export_jobs[job_id]
        job["future"].result()
        with self.lock:
            self._snapshot(request)
            if job["status"] != "ready":
                raise ValueError(job["error"])
            return job["blob"], "image/png", job["name"]

    def export(self, request):
        blob, mime, name = self.export_file(request)
        with blob.open() as stream:
            return stream.read(), mime, name

    def clear(self, image_id=None):
        with self.lock:
            ids = [image_id] if image_id else list(self.images)
            for key in ids:
                self.images.pop(key, None)
            if self.memory_paused_id in ids:
                self.memory_paused_id = None
            stop_processing = self.processing_id in ids
            stop_export = self.exporting_id in ids
        if stop_processing:
            self.image_worker.cancel()
            if hasattr(self.engine, "cancel"):
                self.engine.cancel()
        if stop_export:
            self.export_worker.cancel()
        # Close any region reader before removing its ciphertext on Windows.
        self.preview_worker.cancel()
        for key in ids:
            try:
                self.cache.clear_group(key)
            except OSError:
                pass  # A finishing download owns its reader; session cleanup retries after close.
        with self.lock:
            self._schedule()

    def close(self):
        with self.lock:
            self.closed = True
        self.clear()
        self.executor.shutdown(wait=False, cancel_futures=True)
        if hasattr(self.engine, "close"):
            self.engine.close()
        self.image_worker.close()
        self.preview_worker.close()
        self.export_worker.close()
        self.export_executor.shutdown(wait=False, cancel_futures=True)
        self.cache.close()
        self.budget.close()
