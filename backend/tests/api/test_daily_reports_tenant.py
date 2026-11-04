"""Tenant isolation for the report aggregate and its source references."""

from __future__ import annotations

import pytest
from tests.fixtures.fabric import Fabric


@pytest.mark.asyncio
async def test_daily_report_reads_writes_and_sources_are_organization_scoped(fabric: Fabric):
    payload = {
        "wellbore_id": fabric.alpha.wellbore_id,
        "report_date": "2026-05-02",
        "report_timezone": "America/Denver",
        "shift_code": "night",
        "content": {"summary": "A tenant-scoped manual report."},
    }
    created = await fabric.http.post(
        f"/api/v1/wells/{fabric.alpha.well_id}/daily-reports",
        json=payload,
        headers=fabric.alpha.headers,
    )
    assert created.status_code == 201, created.text
    report_id = created.json()["report"]["id"]

    # A foreign path well is not disclosed, even if the report id is valid in the other tenant.
    foreign_path = await fabric.http.get(
        f"/api/v1/wells/{fabric.alpha.well_id}/daily-reports/{report_id}",
        headers=fabric.bravo.headers,
    )
    assert foreign_path.status_code == 404
    assert report_id not in foreign_path.text

    # A caller cannot substitute its own well while guessing a report id from another organization.
    foreign_report = await fabric.http.get(
        f"/api/v1/wells/{fabric.bravo.well_id}/daily-reports/{report_id}",
        headers=fabric.bravo.headers,
    )
    assert foreign_report.status_code == 404

    foreign_metrics = await fabric.http.get(
        f"/api/v1/wells/{fabric.alpha.well_id}/daily-reports/{report_id}/revisions/1/activity-metrics",
        headers=fabric.bravo.headers,
    )
    assert foreign_metrics.status_code == 404
    assert report_id not in foreign_metrics.text

    foreign_write = await fabric.http.post(
        f"/api/v1/wells/{fabric.alpha.well_id}/daily-reports",
        json=payload,
        headers=fabric.bravo.headers,
    )
    assert foreign_write.status_code == 404

    # Source IDs are resolved under the report's organization before any tenant-authored value is stored.
    foreign_document = await fabric.upload(
        fabric.bravo,
        b"bravo source document",
        filename="bravo-source.txt",
        content_type="text/plain",
    )
    assert foreign_document.status_code in {200, 201, 202}, foreign_document.text
    document_id = foreign_document.json()["document"]["id"]
    forged_source = {**payload, "report_number": "ALPHA-REPORT-WITH-FOREIGN-SOURCE"}
    forged_source["content"] = {"summary": "Do not bind another tenant's evidence.", "source_document_id": document_id}
    source_write = await fabric.http.post(
        f"/api/v1/wells/{fabric.alpha.well_id}/daily-reports",
        json=forged_source,
        headers=fabric.alpha.headers,
    )
    assert source_write.status_code == 404
    assert "bravo" not in source_write.text.lower()

    own_document = await fabric.upload(
        fabric.alpha,
        b"alpha source that is quarantined after upload",
        filename="alpha-quarantined.txt",
        content_type="text/plain",
    )
    assert own_document.status_code in {200, 201, 202}, own_document.text
    own_document_id = own_document.json()["document"]["id"]
    from sqlalchemy import select

    from drillai.db.models import Document, RawArtifact

    async with fabric.session() as session:
        document = (
            await session.execute(
                select(Document).where(Document.id == own_document_id, Document.org_id == fabric.org_id(fabric.alpha))
            )
        ).scalar_one()
        artifact = (
            await session.execute(
                select(RawArtifact).where(
                    RawArtifact.id == document.raw_artifact_id,
                    RawArtifact.org_id == fabric.org_id(fabric.alpha),
                )
            )
        ).scalar_one()
        artifact.is_quarantined = True
        await session.commit()
    quarantined_source = {**payload, "report_number": "ALPHA-REPORT-WITH-QUARANTINED-SOURCE"}
    quarantined_source["content"] = {
        "summary": "Quarantined source evidence must not enter a new report.",
        "source_document_id": own_document_id,
    }
    quarantine_write = await fabric.http.post(
        f"/api/v1/wells/{fabric.alpha.well_id}/daily-reports",
        json=quarantined_source,
        headers=fabric.alpha.headers,
    )
    assert quarantine_write.status_code == 404
