"""Request dependencies: database session, authenticated principal, authorization.

Security rules enforced here (and nowhere else):

* a request is authenticated by ``Authorization: Bearer <token>``; the token is looked up by its
  public prefix and verified in constant time against the stored hash;
* when ``DRILLAI_AUTH_ENABLED=false`` (development and tests only) a caller may impersonate a
  set of *catalogued* system roles via ``X-Dev-Roles``; that path is refused in production;
* **fail closed**: missing or invalid credentials are ``401``; a valid principal without the
  required permission is ``403``; an action above the principal's level ceiling raises
  ``ApprovalRequired`` (mapped to ``409`` with the approval contract in the payload);
* every endpoint declares the permission it needs (``Depends(require("well.read"))``) so the
  security model is reviewable by reading route signatures.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, Query, Request
from pydantic import BeforeValidator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.clock import UTC
from drillai.core.config import get_settings
from drillai.core.errors import AuthenticationRequired, PermissionDenied
from drillai.core.ids import new_id
from drillai.db.models import ApiToken, Membership, Organization, User
from drillai.db.session import Database
from drillai.security.actions import ActionLevel, Principal
from drillai.security.rbac import ROLE_CATALOGUE, SYSTEM_ROLES, principal_from_roles, require_permissions

__all__ = [
    "AuthContext",
    "OptionalFilter",
    "current_auth",
    "current_principal",
    "get_database",
    "get_db",
    "require",
]


#: Optional string query filter. An empty value (``?well_id=``) is treated as "not supplied",
#: which is what a browser sends for an unset <select>; without this, ``?wellbore_id=`` would
#: filter for an empty string and silently return nothing.
OptionalFilter = Annotated[str | None, Query(), BeforeValidator(lambda value: value or None)]


def get_database(request: Request) -> Database:
    database = getattr(request.app.state, "database", None)
    if database is None:  # pragma: no cover - the app factory always sets this
        raise AuthenticationRequired("application database is not configured")
    return database


def get_blob_store(request: Request):
    """The application's blob store (process-scoped so in-memory stores keep their bytes)."""
    store = getattr(request.app.state, "blob_store", None)
    if store is None:  # pragma: no cover - the app factory always sets this
        raise AuthenticationRequired("application blob store is not configured")
    return store


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request, committed on success and rolled back on error."""
    database = get_database(request)
    session = database.session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@dataclass(frozen=True)
class AuthContext:
    """The authenticated request: principal plus the token record that identified it."""

    principal: Principal
    org_id: str | None
    token_id: str | None = None
    locale: str = "en"
    unit_system: str = "field_us"


def _dev_principal(org_id: str, role_keys: list[str]) -> Principal:
    """Build a synthetic development principal for a set of catalogued roles.

    The principal id is derived from the role set, so switching roles switches *identity* and not
    just permissions: rules such as "a requester cannot decide their own approval" stay testable in
    development instead of being bypassed by a single shared fake user.
    """
    keys = sorted(key for key in role_keys if key in SYSTEM_ROLES)
    roles = [SYSTEM_ROLES[key] for key in keys]
    if not roles:
        raise PermissionDenied(
            "no usable development roles supplied",
            details={"known_roles": sorted(SYSTEM_ROLES)},
        )
    return principal_from_roles(
        principal_id=f"usr_dev_{'_'.join(keys)}",
        org_id=org_id,
        roles=roles,
        display_name=f"Development operator ({', '.join(keys)})",
    )


async def _resolve_token(session: AsyncSession, token: str) -> tuple[ApiToken, User]:
    parts = token.split("_")
    if len(parts) < 3:
        raise AuthenticationRequired("malformed bearer token")
    token_prefix = parts[1]
    row = (
        await session.execute(select(ApiToken).where(ApiToken.token_prefix == token_prefix))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        raise AuthenticationRequired("token is not recognised")
    if row.expires_at is not None and row.expires_at <= dt.datetime.now(tz=UTC):
        raise AuthenticationRequired("token has expired")
    from drillai.security.passwords import verify_token

    if not verify_token(token, row.token_prefix, row.token_hash):
        raise AuthenticationRequired("token verification failed")
    user = (await session.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthenticationRequired("token owner is not active")
    row.last_used_at = dt.datetime.now(tz=UTC)
    return row, user


async def _membership_roles(session: AsyncSession, user: User, org_id: str | None) -> list[Any]:
    if org_id is None:
        return []
    stmt = select(Membership).where(Membership.user_id == user.id, Membership.org_id == org_id)
    rows = (await session.execute(stmt)).scalars().all()
    now = dt.datetime.now(tz=UTC)
    keys = [
        row.role_key
        for row in rows
        if row.expires_at is None or row.expires_at > now
    ]
    return [SYSTEM_ROLES[key] for key in keys if key in SYSTEM_ROLES] or [
        role for role in ROLE_CATALOGUE if role.key == "viewer"
    ]


async def current_auth(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
    x_dev_roles: Annotated[str | None, Header()] = None,
    accept_language: Annotated[str | None, Header()] = None,
) -> AuthContext:
    settings = get_settings()
    locale = (accept_language or "en").split(",")[0].split("-")[0].strip() or "en"

    if settings.auth_enabled:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthenticationRequired("a bearer token is required")
        token_row, user = await _resolve_token(session, authorization.split(" ", 1)[1].strip())
        roles = await _membership_roles(session, user, token_row.org_id)
        principal = principal_from_roles(
            principal_id=user.id,
            org_id=token_row.org_id,
            roles=roles,
            display_name=user.display_name,
            scopes=tuple(token_row.scopes or ()),
        )
        return AuthContext(
            principal=principal,
            org_id=token_row.org_id,
            token_id=token_row.id,
            locale=locale,
            unit_system=user.unit_system or "field_us",
        )

    if settings.is_production:  # pragma: no cover - defensive, config validator also guards this
        raise AuthenticationRequired("authentication cannot be disabled in production")
    # Development default: the engineering role plus the platform-administrator role, because a
    # single developer session has to be able to create the container objects it works in. The
    # header stays available so a caller (or the UI's role switcher) can act as any catalogued
    # role and see the authorization model behave.
    requested = [key.strip() for key in (x_dev_roles or "engineer,admin").split(",") if key.strip()]
    org = await ensure_org(session, settings.dev_org_slug)
    principal = _dev_principal(org.id, requested)
    return AuthContext(
        principal=principal,
        org_id=org.id,
        locale=locale,
        unit_system="field_us",
    )


async def current_principal(auth: Annotated[AuthContext, Depends(current_auth)]) -> Principal:
    return auth.principal


def require(*permissions: str, level: ActionLevel | None = None) -> Callable[..., Any]:
    """Declare the permission(s) — and optionally the action level — an endpoint needs."""

    async def dependency(auth: Annotated[AuthContext, Depends(current_auth)]) -> AuthContext:
        require_permissions(auth.principal, *permissions)
        if level is not None and auth.principal.max_action_level.rank < level.rank:
            raise PermissionDenied(
                f"action level {level.value} exceeds the principal's ceiling "
                f"{auth.principal.max_action_level.value}",
                details={"required_level": level.value, "ceiling": auth.principal.max_action_level.value},
            )
        return auth

    return dependency


async def ensure_org(session: AsyncSession, slug: str, *, name: str | None = None) -> Organization:
    """Resolve the org for the authenticated principal, creating the dev org on first use.

    Two requests arriving together on a database that has never been used are a normal first-boot
    condition, not an error: a browser opens a screen and the client fires several queries at once.
    Both would see "no such org" and both would try to insert it, which the unique constraint on
    ``slug`` correctly refuses. The insert therefore runs inside a savepoint so that losing the race
    costs nothing but a re-read — and, in particular, does not roll back the caller's work.
    """
    org = (await session.execute(select(Organization).where(Organization.slug == slug))).scalar_one_or_none()
    if org is not None:
        return org

    candidate = Organization(id=new_id("org"), slug=slug, name=name or slug.replace("-", " ").title())
    try:
        async with session.begin_nested():
            session.add(candidate)
            await session.flush()
    except IntegrityError:
        org = (
            await session.execute(select(Organization).where(Organization.slug == slug))
        ).scalar_one_or_none()
        if org is None:  # pragma: no cover - the row was deleted between the insert and the re-read
            raise
        return org
    return candidate


