from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from .config import get_settings
from .db import init_db
from .service import latest_signals, scan_watchlist


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Stock Guard MVP",
    version="0.1.0",
    description="A-share research, screening and alerting system. Live orders disabled by default.",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "env": settings.app_env,
        "live_order_enabled": settings.enable_live_order,
        "manual_confirm_required": settings.manual_confirm_required,
    }


@app.post("/api/scan")
def scan() -> list[dict]:
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
