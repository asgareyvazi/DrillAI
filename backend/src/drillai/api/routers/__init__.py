"""HTTP routers, one module per bounded area."""

from __future__ import annotations

from drillai.api.routers import (
    assets,
    context,
    documents,
    drilling,
    evidence,
    health,
    operations,
    platform,
    registry,
    runs,
    twin,
    workflows,
)

__all__ = [
    "assets",
    "context",
    "documents",
    "drilling",
    "evidence",
    "health",
    "operations",
    "platform",
    "registry",
    "runs",
    "twin",
    "workflows",
]
