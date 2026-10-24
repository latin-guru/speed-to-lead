from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import Settings
from app.db import db_ok

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz(request: Request) -> JSONResponse:
    ok = db_ok(request.app.state.engine)
    body = {"status": "ok" if ok else "error", "checks": {"db": "ok" if ok else "error"}}
    return JSONResponse(body, status_code=200 if ok else 503)


@router.get("/readyz")
def readyz(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    started = bool(getattr(request.app.state, "ready", False))
    database = db_ok(request.app.state.engine)
    warnings: list[str] = []
    if settings.meta_app_secret is None or settings.meta_verify_token is None:
        warnings.append("meta webhook not configured (META_APP_SECRET / META_VERIFY_TOKEN)")
    ready = started and database
    body = {
        "status": "ready" if ready else "not_ready",
        "checks": {"startup": started, "db": database},
        "ai_provider": settings.ai_provider,
        "warnings": warnings,
    }
    return JSONResponse(body, status_code=200 if ready else 503)
