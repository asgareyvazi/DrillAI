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
import hashlib
import hmac
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
from drillai.db.models import ApiToken, Membership, Organization, User, UserSession
from drillai.db.session import Database
from drillai.security.actions import ActionLevel, Principal
from drillai.security.rbac import SYSTEM_ROLES, principal_from_roles, require_permissions

__all__ = [
    "AuthContext",
    "OptionalFilter",
    "current_auth",
    "current_principal",
    "derive_session_csrf_token",
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
    """The authenticated request: principal plus the session or token record that identified it."""

    principal: Principal
    org_id: str | None
    token_id: str | None = None
    session_id: str | None = None
    identity_source: str = "development_header"
    user_email: str | None = None
    csrf_token: str | None = None
    locale: str = "en"
    unit_system: str = "field_us"


def derive_session_csrf_token(session_id: str, session_token_hash: str) -> str:
    """Derive the per-session CSRF synchronizer token bound to the server's CSRF key and session digest."""
    settings = get_settings()
    key = (settings.session_csrf_secret or settings.secret_master_key or "drillai-csrf-key").encode("utf-8")
    msg = f"csrf:v1:{session_id}:{session_token_hash}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


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
    # Resolve the credential and the two records whose active state gates it in one round trip. This
    # is not an authorization cache: every request still observes revocation, expiry, account state,
    # organization state, and the live membership query below.
    result = await session.execute(
        select(ApiToken, User, Organization)
        .join(User, User.id == ApiToken.user_id)
        .join(Organization, Organization.id == ApiToken.org_id)
        .where(ApiToken.token_prefix == token_prefix)
    )
    matched = result.one_or_none()
    if matched is None:
        raise AuthenticationRequired("token is not recognised")
    row, user, org = matched
    if row.revoked_at is not None:
        raise AuthenticationRequired("token is not recognised")
    if not row.scopes:
        # An empty scope list must never mean "no restriction". Empty-scope legacy credentials fail
        # closed; new token creation also requires at least one explicit scope.
        raise AuthenticationRequired("token has no authorization scopes")
    now = dt.datetime.now(tz=UTC)
    expires_at = row.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= now:
            raise AuthenticationRequired("token has expired")
    from drillai.security.passwords import verify_token

    if not verify_token(token, row.token_prefix, row.token_hash):
        raise AuthenticationRequired("token verification failed")
    if not user.is_active:
        raise AuthenticationRequired("token owner is not active")
    if not org.is_active:
        raise AuthenticationRequired("token organization is not active")
    row.last_used_at = now
    return row, user


async def _resolve_session(session: AsyncSession, cookie_token: str) -> tuple[UserSession, User, str]:
    parts = cookie_token.split("_")
    if len(parts) < 3 or parts[0] != "ds":
        raise AuthenticationRequired("malformed session cookie")
    token_prefix = parts[1]
    candidates = (
        await session.execute(
            select(UserSession).where(UserSession.session_token_prefix == token_prefix)
        )
    ).scalars().all()
    if not candidates:
        raise AuthenticationRequired("session is not recognised")
    from drillai.security.passwords import constant_time_equals, hash_token, verify_token

    matched: UserSession | None = None
    for row in candidates:
        if verify_token(cookie_token, row.session_token_prefix, row.session_token_hash):
            matched = row
            break
    if matched is None or matched.revoked_at is not None:
        raise AuthenticationRequired("session is not active or has been revoked")
    now = dt.datetime.now(tz=UTC)
    expires_at = matched.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        raise AuthenticationRequired("session has expired")
    user = (
        await session.execute(select(User).where(User.id == matched.user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthenticationRequired("session user is not active")
    org = (
        await session.execute(select(Organization).where(Organization.id == matched.org_id))
    ).scalar_one_or_none()
    if org is None or not org.is_active:
        raise AuthenticationRequired("session organization is not active")
    csrf_token = derive_session_csrf_token(matched.id, matched.session_token_hash)
    if not constant_time_equals(hash_token("csrf", csrf_token), matched.csrf_token_hash):
        raise AuthenticationRequired("session CSRF binding is invalid")
    matched.last_seen_at = now
    return matched, user, csrf_token


async def _membership_roles(session: AsyncSession, user: User, org_id: str | None) -> list[Any]:
    if org_id is None:
        return []
    stmt = select(Membership).where(Membership.user_id == user.id, Membership.org_id == org_id)
    rows = (await session.execute(stmt)).scalars().all()
    now = dt.datetime.now(tz=UTC)
    keys: list[str] = []
    for row in rows:
        exp = row.expires_at
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        if exp is None or exp > now:
            keys.append(row.role_key)
    return [SYSTEM_ROLES[key] for key in keys if key in SYSTEM_ROLES]


async def current_auth(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
    x_dev_roles: Annotated[str | None, Header()] = None,
    x_csrf_token: Annotated[str | None, Header()] = None,
    accept_language: Annotated[str | None, Header()] = None,
) -> AuthContext:
    settings = get_settings()
    if accept_language is None and hasattr(request, "headers"):
        accept_language = request.headers.get("accept-language")
    locale = (accept_language or "en").split(",")[0].split("-")[0].strip() or "en"

    if settings.auth_enabled:
        if authorization is None and hasattr(request, "headers"):
            authorization = request.headers.get("authorization")
        if authorization and authorization.lower().startswith("bearer "):
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
                identity_source="bearer_token",
                user_email=user.email,
                locale=locale,
                unit_system=user.unit_system or "field_us",
            )

        cookies = getattr(request, "cookies", None) or {}
        cookie_token = cookies.get(settings.session_cookie_name)
        if cookie_token:
            session_row, user, expected_csrf = await _resolve_session(session, cookie_token.strip())
            method = (getattr(request, "method", "GET") or "GET").upper()
            if method in {"POST", "PUT", "PATCH", "DELETE"}:
                supplied_csrf = x_csrf_token
                if supplied_csrf is None and hasattr(request, "headers"):
                    supplied_csrf = request.headers.get("x-csrf-token")
                from drillai.security.passwords import constant_time_equals

                if not supplied_csrf or not constant_time_equals(supplied_csrf.strip(), expected_csrf):
                    raise PermissionDenied(
                        "CSRF synchronizer token is missing or invalid for cookie-authenticated state change",
                        details={"reason": "csrf_validation_failed", "header": "X-CSRF-Token"},
                    )
            roles = await _membership_roles(session, user, session_row.org_id)
            principal = principal_from_roles(
                principal_id=user.id,
                org_id=session_row.org_id,
                roles=roles,
                display_name=user.display_name,
            )
            return AuthContext(
                principal=principal,
                org_id=session_row.org_id,
                session_id=session_row.id,
                identity_source="session_cookie",
                user_email=user.email,
                csrf_token=expected_csrf,
                locale=locale,
                unit_system=user.unit_system or "field_us",
            )

        raise AuthenticationRequired("a bearer token or active session cookie is required")

    if settings.is_production:  # pragma: no cover - defensive, config validator also guards this
        raise AuthenticationRequired("authentication cannot be disabled in production")
    # Development default: the engineering role plus the platform-administrator role, because a
    # single developer session has to be able to create the container objects it works in. The
    # header stays available so a caller (or the UI's role switcher) can act as any catalogued
    # role and see the authorization model behave.
    #
    # A browser cannot set request headers on a WebSocket handshake, so the same development
    # identity is also accepted from the query string. Both paths sit *below* the
    # ``settings.auth_enabled`` branch above: when authentication is on, neither the header nor the
    # parameter is read at all, and a request without a bearer token is refused before this point.
    requested_roles = x_dev_roles
    if requested_roles is None:
        params = getattr(request, "query_params", None)
        requested_roles = params.get("dev_roles") if params is not None else None
    requested = [key.strip() for key in (requested_roles or "engineer,admin").split(",") if key.strip()]
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


