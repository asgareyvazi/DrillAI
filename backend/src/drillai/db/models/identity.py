"""Identity, tenancy and access primitives.

Tenancy model: **Organization → Project → Field → Well**. Access is granted through
scoped role bindings (``Membership``) so that a service company engineer can be given
access to one well of one project without seeing the operator's portfolio.

Agents and tools are *also* subjects of access policy (``PolicyBinding`` in
``security.py``) — an AI capability is not a privileged back door.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from drillai.db.base import (
    Base,
    CreatedAtMixin,
    IdMixin,
    JsonType,
    OrgScopedMixin,
    TextType,
    TimestampMixin,
    UtcDateTime,
)

# Roles are seeded system roles; custom roles are additive.
SYSTEM_ROLES: dict[str, dict[str, object]] = {
    "org_admin": {
        "name": "Organization administrator",
        "description": "Full control of the organization, its data and its configuration.",
        "permissions": ["*"],
    },
    "project_manager": {
        "name": "Project manager",
        "description": "Manages wells, plans and approvals within assigned projects.",
        "permissions": [
            "project:read",
            "project:write",
            "well:read",
            "well:write",
            "artifact:read",
            "artifact:write",
            "artifact:approve",
            "document:read",
            "document:write",
            "workflow:read",
            "workflow:write",
            "workflow:run",
            "recommendation:read",
            "recommendation:accept",
            "report:read",
            "report:generate",
            "action:approve",
        ],
    },
    "drilling_engineer": {
        "name": "Drilling engineer",
        "description": "Designs, calculates, recommends and documents drilling operations.",
        "permissions": [
            "project:read",
            "well:read",
            "well:write",
            "artifact:read",
            "artifact:write",
            "document:read",
            "document:write",
            "engine:read",
            "engine:execute",
            "optimization:read",
            "optimization:execute",
            "offset:read",
            "recommendation:read",
            "recommendation:raise",
            "workflow:read",
            "workflow:run",
            "report:read",
            "report:generate",
        ],
    },
    "operations_supervisor": {
        "name": "Operations supervisor",
        "description": "Runs the operation, sees real-time state, raises alerts and requests approvals.",
        "permissions": [
            "well:read",
            "artifact:read",
            "document:read",
            "document:write",
            "event:write",
            "operation:write",
            "alert:write",
            "recommendation:read",
            "workflow:run",
            "report:read",
            "report:generate",
            "action:approve",
        ],
    },
    "approver": {
        "name": "Engineering approver",
        "description": "Approves designs, parameter changes and governed actions.",
        "permissions": [
            "well:read",
            "artifact:read",
            "artifact:approve",
            "document:read",
            "recommendation:read",
            "recommendation:accept",
            "action:approve",
            "action:authorize",
            "report:read",
        ],
    },
    "analyst": {
        "name": "Performance analyst",
        "description": "Read-only access with analytics and export capabilities.",
        "permissions": [
            "well:read",
            "artifact:read",
            "document:read",
            "document:export",
            "offset:read",
            "report:read",
            "analytics:read",
            "analytics:export",
        ],
    },
    "service_company": {
        "name": "Service company",
        "description": "External collaborator restricted to explicitly shared wells.",
        "permissions": ["well:read", "document:read", "document:write", "report:read"],
    },
    "viewer": {
        "name": "Viewer",
        "description": "Read-only observer.",
        "permissions": ["well:read", "artifact:read", "document:read", "report:read"],
    },
    "agent_runtime": {
        "name": "Agent runtime",
        "description": (
            "Identity used by AI agents. Deliberately narrow: read context, execute engines "
            "and retrieval, stage proposals. Never grants autonomous field actions."
        ),
        "permissions": [
            "well:read",
            "artifact:read",
            "document:read",
            "engine:read",
            "engine:execute",
            "optimization:read",
            "optimization:execute",
            "offset:read",
            "recommendation:read",
            "recommendation:raise",
            "retrieval:read",
            "workflow:run",
        ],
    },
}


class Organization(Base, IdMixin, TimestampMixin):
    __tablename__ = "organizations"
    id_prefix = "org"

    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), default="operator", nullable=False)
    country: Mapped[str | None] = mapped_column(String(80))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    default_unit_system: Mapped[str] = mapped_column(String(32), default="field_us", nullable=False)
    default_locale: Mapped[str] = mapped_column(String(16), default="en", nullable=False)
    settings: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class User(Base, IdMixin, TimestampMixin):
    __tablename__ = "users"
    id_prefix = "usr"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    job_title: Mapped[str | None] = mapped_column(String(200))
    # scrypt(password) — see drillai.security.passwords (stdlib, no external crypto dep)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    locale: Mapped[str] = mapped_column(String(16), default="en", nullable=False)
    unit_system: Mapped[str | None] = mapped_column(String(32))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_service_account: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    preferences: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class Role(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    __tablename__ = "roles"
    id_prefix = "rol"
    __table_args__ = (UniqueConstraint("org_id", "key"),)

    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(TextType)
    permissions: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_editable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Membership(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """Scoped role binding: (user, role) on (org | project | field | well)."""

    __tablename__ = "memberships"
    id_prefix = "mbr"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope_kind: Mapped[str] = mapped_column(String(32), default="org", nullable=False)
    scope_id: Mapped[str | None] = mapped_column(String(64), index=True)
    granted_by: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)


class ApiToken(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """Personal / service token. Only the hash is stored."""

    __tablename__ = "api_tokens"
    id_prefix = "tok"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    scopes: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
