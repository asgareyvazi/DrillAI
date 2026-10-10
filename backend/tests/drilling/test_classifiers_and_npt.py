"""The drilling classifiers and the NPT read-model.

These are the pure, fast tests for the layer that turns report text into NPT codes, categories and
controllability. The invariants here are the ones that decide whether a loss is reported as
controllable, uncontrollable or unknown — a distinction the product promises never to blur.
"""

from __future__ import annotations

import asyncio

import pytest

from drillai.core.errors import ValidationFailed
from drillai.drilling.advisor import ADVISOR_QUESTIONS, OperationsAdvisor
from drillai.drilling.classifiers import (
    NPT_SIGNALS,
    STANDARD_NPT_CODES,
    classify_npt,
    classify_operation_kind,
    iter_signals,
)
from drillai.drilling.npt import NPT_BASES, NptService
from drillai.drilling.reporting import REPORT_KINDS, ReportingService


def _catalogue() -> dict[str, dict[str, object]]:
    return {str(row["code"]): row for row in STANDARD_NPT_CODES}


def test_every_npt_signal_emits_a_catalogue_code():
    """A signal whose code is not in the catalogue silently loses its controllability judgement."""
    catalogue = _catalogue()
    missing = [
        code for _signal, code, _category, _controllable in NPT_SIGNALS if code not in catalogue
    ]
    assert missing == [], f"signals emitting codes the catalogue does not define: {missing}"


def test_signal_controllability_agrees_with_the_catalogue():
    catalogue = _catalogue()
    disagreements = [
        (code, controllable, catalogue[code]["controllable"])
        for _signal, code, _category, controllable in NPT_SIGNALS
        if code in catalogue and bool(catalogue[code]["controllable"]) is not bool(controllable)
    ]
    assert disagreements == []


def test_signal_category_agrees_with_the_catalogue():
    catalogue = _catalogue()
    disagreements = [
        (code, category, catalogue[code]["category"])
        for _signal, code, category, _controllable in NPT_SIGNALS
        if code in catalogue and catalogue[code]["category"] != category
    ]
    assert disagreements == []


def test_the_catalogue_covers_the_documented_categories():
    categories = {str(row["category"]) for row in STANDARD_NPT_CODES}
    assert {"stuck_pipe", "lost_circulation", "well_control", "waiting", "weather"} <= categories
    assert all(row["name"] for row in STANDARD_NPT_CODES)
    # Codes are unique: two rows with one code would make the join ambiguous.
    codes = [str(row["code"]) for row in STANDARD_NPT_CODES]
    assert len(codes) == len(set(codes))


@pytest.mark.parametrize(
    ("text", "code", "category", "controllable"),
    [
        ("Stuck pipe while POOH at 2408 m", "STUCK_PIPE", "stuck_pipe", True),
        ("Differentially stuck while tripping", "STUCK_PIPE_DIFF", "stuck_pipe", True),
        ("Lost circulation 15 bbl/hr; LCM pill pumped", "LOST_CIRC", "lost_circulation", True),
        ("Kick taken and well shut-in", "WELL_CONTROL", "well_control", True),
        ("Bit balling after 40 m", "BIT_FAILURE", "equipment_failure", True),
        ("Waiting on weather", "WAITING", "waiting", False),
        ("Waiting on supervisor decision", "WAITING_DECISION", "waiting", False),
        ("Tight hole while reaming", "HOLE_PROBLEM_TIGHT", "hole_problems", True),
    ],
)
def test_classify_npt_returns_code_and_category_separately(text, code, category, controllable):
    classification, emitted, operator_controllable = classify_npt(text)
    assert emitted == code
    assert classification.label == category
    assert operator_controllable is controllable
    assert classification.matched_text
    assert 0.0 < classification.confidence <= 0.95


def test_classify_npt_is_silent_when_there_is_no_signal():
    classification, code, controllable = classify_npt("continued drilling ahead")
    # Absence of a signal is not evidence of absence of NPT: the caller is told "nothing matched",
    # never "no loss happened".
    assert classification.label is None
    assert code is None
    assert controllable is None
    assert classification.matched_text is None


def test_classification_is_deterministic():
    text = "Stuck pipe and lost circulation while waiting on weather"
    assert classify_npt(text) == classify_npt(text)


def test_specific_signal_wins_over_the_generic_one():
    _classification, code, _controllable = classify_npt("Differentially stuck pipe after 12 h")
    assert code == "STUCK_PIPE_DIFF"


def test_conflicting_signals_are_reported_as_alternates():
    classification, code, _controllable = classify_npt(
        "Stuck pipe while pulling out of hole, then a fish left in hole"
    )
    assert code == "STUCK_PIPE"
    assert classification.alternates, "the losing signals must be visible, not discarded"


def test_operation_kind_classification_stays_separate_from_npt():
    kind = classify_operation_kind("Drilling ahead 8-1/2 in section from 2350 m to 2410 m")
    assert kind.label == "drilling"
    assert classify_operation_kind("").label is None


def test_iter_signals_exposes_codes_and_categories():
    rows = list(iter_signals())
    assert ("npt", "STUCK_PIPE") in rows
    assert ("npt_category", "stuck_pipe") in rows
    assert all(label for _kind, label in rows)


def test_validation_errors_are_platform_errors_not_bare_value_errors():
    """A bad client argument must be a 422 with the valid set, never an unhandled 500."""
    service = NptService(session=None, org_id="org_test")  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed) as npt_error:
        asyncio.run(service.summarise("well_unknown", basis="guessed"))
    assert npt_error.value.details["known"] == list(NPT_BASES)
    assert npt_error.value.http_status == 422

    reporting = ReportingService(session=None, org_id="org_test")  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed) as report_error:
        asyncio.run(reporting.build("not_a_report", "well_unknown"))
    assert report_error.value.details["known"] == sorted(REPORT_KINDS)

    advisor = OperationsAdvisor(session=None, org_id="org_test")  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed) as advisor_error:
        asyncio.run(advisor.ask("well_unknown", "will_the_well_make_money"))
    assert advisor_error.value.details["known"] == sorted(ADVISOR_QUESTIONS)
