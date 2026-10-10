"""Integrity, offset intelligence, readiness and schematic engines.

These four engines turn platform data into planning and assurance outputs; the tests pin down
the decision rules (what counts as a barrier, what is rejected and why, what blocks readiness)
and the derived geometry.
"""

from __future__ import annotations

import math

import pytest

INCH = 0.0254


# --------------------------------------------------------------------------- integrity
def _barrier(**overrides) -> dict:
    payload = {
        "name": "barrier",
        "kind": "casing",
        "from_depth_si": 0.0,
        "to_depth_si": 2000.0,
        "status": "verified",
        "verification_method": "pressure test",
        "evidence_ref": "doc:pressure-test-001",
    }
    payload.update(overrides)
    return payload


def test_two_independent_verified_barriers_complete_the_envelope(engines):
    _, result = engines.execute(
        "integrity.barrier_envelope",
        {
            "elements": [
                _barrier(name="9-5/8 casing", kind="casing"),
                _barrier(name="cement behind 9-5/8", kind="cement"),
                _barrier(name="wellhead", kind="wellhead", to_depth_si=50.0),
            ],
            "well_depth_si": 2000.0,
        },
    )
    output = result.outputs
    assert output.envelope_complete is True
    assert output.gaps == []
    assert output.verified_barrier_count == 3
    assert output.barrier_kinds == ["casing", "cement", "wellhead"]
    assert result.is_feasible is True


def test_single_barrier_leaves_an_interval_shortfall(engines):
    _, result = engines.execute(
        "integrity.barrier_envelope",
        {"elements": [_barrier(name="surface casing", kind="casing")], "well_depth_si": 2000.0},
    )
    output = result.outputs
    assert output.envelope_complete is False
    assert output.gaps
    assert all(interval.shortfall == 1 for interval in output.gaps)
    assert any(violation.name == "barrier_envelope_shortfall" for violation in result.violations)
    assert result.is_feasible is False


def test_same_independence_group_counts_once(engines):
    """Two casings of the same kind at the same depth are one barrier, not two."""
    _, result = engines.execute(
        "integrity.barrier_envelope",
        {
            "elements": [
                _barrier(name="9-5/8 casing", kind="casing", independence_group="9-5/8"),
                _barrier(name="9-5/8 casing (duplicate record)", kind="casing", independence_group="9-5/8"),
            ],
            "well_depth_si": 2000.0,
        },
    )
    assert result.outputs.envelope_complete is False
    assert result.outputs.gaps[0].verified_barriers == 1


def test_verified_without_evidence_is_not_counted(engines):
    _, result = engines.execute(
        "integrity.barrier_envelope",
        {
            "elements": [
                _barrier(name="casing", kind="casing", evidence_ref=None),
                _barrier(name="cement", kind="cement"),
            ],
            "well_depth_si": 2000.0,
        },
    )
    output = result.outputs
    assert output.envelope_complete is False
    unverified = next(assessment for assessment in output.unverified if assessment.name == "casing")
    assert "evidence" in unverified.reason


def test_evidence_requirement_can_be_relaxed_explicitly(engines):
    _, result = engines.execute(
        "integrity.barrier_envelope",
        {
            "elements": [
                _barrier(name="casing", kind="casing", evidence_ref=None),
                _barrier(name="cement", kind="cement", evidence_ref=None),
            ],
            "well_depth_si": 2000.0,
            "require_evidence": False,
        },
    )
    assert result.outputs.envelope_complete is True


def test_expired_verification_does_not_count(engines):
    _, result = engines.execute(
        "integrity.barrier_envelope",
        {
            "elements": [
                _barrier(name="casing", kind="casing", expiry_date="2000-01-01"),
                _barrier(name="cement", kind="cement"),
            ],
            "well_depth_si": 2000.0,
        },
    )
    output = result.outputs
    assert output.envelope_complete is False
    assert "casing" in output.expired_verifications
    expired = next(assessment for assessment in output.unverified if assessment.name == "casing")
    assert "expired" in expired.reason


def test_failed_barrier_is_an_error_and_listed(engines):
    _, result = engines.execute(
        "integrity.barrier_envelope",
        {
            "elements": [
                _barrier(name="production casing", kind="casing", status="failed"),
                _barrier(name="cement", kind="cement"),
            ],
            "well_depth_si": 2000.0,
        },
    )
    assert any(violation.name == "barrier_failed" for violation in result.violations)
    assert [item.name for item in result.outputs.failed] == ["production casing"]
    assert result.is_feasible is False


