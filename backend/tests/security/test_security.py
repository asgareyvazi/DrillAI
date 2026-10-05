"""Security: action catalogue, action levels, envelopes, RBAC patterns and credentials."""

from __future__ import annotations

import pytest

from drillai.core.errors import ApprovalRequired, PermissionDenied
from drillai.security import catalog as catalogue  # noqa: F401  (registers platform actions)
from drillai.security.actions import (
    ACTION_LEVEL_DESCRIPTIONS,
    ActionLevel,
    Principal,
    authorize,
    envelope_allows,
    level_of,
    permission_matches,
    register_action,
    registered_actions,
)
from drillai.security.passwords import (
    hash_password,
    hash_token,
    needs_rehash,
    new_api_token,
    verify_password,
    verify_token,
)
from drillai.security.rbac import ROLE_CATALOGUE, SYSTEM_ROLES, permissions_match, principal_from_roles

# --------------------------------------------------------------------------- catalogue


def test_every_platform_action_is_registered_with_a_level_and_permission():
    actions = registered_actions()
    assert len(actions) >= 25  # the platform surface, not a smoke test
    for key, action in actions.items():
        assert action.level in ActionLevel
        assert action.description, f"{key} has no description"
        assert action.permission, f"{key} has no permission"


def test_the_catalogue_has_no_duplicate_keys_and_covers_the_expected_domains():
    keys = list(registered_actions())
    assert len(keys) == len(set(keys))
    for prefix in ("context", "document", "engine", "workflow", "twin", "recommendation", "optimization", "offset", "integrity", "approval", "message"):
        assert any(key.startswith(prefix) for key in keys), f"no action registered for {prefix!r}"
    # the destructive/remote capabilities are the ones whose level must never drift down
    for key in ("message.send", "export.create", "artifact.approve", "approval.decide"):
        assert registered_actions()[key].level is ActionLevel.EXECUTE_WITH_APPROVAL
    for key in ("monitor.notify", "report.distribute", "rig.write"):
        assert registered_actions()[key].level is ActionLevel.AUTHORIZED_AUTOMATION
    # Starting a workflow run is draft-level on purpose: the definition is versioned and every
    # node carries its own level, so the side effect is gated where it happens (an L4 node
    # suspends the run and raises an approval naming exactly what will be done). See
    # docs/ADR for the decision record.
    assert registered_actions()["workflow.run"].level is ActionLevel.DRAFT


def test_every_registered_action_is_reachable_by_at_least_one_role():
    """No dead actions: an action nobody can ever be authorized for is either a role gap or a bug.

    This guards the *combination* of the catalogue and the role catalogue — the kind of drift
    that only shows up in production as "the supervisor cannot approve anything".
    """
    from drillai.security.rbac import ROLE_CATALOGUE

    unreachable = [
        (key, action.level.value, action.permission)
        for key, action in registered_actions().items()
        if not any(
            role.grants(action.permission) and role.max_action_level.rank >= action.level.rank
            for role in ROLE_CATALOGUE
        )
    ]
    assert unreachable == []


def test_deciding_an_approval_does_not_require_another_approval():
    """The decision is the human gate; requiring a prior approval would be unbounded recursion."""
    from drillai.security.rbac import ROLE_CATALOGUE

    supervisor = next(role for role in ROLE_CATALOGUE if role.key == "drilling_supervisor")
    principal = principal_from_roles(principal_id="usr_sup", org_id="org_1", roles=[supervisor])
    assert authorize(principal, "approval.decide") is ActionLevel.EXECUTE_WITH_APPROVAL
    # ...and an approver cannot decide without the recorded permission or the level ceiling.
    viewer = next(role for role in ROLE_CATALOGUE if role.key == "viewer")
    with pytest.raises(PermissionDenied):
        authorize(principal_from_roles(principal_id="usr_v", org_id="org_1", roles=[viewer]), "approval.decide")


def test_action_levels_are_ordered_and_documented():
    assert ActionLevel.OBSERVE.rank == 0
    assert ActionLevel.AUTHORIZED_AUTOMATION.rank == 5
    assert set(ACTION_LEVEL_DESCRIPTIONS) == set(ActionLevel)
    for description in ACTION_LEVEL_DESCRIPTIONS.values():
        assert len(description) > 20  # the meaning of a level must be written down


def test_unknown_actions_default_to_the_safe_level():
    assert level_of("nobody.registered.this") is ActionLevel.EXECUTE_WITH_APPROVAL


