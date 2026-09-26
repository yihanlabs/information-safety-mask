from __future__ import annotations

import asyncio
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .inference import PROJECT_ROOT, model_status
from .models import ConfirmRequest, EditRequest, ExportRequest, PerformanceSettings, RuleSet
from .settings import SettingsRepository
from .state import Conflict, SessionStore
from .worker import ProcessEngine


def create_app(token: str, store: SessionStore | None = None, frontend: Path | None = None):
    store = store or SessionStore(ProcessEngine(), SettingsRepository())
    frontend = frontend or PROJECT_ROOT / "frontend" / "dist"

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            store.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    import_gate = asyncio.Lock()

    @app.middleware("http")
    async def protect(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host not in {"127.0.0.1", "localhost"}:
            return JSONResponse({"detail": "拒绝非本机访问"}, status_code=403)
        if request.url.path.startswith(("/api/", "/download/")):
            supplied = request.headers.get("x-session-token", "")
            if request.url.path.startswith("/api/") and not secrets.compare_digest(supplied, token):
                return JSONResponse({"detail": "会话已失效，请通过启动脚本重新打开工作台"}, status_code=401)
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse({"detail": "提交内容格式不正确，请检查字段与遮盖区域"}, status_code=422)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def bad_request(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "图片不存在或已从当前批次移除"}, status_code=404)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        return JSONResponse({"detail": "本地处理发生错误，请重试；原图不会被导出"}, status_code=500)

    @app.get("/api/health")
    def health():
        return {
            "models": model_status(),
            "settings_error": store.settings_error,
            "local_only": True,
            "runtime": store.runtime_status(),
            "memory": store.memory_status(),
        }

    @app.get("/api/settings")
    def settings():
        with store.lock:
            return store.rules.model_dump()

    @app.get("/api/performance")
    def performance():
        return store.runtime_status()

    @app.put("/api/performance")
    def save_performance(settings: PerformanceSettings):
        return store.set_performance(settings.mode)

    @app.put("/api/settings")
    def save_settings(rules: RuleSet):
        return store.update_rules(rules)

    @app.delete("/api/settings")
    def clear_settings():
        return store.update_rules(RuleSet(), reset=True)

    @app.get("/api/images")
    def images():
        return store.list()

    @app.post("/api/images", status_code=201)
    async def import_image(request: Request):
        async with import_gate:
            return await receive_image(request)

    async def receive_image(request: Request):
        from starlette.concurrency import run_in_threadpool

        group = uuid.uuid4().hex
        writer = store.cache.writer(group, memory_limit=min(8 * 1024 * 1024, store.memory_available()))
        name = unquote(request.headers.get("x-image-name", "图片")).replace("\\", "/").split("/")[-1]
        try:
            async for chunk in request.stream():
                await run_in_threadpool(writer.write, chunk)
            upload = writer.finish()
            return await run_in_threadpool(store.import_blob, upload, name, group)
        except BaseException:
            writer.abort()
            store.cache.clear_group(group)
            raise

    @app.get("/api/images/{image_id}/preview")
    def preview(image_id: str, size: int = Query(2048, ge=1, le=2048)):
        return Response(store.preview(image_id, size), media_type="image/png")

    @app.get("/api/images/{image_id}/region")
    def region(
        image_id: str,
        x: int = Query(ge=0),
        y: int = Query(ge=0),
        width: int = Query(gt=0),
        height: int = Query(gt=0),
        output_width: int = Query(gt=0, le=2048),
        output_height: int = Query(gt=0, le=2048),
    ):
        with store.lock:
            item = store.get(image_id)
            if x + width > item.width or y + height > item.height:
                raise ValueError("预览区域超出图片边界")
        return Response(
            store.preview(image_id, 2048, (x, y, width, height), (output_width, output_height)),
            media_type="image/png",
        )

    @app.post("/api/images/{image_id}/retry")
    def retry(image_id: str):
        return store.retry(image_id)

    @app.put("/api/images/{image_id}/edits")
    def edit(image_id: str, request: EditRequest):
        return store.edit(image_id, request.revision, request.edits)

    @app.post("/api/images/{image_id}/confirm")
    def confirm(image_id: str, request: ConfirmRequest):
        return store.confirm(image_id, request.revision)

    @app.delete("/api/images/{image_id}")
    def remove(image_id: str):
        store.clear(image_id)
        return {"ok": True}

    @app.delete("/api/images")
    def clear_images():
        store.clear()
        return {"ok": True}

    @app.post("/api/export")
    def export(request: ExportRequest):
        blob, _mime, name = store.export_file(request)
        return stream_blob(blob, name, store.pin_export(blob))

    def stream_blob(blob, name, job_id=None):
        def chunks():
            try:
                with blob.open() as stream:
                    while data := stream.read(1024 * 1024):
                        yield data
            finally:
                if job_id:
                    store.finish_download(job_id)

        return StreamingResponse(
            chunks(),
            media_type="image/png",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "Content-Length": str(blob.size),
            },
        )

    @app.post("/api/exports", status_code=202)
    def start_export(request: ExportRequest):
        return store.start_export(request)

    @app.get("/api/exports/{job_id}")
    def export_status(job_id: str):
        return store.export_status(job_id)

    @app.post("/api/exports/{job_id}/ticket")
    def download_ticket(job_id: str):
        return {"url": store.issue_ticket(job_id)}

    @app.get("/download/{ticket}")
    def download(ticket: str):
        job_id, blob, name = store.take_download(ticket)
        return stream_blob(blob, name, job_id)

    if (frontend / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

    @app.get("/")
    def index():
        if not (frontend / "index.html").is_file():
            return JSONResponse({"detail": "界面尚未构建，请先运行安装与准备.cmd"}, status_code=503)
        return FileResponse(frontend / "index.html")

    return app