def test_barrier_diversity_requirement(engines):
    """One barrier kind cannot satisfy a two-kind envelope even with several elements."""
    _, result = engines.execute(
        "integrity.barrier_envelope",
        {
            "elements": [
                _barrier(name="casing A", kind="casing", to_depth_si=1500.0),
                _barrier(name="casing B", kind="casing", from_depth_si=1500.0),
            ],
            "well_depth_si": 2000.0,
        },
    )
    assert any(violation.name == "barrier_diversity" for violation in result.violations)


def test_partial_coverage_reports_the_exact_gap_interval(engines):
    _, result = engines.execute(
        "integrity.barrier_envelope",
        {
            "elements": [
                _barrier(name="casing", kind="casing", to_depth_si=1200.0),
                _barrier(name="cement", kind="cement", to_depth_si=1200.0),
            ],
            "well_depth_si": 2000.0,
        },
    )
    gaps = result.outputs.gaps
    assert len(gaps) == 1
    assert gaps[0].from_depth_si == pytest.approx(1200.0)
    assert gaps[0].to_depth_si == pytest.approx(2000.0)
    assert gaps[0].verified_barriers == 0


# --------------------------------------------------------------------------- offsets
def _well(well_id: str, **overrides) -> dict:
    payload = {
        "well_id": well_id,
        "well_name": well_id.upper(),
        "tvd_si": 3000.0,
        "md_si": 3200.0,
        "hole_size_si": 8.5 * INCH,
        "mud_weight_si": 1250.0,
        "max_inclination_deg": 30.0,
        "formations": ["Shale A", "Sand B", "Shale C"],
        "spud_date": "2024-01-01",
    }
    payload.update(overrides)
    return payload


def _offset_input(**overrides) -> dict:
    payload = {
        "reference": _well("ref"),
        "candidates": [
            _well("close", tvd_si=3050.0, mud_weight_si=1260.0, max_inclination_deg=32.0),
            _well("far", tvd_si=4200.0, mud_weight_si=1500.0, max_inclination_deg=70.0, hole_size_si=12.25 * INCH),
        ],
        "as_of": "2025-01-01",
    }
    payload.update(overrides)
    return payload


def test_closer_well_ranks_first_and_contributions_explain_the_score(engines):
    _, result = engines.execute("offsets.similarity", _offset_input())
    output = result.outputs
    assert [candidate.well_id for candidate in output.ranked] == ["close", "far"]
    assert output.ranked[0].rank == 1
    assert output.ranked[0].score > output.ranked[1].score
    best = output.ranked[0]
    contributions = sum(factor.contribution for factor in best.factors if factor.contribution is not None)
    assert contributions / best.coverage == pytest.approx(best.score, rel=1e-9)
    assert sum(output.normalized_weights.values()) == pytest.approx(1.0)
    assert best.factors[0].factor == "depth"


def test_low_coverage_candidate_is_rejected_with_a_reason(engines):
    thin = {"well_id": "thin", "well_name": "THIN", "tvd_si": 3000.0}
    _, result = engines.execute("offsets.similarity", _offset_input(candidates=[_well("ok"), thin]))
    rejected = {candidate.well_id: candidate for candidate in result.outputs.rejected}
    assert "thin" in rejected
    assert "coverage" in (rejected["thin"].exclusion_reason or "")
    assert rejected["thin"].rank is None
    assert any("rejected" in warning for warning in result.warnings)


def test_explicit_exclusion_is_respected(engines):
    excluded = _well("bad", exclude=True, exclude_reason="sidetracked below the target formation")
    _, result = engines.execute("offsets.similarity", _offset_input(candidates=[_well("ok"), excluded]))
    assert result.outputs.rejected[0].exclusion_reason == "sidetracked below the target formation"
    assert [candidate.well_id for candidate in result.outputs.ranked] == ["ok"]


def test_formation_overlap_is_reported_per_candidate(engines):
    candidate = _well("partial", formations=["Shale A", "Shale C", "Other"])
    _, result = engines.execute("offsets.similarity", _offset_input(candidates=[candidate]))
    factor = next(factor for factor in result.outputs.ranked[0].factors if factor.factor == "formation")
    assert factor.score == pytest.approx(2 / 4)  # A and C shared, Other added
    assert "Sand B" in (factor.detail or "")  # the reference spelling is preserved in the explanation