def test_duplicate_registration_is_refused_unless_explicitly_replaced():
    register_action("test.action", ActionLevel.OBSERVE, "test only")
    with pytest.raises(ValueError):
        register_action("test.action", ActionLevel.OBSERVE, "test only")
    replaced = register_action("test.action", ActionLevel.PROPOSE, "test only", replace=True)
    assert replaced.level is ActionLevel.PROPOSE


# --------------------------------------------------------------------------- authorization


def _principal(level: ActionLevel, permissions: set[str] | None = None, kind: str = "user") -> Principal:
    return Principal(
        id="usr_test",
        kind=kind,
        permissions=frozenset(permissions or {"**"}),
        max_action_level=level,
    )


def test_observe_and_advise_actions_need_no_approval():
    assert authorize(_principal(ActionLevel.ADVISE), "context.read") is ActionLevel.OBSERVE
    assert authorize(_principal(ActionLevel.ADVISE), "recommendation.create") is ActionLevel.ADVISE


def test_ceiling_below_the_level_is_denied():
    with pytest.raises(PermissionDenied):
        authorize(_principal(ActionLevel.ADVISE), "engine.run")


def test_missing_permission_is_denied_even_with_a_high_ceiling():
    with pytest.raises(PermissionDenied) as caught:
        authorize(_principal(ActionLevel.AUTHORIZED_AUTOMATION, {"well.read"}), "engine.run")
    assert "engine.run" in str(caught.value)


def test_l4_requires_a_recorded_approval():
    principal = _principal(ActionLevel.EXECUTE_WITH_APPROVAL)
    with pytest.raises(ApprovalRequired):
        authorize(principal, "message.send")
    assert authorize(principal, "message.send", approval_id="apr_1") is ActionLevel.EXECUTE_WITH_APPROVAL


def test_l5_requires_an_explicit_bounded_envelope():
    principal = _principal(ActionLevel.AUTHORIZED_AUTOMATION)

    def l5():
        register_action("test.automation", ActionLevel.AUTHORIZED_AUTOMATION, "test automation", replace=True)

    l5()
    with pytest.raises(ApprovalRequired):
        authorize(principal, "test.automation")
    with pytest.raises(ApprovalRequired):
        authorize(principal, "test.automation", envelope={"enabled": True})  # no bounds declared
    with pytest.raises(ApprovalRequired):
        # bounded for this value, but the envelope is bound to another key: nothing is authorized
        authorize(
            principal,
            "test.automation",
            envelope={"enabled": True, "min_value": 0.0, "max_value": 10.0, "key": "other.action"},
            value=5.0,
        )
    allowed = authorize(
        principal,
        "test.automation",
        envelope={"enabled": True, "max_value": 1200.0, "key": "test.automation"},
        value=1150.0,
    )
    assert allowed is ActionLevel.AUTHORIZED_AUTOMATION


def test_anonymous_principals_may_only_observe():
    anonymous = Principal(id="", kind="anonymous", max_action_level=ActionLevel.OBSERVE)
    with pytest.raises(PermissionDenied):
        authorize(anonymous, "context.read")


def test_envelope_logic_is_conservative():
    assert envelope_allows({})[0] is False  # no envelope, no automation
    allowed, reason = envelope_allows(
        {"enabled": True, "min_value": 0.0, "max_value": 10.0, "equals": {"well_id": "wel_1"}},
        value=11.0,
        context={"well_id": "wel_1"},
    )
    assert allowed is False and "exceeds" in reason
    allowed, reason = envelope_allows(
        {"enabled": True, "max_value": 10.0, "equals": {"well_id": "wel_1"}},
        value=5.0,
        context={"well_id": "wel_2"},
    )
    assert allowed is False and "wel_1" in reason
    allowed, _reason = envelope_allows(
        {"enabled": True, "max_value": 10.0, "equals": {"well_id": "wel_1"}},
        value=5.0,
        context={"well_id": "wel_1"},
    )
    assert allowed is True
    # an envelope that only says "enabled" authorizes nothing at all
    assert envelope_allows({"enabled": True})[0] is False
    # a scope-only envelope is acceptable for an action with no numeric value
    assert envelope_allows({"enabled": True, "equals": {"well_id": "wel_1"}}, context={"well_id": "wel_1"})[0] is True


# --------------------------------------------------------------------------- RBAC


