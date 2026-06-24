from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import get_settings
from .api.auth import router as auth_router
from .api.market import router as market_router
from .api.paper import router as paper_router
from .api.v1 import router as v1_router
from .db import engine
from .risk_service import latest_order_proposals, latest_risk_decisions
from .schema import assert_schema_ready_for_writes, validate_schema_against_metadata
from .service import latest_signals, scan_watchlist

logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield


app = FastAPI(
    title="Stock Guard MVP",
    version=settings.app_version,
    description="A-share research, screening and alerting system. Live orders disabled by default.",
    lifespan=lifespan,
    docs_url=None if settings.app_env.lower() == "prod" else "/docs",
    redoc_url=None if settings.app_env.lower() == "prod" else "/redoc",
    openapi_url=None if settings.app_env.lower() == "prod" else "/openapi.json",
)
app.include_router(paper_router)
app.include_router(auth_router)
app.include_router(market_router)
app.include_router(v1_router)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


def _error_payload(request: Request, *, code: str, message: str, details: dict | list | None = None) -> dict:
    return {
        "success": False,
        "error": {"code": code, "message": message, "details": details or {}},
        "request_id": getattr(request.state, "request_id", ""),
        "environment": "PAPER_TRADING",
    }


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if not request.url.path.startswith("/api/v1"):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
    code = str(exc.detail) if isinstance(exc.detail, str) and exc.detail.isupper() else "HTTP_ERROR"
    message = str(exc.detail)
    return JSONResponse(status_code=exc.status_code, content=_error_payload(request, code=code, message=message))


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    if not request.url.path.startswith("/api/v1"):
        return JSONResponse(status_code=422, content={"detail": exc.errors()})
    return JSONResponse(
        status_code=422,
        content=_error_payload(request, code="VALIDATION_ERROR", message="invalid request", details={"errors": exc.errors()}),
    )


@app.exception_handler(Exception)
async def unexpected_exception_handler(request: Request, exc: Exception):
    logger.exception("unhandled request error", extra={"request_id": getattr(request.state, "request_id", ""), "path": request.url.path})
    if not request.url.path.startswith("/api/v1"):
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
    return JSONResponse(status_code=500, content=_error_payload(request, code="INTERNAL_ERROR", message="internal server error"))


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    schema_report = validate_schema_against_metadata(engine)
    return {
        "status": "MIGRATION_REQUIRED" if schema_report.migration_required or schema_report.recommended_action not in {"none"} else "ok",
        "env": settings.app_env,
        "live_order_enabled": settings.enable_live_order,
        "manual_confirm_required": settings.manual_confirm_required,
        "schema": {
            "current_revision": schema_report.current_revision,
            "head_revision": schema_report.target_revision,
            "migration_required": schema_report.migration_required,
            "recommended_action": schema_report.recommended_action,
        },
    }


@app.get("/health/live")
def health_live() -> dict:
    return {"status": "ok"}


@app.post("/api/scan")
def scan() -> list[dict]:
    assert_schema_ready_for_writes(engine)
    settings = get_settings()
    if settings.enable_live_order:
        raise HTTPException(
            status_code=403,
            detail="This MVP does not permit live order execution. Use alerts/paper trading only.",
        )
    return scan_watchlist()


@app.get("/api/signals")
def signals(limit: int = 50) -> list[dict]:
    return latest_signals(min(max(limit, 1), 500))


@app.get("/api/risk-decisions")
def risk_decisions(limit: int = 50) -> list[dict]:
    return latest_risk_decisions(min(max(limit, 1), 500))


@app.get("/api/order-proposals")
def order_proposals(limit: int = 50) -> list[dict]:
    return latest_order_proposals(min(max(limit, 1), 500))