def test_statistics_are_weighted_by_similarity(engines):
    _, result = engines.execute("offsets.similarity", _offset_input())
    output = result.outputs
    metrics = {stat.metric: stat for stat in output.statistics}
    assert set(metrics) == {"tvd_si", "md_si", "mud_weight_si", "max_inclination_deg"}
    depth = metrics["tvd_si"]
    assert depth.count == 2
    assert depth.minimum <= depth.weighted_mean <= depth.maximum
    assert depth.p10 <= depth.median <= depth.p90
    assert depth.unit == "m"
    # the closer well pulls the weighted mean towards its own value
    assert depth.weighted_mean < (3050.0 + 4200.0) / 2.0


def test_hazard_profile_uses_only_ranked_offsets(engines):
    lessons = [
        {"well_id": "close", "category": "lost_circulation", "description": "total losses at 1800 m", "severity": "high", "depth_si": 1800.0},
        {"well_id": "close", "category": "lost_circulation", "description": "partial losses", "severity": "low", "depth_si": 1200.0},
        {"well_id": "rejected_well", "category": "stuck_pipe", "description": "pack-off in shale", "severity": "medium"},
    ]
    _, result = engines.execute("offsets.similarity", _offset_input(lessons=lessons))
    hazards = {profile.category: profile for profile in result.outputs.hazards}
    assert set(hazards) == {"lost_circulation"}
    assert hazards["lost_circulation"].occurrences == 2
    assert hazards["lost_circulation"].wells == ["close"]
    assert hazards["lost_circulation"].severity == {"high": 1, "low": 1}
    assert result.outputs.lessons_considered == 3


def test_depth_window_score_reaches_zero_beyond_the_tolerance(engines):
    beyond = _well("beyond", tvd_si=3000.0 + 600.0)
    _, result = engines.execute("offsets.similarity", _offset_input(candidates=[beyond]))
    factor = next(factor for factor in result.outputs.ranked[0].factors if factor.factor == "depth")
    assert factor.score == 0.0


# --------------------------------------------------------------------------- readiness
def _requirement(**overrides) -> dict:
    payload = {
        "item_id": "eqp-1",
        "name": "9-5/8 casing",
        "category": "material",
        "quantity_required": 10.0,
        "quantity_available": 10.0,
        "unit": "joint",
        "status": "available",
        "criticality": "critical",
        "required_by": "2025-06-01",
        "lead_time_days": 30.0,
        "certification_valid_until": "2026-01-01",
    }
    payload.update(overrides)
    return payload


def _readiness_input(requirements: list[dict], **overrides) -> dict:
    payload = {"requirements": requirements, "as_of": "2025-01-01", "scope": "spud"}
    payload.update(overrides)
    return payload


def test_fully_available_scope_is_ready(engines):
    _, result = engines.execute("readiness.assessment", _readiness_input([_requirement()]))
    output = result.outputs
    assert output.overall_state == "ready"
    assert output.ready_count == 1
    assert output.critical_items_not_ready == []
    assert result.is_feasible is True


def test_shortage_makes_the_item_not_ready(engines):
    _, result = engines.execute(
        "readiness.assessment", _readiness_input([_requirement(quantity_available=7.0)])
    )
    output = result.outputs
    assert output.overall_state == "not_ready"
    assert output.items[0].state == "not_ready"
    assert output.items[0].quantity_shortfall == pytest.approx(3.0)
    assert any("short by 3.00 joint" in reason for reason in output.items[0].reasons)
    assert any(violation.name == "critical_item_not_ready" for violation in result.violations)


def test_lead_time_makes_the_order_window_explicit(engines):
    requirement = _requirement(status="on_order", required_by="2025-02-01", lead_time_days=60.0)
    _, result = engines.execute("readiness.assessment", _readiness_input([requirement]))
    item = result.outputs.items[0]
    assert item.latest_order_date == "2024-12-03"  # 2025-02-01 minus 60 days
    assert item.state == "not_ready"
    assert any("had to be ordered by" in reason for reason in item.reasons)


def test_lead_time_still_feasible_keeps_the_item_at_risk_not_blocked(engines):
    requirement = _requirement(status="on_order", required_by="2025-06-01", lead_time_days=30.0)
    _, result = engines.execute("readiness.assessment", _readiness_input([requirement]))
    item = result.outputs.items[0]
    assert item.state == "at_risk"
    assert item.latest_order_date == "2025-05-02"
    assert result.outputs.overall_state == "at_risk"
    assert any(violation.name == "item_at_risk" and violation.severity == "warning" for violation in result.violations)


