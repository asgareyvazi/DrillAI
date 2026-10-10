"""Health, version and metrics.

Readiness is honest: the endpoint reports what is *actually* reachable (database ping, catalogue
counts, whether an LLM provider is configured) instead of a hard-coded ``{"status": "ok"}``.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from drillai.api.deps import current_auth, get_database
from drillai.api.version import API_VERSION
from drillai.core.config import get_settings
from drillai.db.session import Database
from drillai.observability.tracing import get_metrics

router = APIRouter(tags=["platform"])


@router.get("/health", summary="Liveness")
async def health(request: Request) -> dict[str, Any]:
    settings = get_settings()
    catalogues = getattr(request.app.state, "catalogues", {})
    started_at = getattr(request.app.state, "started_at", None)
    return {
        "status": "ok",
        "api_version": API_VERSION,
        "environment": settings.environment,
        "uptime_seconds": round(time.time() - started_at, 2) if started_at else 0.0,
        "catalogues": catalogues,
    }


@router.get("/health/ready", summary="Readiness (checks dependencies)")
async def readiness(
    database: Annotated[Database, Depends(get_database)],
    _: Annotated[Any, Depends(current_auth)],
) -> dict[str, Any]:
    settings = get_settings()
    checks: dict[str, Any] = {}
    healthy = True
    try:
        checks["database"] = await database.healthcheck()
    except Exception as exc:  # readiness must report the failure, not raise it
        healthy = False
        checks["database"] = {"status": "error", "detail": str(exc)}
    checks["llm_provider"] = {
        "provider": settings.llm_provider,
        "configured": settings.llm_provider != "stub",
    }
    checks["vector_backend"] = settings.vector_backend
    checks["blob_backend"] = settings.blob_backend
    return {"status": "ok" if healthy else "degraded", "checks": checks}


@router.get("/version", summary="API and platform version")
async def version(request: Request) -> dict[str, Any]:
    settings = get_settings()
    return {
        "api_version": API_VERSION,
        "app_name": settings.app_name,
        "environment": settings.environment,
        "catalogues": getattr(request.app.state, "catalogues", {}),
    }


@router.get("/metrics", summary="In-process metrics snapshot (counters and histograms)")
async def metrics(_: Annotated[Any, Depends(current_auth)]) -> dict[str, Any]:
    return get_metrics().snapshot()
