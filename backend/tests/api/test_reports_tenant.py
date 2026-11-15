"""Tenant isolation for generic structured-report document exports."""

from __future__ import annotations

import pytest
from tests.fixtures.fabric import Fabric


@pytest.mark.asyncio
async def test_structured_report_binary_exports_do_not_cross_organizations(fabric: Fabric):
    alpha_url = f"/api/v1/wells/{fabric.alpha.well_id}/reports/daily_drilling/export"
    for export_format, expected_prefix in (("pdf", b"%PDF-"), ("xlsx", b"PK\x03\x04")):
        own = await fabric.http.get(
            alpha_url,
            params={"format": export_format, "locale": "fa"},
            headers=fabric.alpha.headers,
        )
        assert own.status_code == 200, own.text
        assert own.content.startswith(expected_prefix)
        assert own.headers["content-disposition"].endswith(f'.{export_format}"')

        foreign = await fabric.http.get(
            alpha_url,
            params={"format": export_format, "locale": "fa"},
            headers=fabric.bravo.headers,
        )
        assert foreign.status_code == 404, foreign.text
        assert "ALPHA-1" not in foreign.text
        assert "well not found" in foreign.text.lower()

    audit = await fabric.http.get(
        f"/api/v1/wells/{fabric.alpha.well_id}/audit-log",
        headers=fabric.alpha.headers,
    )
    assert audit.status_code == 200, audit.text
    exports = [item for item in audit.json()["items"] if item["action"] == "report.export"]
    assert len(exports) == 2
    assert {item["details"]["format"] for item in exports} == {"pdf", "xlsx"}
    assert all(item["permission_decision"]["granted"] is True for item in exports)