def test_expired_certification_blocks_the_item(engines):
    _, result = engines.execute(
        "readiness.assessment",
        _readiness_input([_requirement(certification_valid_until="2024-12-31", criticality="standard")]),
    )
    item = result.outputs.items[0]
    assert item.state == "not_ready"
    assert any("certification expired" in reason for reason in item.reasons)


def test_non_critical_shortage_does_not_block_the_scope(engines):
    _, result = engines.execute(
        "readiness.assessment",
        _readiness_input([_requirement(criticality="standard", quantity_available=0.0)]),
    )
    output = result.outputs
    assert output.overall_state == "ready"  # nothing critical is blocked
    assert output.not_ready_count == 1
    assert output.critical_items_not_ready == []


def test_next_required_date_and_longest_lead_time_are_summarised(engines):
    _, result = engines.execute(
        "readiness.assessment",
        _readiness_input(
            [
                _requirement(item_id="a", required_by="2025-05-01", lead_time_days=10.0),
                _requirement(item_id="b", required_by="2025-04-01", lead_time_days=45.0),
            ]
        ),
    )
    output = result.outputs
    assert output.next_required_date == "2025-04-01"
    assert output.longest_lead_time_days == pytest.approx(45.0)
    assert output.as_of == "2025-01-01"


# --------------------------------------------------------------------------- schematic
def _schematic_input(**overrides) -> dict:
    payload = {
        "well_name": "W-01",
        "hole_sections": [
            {"name": '17-1/2"', "top_depth_si": 0.0, "bottom_depth_si": 500.0, "hole_diameter_si": 17.5 * INCH},
            {"name": '12-1/4"', "top_depth_si": 500.0, "bottom_depth_si": 1500.0, "hole_diameter_si": 12.25 * INCH},
            {"name": '8-1/2"', "top_depth_si": 1500.0, "bottom_depth_si": 3000.0, "hole_diameter_si": 8.5 * INCH},
        ],
        "casing_strings": [
            {
                "name": '13-3/8" surface',
                "kind": "surface",
                "outer_diameter_si": 13.375 * INCH,
                "inner_diameter_si": 12.415 * INCH,
                "top_depth_si": 0.0,
                "shoe_depth_si": 500.0,
                "cement_top_si": 0.0,
            },
            {
                "name": '9-5/8" intermediate',
                "kind": "intermediate",
                "outer_diameter_si": 9.625 * INCH,
                "inner_diameter_si": 8.681 * INCH,
                "top_depth_si": 0.0,
                "shoe_depth_si": 1500.0,
                "cement_top_si": 0.0,
            },
            {
                "name": '7" liner',
                "kind": "production_liner",
                "outer_diameter_si": 7.0 * INCH,
                "inner_diameter_si": 6.184 * INCH,
                "top_depth_si": 1400.0,
                "shoe_depth_si": 3000.0,
                "cement_top_si": 1500.0,
                "liner": True,
            },
        ],
        "completion": [
            {"name": "3-1/2\" tubing", "kind": "tubing", "top_depth_si": 0.0, "bottom_depth_si": 2900.0, "outer_diameter_si": 3.5 * INCH, "inner_diameter_si": 2.992 * INCH, "host_string": '7" liner'},
            {"name": "perforations", "kind": "perforation", "top_depth_si": 2700.0, "bottom_depth_si": 2800.0, "host_string": '7" liner'},
        ],
        "formation_tops": {"Shale C": 2600.0, "Sand B": 2400.0},
        "total_depth_si": 3000.0,
        "wellhead_elevation_si": 25.0,
        "ground_level_si": 20.0,
    }
    payload.update(overrides)
    return payload


def test_schematic_model_is_complete_and_consistent(engines):
    _, result = engines.execute("schematic.well_model", _schematic_input())
    output = result.outputs
    assert result.is_feasible is True
    assert [string["name"] for string in output.strings] == ['13-3/8" surface', '9-5/8" intermediate', '7" liner']
    assert output.total_depth_si == 3000.0
    assert output.render_hints["depth_axis"] == "increasing_downward"
    assert output.render_hints["units"] == "SI (metres)"
    assert [top["formation"] for top in output.formation_tops] == ["Sand B", "Shale C"]  # sorted by depth


