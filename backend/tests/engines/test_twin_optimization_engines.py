"""Twin reconciliation and optimization engines.

These tests pin the behaviours that make the two engines trustworthy: a plan-vs-actual comparison
that does not drift from the trajectory maths, and an optimizer that never invents a value and
explains its ranking.
"""

from __future__ import annotations

import pytest

from drillai.engines.registry import load_default_engines


@pytest.fixture(scope="module")
def registry():
    return load_default_engines()


def _survey(*stations: tuple[float, float, float]) -> dict:
    return {
        "stations": [
            {"md_si": md, "inclination_deg": inclination, "azimuth_deg": azimuth}
            for md, inclination, azimuth in stations
        ]
    }


# --------------------------------------------------------------------------- twin


def test_identical_surveys_report_no_deviation(registry):
    survey = _survey((0.0, 0.0, 0.0), (1000.0, 0.0, 0.0), (1300.0, 30.0, 0.0))
    _spec, result = registry.execute("twin.reconciliation", {"planned": survey, "actual": survey})
    outputs = result.outputs
    assert outputs.tracking_status == "on_plan"
    assert outputs.max_lateral_offset_si == pytest.approx(0.0, abs=1e-6)
    assert outputs.max_vertical_offset_si == pytest.approx(0.0, abs=1e-6)
    assert outputs.stations_out_of_tolerance == 0
    assert outputs.station_count == outputs.compared_station_count == 3
    assert not result.violations


def test_lateral_deviation_beyond_tolerance_raises_violation(registry):
    planned = _survey((0.0, 0.0, 0.0), (1000.0, 0.0, 0.0))
    # Build an actual well that walks 40 m east over the same interval.
    actual = {
        "stations": [
            {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 90.0},
            {"md_si": 1000.0, "inclination_deg": 4.0, "azimuth_deg": 90.0},
        ]
    }
    _spec, result = registry.execute(
        "twin.reconciliation",
        {"planned": planned, "actual": actual, "lateral_tolerance_si": 20.0, "vertical_tolerance_si": 20.0},
    )
    names = {violation.name for violation in result.violations}
    assert "deviation_exceeds_tolerance" in names
    assert result.outputs.tracking_status == "out_of_tolerance"
    assert result.outputs.stations_out_of_tolerance >= 1
    assert result.outputs.max_lateral_offset_si > 20.0


def test_offsets_inside_the_watch_band_are_flagged_not_failed(registry):
    # 25 m of lateral offset against a 30 m tolerance == 83 % -> watch, not violation.
    planned = _survey((0.0, 0.0, 0.0), (500.0, 0.0, 0.0))
    # 5.739° over 500 m gives 250·(sin 0 + sin 5.739°) ≈ 25 m of eastward offset.
    actual = _survey((0.0, 0.0, 0.0), (500.0, 5.739, 90.0))
    _spec, result = registry.execute(
        "twin.reconciliation",
        {"planned": planned, "actual": actual, "lateral_tolerance_si": 30.0, "vertical_tolerance_si": 15.0},
    )
    statuses = {station.status for station in result.outputs.stations}
    assert "watch" in statuses
    assert result.outputs.tracking_status == "watch"
    assert not result.violations


def test_short_actual_survey_warns_about_uncovered_stations(registry):
    planned = _survey((0.0, 0.0, 0.0), (1000.0, 0.0, 0.0), (2000.0, 0.0, 0.0))
    actual = _survey((0.0, 0.0, 0.0), (1000.0, 0.0, 0.0))
    _spec, result = registry.execute("twin.reconciliation", {"planned": planned, "actual": actual})
    assert result.outputs.compared_station_count == 2
    assert any("does not cover" in warning for warning in result.warnings)
    assert result.outputs.progress_fraction == pytest.approx(0.5)


