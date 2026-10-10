"""Action levels and the authorization gate for everything the platform can *do*.

The single most important safety property of an engineering platform is that no actor —
human, workflow, agent or integration — can cause an operational consequence it was not
authorized for. This module makes that property explicit and testable.

Levels (a fixed, platform-wide vocabulary):

* ``L0`` **Observe** — read-only. Summaries, dashboards, context assembly, RAG answers.
* ``L1`` **Advise** — produce a recommendation. Never changes a plan or an operation.
* ``L2`` **Draft** — create or modify a *candidate* artefact (a draft program, a scenario,
  a what-if run). Nothing that is part of the approved plan changes.
* ``L3`` **Propose change** — open a change request / approval request against a plan,
  schedule or field configuration. Still no external effect.
* ``L4`` **Execute with approval** — run an action that changes state or reaches outside the
  platform (publish a plan, send a message, write to a rig system) *after* a human approval.
* ``L5`` **Authorized automated action** — execute without a per-instance human gate, only
  inside a pre-authorized envelope (limits, time window, scope) with audit and a kill switch.

Fail-closed rules implemented here:

* every action declares its level; unknown actions are treated as ``L4`` (maximum gate);
* ``L5`` is disabled unless explicitly enabled for the org **and** the action appears in the
  org's allow-list with a bounded envelope;
* an actor's *granted* ceiling (role permission) must be >= the action's level;
* anything above ``L1`` must be attributable to a non-anonymous principal;
* ``require(...)`` raises :class:`ApprovalRequired` rather than returning a boolean, so a caller
  cannot accidentally ignore the answer.
"""

from __future__ import annotations

import enum
import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from drillai.core.errors import ApprovalRequired, PermissionDenied

__all__ = [
    "ACTION_LEVEL_DESCRIPTIONS",
    "ActionLevel",
    "ActionRef",
    "authorize",
    "envelope_allows",
    "level_of",
    "permission_matches",
    "register_action",
    "registered_actions",
]


class ActionLevel(enum.StrEnum):
    OBSERVE = "L0"
    ADVISE = "L1"
    DRAFT = "L2"
    PROPOSE = "L3"
    EXECUTE_WITH_APPROVAL = "L4"
    AUTHORIZED_AUTOMATION = "L5"

    @property
    def rank(self) -> int:
        return int(self.value[1:])


ACTION_LEVEL_DESCRIPTIONS: dict[ActionLevel, str] = {
    ActionLevel.OBSERVE: "Read-only: context, summaries, dashboards, retrieval answers.",
    ActionLevel.ADVISE: "Produce a recommendation for a human to consider; no state change.",
    ActionLevel.DRAFT: "Create or edit a candidate artefact (draft, scenario, what-if).",
    ActionLevel.PROPOSE: "Raise a change/approval request against an approved plan or schedule.",
    ActionLevel.EXECUTE_WITH_APPROVAL: "Change state or act outside the platform after human approval.",
    ActionLevel.AUTHORIZED_AUTOMATION: "Act without a per-instance gate, only inside a pre-authorized envelope.",
}


@dataclass(frozen=True)
class ActionRef:
    """A registered action: the unit of authorization."""

    key: str
    level: ActionLevel
    description: str
    permission: str = field(default="", metadata={"doc": "RBAC permission required to request the action"})
    #: ``True`` for actions that *are* the human gate (deciding an approval). They require the
    #: permission and an action-level ceiling at or above the level, but cannot require a prior
    #: approval: that would be unbounded recursion, and the decision itself is the recorded control.
    self_authorizing: bool = False

    def __post_init__(self) -> None:
        if not self.permission:
            object.__setattr__(self, "permission", f"action:{self.key}")


_ACTIONS: dict[str, ActionRef] = {}
DEFAULT_UNKNOWN_LEVEL = ActionLevel.EXECUTE_WITH_APPROVAL


def register_action(
    key: str,
    level: ActionLevel,
    description: str,
    *,
    permission: str | None = None,
    self_authorizing: bool = False,
    replace: bool = False,
) -> ActionRef:
    """Register an action's level. Registration is explicit — no implicit discovery."""
    if key in _ACTIONS and not replace:
        raise ValueError(f"action {key!r} is already registered with level {_ACTIONS[key].level}")
    action = ActionRef(
        key=key,
        level=level,
        description=description,
        permission=permission or f"action:{key}",
        self_authorizing=self_authorizing,
    )
    _ACTIONS[key] = action
    return action


def permission_matches(patterns: Iterable[str], permission: str) -> bool:
    """Does a permission set grant ``permission``?

    Permission sets hold *role patterns* (``engine.*``, ``well.**``), not expanded literals, so an
    exact string comparison would deny an engineer the very action their role grants. ``**`` is a
    global grant, ``*`` grants everything as well (used by break-glass principals), ``domain.*``
    matches exactly one further level, and ``domain.**`` matches any depth.
    """
    patterns = set(patterns)
    if "*" in patterns or "**" in patterns:
        return True
    for pattern in patterns:
        if pattern == permission:
            return True
        if pattern.endswith(".**"):
            prefix = pattern[:-3]
            if permission.startswith(prefix + "."):
                return True
            continue
        if pattern.endswith(".*"):
            prefix = pattern[:-2]
            if permission.count(".") == prefix.count(".") + 1 and permission.startswith(prefix + "."):
                return True
            continue
        if "*" in pattern and fnmatch.fnmatchcase(permission, pattern):
            return True
    return False


