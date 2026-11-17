"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy import text

from . import __version__
from .config import get_settings
from .db import session_scope, upgrade_db
from .jobs import Worker
from .security import configure_logging, redact

log = logging.getLogger("kaizeval.api")
WEBUI = Path(__file__).parent / "webui"

NO_UI = """<!doctype html><meta charset="utf-8"><title>Kaizeval</title>
<body style="font-family:system-ui;max-width:640px;margin:10vh auto;padding:0 16px;line-height:1.5">
<h1>Kaizeval API is running</h1>
<p>The web console has not been built in this checkout. Build it once with:</p>
<pre>cd web &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>Released wheels already include the console. The OpenAPI document is at
<a href="/api/openapi.json">/api/openapi.json</a>.</p>
</body>"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging()
    upgrade_db()
    from . import handlers  # noqa: F401  (registers job handlers)

    worker = None
    if settings.worker_mode == "inline":
        worker = Worker()
        worker.start()
    log.info("kaizeval %s started (offline=%s, worker=%s)", __version__, settings.offline, settings.worker_mode)
    yield
    if worker:
        worker.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Kaizeval API", version=__version__, lifespan=lifespan,
        description="Evaluate, diagnose and safely optimize AI agents. All routes are under /api/v1.",
        docs_url=None, redoc_url=None, openapi_url="/api/openapi.json",
    )
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - last-resort boundary
            log.exception("unhandled error request_id=%s path=%s", request_id, request.url.path)
            response = JSONResponse(status_code=500, content={"error": {
                "code": "INTERNAL_ERROR", "message": "unexpected server error", "request_id": request_id}})
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            # Paths only: request and response bodies are never logged.
            log.info("request_id=%s method=%s path=%s status=%s ms=%d", request_id, request.method,
                     request.url.path, response.status_code, int((time.perf_counter() - started) * 1000))
        return response

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        detail: Any = exc.detail
        if not isinstance(detail, dict):
            detail = {"code": "ERROR", "message": str(detail)}
        detail["message"] = redact(str(detail.get("message", "")))
        return JSONResponse(status_code=exc.status_code, content={"error": detail})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        issues = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"error": {
            "code": "VALIDATION_ERROR", "message": "the request is not valid", "issues": issues}})

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        try:
            with session_scope() as db:
                db.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=503, content={"status": "database unavailable"})
        return JSONResponse(content={"status": "ready", "worker_mode": settings.worker_mode, "offline": settings.offline})

    from .api import auth, knowledge, runs, setup

    for module in (auth, setup, knowledge, runs):
        app.include_router(module.router, prefix="/api/v1")

    index = WEBUI / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    def console(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown API route"})
        if not index.exists():
            return HTMLResponse(NO_UI)
        target = (WEBUI / path).resolve()
        if path and WEBUI.resolve() in target.parents and target.is_file():
            return FileResponse(target)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app
