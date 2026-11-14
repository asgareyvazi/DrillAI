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

    # The wellbore is a first-class report scope within an organization. The same report identity
    # may exist on a different bore, but list/read paths must still resolve against the selected one.
    second_bore_id = await fabric.add_wellbore(fabric.alpha, fabric.alpha.well_id, "Alternate bore")
    second_bore_report = await fabric.http.post(
        f"/api/v1/wells/{fabric.alpha.well_id}/daily-reports",
        json={**payload, "wellbore_id": second_bore_id},
        headers=fabric.alpha.headers,
    )
    assert second_bore_report.status_code == 201, second_bore_report.text
    second_bore_report_id = second_bore_report.json()["report"]["id"]
    all_alpha_reports = await fabric.http.get(
        f"/api/v1/wells/{fabric.alpha.well_id}/daily-reports",
        headers=fabric.alpha.headers,
    )
    assert all_alpha_reports.status_code == 200, all_alpha_reports.text
    assert all_alpha_reports.json()["total"] == 2
    assert {item["id"] for item in all_alpha_reports.json()["items"]} == {report_id, second_bore_report_id}
    for wellbore_id, expected_id in (
        (fabric.alpha.wellbore_id, report_id),
        (second_bore_id, second_bore_report_id),
    ):
        scoped_list = await fabric.http.get(
            f"/api/v1/wells/{fabric.alpha.well_id}/daily-reports",
            params={"wellbore_id": wellbore_id},
            headers=fabric.alpha.headers,
        )
        assert scoped_list.status_code == 200, scoped_list.text
        assert scoped_list.json()["total"] == 1
        assert [item["id"] for item in scoped_list.json()["items"]] == [expected_id]

    other_well_id = await fabric.add_well(fabric.alpha, "ALPHA-2")
    other_wellbore_id = await fabric.add_wellbore(fabric.alpha, other_well_id, "Other well bore")
    for wrong_path in (
        f"/api/v1/wells/{other_well_id}/daily-reports/{report_id}",
        f"/api/v1/wells/{other_well_id}/daily-reports/{report_id}/history",
        f"/api/v1/wells/{other_well_id}/daily-reports/{report_id}/revisions/1",
        f"/api/v1/wells/{other_well_id}/daily-reports/{report_id}/revisions/1/activity-metrics",
    ):
        hidden = await fabric.http.get(wrong_path, headers=fabric.alpha.headers)
        assert hidden.status_code == 404, (wrong_path, hidden.text)
        assert "tenant-scoped manual report" not in hidden.text

    wrong_path_submit = await fabric.http.post(
        f"/api/v1/wells/{other_well_id}/daily-reports/{report_id}/revisions/1/submit",
        json={"expected_edit_version": 1, "reason": "Wrong-well scope refusal check."},
        headers=fabric.alpha.headers,
    )
    assert wrong_path_submit.status_code == 404, wrong_path_submit.text
    wrong_path_correction = await fabric.http.post(
        f"/api/v1/wells/{other_well_id}/daily-reports/{report_id}/corrections",
        json={"reason": "Wrong-well scope refusal check."},
        headers=fabric.alpha.headers,
    )
    assert wrong_path_correction.status_code == 404, wrong_path_correction.text
    original_after_wrong_path_writes = await fabric.http.get(
        f"/api/v1/wells/{fabric.alpha.well_id}/daily-reports/{report_id}",
        headers=fabric.alpha.headers,
    )
    assert original_after_wrong_path_writes.status_code == 200, original_after_wrong_path_writes.text
    assert original_after_wrong_path_writes.json()["report"]["latest_revision_number"] == 1
    assert original_after_wrong_path_writes.json()["latest_revision"]["status"] == "draft"

    wrong_wellbore = await fabric.http.post(
        f"/api/v1/wells/{fabric.alpha.well_id}/daily-reports",
        json={**payload, "report_date": "2026-05-03", "wellbore_id": other_wellbore_id},
        headers=fabric.alpha.headers,
    )
    assert wrong_wellbore.status_code == 422, wrong_wellbore.text
    wrong_scope_filter = await fabric.http.get(
        f"/api/v1/wells/{fabric.alpha.well_id}/daily-reports",
        params={"wellbore_id": other_wellbore_id},
        headers=fabric.alpha.headers,
    )
    assert wrong_scope_filter.status_code == 422, wrong_scope_filter.text

    # A source document in the same organization but another wellbore is not valid report evidence.
    bore_document = await fabric.upload(
        fabric.alpha,
        b"alpha alternate-bore evidence",
        filename="alternate-bore-source.txt",
        content_type="text/plain",
        well_id=fabric.alpha.well_id,
        wellbore_id=second_bore_id,
    )
    assert bore_document.status_code in {200, 201, 202}, bore_document.text
    wrong_bore_source = await fabric.http.post(
        f"/api/v1/wells/{fabric.alpha.well_id}/daily-reports",
        json={
            **payload,
            "report_date": "2026-05-03",
            "content": {
                "summary": "A source from a different bore must not be bound.",
                "source_document_id": bore_document.json()["document"]["id"],
            },
        },
        headers=fabric.alpha.headers,
    )
    assert wrong_bore_source.status_code == 422, wrong_bore_source.text
    assert "wellbore" in wrong_bore_source.text.lower()

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

    bravo_report = await fabric.http.post(
        f"/api/v1/wells/{fabric.bravo.well_id}/daily-reports",
        json={**payload, "wellbore_id": fabric.bravo.wellbore_id},
        headers=fabric.bravo.headers,
    )
    assert bravo_report.status_code == 201, bravo_report.text
    bravo_report_id = bravo_report.json()["report"]["id"]
    bravo_list = await fabric.http.get(
        f"/api/v1/wells/{fabric.bravo.well_id}/daily-reports",
        headers=fabric.bravo.headers,
    )
    assert bravo_list.status_code == 200, bravo_list.text
    assert bravo_list.json()["total"] == 1
    assert [item["id"] for item in bravo_list.json()["items"]] == [bravo_report_id]
    alpha_list_after_bravo_write = await fabric.http.get(
        f"/api/v1/wells/{fabric.alpha.well_id}/daily-reports",
        headers=fabric.alpha.headers,
    )
    assert alpha_list_after_bravo_write.status_code == 200, alpha_list_after_bravo_write.text
    assert {item["id"] for item in alpha_list_after_bravo_write.json()["items"]} == {report_id, second_bore_report_id}
    assert bravo_report_id not in alpha_list_after_bravo_write.text

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
