"""Life-cycle transitions for the asset hierarchy.

A status column that any caller can set to any string is not a life cycle; it is a text field. This
module is the part that makes it one: the legal transitions are declared once, as data, and every
transition — API, workflow node or script — is checked against the same table.

The tables encode domain rules that are easy to get wrong in a router:

* **Nothing is implicit.** A status only moves along a declared edge; ``planned → producing`` is
  refused even though both states exist.
* **``abandoned`` and ``p&a`` are one-way.** A well that has been abandoned and a well that has been
  plugged and abandoned do not return to drilling through this API. Re-entry is a *wellbore*
  operation (see the lineage rules), not a well-status rollback, and modelling it as one would let a
  single field edit erase the fact that the well was ever abandoned.
* **``permitting → suspended`` is not an edge.** A well that never spudded is not suspended; it is
  still waiting for a permit, and saying otherwise would put a well that has no hole into the same
  state as one that does.
* **Reinstatement is explicit.** ``suspended`` and ``shut_in`` can return to the states that are
  physically reachable from them, so the common "suspend and resume" cycle is legal without opening
  the door to arbitrary jumps.

The tables are the *domain* half of the guard. Permission is the other half, and it is enforced
before this code runs (``well.lifecycle`` through the action catalogue); neither substitutes for the
other — a well manager who is allowed to change any status is still not allowed to change it to a
state the process cannot reach.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from drillai.core.errors import ValidationFailed

__all__ = [
    "SECTION_TRANSITIONS",
    "WELLBORE_TRANSITIONS",
    "WELL_TRANSITIONS",
    "TransitionPlan",
    "allowed_transitions",
    "plan_section_transition",
    "plan_well_transition",
    "plan_wellbore_transition",
]

# --------------------------------------------------------------------------- well
#
# Read as: "from this state, these states are reachable". `planned` is reachable from nowhere except
# permitting, because a spudded well cannot become un-spudded.
WELL_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"permitting", "drilling"}),
    "permitting": frozenset({"planned", "drilling"}),
    "drilling": frozenset({"completing", "suspended", "intervention", "abandoned"}),
    "completing": frozenset({"producing", "suspended", "intervention", "abandoned"}),
    "producing": frozenset({"shut_in", "intervention", "suspended", "abandoned"}),
    "shut_in": frozenset({"producing", "intervention", "suspended", "abandoned"}),
    "intervention": frozenset({"producing", "drilling", "completing", "suspended", "abandoned"}),
    "suspended": frozenset({"drilling", "completing", "intervention", "producing", "abandoned"}),
    # Terminal: an abandoned well can still be plugged and abandoned, and nothing else.
    "abandoned": frozenset({"p&a"}),
    "p&a": frozenset(),
}

# --------------------------------------------------------------------------- wellbore
# A hole that has been abandoned is not re-opened; a sidetrack is a *new* wellbore whose parent is the
# one that was left. That is the rule the lineage tables enforce, and this one keeps it honest.
# A hole that was never drilled cannot have been *suspended* — suspension means drilling stopped with
# the hole open — but it can be dropped, which is what `planned → abandoned` says. And a hole that has
# been started cannot become un-started: `drilling` does not lead back to `planned`, for the same reason
# a spudded well does not lead back to `planned` in the table above. Correcting a mistaken spud is a
# suspension, which keeps the fact that the hole was started.
WELLBORE_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"drilling", "abandoned"}),
    "drilling": frozenset({"suspended", "abandoned"}),
    "suspended": frozenset({"drilling", "abandoned"}),
    "abandoned": frozenset(),
}

# --------------------------------------------------------------------------- section
SECTION_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"drilling", "abandoned"}),
    "drilling": frozenset({"drilled", "abandoned"}),
    # A section that has been drilled was drilled; re-opening it is a reaming or a sidetrack operation
    # with its own record, not a status change.
    "drilled": frozenset(),
    "abandoned": frozenset(),
}


@dataclass(frozen=True)
class TransitionPlan:
    """A transition that has been checked and can be applied.

    Returned by the ``plan_*`` functions so the caller cannot apply an unchecked edge: the only way to
    obtain a plan is to pass the guard that produces it.
    """

    subject: str
    subject_id: str
    from_status: str
    to_status: str
    changed: list[str] = field(default_factory=lambda: ["status"])
    details: dict[str, Any] = field(default_factory=dict)


def allowed_transitions(transitions: dict[str, frozenset[str]], status: str) -> list[str]:
    """The states reachable from ``status``, sorted for a stable error message and UI hint."""
    return sorted(transitions.get(status, frozenset()))


def _plan(
    transitions: dict[str, frozenset[str]],
    *,
    subject: str,
    subject_id: str,
    current: str,
    requested: str,
) -> TransitionPlan:
    if requested == current:
        raise ValidationFailed(
            f"{subject} is already {current!r}",
            details={
                "subject": subject,
                "id": subject_id,
                "current": current,
                "requested": requested,
                "allowed": allowed_transitions(transitions, current),
            },
        )
    reachable = transitions.get(current)
    if reachable is None:
        # A status outside the vocabulary: the row predates validation or was written around it.
        raise ValidationFailed(
            f"{subject} has an unrecognised status {current!r} and cannot be transitioned",
            details={"subject": subject, "id": subject_id, "current": current, "known": sorted(transitions)},
        )
    if requested not in reachable:
        raise ValidationFailed(
            f"{subject} cannot move from {current!r} to {requested!r}",
            details={
                "subject": subject,
                "id": subject_id,
                "current": current,
                "requested": requested,
                "allowed": allowed_transitions(transitions, current),
            },
        )
    return TransitionPlan(subject=subject, subject_id=subject_id, from_status=current, to_status=requested)


def plan_well_transition(*, well_id: str, current: str, requested: str) -> TransitionPlan:
    """Check a well status change against :data:`WELL_TRANSITIONS`."""
    return _plan(WELL_TRANSITIONS, subject="well", subject_id=well_id, current=current, requested=requested)


def plan_wellbore_transition(*, wellbore_id: str, current: str, requested: str) -> TransitionPlan:
    """Check a wellbore status change against :data:`WELLBORE_TRANSITIONS`."""
    return _plan(
        WELLBORE_TRANSITIONS, subject="wellbore", subject_id=wellbore_id, current=current, requested=requested
    )


def plan_section_transition(*, section_id: str, current: str, requested: str) -> TransitionPlan:
    """Check a section status change against :data:`SECTION_TRANSITIONS`."""
    return _plan(
        SECTION_TRANSITIONS, subject="section", subject_id=section_id, current=current, requested=requested
    )
