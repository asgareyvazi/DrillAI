"""Who may read telemetry, who may write it, and where the ceiling stops them.

Two layers are tested and they answer different questions. The catalogue tests say what the *model*
grants, role by role, and would fail the moment somebody widened a role to make a screen work. The HTTP
tests say what the *endpoints* actually enforce, against the real application with a catalogued
principal — a catalogue no route consults is a document, not a control.
"""

from __future__ import annotations

from drillai.security.catalog import ensure_platform_actions_registered
from drillai.security.rbac import ROLE_CATALOGUE, SYSTEM_ROLES

#: Every action the telemetry layer introduced, with the level it is registered at. An action added
#: without a line here is a permission nobody decided about.
TELEMETRY_ACTIONS = {
    "timeseries.read": "L0",
    "timeseries.create": "L2",
    "timeseries.append": "L2",
    "timeseries.manage": "L2",
    "alert.read": "L0",
    "alert.acknowledge": "L2",
    "alert.clear": "L2",
    "alert.cancel": "L2",
    "alert.rule_manage": "L2",
    "live.read": "L0",
}

#: Reading the operational picture is not a privilege; acting on it is. Asserted across the whole
#: catalogue rather than for the roles that exist today, because the next role added is the risky one.
ALL_ROLES = tuple(role.key for role in ROLE_CATALOGUE)


# --------------------------------------------------------------------------- the model


def test_every_telemetry_action_is_registered_at_the_level_it_claims() -> None:
    registered_actions = ensure_platform_actions_registered()
    for action, level in TELEMETRY_ACTIONS.items():
        registered = registered_actions.get(action)
        assert registered is not None, f"{action} is not in the action catalogue"
        assert registered.level.value == level, f"{action} is registered at {registered.level.value}"


def test_every_telemetry_action_is_reachable_by_someone() -> None:
    """An action no role holds cannot be exercised — usually a typo in the role that meant to hold it."""

    for action in TELEMETRY_ACTIONS:
        holders = [role.key for role in ROLE_CATALOGUE if role.grants(action)]
        assert holders, f"{action} is granted to no role"


def test_reading_the_monitor_comes_with_reading_a_well() -> None:
    """A role that may look at a well may see what the well is doing. Not being able to is not a control,
    it is a broken page: the well, its channels and its alerts are one answer."""

    for role in ROLE_CATALOGUE:
        if not role.grants("well.read"):
            continue
        for action in ("timeseries.read", "alert.read", "live.read"):
            assert role.grants(action), f"{role.key} may read wells but not {action}"


def test_writing_telemetry_is_not_granted_to_a_reader() -> None:
    """Widening read must never widen write: the roles that read the monitor cannot append measurements,
    and cannot resolve an alert."""

    for key in ("viewer", "auditor"):
        role = SYSTEM_ROLES[key]
        for action in ("timeseries.create", "timeseries.append", "timeseries.manage"):
            assert not role.grants(action), f"{key} must not hold {action}"
        for action in ("alert.acknowledge", "alert.clear", "alert.cancel", "alert.rule_manage"):
            assert not role.grants(action), f"{key} must not hold {action}"


def test_a_data_manager_owns_ingestion_and_does_not_decide_alerts() -> None:
    """The acquisition boundary is theirs — registering a channel and appending to it is what a data
    manager is for. What they may not do is decide an alert: seeing that a threshold was crossed is
    data, deciding that a shift may stand down is engineering."""

    manager = SYSTEM_ROLES["data_manager"]
    assert manager.grants("alert.read")
    assert manager.grants("timeseries.append")
    for action in ("alert.acknowledge", "alert.clear", "alert.cancel"):
        assert not manager.grants(action), f"a data manager must not hold {action}"