def registered_actions() -> dict[str, ActionRef]:
    return dict(_ACTIONS)


def level_of(action_key: str) -> ActionLevel:
    """Level of an action; *unknown actions are treated as L4* (fail closed)."""
    known = _ACTIONS.get(action_key)
    return known.level if known else DEFAULT_UNKNOWN_LEVEL


@dataclass(frozen=True)
class Principal:
    """The authenticated actor a decision is made for."""

    id: str
    kind: str = "user"  # user | service | agent | integration | system
    display_name: str | None = None
    org_id: str | None = None
    role_keys: tuple[str, ...] = ()
    permissions: frozenset[str] = frozenset()
    max_action_level: ActionLevel = ActionLevel.OBSERVE
    scopes: tuple[str, ...] = ()

    @property
    def is_anonymous(self) -> bool:
        return self.kind == "anonymous" or not self.id

    def has_permission(self, permission: str) -> bool:
        """Pattern-aware: ``engine.*`` in the role must satisfy ``engine.run``."""
        return permission_matches(self.permissions, permission)


def envelope_allows(
    envelope: dict[str, Any],
    *,
    value: float | None = None,
    key: str | None = None,
    context: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Evaluate a pre-authorization envelope for an L5 action.

    Envelopes are data, not code: ``{"max_value": 1200.0, "key": "mud_weight_si",
    "equals": {"well_id": "wel_...", "phase": "drilling"}, "not_before": ..., "not_after": ...}``.
    Anything not explicitly permitted is refused. Returns ``(allowed, reason)``.
    """
    if not envelope:
        return False, "no envelope declared: L5 automation requires an explicit, bounded envelope"
    if key is not None and envelope.get("key") not in (None, key):
        return False, f"envelope is bound to parameter {envelope.get('key')!r}, not {key!r}"
    if value is not None:
        upper = envelope.get("max_value")
        lower = envelope.get("min_value")
        if upper is None and lower is None:
            return False, "envelope declares neither min_value nor max_value for a numeric action"
        if upper is not None and value > float(upper):
            return False, f"value {value} exceeds envelope max {upper}"
        if lower is not None and value < float(lower):
            return False, f"value {value} is below envelope min {lower}"
    elif not envelope.get("equals"):
        # No value to bound and no scope constraint either: this envelope says only "enabled",
        # which authorizes nothing. Automation must be bounded *or* scoped, never merely switched on.
        return False, "envelope declares no numeric bound and no scope constraint: nothing is authorized"
    for field_name, expected in (envelope.get("equals") or {}).items():
        actual = (context or {}).get(field_name)
        if actual != expected:
            return False, f"envelope requires {field_name}={expected!r}, got {actual!r}"
    if envelope.get("enabled") is False:
        return False, "envelope is disabled"
    return True, "inside the pre-authorized envelope"


def authorize(
    principal: Principal,
    action_key: str,
    *,
    approval_id: str | None = None,
    envelope: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    value: float | None = None,
    required_permission: str | None = None,
) -> ActionLevel:
    """Authorize an action, or raise.

    Returns the level that was authorized. Raises :class:`PermissionDenied` when the actor may
    never perform the action and :class:`ApprovalRequired` when a human gate is still missing.
    """
    action = _ACTIONS.get(action_key)
    level = action.level if action else DEFAULT_UNKNOWN_LEVEL
    permission = required_permission or (action.permission if action else f"action:{action_key}")

    if principal.is_anonymous:
        raise PermissionDenied(
            "anonymous principals may only observe",
            details={"action": action_key, "reason": "no authenticated principal"},
        )
    if principal.max_action_level.rank < level.rank:
        raise PermissionDenied(
            f"principal ceiling {principal.max_action_level} is below action level {level}",
            details={"action": action_key, "required_level": level.value, "ceiling": principal.max_action_level.value},
        )
    if not principal.has_permission(permission):
        raise PermissionDenied(
            f"missing permission {permission!r}",
            details={"action": action_key, "permission": permission, "role_keys": list(principal.role_keys)},
        )
    if level.rank <= ActionLevel.PROPOSE.rank:
        return level
    if level is ActionLevel.EXECUTE_WITH_APPROVAL:
        if action is not None and action.self_authorizing:
            return level
        if not approval_id:
            raise ApprovalRequired(
                f"action {action_key!r} requires a recorded approval before execution",
                details={"action": action_key, "level": level.value},
            )
        return level
    # L5: pre-authorized envelope only.
    allowed, reason = envelope_allows(envelope or {}, value=value, key=action_key, context=context)
    if not allowed:
        raise ApprovalRequired(
            f"automation of {action_key!r} is not inside a pre-authorized envelope: {reason}",
            details={"action": action_key, "level": level.value, "reason": reason},
        )
    return level


def permissions_for(levels: Iterable[ActionLevel], action_keys: Iterable[str]) -> frozenset[str]:
    """Helper for seeding roles: permissions for the given registered actions at or below ``levels``."""
    ceilings = {level.rank for level in levels}
    return frozenset(
        registered_actions()[key].permission for key in action_keys if key in registered_actions() and registered_actions()[key].level.rank in ceilings
    )
