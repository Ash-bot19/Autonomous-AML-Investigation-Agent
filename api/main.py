"""FastAPI application — AML Investigation Agent API (Phase 6)."""
from __future__ import annotations

import structlog
from dotenv import load_dotenv
from fastapi import FastAPI
from prometheus_client import make_asgi_app

load_dotenv()

from api.routes.investigate import router as investigate_router
from api.routes.report import router as report_router
from api.routes.status import router as status_router

log = structlog.get_logger()

app = FastAPI(
    title="AML Investigation Agent",
    description="Autonomous AML investigation agent API",
    version="0.6.0",
)

# Prometheus metrics endpoint
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

app.include_router(investigate_router)
app.include_router(status_router)
app.include_router(report_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