def test_role_catalogue_is_coherent():
    assert set(SYSTEM_ROLES) == {role.key for role in ROLE_CATALOGUE}
    ceilings = {role.key: role.max_action_level for role in ROLE_CATALOGUE}
    assert ceilings["viewer"] is ActionLevel.OBSERVE
    assert ceilings["engineer"] is ActionLevel.DRAFT
    assert ceilings["drilling_supervisor"] is ActionLevel.EXECUTE_WITH_APPROVAL
    assert ceilings["well_manager"] is ActionLevel.AUTHORIZED_AUTOMATION
    assert ceilings["auditor"] is ActionLevel.OBSERVE
    for role in ROLE_CATALOGUE:
        assert role.permissions, f"{role.key} grants nothing"
        assert role.description


def test_a_role_that_reads_operations_can_read_events():
    """The operational record is one surface, so a role is not given half of it.

    Operations and events answer one question — what happened on this well — and the workspace, the
    timeline and the NPT account all render them together. The two record kinds are served by separate
    endpoints, each gated on its own permission (`operation.read`, `event.read`), so a role with one
    and not the other opens the page to an authorization error on a tab of its own record. That is
    exactly what the role catalogue shipped: `operation.read` on the viewer, the engineer and the
    drilling supervisor, and `event.read` on none of them — a supervisor who could not read the events
    the shift had recorded. The test is written over the whole catalogue rather than the three keys,
    because the next role added is the one that would repeat it.
    """
    from drillai.security.rbac import ROLE_CATALOGUE

    half = [
        role.key
        for role in ROLE_CATALOGUE
        if role.grants("operation.read") and not role.grants("event.read")
    ]
    assert half == []


def test_engineer_cannot_cross_into_execution():
    engineer = principal_from_roles(principal_id="usr_1", org_id="org_1", roles=[SYSTEM_ROLES["engineer"]])
    assert engineer.max_action_level is ActionLevel.DRAFT
    assert engineer.has_permission("engine.run")
    with pytest.raises(PermissionDenied):
        authorize(engineer, "connector.send")


def test_manager_roles_do_not_get_unbounded_automation_implicitly():
    manager = principal_from_roles(principal_id="usr_2", org_id="org_1", roles=[SYSTEM_ROLES["well_manager"]])
    assert manager.max_action_level is ActionLevel.AUTHORIZED_AUTOMATION
    with pytest.raises(ApprovalRequired):
        authorize(manager, "test.automation")  # registered L5 above: still needs an envelope


def test_permission_patterns_are_understood_everywhere():
    assert permission_matches({"engine.*"}, "engine.run")
    assert permission_matches({"engine.*"}, "engine.read")
    assert not permission_matches({"engine.*"}, "engine.run.scheduled")  # one level only
    assert permission_matches({"**"}, "anything.at.all")
    assert permission_matches({"*"}, "anything")
    assert permission_matches({"well.**"}, "well.section.update")
    assert permissions_match({"engine.*"}, "engine.run")  # single implementation, two names


def test_principal_without_roles_gets_nothing():
    nobody = principal_from_roles(principal_id="usr_3", org_id="org_1", roles=[])
    assert nobody.permissions == frozenset()
    with pytest.raises(PermissionDenied):
        authorize(nobody, "context.read")


# --------------------------------------------------------------------------- credentials


def test_password_hashing_round_trip_and_format():
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("scrypt$")
    assert "correct horse" not in stored
    assert verify_password("correct horse battery staple", stored) is True
    assert verify_password("wrong password", stored) is False
    # the same password hashes differently each time (unique salt)
    assert hash_password("correct horse battery staple") != stored
    assert verify_password("anything", "not-a-hash") is False


def test_api_token_round_trip_and_constant_time_behaviour():
    token, token_id, digest = new_api_token()
    assert token.startswith("dk_") and token_id in token
    assert digest.startswith("sha256$") and token not in digest
    assert verify_token(token, token_id, digest) is True
    assert verify_token(token + "x", token_id, digest) is False
    other, other_id, _other_digest = new_api_token()
    assert other != token and other_id != token_id
    # the digest is over (token_id, secret): a token with the wrong secret cannot verify
    assert hash_token(token_id, "wrong") != digest
    assert verify_token(token, other_id, digest) is False


def test_passwords_are_rejected_when_empty_and_rehash_is_detected():
    with pytest.raises(ValueError):
        hash_password("")
    stored = hash_password("another long passphrase")
    assert needs_rehash(stored) is False
    assert needs_rehash("scrypt$n=2,r=1,p=1$c2FsdA$aGFzaA") is True
    assert verify_password("anything", "garbage") is False
