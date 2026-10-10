"""Alerts over HTTP, on the real application with two bearer-token tenants.

The life cycle's arithmetic is asserted in ``test_alert_lifecycle.py``; what matters here is the contract
a client sees: the shapes, the status codes, the idempotency of a retried mutation, the deliberate absence
of a delete route, and the fact that one organisation cannot reach another's alerts, rules or evidence.

The ingestion path is exercised too, because that is where an alert is usually born: a batch that breaches
a rule raises the alert *in the same transaction*, and the append response says how many were raised.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from tests.fixtures.fabric import Fabric

from drillai.db.models import AlertRule

PREFIX = "/api/v1"
NOW = dt.datetime(2026, 3, 15, 12, 0, tzinfo=dt.UTC)
PSI = 6894.757293168


async def _channel(fabric: Fabric, tenant, *, key: str = "spp", unit: str = "psi") -> str:
    response = await fabric.http.post(
        f"{PREFIX}/timeseries",
        json={
            "well_id": tenant.well_id,
            "channel_key": key,
            "name": "Standpipe pressure",
            "dimension": "pressure",
            "unit": unit,
            "source": "witsml",
        },
        headers=tenant.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _append(fabric: Fabric, tenant, series_id: str, points: list[dict], **extra) -> dict:
    response = await fabric.http.post(
        f"{PREFIX}/timeseries/{series_id}/points",
        json={"points": points, **extra},
        headers=tenant.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _rule(fabric: Fabric, tenant, **overrides) -> dict:
    body = {
        "rule_key": "spp-high",
        "name": "Standpipe pressure high",
        "channel_key": "spp",
        "operator": "gt",
        "threshold": 4000.0,
        "unit": "psi",
        "severity": "high",
        "well_id": tenant.well_id,
        **overrides,
    }
    response = await fabric.http.post(f"{PREFIX}/alert-rules", json=body, headers=tenant.headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _raise_one(fabric: Fabric, tenant, series_id: str, **rule_overrides) -> dict:
    """Break a rule and let the ingestion path raise the alert, returning that alert."""

    await _rule(fabric, tenant, **rule_overrides)
    await _append(
        fabric,
        tenant,
        series_id,
        [
            {"ts": (NOW - dt.timedelta(seconds=20)).isoformat(), "value": 4200.0, "unit": "psi"},
            {"ts": NOW.isoformat(), "value": 4250.0, "unit": "psi"},
        ],
    )
    response = await fabric.http.get(f"{PREFIX}/alerts", headers=tenant.headers)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1, response.text
    return items[0]


# --------------------------------------------------------------------------- rules


async def test_a_rule_is_created_listed_and_read_back_with_its_clear_line(fabric: Fabric) -> None:
    created = await _rule(fabric, fabric.alpha)
    assert created["id"].startswith("arl_")
    assert created["unit"] == "psi"
    assert created["operator"] == "gt" and created["threshold"] == 4000.0
    assert created["clear_operator"] == "lte", "the engine's default clear line is reported, not implied"
    assert created["clear_is_explicit"] is False
    assert created["enabled"] is True

    listed = await fabric.http.get(f"{PREFIX}/alert-rules", headers=fabric.alpha.headers)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == 1 and [item["id"] for item in body["items"]] == [created["id"]]
    assert body["operators"] == ["gt", "gte", "lt", "lte", "eq"]
    assert body["severities"] == ["low", "medium", "high", "critical"]

    one = await fabric.http.get(f"{PREFIX}/alert-rules/{created['id']}", headers=fabric.alpha.headers)
    assert one.status_code == 200 and one.json() == created


async def test_an_unknown_rule_is_not_found(fabric: Fabric) -> None:
    response = await fabric.http.get(f"{PREFIX}/alert-rules/arl_missing", headers=fabric.alpha.headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "platform.not_found"


async def test_creating_a_rule_twice_with_one_key_creates_one_rule(fabric: Fabric) -> None:
    """A retried create after a timeout must not leave two rules watching the same channel."""

    body = {
        "rule_key": "spp-high",
        "name": "Standpipe pressure high",
        "channel_key": "spp",
        "operator": "gt",
        "threshold": 4000.0,
        "unit": "psi",
        "severity": "high",
        "well_id": fabric.alpha.well_id,
    }
    headers = {**fabric.alpha.headers, "Idempotency-Key": "rule-key-1"}
    first = await fabric.http.post(f"{PREFIX}/alert-rules", json=body, headers=headers)
    second = await fabric.http.post(f"{PREFIX}/alert-rules", json=body, headers=headers)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    async with fabric.session() as session:
        count = len(
            (
                await session.execute(
                    select(AlertRule).where(AlertRule.org_id == fabric.org_id(fabric.alpha))
                )
            )
            .scalars()
            .all()
        )
    assert count == 1


async def test_a_rule_change_requires_a_version_and_a_reason(fabric: Fabric) -> None:
    rule = await _rule(fabric, fabric.alpha)

    no_reason = await fabric.http.patch(
        f"{PREFIX}/alert-rules/{rule['id']}",
        json={"expected_updated_at": rule["updated_at"], "threshold": 4100.0},
        headers=fabric.alpha.headers,
    )
    assert no_reason.status_code == 422, no_reason.text

    blank_reason = await fabric.http.patch(
        f"{PREFIX}/alert-rules/{rule['id']}",
        json={"expected_updated_at": rule["updated_at"], "reason": "  ", "threshold": 4100.0},
        headers=fabric.alpha.headers,
    )
    assert blank_reason.status_code in {409, 422}, blank_reason.text

    changed = await fabric.http.patch(
        f"{PREFIX}/alert-rules/{rule['id']}",
        json={
            "expected_updated_at": rule["updated_at"],
            "reason": "tightening after a false positive",
            "threshold": 4100.0,
            "unit": "psi",
        },
        headers=fabric.alpha.headers,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["threshold"] == 4100.0

    stale = await fabric.http.patch(
        f"{PREFIX}/alert-rules/{rule['id']}",
        json={
            "expected_updated_at": rule["updated_at"],
            "reason": "another writer",
            "threshold": 4200.0,
        },
        headers=fabric.alpha.headers,
    )
    assert stale.status_code == 409, stale.text


async def test_an_impossible_rule_is_refused_at_the_boundary(fabric: Fabric) -> None:
    for body, field in (
        ({"operator": "regex"}, "operator"),
        ({"operator": "gt", "threshold": 4000.0, "clear_operator": "lt", "clear_threshold": 4500.0}, "clear_threshold"),
        ({"unit": "furlongs"}, "unit"),
    ):
        payload = {
            "rule_key": "bad-rule",
            "name": "Bad rule",
            "channel_key": "spp",
            "operator": "gt",
            "threshold": 4000.0,
            "severity": "high",
            **body,
        }
        response = await fabric.http.post(f"{PREFIX}/alert-rules", json=payload, headers=fabric.alpha.headers)
        assert response.status_code in {409, 422}, response.text
        if response.status_code == 409:
            assert response.json()["error"]["details"]["field"] == field


# --------------------------------------------------------------------------- alerts over HTTP


async def test_the_ingestion_batch_raises_its_alert_in_the_same_transaction(fabric: Fabric) -> None:
    series_id = await _channel(fabric, fabric.alpha)
    await _rule(fabric, fabric.alpha, sustain_seconds=20.0)

    report = await _append(
        fabric,
        fabric.alpha,
        series_id,
        [
            {"ts": (NOW - dt.timedelta(seconds=20)).isoformat(), "value": 4100.0, "unit": "psi"},
            {"ts": NOW.isoformat(), "value": 4250.0, "unit": "psi"},
        ],
    )
    assert report["accepted"] == 2
    assert report["alerts_raised"] == 1, "the batch that breached the rule says so"

    listed = await fabric.http.get(f"{PREFIX}/alerts", headers=fabric.alpha.headers)
    body = listed.json()
    assert body["total"] == 1
    alert = body["items"][0]
    assert alert["status"] == "raised" and alert["severity"] == "high"
    assert alert["rule_ref"] == "spp-high"
    assert alert["observed"]["value"] == 4250.0 * PSI
    assert alert["observed"]["unit"] == "Pa"
    assert alert["observed"]["declared_unit"] == "psi"
    assert alert["evidence_url"].endswith(f"/alerts/{alert['id']}/evidence")

    # The same numbers again raise nothing: it is the same open condition.
    again = await _append(
        fabric,
        fabric.alpha,
        series_id,
        [{"ts": (NOW + dt.timedelta(minutes=1)).isoformat(), "value": 4300.0, "unit": "psi"}],
    )
    assert again["alerts_raised"] == 0
    assert (await fabric.http.get(f"{PREFIX}/alerts", headers=fabric.alpha.headers)).json()["total"] == 1


async def test_listing_an_empty_set_is_an_empty_answer_not_an_error(fabric: Fabric) -> None:
    response = await fabric.http.get(f"{PREFIX}/alerts", headers=fabric.alpha.headers)
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == [] and body["total"] == 0
    assert body["statuses"] == ["raised", "acknowledged", "cleared", "cancelled"]
    assert body["severities"] == ["low", "medium", "high", "critical"]


async def test_a_bad_status_filter_is_refused_not_ignored(fabric: Fabric) -> None:
    response = await fabric.http.get(
        f"{PREFIX}/alerts?status=exploded", headers=fabric.alpha.headers
    )
    assert response.status_code in {409, 422}, response.text


async def test_a_missing_alert_is_not_found(fabric: Fabric) -> None:
    response = await fabric.http.get(f"{PREFIX}/alerts/alt_missing", headers=fabric.alpha.headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "platform.not_found"


async def test_acknowledging_then_clearing_over_http(fabric: Fabric) -> None:
    series_id = await _channel(fabric, fabric.alpha)
    alert = await _raise_one(fabric, fabric.alpha, series_id)

    acknowledged = await fabric.http.post(
        f"{PREFIX}/alerts/{alert['id']}/acknowledge",
        json={"expected_updated_at": alert["version"], "reason": "watching the pressure"},
        headers=fabric.alpha.headers,
    )
    assert acknowledged.status_code == 200, acknowledged.text
    body = acknowledged.json()
    assert body["status"] == "acknowledged"
    assert body["acknowledged_at"] is not None
    assert body["version"] != alert["version"], "the row version moves with the state"

    cleared = await fabric.http.post(
        f"{PREFIX}/alerts/{alert['id']}/clear",
        json={
            "expected_updated_at": body["version"],
            "reason": "bled off to 3 200",
            "observed_value": 3200.0 * PSI,
        },
        headers=fabric.alpha.headers,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["status"] == "cleared"
    assert cleared.json()["observed"]["clear_value"] == 3200.0 * PSI


async def test_a_stale_version_is_a_conflict_over_http(fabric: Fabric) -> None:
    series_id = await _channel(fabric, fabric.alpha)
    alert = await _raise_one(fabric, fabric.alpha, series_id)

    first = await fabric.http.post(
        f"{PREFIX}/alerts/{alert['id']}/acknowledge",
        json={"expected_updated_at": alert["version"], "reason": "first"},
        headers=fabric.alpha.headers,
    )
    assert first.status_code == 200

    second = await fabric.http.post(
        f"{PREFIX}/alerts/{alert['id']}/acknowledge",
        json={"expected_updated_at": alert["version"], "reason": "second"},
        headers=fabric.alpha.headers,
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"].startswith("platform.")


async def test_the_life_cycle_refuses_a_transition_the_vocabulary_does_not_allow(fabric: Fabric) -> None:
    series_id = await _channel(fabric, fabric.alpha)
    alert = await _raise_one(fabric, fabric.alpha, series_id)

    cancelled = await fabric.http.post(
        f"{PREFIX}/alerts/{alert['id']}/cancel",
        json={"expected_updated_at": alert["version"], "reason": "instrument calibration"},
        headers=fabric.alpha.headers,
    )
    assert cancelled.status_code == 200
    version = cancelled.json()["version"]

    for action in ("acknowledge", "clear", "cancel"):
        response = await fabric.http.post(
            f"{PREFIX}/alerts/{alert['id']}/{action}",
            json={"expected_updated_at": version, "reason": "again"},
            headers=fabric.alpha.headers,
        )
        assert response.status_code == 409, f"{action}: {response.text}"


async def test_there_is_no_delete_route_for_an_alert(fabric: Fabric) -> None:
    series_id = await _channel(fabric, fabric.alpha)
    alert = await _raise_one(fabric, fabric.alpha, series_id)

    response = await fabric.http.delete(f"{PREFIX}/alerts/{alert['id']}", headers=fabric.alpha.headers)
    assert response.status_code == 405, "cancelling is the only way to close an alert"
    still_there = await fabric.http.get(f"{PREFIX}/alerts/{alert['id']}", headers=fabric.alpha.headers)
    assert still_there.status_code == 200


async def test_a_retried_acknowledge_returns_the_first_answer(fabric: Fabric) -> None:
    series_id = await _channel(fabric, fabric.alpha)
    alert = await _raise_one(fabric, fabric.alpha, series_id)
    headers = {**fabric.alpha.headers, "Idempotency-Key": "ack-1"}
    body = {"expected_updated_at": alert["version"], "reason": "on it"}

    first = await fabric.http.post(f"{PREFIX}/alerts/{alert['id']}/acknowledge", json=body, headers=headers)
    second = await fabric.http.post(f"{PREFIX}/alerts/{alert['id']}/acknowledge", json=body, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


async def test_evidence_over_http_explains_the_alert(fabric: Fabric) -> None:
    series_id = await _channel(fabric, fabric.alpha)
    alert = await _raise_one(fabric, fabric.alpha, series_id)

    response = await fabric.http.get(
        f"{PREFIX}/alerts/{alert['id']}/evidence", headers=fabric.alpha.headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["alert"]["id"] == alert["id"]
    assert body["rule"]["id"] == alert["rule_id"]
    assert body["rule_snapshot"]["rule_key"] == "spp-high"
    assert body["rule_snapshot"]["threshold"] == 4000.0
    assert body["rule_snapshot"]["unit"] == "psi"
    observed = body["observed"]
    assert observed["value"] == 4250.0 * PSI
    assert observed["threshold"] == 4000.0 * PSI
    assert observed["unit"] == "Pa"
    assert observed["declared_threshold"] == 4000.0
    assert observed["declared_unit"] == "psi"
    assert body["provenance"]["series_id"] == series_id
    assert body["provenance"]["point_id"], "the point that breached is named"
    assert next(point["value"] for point in body["points"]) == 4250.0 * PSI
    assert body["timeline"][0]["state"] == "raised"
    assert "spp" in body["timeline"][0]["reason"]


async def test_evaluating_a_well_twice_gives_the_same_report(fabric: Fabric) -> None:
    series_id = await _channel(fabric, fabric.alpha)
    # The measurements arrive *before* the rule exists, so the append raises nothing and the two
    # evaluation passes below are the first thing to act on the stored points.
    await _append(
        fabric,
        fabric.alpha,
        series_id,
        [
            {"ts": (NOW - dt.timedelta(seconds=20)).isoformat(), "value": 4100.0, "unit": "psi"},
            {"ts": NOW.isoformat(), "value": 4250.0, "unit": "psi"},
        ],
    )
    await _rule(fabric, fabric.alpha, sustain_seconds=20.0)

    first = await fabric.http.post(
        f"{PREFIX}/wells/{fabric.alpha.well_id}/alerts/evaluate", headers=fabric.alpha.headers
    )
    second = await fabric.http.post(
        f"{PREFIX}/wells/{fabric.alpha.well_id}/alerts/evaluate", headers=fabric.alpha.headers
    )
    assert first.status_code == second.status_code == 200
    first_body, second_body = first.json(), second.json()
    assert first_body["raised"] == 1 and first_body["reconciled"] is True
    assert second_body["raised"] == 0 and second_body["already_open"] == 1
    assert first_body["evaluations"][0]["outcome"] == second_body["evaluations"][0]["outcome"] == "raise"
    assert first_body["evaluations"][0]["sustained_seconds"] == second_body["evaluations"][0]["sustained_seconds"]


async def test_evaluating_a_well_that_is_not_yours_is_not_found(fabric: Fabric) -> None:
    response = await fabric.http.post(
        f"{PREFIX}/wells/{fabric.bravo.well_id}/alerts/evaluate", headers=fabric.alpha.headers
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "platform.not_found"


# --------------------------------------------------------------------------- tenancy


async def test_a_second_organisation_sees_none_of_the_first_ones_alerts_or_rules(fabric: Fabric) -> None:
    series_id = await _channel(fabric, fabric.alpha)
    alert = await _raise_one(fabric, fabric.alpha, series_id)
    rule_id = alert["rule_id"]

    bravo_alerts = await fabric.http.get(f"{PREFIX}/alerts", headers=fabric.bravo.headers)
    assert bravo_alerts.status_code == 200
    assert bravo_alerts.json()["total"] == 0 and bravo_alerts.json()["items"] == []

    bravo_rules = await fabric.http.get(f"{PREFIX}/alert-rules", headers=fabric.bravo.headers)
    assert bravo_rules.json()["total"] == 0

    # Naming another organisation's rows by id is answered the same way as naming a row that does not
    # exist: the identifier is not a tenant capability.
    for path in (
        f"{PREFIX}/alerts/{alert['id']}",
        f"{PREFIX}/alerts/{alert['id']}/evidence",
        f"{PREFIX}/alert-rules/{rule_id}",
    ):
        response = await fabric.http.get(path, headers=fabric.bravo.headers)
        assert response.status_code == 404, f"{path}: {response.text}"
        assert response.json()["error"]["code"] == "platform.not_found"

    for action in ("acknowledge", "clear", "cancel"):
        response = await fabric.http.post(
            f"{PREFIX}/alerts/{alert['id']}/{action}",
            json={"expected_updated_at": alert["version"], "reason": "crossing tenants"},
            headers=fabric.bravo.headers,
        )
        assert response.status_code == 404, f"{action}: {response.text}"

    # Alpha's alert is untouched by any of it.
    mine = await fabric.http.get(f"{PREFIX}/alerts/{alert['id']}", headers=fabric.alpha.headers)
    assert mine.json()["status"] == "raised"


async def test_the_same_channel_key_in_two_organisations_is_two_channels_and_two_alerts(
    fabric: Fabric,
) -> None:
    """The isolation has to hold on the *key*, not only on the id: two operators both call a channel
    'spp', and one operator's alert must not be raised against the other's measurements."""

    alpha_series = await _channel(fabric, fabric.alpha)
    bravo_series = await _channel(fabric, fabric.bravo)
    assert alpha_series != bravo_series

    await _rule(fabric, fabric.alpha)
    await _rule(fabric, fabric.bravo)
    await _append(
        fabric,
        fabric.alpha,
        alpha_series,
        [{"ts": NOW.isoformat(), "value": 4200.0, "unit": "psi"}],
    )

    alpha_alerts = (await fabric.http.get(f"{PREFIX}/alerts", headers=fabric.alpha.headers)).json()
    bravo_alerts = (await fabric.http.get(f"{PREFIX}/alerts", headers=fabric.bravo.headers)).json()
    assert alpha_alerts["total"] == 1
    assert bravo_alerts["total"] == 0, "bravo's identically-named channel reported nothing"
    assert alpha_alerts["items"][0]["series_id"] == alpha_series