def test_string_capacities_are_derived_from_the_inner_diameter(engines):
    _, result = engines.execute("schematic.well_model", _schematic_input())
    liner = next(string for string in result.outputs.strings if string["name"] == '7" liner')
    assert liner["capacity_m3_per_m"] == pytest.approx(math.pi / 4 * (6.184 * INCH) ** 2, rel=1e-12)


def test_annuli_are_derived_from_enclosing_geometry(engines):
    _, result = engines.execute("schematic.well_model", _schematic_input())
    annuli = {annulus.name: annulus for annulus in result.outputs.annuli}
    assert '7" liner/open hole' in annuli
    barefoot = annuli['7" liner/open hole']
    # open hole annulus spans from the base of the previous string (1400 m liner top region) to the shoe
    assert barefoot.to_depth_si == pytest.approx(3000.0)
    assert barefoot.equivalent_diameter_si == pytest.approx((8.5 - 7.0) * INCH, rel=1e-6)
    expected_capacity = math.pi / 4 * ((8.5 * INCH) ** 2 - (7.0 * INCH) ** 2)
    assert barefoot.capacity_m3_per_m == pytest.approx(expected_capacity, rel=1e-12)
    assert '7" liner/9-5/8" intermediate' in annuli


def test_casing_that_does_not_fit_the_hole_is_rejected(engines):
    payload = _schematic_input()
    payload["casing_strings"][2]["outer_diameter_si"] = 9.0 * INCH  # 9" casing in an 8.5" hole
    _, result = engines.execute("schematic.well_model", payload)
    assert any(violation.name == "string_does_not_fit" for violation in result.outputs.issues)
    assert result.is_feasible is False


def test_two_strings_shoed_in_the_same_hole_section_are_rejected(engines):
    """A shoe belongs at the bottom of its own hole section; two shoes in one section is a model error."""
    payload = _schematic_input()
    payload["casing_strings"][1]["shoe_depth_si"] = 400.0  # inside the 17-1/2" section
    _, result = engines.execute("schematic.well_model", payload)
    assert any(violation.name == "multiple_shoes_in_section" for violation in result.outputs.issues)


def test_duplicate_shoe_depths_are_rejected(engines):
    payload = _schematic_input()
    payload["casing_strings"][2]["shoe_depth_si"] = 1500.0
    _, result = engines.execute("schematic.well_model", payload)
    assert any(violation.name == "duplicate_shoe_depths" for violation in result.outputs.issues)


def test_shoe_on_a_section_boundary_belongs_to_the_section_above(engines):
    """A 13-3/8" shoe at 500 m is set in the 17-1/2" section, not in the 12-1/4" hole below it."""
    _, result = engines.execute("schematic.well_model", _schematic_input())
    assert not any(violation.name == "string_does_not_fit" for violation in result.outputs.issues)


def test_tubing_may_span_several_strings(engines):
    """Tubing hung at surface and landing in a liner passes through more than one host."""
    _, result = engines.execute("schematic.well_model", _schematic_input())
    assert not any(violation.name in {"completion_outside_host", "completion_does_not_fit"} for violation in result.outputs.issues)


def test_completion_outside_its_host_string_is_rejected(engines):
    payload = _schematic_input()
    payload["completion"][1]["bottom_depth_si"] = 3100.0  # beyond the liner shoe
    _, result = engines.execute("schematic.well_model", payload)
    assert any(violation.name == "completion_outside_host" for violation in result.outputs.issues)


def test_completion_that_does_not_fit_its_host_is_rejected(engines):
    payload = _schematic_input()
    payload["completion"][0]["outer_diameter_si"] = 6.5 * INCH  # 6.5" tubing inside 6.184" liner ID
    _, result = engines.execute("schematic.well_model", payload)
    assert any(violation.name == "completion_does_not_fit" for violation in result.outputs.issues)


def test_total_depth_above_the_deepest_element_is_rejected(engines):
    payload = _schematic_input(total_depth_si=2000.0)
    _, result = engines.execute("schematic.well_model", payload)
    assert any(violation.name == "total_depth_above_equipment" for violation in result.outputs.issues)


def test_missing_hole_sections_downgrade_the_model_with_a_warning(engines):
    payload = _schematic_input(hole_sections=[])
    _, result = engines.execute("schematic.well_model", payload)
    assert any("no hole sections" in warning for warning in result.warnings)
    # casing-in-casing annuli are still derived from the strings; open-hole annuli cannot be
    assert result.outputs.annuli
    assert all("open hole" not in annulus.name for annulus in result.outputs.annuli)
