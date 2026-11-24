"""Synthetic cross-surface checks for legacy NPT semantics, not field validation."""
from __future__ import annotations

from tests.fixtures.fabric import Fabric

from drillai.db.models import Event, Operation
from drillai.drilling.advisor import OperationsAdvisor
from drillai.drilling.npt import NptService
from drillai.drilling.state import WellStateService
from drillai.security.actions import ActionLevel, Principal
from drillai.workflow.nodes import NodeContext, NptSummaryConfig, _npt_summary


async def test_state_advisor_and_workflow_preserve_npt_contract(fabric: Fabric) -> None:
    org_id = fabric.org_id(fabric.alpha)
    well_id = fabric.alpha.well_id
    common = {"org_id": org_id, "well_id": well_id, "wellbore_id": fabric.alpha.wellbore_id}
    async with fabric.session() as session:
        session.add_all([
            Event(**common, title="explicit zero", is_npt=True, npt_hours=0, duration_hours=7),
            Event(**common, title="generic duration only", is_npt=True, duration_hours=3),
            Event(**common, title="missing hours", is_npt=True),
            Event(org_id=fabric.org_id(fabric.bravo), well_id=fabric.bravo.well_id,
                  wellbore_id=fabric.bravo.wellbore_id, title="other tenant", is_npt=True, npt_hours=40),
            Operation(**common, name="productive actual", operation_class="actual", sequence=1,
                      is_productive=True, actual_duration_hours=4, npt_hours=1),
            Operation(**common, name="nonproductive actual", operation_class="actual", sequence=2,
                      is_productive=False, actual_duration_hours=5, npt_hours=2),
            Operation(**common, name="planned", operation_class="planned", sequence=3,
                      is_productive=True, actual_duration_hours=11, npt_hours=9),
        ])
        await session.commit()
        summary = await NptService(session, org_id).summarise(well_id, include_offsets=False)
        state = (await WellStateService(session, org_id).state(well_id))["npt"]
        assert summary.total_hours == state["total_hours"] == 3
        assert summary.hours_operations == state["hours_from_operations"] == 3
        assert summary.productive_hours == 4
        assert state["percent_of_well_time"] == round(3 / 7 * 100, 2)
        assert "generic event duration" in state["note"]
        assert "Neither the sum nor its percentage establishes verified NPT" in state["note"]
        response = await fabric.http.get(f"/api/v1/wells/{well_id}/state", headers=fabric.alpha.headers)
        assert response.status_code == 200
        assert response.json()["state"]["npt"] == state
        denied = await fabric.http.get(f"/api/v1/wells/{well_id}/state", headers=fabric.bravo.headers)
        assert denied.status_code in (403, 404)

        answer = (await OperationsAdvisor(session, org_id).ask(well_id, "npt")).to_dict()
        total = next(f for f in answer["facts"] if f["key"] == "npt.total")
        assert total["value"] == 3
        assert "not verified NPT" in total["statement"]
        assert "metric_contract" in total["provenance"]
        assert "interval_reconciliation" in total["provenance"]
        cases = {f["provenance"]["hours_source"]: f for f in answer["facts"] if f["key"].startswith("npt.case.")}
        assert cases["explicit_npt_hours"]["value"] == 0
        assert cases["generic_event_duration"]["value"] == 3
        assert "not established NPT" in cases["generic_event_duration"]["statement"]
        assert cases["missing"]["value"] is None

        context = NodeContext(session=session, org_id=org_id,
            principal=Principal(id="synthetic", max_action_level=ActionLevel.OBSERVE),
            run_id="synthetic", node_id="npt", node_name="npt", inputs={}, variables={},
            node_outputs={}, well_id=well_id)
        outcome = await _npt_summary(context, NptSummaryConfig(include_offsets=False))
        assert outcome.outputs["total_hours"] == 3
        assert outcome.outputs["metric_contract"]["selected_basis"] == "events"
        assert outcome.outputs["interval_reconciliation"] == summary.interval_reconciliation
        assert "not verified NPT" in outcome.notes[0]
        operations = await _npt_summary(context, NptSummaryConfig(basis="operations", include_offsets=False))
        assert operations.outputs["total_hours"] == 3
        assert operations.outputs["metric_contract"]["selected_basis"] == "operations"
        assert "do not add" in operations.notes[0]