def test_tie_in_anchor_absorbs_a_constant_lateral_shift(registry):
    stations = [
        {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
        {"md_si": 1000.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
    ]
    # The actual survey is the plan but tied in 40 m north (a different slot / KB).
    actual = {"stations": stations, "tie_in": {"ns_si": 40.0, "ew_si": 0.0, "tvd_si": 0.0}}
    payload = {"planned": {"stations": stations}, "actual": actual}
    _spec, anchored = registry.execute(
        "twin.reconciliation", {**payload, "anchor_actual_to_planned_start": True}
    )
    assert anchored.outputs.max_lateral_offset_si == pytest.approx(0.0, abs=1e-6)
    assert any("anchored" in note for note in anchored.outputs.notes)

    # Without the anchor the tie-in shift is reported as deviation, and the mode used is stated
    # in the assumptions: the engine never silently hides a datum difference.
    _spec, unanchored = registry.execute(
        "twin.reconciliation", {**payload, "anchor_actual_to_planned_start": False}
    )
    assert unanchored.outputs.max_lateral_offset_si == pytest.approx(40.0, abs=1e-6)
    assert unanchored.assumptions_applied != anchored.assumptions_applied


def test_twin_reconciliation_is_deterministic(registry):
    planned = _survey((0.0, 0.0, 0.0), (1000.0, 10.0, 45.0), (1500.0, 30.0, 45.0))
    actual = _survey((0.0, 0.0, 0.0), (1000.0, 11.0, 46.0), (1500.0, 28.0, 44.0))
    payload = {"planned": planned, "actual": actual}
    first = registry.execute("twin.reconciliation", payload)[1].outputs.model_dump()
    second = registry.execute("twin.reconciliation", payload)[1].outputs.model_dump()
    assert first == second


# --------------------------------------------------------------------------- sweep


def test_grid_sweep_produces_the_full_factorial(registry):
    _spec, result = registry.execute(
        "optimization.sweep",
        {
            "parameters": [
                {"name": "mud_weight_si", "min_si": 1100.0, "max_si": 1300.0, "steps": 3, "unit": "kg/m3"},
                {"name": "flow_si", "min_si": 0.02, "max_si": 0.04, "steps": 4},
            ],
            "method": "grid",
        },
    )
    assert result.outputs.candidate_count == 12
    assert result.outputs.truncated is False
    weights = sorted({candidate.values["mud_weight_si"] for candidate in result.outputs.candidates})
    assert weights == [1100.0, 1200.0, 1300.0]
    assert len({candidate.candidate_id for candidate in result.outputs.candidates}) == 12


def test_grid_sweep_truncates_and_warns_instead_of_exploding(registry):
    _spec, result = registry.execute(
        "optimization.sweep",
        {
            "parameters": [
                {"name": "a", "min_si": 0.0, "max_si": 10.0, "steps": 10},
                {"name": "b", "min_si": 0.0, "max_si": 10.0, "steps": 10},
                {"name": "c", "min_si": 0.0, "max_si": 10.0, "steps": 10},
            ],
            "max_candidates": 100,
        },
    )
    assert result.outputs.truncated is True
    assert result.outputs.candidate_count == 100
    assert any("truncated" in warning for warning in result.warnings)


def test_halton_sweep_is_deterministic_and_within_bounds(registry):
    payload = {
        "parameters": [
            {"name": "wob_si", "min_si": 20_000.0, "max_si": 120_000.0, "scale": "linear"},
            {"name": "rpm", "min_si": 60.0, "max_si": 180.0, "scale": "log"},
        ],
        "method": "halton",
        "samples": 25,
    }
    first = registry.execute("optimization.sweep", payload)[1].outputs
    second = registry.execute("optimization.sweep", payload)[1].outputs
    assert first.model_dump() == second.model_dump()
    assert first.candidate_count == 25
    for candidate in first.candidates:
        assert 20_000.0 <= candidate.values["wob_si"] <= 120_000.0
        assert 60.0 <= candidate.values["rpm"] <= 180.0


def test_sweep_rejects_duplicate_parameters_and_bad_log_ranges(registry):
    _spec, duplicates = registry.execute(
        "optimization.sweep",
        {"parameters": [{"name": "x", "min_si": 0.0, "max_si": 1.0}, {"name": "x", "min_si": 2.0, "max_si": 3.0}]},
    )
    assert duplicates.violations[0].name == "sweep_duplicate_parameter"
    _spec, bad_log = registry.execute(
        "optimization.sweep",
        {"parameters": [{"name": "y", "min_si": 0.0, "max_si": 10.0, "scale": "log"}]},
    )
    assert bad_log.violations[0].name == "sweep_invalid_range"
    _spec, unknown = registry.execute(
        "optimization.sweep",
        {"parameters": [{"name": "y", "min_si": 0.0, "max_si": 10.0}], "method": "genetic"},
    )
    assert unknown.violations[0].name == "sweep_unknown_method"


# --------------------------------------------------------------------------- pareto


CANDIDATES = [
    {"candidate_id": "cheap_dirty", "values": {"mw": 1200.0}, "objectives": {"cost": 100.0, "risk": 9.0}},
    {"candidate_id": "balanced", "values": {"mw": 1250.0}, "objectives": {"cost": 130.0, "risk": 4.0}},
    {"candidate_id": "expensive_safe", "values": {"mw": 1300.0}, "objectives": {"cost": 180.0, "risk": 1.0}},
    {"candidate_id": "worst", "values": {"mw": 1350.0}, "objectives": {"cost": 200.0, "risk": 10.0}},
]
OBJECTIVES = [
    {"key": "cost", "sense": "minimize", "weight": 1.0},
    {"key": "risk", "sense": "minimize", "weight": 1.0},
]


def test_pareto_frontier_excludes_dominated_candidates(registry):
    _spec, result = registry.execute(
        "optimization.pareto", {"candidates": CANDIDATES, "objectives": OBJECTIVES}
    )
    outputs = result.outputs
    assert outputs.feasible_count == 4
    assert "worst" not in outputs.pareto_frontier
    assert set(outputs.pareto_frontier) == {"cheap_dirty", "balanced", "expensive_safe"}
    dominated = next(entry for entry in outputs.ranking if entry.candidate_id == "worst")
    assert dominated.on_frontier is False
    assert "balanced" in dominated.dominated_by or "expensive_safe" in dominated.dominated_by


def test_weighting_changes_the_ranking_and_is_reported(registry):
    _spec, result = registry.execute(
        "optimization.pareto",
        {
            "candidates": CANDIDATES,
            "objectives": [
                {"key": "cost", "sense": "minimize", "weight": 1.0},
                {"key": "risk", "sense": "minimize", "weight": 5.0},
            ],
        },
    )
    assert result.outputs.ranking[0].candidate_id == "expensive_safe"
    assert result.outputs.ranking[0].score == pytest.approx(1.0, abs=0.2)
    assert any("weight" in assumption for assumption in result.assumptions_applied)
    assert result.outputs.explanation


def test_infeasible_candidates_are_excluded_with_reasons(registry):
    candidates = [
        *CANDIDATES,
        {"candidate_id": "over_pressure", "values": {"mw": 1400.0}, "objectives": {"cost": 150.0, "risk": 2.0}, "violations": ["pore pressure margin below 0.5 ppg"]},
        {"candidate_id": "incomplete", "values": {"mw": 1230.0}, "objectives": {"cost": 120.0}},
    ]
    _spec, result = registry.execute("optimization.pareto", {"candidates": candidates, "objectives": OBJECTIVES})
    assert result.outputs.feasible_count == 4
    assert result.outputs.infeasible_count == 2
    assert not any(entry.candidate_id in {"over_pressure", "incomplete"} for entry in result.outputs.ranking)


def test_no_feasible_candidate_is_an_explicit_violation(registry):
    candidates = [
        {"candidate_id": "a", "objectives": {"cost": 1.0, "risk": 1.0}, "violations": ["barrier gap"]},
        {"candidate_id": "b", "objectives": {"cost": 2.0, "risk": 2.0}, "violations": ["kick margin"]},
    ]
    _spec, result = registry.execute("optimization.pareto", {"candidates": candidates, "objectives": OBJECTIVES})
    assert result.violations[0].name == "no_feasible_candidate"
    assert result.outputs.explanation[0].startswith("no candidate")


def test_trade_offs_and_sensitivity_are_reported(registry):
    candidates = [
        {"candidate_id": f"c{index}", "values": {"mw": 1200.0 + 10.0 * index}, "objectives": {"cost": 100.0 + 5.0 * index, "risk": 10.0 - index}}
        for index in range(6)
    ]
    _spec, result = registry.execute("optimization.pareto", {"candidates": candidates, "objectives": OBJECTIVES})
    trade_offs = {trade_off.objective: trade_off for trade_off in result.outputs.trade_offs}
    assert trade_offs["cost"].best_candidate_id == "c0"
    assert trade_offs["risk"].best_candidate_id == "c5"
    assert trade_offs["cost"].spread == pytest.approx(25.0)
    sensitivities = {(item.parameter, item.objective): item for item in result.outputs.sensitivities}
    assert sensitivities[("mw", "cost")].spearman_rho == pytest.approx(1.0)
    assert sensitivities[("mw", "risk")].spearman_rho == pytest.approx(-1.0)
    assert result.outputs.feasible_ranges["mw"]["min"] == pytest.approx(1200.0)


def test_robust_ranking_penalises_wide_uncertainty(registry):
    candidates = [
        {"candidate_id": "tight", "objectives": {"cost": 100.0, "risk": 5.0}, "uncertainty": {"cost": 1.0}},
        {"candidate_id": "wide", "objectives": {"cost": 100.0, "risk": 5.0}, "uncertainty": {"cost": 40.0}},
        # A third candidate provides the objective span; without it min-max normalisation is
        # degenerate and every robust score would be neutral.
        {"candidate_id": "baseline", "objectives": {"cost": 200.0, "risk": 10.0}, "uncertainty": {"cost": 0.0}},
    ]
    _spec, result = registry.execute(
        "optimization.pareto", {"candidates": candidates, "objectives": OBJECTIVES, "robust": True}
    )
    scores = {entry.candidate_id: entry.robust_score for entry in result.outputs.ranking}
    assert scores["tight"] > scores["wide"]


def test_small_candidate_sets_skip_sensitivity_with_a_note(registry):
    _spec, result = registry.execute(
        "optimization.pareto", {"candidates": CANDIDATES[:2], "objectives": OBJECTIVES}
    )
    assert result.outputs.sensitivities == []
    assert any("sensitivity" in note for note in result.outputs.notes)
