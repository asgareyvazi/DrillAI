"""Who may see an alert, who may decide on it, and who may define the rule that raised it.

The model matrix lives in ``tests/telemetry/test_rbac_telemetry.py``; what is asserted here is that the
routes consult it. The distinction that matters operationally is between *reading* the monitor — a viewer,
an auditor, a data manager and an integrity engineer all need it — and *closing* an alert, which is a
decision with a name attached to it and is deliberately narrower.

These run against the development identity (``X-Dev-Roles``), which is why they live in the API suite:
the tenant fabric's bearer tokens are exercised in ``tests/telemetry/test_alerts_api.py``.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from tests.api.conftest import headers

from drillai.db.models import AuditLog

PREFIX = "/api/v1"
BASE_TS = dt.datetime(2026, 3, 15, 12, 0, tzinfo=dt.UTC)


async def _tree(client, *, channel_key: str = "spp") -> tuple[str, str]:
    project = await client.post(
        f"{PREFIX}/projects", json={"name": "Alert RBAC"}, headers=headers("well_manager")
    )
    assert project.status_code in {200, 201}, project.text
    well = await client.post(
        f"{PREFIX}/wells",
        json={
            "project_id": project.json()["id"],
            "name": "A-RBAC-1",
            "well_type": "development_producer",
        },
        headers=headers("well_manager"),
    )
    assert well.status_code == 201, well.text
    channel = await client.post(
        f"{PREFIX}/timeseries",
        json={
            "well_id": well.json()["id"],
            "channel_key": channel_key,
            "name": "Standpipe pressure",
            "dimension": "pressure",
            "unit": "psi",
            "source": "witsml",
        },
        headers=headers("engineer"),
    )
    assert channel.status_code == 201, channel.text
    return well.json()["id"], channel.json()["id"]


async def _one_alert(client) -> dict:
    """A well, a rule and a breached measurement — an alert as a rule would actually raise one."""

    well_id, series_id = await _tree(client)
    rule = await client.post(
        f"{PREFIX}/alert-rules",
        json={
            "rule_key": "spp-high",
            "name": "Standpipe pressure high",
            "channel_key": "spp",
            "operator": "gt",
            "threshold": 4000.0,
            "unit": "psi",
            "severity": "high",
            "well_id": well_id,
        },
        headers=headers("engineer"),
    )
    assert rule.status_code == 201, rule.text
    appended = await client.post(
        f"{PREFIX}/timeseries/{series_id}/points",
        json={
            "points": [
                {"ts": BASE_TS.isoformat(), "value": 4300.0, "unit": "psi"},
            ]
        },
        headers=headers("engineer"),
    )
    assert appended.status_code == 201, appended.text
    assert appended.json()["alerts_raised"] == 1, appended.text
    listed = await client.get(f"{PREFIX}/alerts", headers=headers("engineer"))
    assert listed.status_code == 200 and listed.json()["total"] == 1, listed.text
    return listed.json()["items"][0]


# --------------------------------------------------------------------------- reads


async def test_every_read_role_can_see_the_monitor_surfaces(client) -> None:
    alert = await _one_alert(client)
    for role in ("viewer", "auditor", "data_manager", "integrity_engineer", "engineer"):
        listed = await client.get(f"{PREFIX}/alerts", headers=headers(role))
        assert listed.status_code == 200, f"{role}: {listed.text}"
        assert listed.json()["total"] == 1, role

        one = await client.get(f"{PREFIX}/alerts/{alert['id']}", headers=headers(role))
        assert one.status_code == 200, f"{role}: {one.text}"

        evidence = await client.get(f"{PREFIX}/alerts/{alert['id']}/evidence", headers=headers(role))
        assert evidence.status_code == 200, f"{role}: {evidence.text}"
        assert evidence.json()["observed"]["unit"] == "Pa"

        rules = await client.get(f"{PREFIX}/alert-rules", headers=headers(role))
        assert rules.status_code == 200 and rules.json()["total"] == 1, f"{role}: {rules.text}"


async def test_an_empty_alert_list_is_an_empty_answer_not_a_refusal(client) -> None:
    await _tree(client)
    for role in ("viewer", "auditor", "data_manager"):
        response = await client.get(f"{PREFIX}/alerts", headers=headers(role))
        assert response.status_code == 200
        assert response.json() == {
            "items": [],
            "total": 0,
            "limit": 100,
            "offset": 0,
            "statuses": ["raised", "acknowledged", "cleared", "cancelled"],
            "severities": ["low", "medium", "high", "critical"],
        }


async def test_a_missing_alert_is_not_found_for_a_reader_too(client) -> None:
    response = await client.get(f"{PREFIX}/alerts/alt_nope", headers=headers("viewer"))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "platform.not_found"


# --------------------------------------------------------------------------- decisions


async def test_a_viewer_may_read_an_alert_but_not_decide_it(client) -> None:
    alert = await _one_alert(client)

    for action in ("acknowledge", "clear", "cancel"):
        response = await client.post(
            f"{PREFIX}/alerts/{alert['id']}/{action}",
            json={"expected_updated_at": alert["version"], "reason": "as a viewer"},
            headers=headers("viewer"),
        )
        assert response.status_code == 403, f"{action}: {response.text}"
        assert response.json()["error"]["code"] == "security.permission_denied"

    unchanged = await client.get(f"{PREFIX}/alerts/{alert['id']}", headers=headers("engineer"))
    assert unchanged.json()["status"] == "raised", "the refused calls changed nothing"


async def test_a_data_manager_may_ingest_but_not_close_an_alert(client) -> None:
    """Ingestion and the alert life cycle are different jobs: the data manager owns the first."""

    alert = await _one_alert(client)

    _, series_id = await _tree(client, channel_key="flow_rate")
    appended = await client.post(
        f"{PREFIX}/timeseries/{series_id}/points",
        json={"points": [{"ts": BASE_TS.isoformat(), "value": 500.0, "unit": "gpm"}]},
        headers=headers("data_manager"),
    )
    assert appended.status_code == 201, appended.text

    for action in ("acknowledge", "clear", "cancel"):
        response = await client.post(
            f"{PREFIX}/alerts/{alert['id']}/{action}",
            json={"expected_updated_at": alert["version"], "reason": "as data manager"},
            headers=headers("data_manager"),
        )
        assert response.status_code == 403, f"{action}: {response.text}"


async def test_an_auditor_cannot_acknowledge_an_alert(client) -> None:
    alert = await _one_alert(client)
    response = await client.post(
        f"{PREFIX}/alerts/{alert['id']}/acknowledge",
        json={"expected_updated_at": alert["version"]},
        headers=headers("auditor"),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "security.permission_denied"


async def test_a_viewer_cannot_define_a_rule(client) -> None:
    well_id, _ = await _tree(client)
    response = await client.post(
        f"{PREFIX}/alert-rules",
        json={
            "rule_key": "viewer-rule",
            "name": "A rule a viewer tried to write",
            "channel_key": "spp",
            "operator": "gt",
            "threshold": 4000.0,
            "severity": "high",
            "well_id": well_id,
        },
        headers=headers("viewer"),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "security.permission_denied"

    rules = await client.get(f"{PREFIX}/alert-rules", headers=headers("engineer"))
    assert rules.json()["total"] == 0, "the refused create left no rule behind"


async def test_an_engineer_may_acknowledge_and_clear_with_a_reason(client) -> None:
    alert = await _one_alert(client)

    acknowledged = await client.post(
        f"{PREFIX}/alerts/{alert['id']}/acknowledge",
        json={"expected_updated_at": alert["version"], "reason": "watching it"},
        headers=headers("engineer"),
    )
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["status"] == "acknowledged"

    cleared = await client.post(
        f"{PREFIX}/alerts/{alert['id']}/clear",
        json={
            "expected_updated_at": acknowledged.json()["version"],
            "reason": "pressure recovered",
        },
        headers=headers("engineer"),
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["status"] == "cleared"


async def test_closing_without_a_reason_is_refused_by_the_contract(client) -> None:
    alert = await _one_alert(client)
    response = await client.post(
        f"{PREFIX}/alerts/{alert['id']}/cancel",
        json={"expected_updated_at": alert["version"]},
        headers=headers("engineer"),
    )
    assert response.status_code == 422, response.text


# --------------------------------------------------------------------------- the ledger


async def test_the_decision_is_recorded_with_the_actor_and_the_reason(client, db) -> None:
    alert = await _one_alert(client)
    response = await client.post(
        f"{PREFIX}/alerts/{alert['id']}/acknowledge",
        json={"expected_updated_at": alert["version"], "reason": "taking the call"},
        headers=headers("engineer"),
    )
    assert response.status_code == 200, response.text

    async with db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.resource_id == alert["id"])
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert [row.action for row in rows] == ["alert.raise", "alert.acknowledge"]
    entry = rows[1]
    assert entry.actor_id, "the ledger names who decided"
    assert entry.details["reason"] == "taking the call"
    assert entry.before["status"] == "raised" and entry.after["status"] == "acknowledged"
    assert entry.action_level == "L2", "resolved from the action catalogue, not from the caller"


async def test_raising_an_alert_is_audited_too(client, db) -> None:
    """The rule that fired is as much a decision as the acknowledgement: both are on the record."""

    alert = await _one_alert(client)
    async with db.session() as session:
        rows = list(
            (
                await session.execute(select(AuditLog).where(AuditLog.resource_id == alert["id"]))
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    entry = rows[0]
    assert entry.action == "alert.raise"
    assert entry.details["rule_ref"] == "spp-high"
    assert entry.details["observed"]["unit"] == "Pa"
    assert entry.details["observed"]["declared_unit"] == "psi"
    assert "spp" in entry.details["reason"]
