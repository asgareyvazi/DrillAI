"""RBAC: roles, permissions and policy evaluation.

The permission model is intentionally small and explicit:

* a permission is a dotted string ``<area>.<object>.<verb>`` (``well.read``,
  ``workflow.publish``, ``action.engine.run``);
* a **role** is a named set of permission patterns where ``*`` matches one segment and
  ``**`` matches the rest (``well.*``, ``engines.**``);
* a **principal** holds roles and an explicit action-level ceiling;
* every check is **org-scoped** — a role assignment always carries the org it belongs to, and
  cross-org access is denied before any object is loaded.

Patterns (rather than enumerated permission lists) keep role definitions readable while the
matching function stays deterministic and testable. Nothing here hits the database: services
resolve role assignments to principals, then ask this module.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from drillai.security.actions import ActionLevel, Principal

__all__ = [
    "ROLE_CATALOGUE",
    "SYSTEM_ROLES",
    "Role",
    "effective_permissions",
    "permission_granted",
    "permissions_match",
    "principal_from_roles",
    "require_permissions",
]

from drillai.core.errors import PermissionDenied
from drillai.security.actions import permission_matches


@dataclass(frozen=True)
class Role:
    """A role definition. ``system=True`` roles are seeded and editable-but-not-deletable."""

    key: str
    name: str
    description: str
    permissions: tuple[str, ...] = ()
    max_action_level: ActionLevel = ActionLevel.OBSERVE
    is_system: bool = True

    def grants(self, permission: str) -> bool:
        return any(_pattern_matches(pattern, permission) for pattern in self.permissions)


def _pattern_matches(pattern: str, permission: str) -> bool:
    if pattern == permission:
        return True
    if pattern.endswith(".**"):
        return permission.startswith(pattern[:-3] + ".")
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        return permission.count(".") == prefix.count(".") + 1 and permission.startswith(prefix + ".")
    if "*" in pattern:
        return fnmatch.fnmatchcase(permission, pattern)
    return False


#: System roles shipped with the platform. Domain packs add roles; orgs may fork these.
ROLE_CATALOGUE: tuple[Role, ...] = (
    Role(
        key="viewer",
        name="Viewer",
        description="Read-only access to wells, documents, context and results.",
        permissions=(
            "well.read",
            "field.read",
            "project.read",
            "document.read",
            "evidence.read",
            # Operations and events are well data a reader is entitled to see: the timeline, the NPT
            # account and the source of a twin value all read from them. Read only — the writes are
            # DRAFT-level actions this role's ceiling (OBSERVE) refuses at authorization time.
            #
            # Both are listed, and both are needed: the operational workspace reads the two record
            # kinds through separate endpoints, each gated on its own permission, so a role holding
            # only `operation.read` opens the page to an authorization error on the events tab. The
            # same slip is what the note below about `operations.*` records.
            "operation.read",
            "event.read",
            # The operational monitor is a *read* of the well: the current channel values, how old they
            # are and which alerts are open. A viewer who may read the well may read what it is doing;
            # the writes (recording a measurement, acknowledging an alert) are DRAFT-level actions this
            # role's ceiling refuses at authorization time.
            "timeseries.read",
            "connector.read",
            "alert.read",
            "live.read",
            "context.read",
            "engine.read",
            "workflow.read",
            "twin.read",
            "recommendation.read",
            "dashboard.read",
            "registry.read",
        ),
        max_action_level=ActionLevel.OBSERVE,
    ),
    Role(
        key="engineer",
        name="Drilling engineer",
        description="Full read access plus running engines, drafting scenarios and advising.",
        permissions=(
            "well.*",
            "field.*",
            "project.read",
            "document.*",
            "evidence.*",
            # Recording and correcting operations is what an engineer does with a daily report;
            # the action level for every operation.* write is DRAFT, which this role may reach.
            #
            # The events endpoints are gated on `event.read` even for writes — the event actions in
            # the catalogue carry the `operation.write` permission, and the read permission is the
            # gate that decides whether the role sees the operational record at all. Without it the
            # engineer who records the day's events cannot read them back.
            "operation.*",
            "event.read",
            # Recording telemetry and answering its alerts: the action levels are DRAFT, which is this
            # role's ceiling. `live.read` is separate from `timeseries.read` on purpose — a socket is a
            # different entitlement from a query, and a role can be given one without the other.
            "timeseries.*",
            "connector.*",
            "alert.*",
            "live.read",
            "context.read",
            "engine.*",
            "workflow.read",
            "workflow.draft",
            "workflow.run",
            "twin.*",
            "recommendation.*",
            "offset.*",
            "optimization.*",
            "dashboard.*",
            "artifact.draft",
            "scenario.create",
            "action:engine.run",
            "action:offset.analyze",
            "action:twin.update",
            "registry.read",
        ),
        max_action_level=ActionLevel.DRAFT,
    ),
    Role(
        key="drilling_supervisor",
        name="Drilling supervisor",
        description="Advises, drafts and proposes changes to the drilling program; approves L4 execution.",
        permissions=(
            "well.*",
            "field.*",
            "project.read",
            "document.*",
            "evidence.*",
            "context.read",
            "engine.*",
            "workflow.*",
            "twin.*",
            "recommendation.*",
            "offset.*",
            "optimization.*",
            # The pattern read "operations.*" — plural, matching nothing, since every permission and
            # every action in this area is named in the singular (`operation.read`, `operation.create`).
            # It looked like the drilling supervisor could record operations and in fact granted no such
            # thing.
            "operation.*",
            # Same read gate as the engineer's: the supervisor reviews the events the shift recorded,
            # and `event.read` is the permission those endpoints check.
            "event.read",
            "timeseries.*",
            "connector.*",
            "alert.*",
            "live.read",
            "dashboard.*",
            "action:**",
            "registry.read",
        ),
        max_action_level=ActionLevel.EXECUTE_WITH_APPROVAL,
    ),
    Role(
        key="well_manager",
        name="Well manager",
        description="Accountable authority: approves plans, permits automation within an envelope.",
        permissions=("**", "registry.read"),
        max_action_level=ActionLevel.AUTHORIZED_AUTOMATION,
    ),
    Role(
        key="integrity_engineer",
        name="Well integrity engineer",
        description="Barrier, verification and certification authority (L3 proposal rights).",
        permissions=(
            "well.*",
            "field.*",
            "document.*",
            "evidence.*",
            "context.read",
            "engine.*",
            "twin.*",
            "integrity.*",
            "requirement.*",
            "recommendation.*",
            "dashboard.*",
            "action:integrity.verify",
            "registry.read",
            # Well integrity *is* a question about measurements: a barrier is verified against the
            # pressures and volumes a well actually reported, and a certification authority that cannot
            # read them can only certify the paperwork. These three are the monitor's read surfaces,
            # granted explicitly rather than through a wildcard — reading a channel is not appending one
            # and not deciding an alert, both of which stay outside this role.
            "timeseries.read",
            "connector.read",
            "alert.read",
            "live.read",
        ),
        max_action_level=ActionLevel.PROPOSE,
    ),
    Role(
        key="data_manager",
        name="Data manager",
        description="Owns ingestion, catalogues and data quality; no engineering approvals.",
        permissions=(
            "document.*",
            "evidence.*",
            "catalog.*",
            "ingestion.*",
            # Telemetry is ingestion: the data manager owns the acquisition boundary, and the alert
            # *lifecycle* is not theirs — they read alerts and do not decide them.
            "timeseries.*",
            "connector.*",
            "alert.read",
            "live.read",
            "context.read",
            "well.read",
            "field.read",
            "project.read",
            "dashboard.read",
            "registry.read",
        ),
        max_action_level=ActionLevel.DRAFT,
    ),
    Role(
        key="auditor",
        name="Auditor",
        description="Reads everything, writes nothing; used for compliance review.",
        permissions=("*.read", "audit.read", "workflow.read", "evidence.read", "registry.read"),
        max_action_level=ActionLevel.OBSERVE,
    ),
    Role(
        key="admin",
        name="Platform administrator",
        description="Manages users, roles, integrations and registries; not an engineering authority.",
        permissions=(
            "admin.**",
            "registry.*",
            "user.*",
            "role.*",
            "integration.*",
            "connector.*",
            "audit.read",
            "*.read",
            "project.write",
        ),
        max_action_level=ActionLevel.EXECUTE_WITH_APPROVAL,
    ),
)

SYSTEM_ROLES: dict[str, Role] = {role.key: role for role in ROLE_CATALOGUE}


def effective_permissions(roles: list[Role]) -> frozenset[str]:
    """Expand role patterns into a concrete permission set (patterns are kept for matching)."""
    return frozenset(pattern for role in roles for pattern in role.permissions)


#: One implementation of permission matching, shared with the action gate so that a role's
#: ``engine.*`` pattern means the same thing everywhere.
permissions_match = permission_matches


def permission_granted(principal: Principal, permission: str) -> bool:
    return permissions_match(principal.permissions, permission)


def require_permissions(principal: Principal, *permissions: str) -> None:
    missing = [permission for permission in permissions if not permission_granted(principal, permission)]
    if missing:
        raise PermissionDenied(
            f"missing permission(s): {', '.join(missing)}",
            details={"missing": missing, "role_keys": list(principal.role_keys)},
        )


def principal_from_roles(
    *,
    principal_id: str,
    org_id: str | None,
    roles: list[Role],
    kind: str = "user",
    display_name: str | None = None,
    scopes: tuple[str, ...] = (),
) -> Principal:
    """Compose a principal from role assignments: permissions union, ceiling = highest role."""
    ceiling = max(
        (role.max_action_level for role in roles), key=lambda level: level.rank, default=ActionLevel.OBSERVE
    )
    return Principal(
        id=principal_id,
        kind=kind,
        display_name=display_name,
        org_id=org_id,
        role_keys=tuple(role.key for role in roles),
        permissions=effective_permissions(roles),
        max_action_level=ceiling,
        scopes=scopes,
    )


@dataclass(frozen=True)
class OrgPolicy:
    """Org-level switches that gate dangerous capability globally.

    Defaults are the safe ones: automation off, no outbound messaging, no auto-publish.
    """

    automation_enabled: bool = False
    automation_envelopes: dict[str, dict] = field(default_factory=dict)
    require_dual_approval: bool = False
    allowed_integrations: tuple[str, ...] = ()
    data_residency: str | None = None
    retention_days: int = 3650
    private_llm_only: bool = False
