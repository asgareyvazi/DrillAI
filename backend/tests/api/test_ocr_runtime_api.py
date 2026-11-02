"""Readiness reports enabled-but-uncommissioned OCR rather than implying service readiness."""

from __future__ import annotations

import pytest

from drillai.core.config import Settings


@pytest.mark.asyncio
async def test_readiness_degrades_when_enabled_ocr_runtime_is_missing(client, monkeypatch) -> None:
    from drillai.api.routers import health as health_router
    from drillai.ingestion import ocr_runtime

    settings = Settings(_env_file=None, ocr_enabled=True)
    monkeypatch.setattr(health_router, "get_settings", lambda: settings)
    monkeypatch.setattr(
        ocr_runtime,
        "inspect_ocr_runtime",
        lambda _settings, *, probe_when_disabled: {
            "enabled": True,
            "status": "blocked",
            "runtime_status": "blocked",
            "runtime_ready": False,
            "required_ready": False,
            "quality_evaluated": False,
            "reason": "Tesseract executable was not found on the server PATH",
        },
    )

    response = await client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["ocr"]["status"] == "blocked"
    assert response.json()["checks"]["ocr"]["quality_evaluated"] is False


@pytest.mark.asyncio
async def test_readiness_keeps_disabled_ocr_optional_and_does_not_probe(client, monkeypatch) -> None:
    from drillai.api.routers import health as health_router
    from drillai.ingestion import ocr_runtime

    settings = Settings(_env_file=None, ocr_enabled=False)
    monkeypatch.setattr(health_router, "get_settings", lambda: settings)

    def disabled_without_probe(_settings, *, probe_when_disabled):
        assert probe_when_disabled is False
        return {
            "enabled": False,
            "status": "disabled",
            "runtime_status": "not_checked",
            "runtime_ready": None,
            "required_ready": True,
            "quality_evaluated": False,
        }

    monkeypatch.setattr(ocr_runtime, "inspect_ocr_runtime", disabled_without_probe)
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["checks"]["ocr"]["status"] == "disabled"
    assert response.json()["checks"]["ocr"]["runtime_status"] == "not_checked"
