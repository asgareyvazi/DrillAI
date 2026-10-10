"""The asset domain rules on their own: vocabulary, life cycle, identity and the ledger helpers.

These are the assertions that do not need a database. They exist because the failures they describe
are the ones that used to pass validation: a well type nothing recognised, a life-cycle edge that
skipped the states in between, a flag a client could set independently of the record it describes.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from drillai.assets.lifecycle import (
    SECTION_TRANSITIONS,
    WELL_TRANSITIONS,
    WELLBORE_TRANSITIONS,
    allowed_transitions,
    plan_section_transition,
    plan_well_transition,
    plan_wellbore_transition,
)
from drillai.assets.vocabulary import (
    DATUMS,
    DEFAULT_ELEVATION_DATUM,
    DEFAULT_SECTION_KIND,
    DEFAULT_WELL_TYPE,
    DEFAULT_WELLBORE_PURPOSE,
    SECTION_AS_DRILLED_FIELDS,
    SECTION_KINDS,
    SECTION_NUMBER_SEMANTICS,
    SECTION_STATUSES,
    WELL_STATUSES,
    WELL_TYPES,
    WELLBORE_PURPOSES,
    WELLBORE_STATUSES,
    canonical_choice,
    has_choice,
)
from drillai.core.audit import audit_identity_of, snapshot
from drillai.core.errors import ValidationFailed
from drillai.core.idempotency import fingerprint
from drillai.security.rbac import SYSTEM_ROLES, principal_from_roles

VOCABULARIES = (
    ("well_type", WELL_TYPES, DEFAULT_WELL_TYPE),
    ("status", WELL_STATUSES, "planned"),
    ("purpose", WELLBORE_PURPOSES, DEFAULT_WELLBORE_PURPOSE),
    ("section kind", SECTION_KINDS, DEFAULT_SECTION_KIND),
    ("datum", DATUMS, DEFAULT_ELEVATION_DATUM),
    ("wellbore status", WELLBORE_STATUSES, "planned"),
    ("section status", SECTION_STATUSES, "planned"),
)


# --------------------------------------------------------------------------- vocabulary


@pytest.mark.parametrize(("label", "vocabulary", "default"), VOCABULARIES)
def test_every_default_is_a_member_of_its_own_vocabulary(label, vocabulary, default):
    """The property the platform used to violate: ``development`` was the default and not a member."""
    assert default in vocabulary, f"the {label} default is not an accepted {label}"


@pytest.mark.parametrize(("label", "vocabulary", "default"), VOCABULARIES)
def test_no_vocabulary_contains_duplicates_or_empty_values(label, vocabulary, default):
    assert len(set(vocabulary)) == len(vocabulary), f"{label} repeats a value"
    assert all(value and value == value.strip() for value in vocabulary), label


def test_the_drifted_values_are_refused_with_the_accepted_set():
    """``development`` and ``production`` are the two values the old API accepted and never stored."""
    with pytest.raises(ValidationFailed) as well_type:
        canonical_choice("development", WELL_TYPES, field="well_type")
    assert well_type.value.details["allowed"] == sorted(WELL_TYPES)
    assert well_type.value.details["value"] == "development"

    with pytest.raises(ValidationFailed) as purpose:
        canonical_choice("production", WELLBORE_PURPOSES, field="purpose")
    assert purpose.value.details["allowed"] == sorted(WELLBORE_PURPOSES)


def test_an_omitted_value_takes_the_canonical_default_and_an_empty_string_is_omitted():
    assert (
        canonical_choice(None, WELL_TYPES, field="well_type", default=DEFAULT_WELL_TYPE) == DEFAULT_WELL_TYPE
    )
    assert canonical_choice("", WELL_TYPES, field="well_type", default=DEFAULT_WELL_TYPE) == DEFAULT_WELL_TYPE


def test_a_required_choice_with_no_default_fails_closed():
    with pytest.raises(ValidationFailed) as required:
        canonical_choice(None, WELL_TYPES, field="well_type")
    assert required.value.details["allowed"] == sorted(WELL_TYPES)


def test_has_choice_treats_none_as_absent_rather_than_valid():
    assert has_choice("exploration", WELL_TYPES) is True
    assert has_choice(None, WELL_TYPES) is False
    assert has_choice("development", WELL_TYPES) is False


def test_every_recorded_section_number_is_classified_and_the_classes_are_the_documented_ones():
    """A depth readout is only meaningful if the reader knows which kind of number it is."""
    assert set(SECTION_NUMBER_SEMANTICS.values()) <= {"plan", "actual", "computed", "interpreted", "progress"}
    # The two depths that must never be confused with each other, stated as data:
    assert SECTION_NUMBER_SEMANTICS["planned_bottom_md_si"] == "plan"
    assert SECTION_NUMBER_SEMANTICS["current_md_si"] == "progress"
    assert SECTION_NUMBER_SEMANTICS["actual_bottom_md_si"] == "actual"
    # An interpretation read off a measurement is not a design assumption.
    assert SECTION_NUMBER_SEMANTICS["lot_fit_equivalent_mw_si"] == "interpreted"


def test_the_as_drilled_fields_are_the_measured_ones():
    assert set(SECTION_AS_DRILLED_FIELDS) == {"actual_top_md_si", "actual_bottom_md_si", "current_md_si"}
    assert all(
        SECTION_NUMBER_SEMANTICS[field] in {"actual", "progress"} for field in SECTION_AS_DRILLED_FIELDS
    )


# --------------------------------------------------------------------------- lifecycle


def test_the_well_life_cycle_is_a_real_graph_with_the_states_the_platform_uses():
    assert set(WELL_TRANSITIONS) == set(WELL_STATUSES)
    for state, targets in WELL_TRANSITIONS.items():
        assert targets <= set(WELL_STATUSES), f"{state} points at a state that does not exist"
        assert state not in targets, f"{state} transitions to itself"


def test_terminal_states_are_terminal():
    assert WELL_TRANSITIONS["p&a"] == frozenset()
    assert WELL_TRANSITIONS["abandoned"] <= {"p&a"}, "an abandoned well may only move on to be plugged"


def test_no_transition_skips_the_states_in_between():
    """A well that was never spudded cannot be suspended, and a planned well cannot already produce."""
    # The plan refuses rather than planning: raising is how "not a legal edge" is expressed.
    with pytest.raises(ValidationFailed):
        plan_well_transition(well_id="wel_1", current="planned", requested="producing")
    with pytest.raises(ValidationFailed):
        plan_well_transition(well_id="wel_1", current="planned", requested="shut_in")


def test_a_legal_transition_plans_the_new_state_and_names_the_old_one():
    plan = plan_well_transition(well_id="wel_1", current="planned", requested="drilling")
    assert plan.to_status == "drilling"
    assert plan.from_status == "planned"
    assert plan.subject == "well"
    assert plan.subject_id == "wel_1"


def test_an_unknown_target_can_never_traverse_the_graph():
    """Whatever a caller invents is refused by the edges, because it is in no target set.

    The vocabulary check that produces the friendlier "not a recognised value, here are the accepted
    ones" message happens a layer up (``canonical_choice`` in the service), so the *state machine*
    cannot depend on the caller having passed through it.
    """
    with pytest.raises(ValidationFailed) as unknown:
        plan_well_transition(well_id="wel_1", current="planned", requested="producing_soon")
    assert "producing_soon" not in unknown.value.details["allowed"]
    assert unknown.value.details["current"] == "planned"


def test_suspension_and_reinstatement_cycle_without_being_a_one_way_door():
    assert "suspended" in WELL_TRANSITIONS["producing"]
    assert "producing" in WELL_TRANSITIONS["suspended"]
    assert "drilling" in WELL_TRANSITIONS["suspended"]


def test_allowed_transitions_returns_the_legal_next_states_sorted_for_a_stable_menu():
    assert allowed_transitions(WELL_TRANSITIONS, "planned") == sorted(WELL_TRANSITIONS["planned"])
    assert allowed_transitions(WELL_TRANSITIONS, "p&a") == []
    assert allowed_transitions(WELL_TRANSITIONS, "not_a_state") == []


def test_wellbore_and_section_life_cycles_cover_their_vocabularies():
    assert set(WELLBORE_TRANSITIONS) == set(WELLBORE_STATUSES)
    assert set(SECTION_TRANSITIONS) == set(SECTION_STATUSES)
    assert (
        plan_wellbore_transition(wellbore_id="wlb_1", current="planned", requested="drilling").to_status
        == "drilling"
    )
    assert (
        plan_section_transition(section_id="sec_1", current="planned", requested="drilling").to_status
        == "drilling"
    )
    # A planned section may be abandoned — the programme can drop a hole that was never drilled — but a
    # drilled section is finished for good: re-opening it is a reaming or a sidetrack with its own
    # record, not a status change.
    assert (
        plan_section_transition(section_id="sec_1", current="planned", requested="abandoned").to_status
        == "abandoned"
    )
    assert allowed_transitions(SECTION_TRANSITIONS, "drilled") == []
    with pytest.raises(ValidationFailed):
        plan_section_transition(section_id="sec_1", current="drilled", requested="drilling")


# --------------------------------------------------------------------------- audit helpers


def test_the_actor_columns_come_from_the_principal_and_never_claim_a_user_by_default():
    assert audit_identity_of(None) == {"actor_kind": "system", "actor_id": None, "actor_display": None}
    principal = principal_from_roles(
        principal_id="usr_1", org_id="org_1", roles=[SYSTEM_ROLES["engineer"]], display_name="Ada"
    )
    assert audit_identity_of(principal) == {"actor_kind": "user", "actor_id": "usr_1", "actor_display": "Ada"}


def test_a_snapshot_is_json_safe_and_never_copies_the_free_form_bag():
    class Row:
        id = "wel_1"
        name = "VAL-1"
        # Deliberately hostile values: a naive datetime and an unserialisable object.
        updated_at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
        attributes = {"anything": object()}
        blob = object()

    snap = snapshot(Row(), ("id", "name", "updated_at", "attributes"))
    json.dumps(snap)  # would raise if the snapshot were not JSON-safe
    assert snap["id"] == "wel_1"
    assert snap["updated_at"] == "2026-01-01T12:00:00+00:00"
    assert "attributes" not in snap, "the free-form bag is not part of the ledger picture"

    # An object that cannot be serialised is described rather than allowed to fail the write it was
    # recording — and it is never silently dropped.
    limited = snapshot(Row(), ("id", "blob"))
    json.dumps(limited)
    assert limited["blob"] == "<unserialisable object>"


def test_the_fingerprint_is_stable_across_key_order_and_changes_with_the_body():
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})
