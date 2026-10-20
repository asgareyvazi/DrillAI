"""Multi-user authentication, browser session lifecycle, user provisioning, and API token management.

Security invariants enforced here:
- Interactive browser sessions use an `HttpOnly`, `SameSite`, `Secure` (in production) session cookie
  (`drillai_session`) paired with a per-session HMAC-bound CSRF synchronizer token (`X-CSRF-Token`).
- Only cryptographic digests (`sha256$<hex>`) of session tokens, API tokens, and password-reset tokens
  are stored in the database; clear tokens are returned at creation time only.
- Failed login, password-reset, and token-creation attempts are rate-limited across workers via the
  durable `auth_rate_limits` table (`AuthRateLimitBucket`), committed in an independent transaction so
  that raising 401/403/429 does not roll back the failure counter or audit record.
- Tenant administrators (`org_admin`) are strictly confined to their own `org_id` and cannot assign the
  platform-wide `admin` role or mint API tokens with `system.egress_policy.manage` / `admin.**` scopes.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import (
    AuthContext,
    current_auth,
    derive_session_csrf_token,
    get_database,
    get_db,
    require,
)
from drillai.core.clock import UTC
from drillai.core.config import get_settings
from drillai.core.errors import (
    AuthenticationRequired,
    Conflict,
    NotFound,
    PermissionDenied,
    RateLimited,
    ValidationFailed,
)
from drillai.core.ids import new_id
from drillai.db.models import (
    ApiToken,
    AuditLog,
    AuthRateLimitBucket,
    Membership,
    Organization,
    PasswordResetToken,
    User,
    UserSession,
)
from drillai.db.session import Database
from drillai.security.actions import Principal
from drillai.security.passwords import (
    hash_password,
    hash_password_async,
    hash_token,
    needs_rehash,
    new_api_token,
    verify_password_async,
    verify_token,
)
from drillai.security.rbac import (
    ROLE_CATALOGUE,
    SYSTEM_ROLES,
    permission_granted,
    principal_from_roles,
)

router = APIRouter(tags=["auth"])

# Pre-computed dummy password hash so login for non-existent emails performs identical KDF work.
_DUMMY_PASSWORD_HASH = hash_password("drillai-constant-time-dummy-password-verification")

MIN_PASSWORD_LENGTH = 12


def _validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationFailed(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters long",
            details={"min_length": MIN_PASSWORD_LENGTH},
        )
    if password.lower() in {
        "password1234",
        "123456789012",
        "qwertyuiop12",
        "changeme1234",
        "adminadmin12",
    }:
        raise ValidationFailed(
            "password is too common; choose a stronger passphrase",
            details={"reason": "common_password"},
        )


def _client_ip(request: Request) -> str:
    """Return the transport peer resolved by the ASGI server, never a caller-supplied header.

    Uvicorn only rewrites ``request.client.host`` from X-Forwarded-For when the immediate peer is
    explicitly listed in ``FORWARDED_ALLOW_IPS``. The Compose deployment pins that peer to the Nginx
    frontend address. Reading X-Forwarded-For here would let any direct client choose its own rate-limit
    bucket and forge security-audit IP addresses.
    """
    client_host = request.client.host if request.client is not None else None
    return str(client_host)[:64] if client_host else "unknown"


def _user_agent(request: Request) -> str:
    return (request.headers.get("user-agent") or "")[:300]


async def _check_rate_limits(
    database: Database,
    bucket_keys: list[ tuple[str, str] ],
) -> None:
    """Verify none of the given `(bucket_key, scope)` buckets are currently locked out."""
    now = dt.datetime.now(tz=UTC)
    async with database.session() as rl_session:
        for bucket_key, scope in bucket_keys:
            row = (
                await rl_session.execute(
                    select(AuthRateLimitBucket).where(AuthRateLimitBucket.bucket_key == bucket_key)
                )
            ).scalar_one_or_none()
            if row is None or row.blocked_until is None:
                continue
            blocked_until = row.blocked_until
            if blocked_until.tzinfo is None:
                blocked_until = blocked_until.replace(tzinfo=UTC)
            if blocked_until > now:
                retry_after = max(1, int((blocked_until - now).total_seconds()))
                raise RateLimited(
                    "too many failed authentication attempts; please wait before retrying",
                    details={
                        "scope": scope,
                        "retry_after_seconds": retry_after,
                        "blocked_until": blocked_until.isoformat(),
                    },
                )


async def _record_rate_limit_failure(
    database: Database,
    bucket_keys: list[tuple[str, str]],
) -> None:
    """Increment failure counters in an independent transaction so 401/403 does not roll them back."""
    settings = get_settings()
    now = dt.datetime.now(tz=UTC)
    window_delta = dt.timedelta(seconds=max(1, settings.auth_rate_limit_window_seconds))
    lockout_delta = dt.timedelta(seconds=max(1, settings.auth_rate_limit_lockout_seconds))
    max_failures = max(1, settings.auth_rate_limit_max_failures)

    async with database.session() as rl_session:
        for bucket_key, scope in bucket_keys:
            row = (
                await rl_session.execute(
                    select(AuthRateLimitBucket).where(AuthRateLimitBucket.bucket_key == bucket_key)
                )
            ).scalar_one_or_none()
            if row is None:
                failure_count = 1
                blocked_until = now + lockout_delta if failure_count >= max_failures else None
                rl_session.add(
                    AuthRateLimitBucket(
                        id=new_id("rlb"),
                        bucket_key=bucket_key,
                        scope=scope,
                        window_started_at=now,
                        failure_count=failure_count,
                        blocked_until=blocked_until,
                        last_attempt_at=now,
                    )
                )
            else:
                win_start = row.window_started_at
                if win_start.tzinfo is None:
                    win_start = win_start.replace(tzinfo=UTC)
                if (now - win_start) > window_delta:
                    row.window_started_at = now
                    row.failure_count = 1
                    row.blocked_until = None
                else:
                    row.failure_count = int(row.failure_count or 0) + 1
                row.last_attempt_at = now
                if row.failure_count >= max_failures:
                    row.blocked_until = now + lockout_delta


async def _reset_rate_limit_buckets(database: Database, bucket_keys: list[str]) -> None:
    async with database.session() as rl_session:
        for key in bucket_keys:
            row = (
                await rl_session.execute(
                    select(AuthRateLimitBucket).where(AuthRateLimitBucket.bucket_key == key)
                )
            ).scalar_one_or_none()
            if row is not None:
                row.failure_count = 0
                row.blocked_until = None


async def _record_audit_independent(
    database: Database,
    *,
    org_id: str,
    actor_id: str | None,
    actor_display: str | None,
    action: str,
    resource_kind: str,
    resource_id: str | None,
    outcome: str,
    request: Request,
    details: dict[str, Any] | None = None,
) -> None:
    """Persist a security audit event in an independent session so failed logins are still audited."""
    now = dt.datetime.now(tz=UTC)
    req_id = getattr(request.state, "request_id", None) or request.headers.get("x-request-id")
    async with database.session() as aud_session:
        aud_session.add(
            AuditLog(
                id=new_id("aud"),
                org_id=org_id,
                actor_kind="user",
                actor_id=actor_id,
                actor_display=actor_display,
                action=action,
                action_level="L0",
                resource_kind=resource_kind,
                resource_id=resource_id,
                outcome=outcome,
                details=dict(details or {}),
                ip_address=_client_ip(request),
                user_agent=_user_agent(request),
                request_id=req_id,
                occurred_at=now,
            )
        )


def _set_session_cookie(response: Response, raw_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw_token,
        httponly=True,
        secure=bool(settings.session_cookie_secure or settings.is_production),
        samesite=settings.session_cookie_samesite,
        max_age=max(60, int(settings.session_ttl_hours * 3600)),
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.session_cookie_name,
        httponly=True,
        secure=bool(settings.session_cookie_secure or settings.is_production),
        samesite=settings.session_cookie_samesite,
        path="/",
    )


# --------------------------------------------------------------------------- schemas


class BootstrapRequest(BaseModel):
    org_slug: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    org_name: str = Field(min_length=2, max_length=200)
    email: str = Field(min_length=3, max_length=255)
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)
    role_keys: list[str] = Field(default_factory=lambda: ["admin", "well_manager"])


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)
    org_slug: str | None = Field(default=None, max_length=80)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)


class PasswordResetIssueRequest(BaseModel):
    user_id: str = Field(min_length=3, max_length=64)
    ttl_minutes: int = Field(default=60, ge=5, le=1440)


class PasswordResetRedeemRequest(BaseModel):
    token: str = Field(min_length=10, max_length=256)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)


class UserCreateRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    display_name: str = Field(min_length=1, max_length=200)
    job_title: str | None = Field(default=None, max_length=200)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)
    role_keys: list[str] = Field(min_length=1)
    unit_system: str | None = Field(default="field_us", max_length=32)


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    job_title: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None
    role_keys: list[str] | None = None


class ApiTokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(default_factory=list)
    expires_in_days: int | None = Field(default=90, ge=1, le=3650)
    user_id: str | None = Field(default=None, max_length=64)


# --------------------------------------------------------------------------- endpoints


@router.post("/auth/bootstrap", status_code=201, summary="Bootstrap initial organization and admin user")
async def bootstrap_admin(
    body: BootstrapRequest,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """One-time bootstrap on an uninitialized database (zero password-enabled users)."""
    _validate_password_strength(body.password)
    existing_users = (
        await session.execute(
            select(func.count()).select_from(User).where(User.password_hash.is_not(None))
        )
    ).scalar_one()
    if int(existing_users or 0) > 0:
        raise Conflict(
            "platform bootstrap has already been completed; use admin user provisioning instead",
            details={"reason": "bootstrap_already_completed"},
        )

    valid_roles = [key for key in body.role_keys if key in SYSTEM_ROLES]
    if not valid_roles:
        raise ValidationFailed("at least one valid system role_key is required")

    org = (
        await session.execute(select(Organization).where(Organization.slug == body.org_slug))
    ).scalar_one_or_none()
    if org is None:
        org = Organization(
            id=new_id("org"),
            slug=body.org_slug,
            name=body.org_name,
            kind="operator",
            is_active=True,
        )
        session.add(org)
        await session.flush()

    email_norm = body.email.strip().lower()
    user = User(
        id=new_id("usr"),
        email=email_norm,
        display_name=body.display_name.strip(),
        password_hash=await hash_password_async(body.password),
        is_active=True,
        last_login_at=dt.datetime.now(tz=UTC),
    )
    session.add(user)
    await session.flush()

    for role_key in valid_roles:
        session.add(
            Membership(
                id=new_id("mbr"),
                org_id=org.id,
                user_id=user.id,
                role_key=role_key,
                scope_kind="org",
                granted_by=user.id,
            )
        )

    now = dt.datetime.now(tz=UTC)
    settings = get_settings()
    raw_token, token_prefix, token_hash = new_api_token(prefix="ds")
    session_id = new_id("ses")
    csrf_token = derive_session_csrf_token(session_id, token_hash)
    session_row = UserSession(
        id=session_id,
        org_id=org.id,
        user_id=user.id,
        session_token_prefix=token_prefix,
        session_token_hash=token_hash,
        csrf_token_hash=hash_token("csrf", csrf_token),
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
        expires_at=now + dt.timedelta(hours=settings.session_ttl_hours),
        last_seen_at=now,
    )
    session.add(session_row)
    session.add(
        AuditLog(
            id=new_id("aud"),
            org_id=org.id,
            actor_kind="user",
            actor_id=user.id,
            actor_display=user.display_name,
            action="auth.bootstrap",
            action_level="L4",
            resource_kind="user",
            resource_id=user.id,
            outcome="success",
            details={"org_slug": org.slug, "role_keys": valid_roles},
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
            occurred_at=now,
        )
    )
    await session.flush()

    _set_session_cookie(response, raw_token)
    roles = [SYSTEM_ROLES[k] for k in valid_roles]
    principal = principal_from_roles(
        principal_id=user.id,
        org_id=org.id,
        roles=roles,
        display_name=user.display_name,
    )
    return {
        "session_id": session_row.id,
        "csrf_token": csrf_token,
        "expires_at": session_row.expires_at.isoformat(),
        "user": {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "job_title": user.job_title,
        },
        "organization": {"id": org.id, "slug": org.slug, "name": org.name},
        "principal": {
            "principal_id": principal.id,
            "org_id": org.id,
            "role_keys": list(principal.role_keys),
            "permissions": sorted(principal.permissions),
            "max_action_level": principal.max_action_level.value,
            "identity_source": "session_cookie",
        },
    }


@router.post("/auth/login", summary="Authenticate with email and password and establish an HttpOnly session")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
    database: Annotated[Database, Depends(get_database)],
) -> dict[str, Any]:
    email_norm = body.email.strip().lower()
    ip = _client_ip(request)
    buckets = [
        (f"login_ip:{ip}", "login_ip"),
        (f"login_acct:{email_norm}", "login_account"),
    ]
    await _check_rate_limits(database, buckets)

    user = (
        await session.execute(select(User).where(func.lower(User.email) == email_norm))
    ).scalar_one_or_none()

    if user is None or not user.password_hash:
        await verify_password_async(body.password, _DUMMY_PASSWORD_HASH)
        await _record_rate_limit_failure(database, buckets)
        await _record_audit_independent(
            database,
            org_id="system",
            actor_id=None,
            actor_display=email_norm,
            action="auth.login",
            resource_kind="session",
            resource_id=None,
            outcome="failure",
            request=request,
            details={"reason": "invalid_credentials"},
        )
        raise AuthenticationRequired("invalid email or password")

    if not await verify_password_async(body.password, user.password_hash):
        await _record_rate_limit_failure(database, buckets)
        await _record_audit_independent(
            database,
            org_id="system",
            actor_id=user.id,
            actor_display=user.email,
            action="auth.login",
            resource_kind="session",
            resource_id=None,
            outcome="failure",
            request=request,
            details={"reason": "invalid_credentials"},
        )
        raise AuthenticationRequired("invalid email or password")

    if not user.is_active:
        await _record_rate_limit_failure(database, buckets)
        raise AuthenticationRequired("user account is deactivated")

    if needs_rehash(user.password_hash):
        user.password_hash = await hash_password_async(body.password)

    now = dt.datetime.now(tz=UTC)
    mbr_rows = (
        await session.execute(select(Membership).where(Membership.user_id == user.id))
    ).scalars().all()
    active_mbrs = [
        m
        for m in mbr_rows
        if m.expires_at is None
        or (
            (m.expires_at.replace(tzinfo=UTC) if m.expires_at.tzinfo is None else m.expires_at)
            > now
        )
    ]
    if not active_mbrs:
        raise PermissionDenied("user has no active organization membership")

    org_ids = sorted({m.org_id for m in active_mbrs})
    orgs = (
        await session.execute(
            select(Organization).where(Organization.id.in_(org_ids), Organization.is_active.is_(True))
        )
    ).scalars().all()
    if not orgs:
        raise PermissionDenied("user belongs to no active organization")

    target_org: Organization | None = None
    if body.org_slug:
        for candidate in orgs:
            if candidate.slug == body.org_slug.strip():
                target_org = candidate
                break
        if target_org is None:
            raise PermissionDenied(
                f"user does not have an active membership in organization {body.org_slug!r}"
            )
    else:
        target_org = orgs[0]

    org_role_keys = [
        m.role_key
        for m in active_mbrs
        if m.org_id == target_org.id and m.role_key in SYSTEM_ROLES
    ]
    if not org_role_keys:
        raise PermissionDenied("user has no recognized system roles in the selected organization")

    await _reset_rate_limit_buckets(database, [f"login_acct:{email_norm}", f"login_ip:{ip}"])

    settings = get_settings()
    raw_token, token_prefix, token_hash = new_api_token(prefix="ds")
    session_id = new_id("ses")
    csrf_token = derive_session_csrf_token(session_id, token_hash)
    session_row = UserSession(
        id=session_id,
        org_id=target_org.id,
        user_id=user.id,
        session_token_prefix=token_prefix,
        session_token_hash=token_hash,
        csrf_token_hash=hash_token("csrf", csrf_token),
        ip_address=ip,
        user_agent=_user_agent(request),
        expires_at=now + dt.timedelta(hours=settings.session_ttl_hours),
        last_seen_at=now,
    )
    user.last_login_at = now
    session.add(session_row)
    session.add(
        AuditLog(
            id=new_id("aud"),
            org_id=target_org.id,
            actor_kind="user",
            actor_id=user.id,
            actor_display=user.display_name,
            action="auth.login",
            action_level="L0",
            resource_kind="session",
            resource_id=session_row.id,
            outcome="success",
            details={"org_slug": target_org.slug, "role_keys": org_role_keys},
            ip_address=ip,
            user_agent=_user_agent(request),
            occurred_at=now,
        )
    )
    await session.flush()

    _set_session_cookie(response, raw_token)
    roles = [SYSTEM_ROLES[k] for k in org_role_keys]
    principal = principal_from_roles(
        principal_id=user.id,
        org_id=target_org.id,
        roles=roles,
        display_name=user.display_name,
    )
    return {
        "session_id": session_row.id,
        "csrf_token": csrf_token,
        "expires_at": session_row.expires_at.isoformat(),
        "user": {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "job_title": user.job_title,
            "locale": user.locale,
            "unit_system": user.unit_system or "field_us",
        },
        "organization": {
            "id": target_org.id,
            "slug": target_org.slug,
            "name": target_org.name,
        },
        "organizations": [
            {"id": o.id, "slug": o.slug, "name": o.name}
            for o in orgs
        ],
        "principal": {
            "principal_id": principal.id,
            "principal_kind": "user",
            "display_name": user.display_name,
            "user_email": user.email,
            "org_id": target_org.id,
            "role_keys": list(principal.role_keys),
            "roles": [
                {
                    "key": role.key,
                    "name": role.name,
                    "description": role.description,
                    "max_action_level": role.max_action_level.value,
                }
                for role in ROLE_CATALOGUE
                if role.key in principal.role_keys
            ],
            "permissions": sorted(principal.permissions),
            "max_action_level": principal.max_action_level.value,
            "auth_enabled": settings.auth_enabled,
            "identity_source": "session_cookie",
        },
    }


@router.post("/auth/logout", summary="Invalidate the active server-side session and clear the session cookie")
async def logout(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(current_auth)],
) -> dict[str, Any]:
    now = dt.datetime.now(tz=UTC)
    if auth.session_id:
        row = (
            await session.execute(
                select(UserSession).where(UserSession.id == auth.session_id)
            )
        ).scalar_one_or_none()
        if row is not None and row.revoked_at is None:
            row.revoked_at = now
            row.revoked_reason = "user_logout"
    if auth.org_id:
        session.add(
            AuditLog(
                id=new_id("aud"),
                org_id=auth.org_id,
                actor_kind="user",
                actor_id=auth.principal.id,
                actor_display=auth.principal.display_name,
                action="auth.logout",
                action_level="L0",
                resource_kind="session",
                resource_id=auth.session_id,
                outcome="success",
                ip_address=_client_ip(request),
                user_agent=_user_agent(request),
                occurred_at=now,
            )
        )
    _clear_session_cookie(response)
    return {"status": "logged_out", "session_id": auth.session_id}


@router.get("/auth/me", summary="Current authenticated user, organization, session, and permissions")
async def auth_me(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(current_auth)],
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    settings = get_settings()
    user = (
        await session.execute(select(User).where(User.id == auth.principal.id))
    ).scalar_one_or_none()
    org = (
        await session.execute(select(Organization).where(Organization.id == auth.org_id))
    ).scalar_one_or_none() if auth.org_id else None

    principal = auth.principal
    payload: dict[str, Any] = {
        "authenticated": True,
        "auth_enabled": settings.auth_enabled,
        "identity_source": auth.identity_source if settings.auth_enabled else "development_header",
        "session_id": auth.session_id,
        "user": (
            {
                "id": user.id,
                "email": user.email,
                "display_name": user.display_name,
                "job_title": user.job_title,
                "locale": user.locale,
                "unit_system": user.unit_system or "field_us",
            }
            if user is not None
            else {
                "id": principal.id,
                "email": auth.user_email,
                "display_name": principal.display_name,
                "job_title": None,
                "locale": auth.locale,
                "unit_system": auth.unit_system,
            }
        ),
        "organization": (
            {"id": org.id, "slug": org.slug, "name": org.name}
            if org is not None
            else {"id": auth.org_id, "slug": None, "name": None}
        ),
        "principal": {
            "principal_id": principal.id,
            "principal_kind": getattr(principal, "kind", "user"),
            "display_name": principal.display_name,
            "org_id": auth.org_id,
            "role_keys": list(principal.role_keys),
            "roles": [
                {
                    "key": role.key,
                    "name": role.name,
                    "description": role.description,
                    "max_action_level": role.max_action_level.value,
                }
                for role in ROLE_CATALOGUE
                if role.key in principal.role_keys
            ],
            "permissions": sorted(principal.permissions),
            "scopes": list(principal.scopes),
            "max_action_level": principal.max_action_level.value,
        },
    }
    if auth.csrf_token:
        payload["csrf_token"] = auth.csrf_token
    return payload


@router.post("/auth/password/change", summary="Change the authenticated user's password and revoke other sessions")
async def change_password(
    body: PasswordChangeRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(current_auth)],
) -> dict[str, Any]:
    _validate_password_strength(body.new_password)
    user = (
        await session.execute(select(User).where(User.id == auth.principal.id))
    ).scalar_one_or_none()
    if user is None or not user.password_hash:
        raise AuthenticationRequired("authenticated user record does not support password change")
    if not await verify_password_async(body.current_password, user.password_hash):
        raise AuthenticationRequired("current password is incorrect")

    user.password_hash = await hash_password_async(body.new_password)
    now = dt.datetime.now(tz=UTC)
    other_sessions = (
        await session.execute(
            select(UserSession).where(
                UserSession.user_id == user.id,
                UserSession.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    revoked_count = 0
    for s_row in other_sessions:
        if s_row.id != auth.session_id:
            s_row.revoked_at = now
            s_row.revoked_reason = "password_changed"
            revoked_count += 1

    if auth.org_id:
        session.add(
            AuditLog(
                id=new_id("aud"),
                org_id=auth.org_id,
                actor_kind="user",
                actor_id=user.id,
                actor_display=user.display_name,
                action="auth.password.change",
                action_level="L2",
                resource_kind="user",
                resource_id=user.id,
                outcome="success",
                details={"revoked_other_sessions": revoked_count},
                ip_address=_client_ip(request),
                user_agent=_user_agent(request),
                occurred_at=now,
            )
        )
    return {"status": "password_changed", "revoked_other_sessions": revoked_count}


@router.post(
    "/auth/password/reset-tokens",
    status_code=201,
    summary="Issue a single-use password reset token for a user in the caller's organization",
)
async def issue_password_reset_token(
    body: PasswordResetIssueRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("user.manage"))],
) -> dict[str, Any]:
    org_id = auth.org_id or ""
    membership = (
        await session.execute(
            select(Membership).where(
                Membership.org_id == org_id,
                Membership.user_id == body.user_id,
            )
        )
    ).scalars().first()
    if membership is None:
        raise NotFound(f"user {body.user_id!r} not found in this organization")

    user = (
        await session.execute(select(User).where(User.id == body.user_id))
    ).scalar_one_or_none()
    if user is None:
        raise NotFound(f"user {body.user_id!r} not found")

    # Passwords are global to the account rather than scoped to one organization. A tenant admin must
    # not be able to reset a shared identity used by another organization.
    other_org_membership = (
        await session.execute(
            select(Membership.id)
            .where(Membership.user_id == user.id, Membership.org_id != org_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if other_org_membership is not None:
        raise PermissionDenied(
            "cannot issue an organization-scoped password reset for a user who belongs to another organization",
            details={"reason": "cross_organization_account"},
        )

    now = dt.datetime.now(tz=UTC)
    raw_token, token_prefix, token_hash = new_api_token(prefix="prt")
    expires_at = now + dt.timedelta(minutes=body.ttl_minutes)
    reset_row = PasswordResetToken(
        id=new_id("prt"),
        org_id=org_id,
        user_id=user.id,
        token_prefix=token_prefix,
        token_hash=token_hash,
        issued_by_id=auth.principal.id,
        expires_at=expires_at,
    )
    session.add(reset_row)
    session.add(
        AuditLog(
            id=new_id("aud"),
            org_id=org_id,
            actor_kind="user",
            actor_id=auth.principal.id,
            actor_display=auth.principal.display_name,
            action="auth.password.reset_token_issued",
            action_level="L3",
            resource_kind="user",
            resource_id=user.id,
            outcome="success",
            details={"reset_token_id": reset_row.id, "expires_at": expires_at.isoformat()},
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
            occurred_at=now,
        )
    )
    await session.flush()
    return {
        "id": reset_row.id,
        "user_id": user.id,
        "token_prefix": token_prefix,
        "reset_token": raw_token,
        "expires_at": expires_at.isoformat(),
    }


@router.post("/auth/password/reset", summary="Redeem a single-use password reset token")
async def redeem_password_reset_token(
    body: PasswordResetRedeemRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    database: Annotated[Database, Depends(get_database)],
) -> dict[str, Any]:
    ip = _client_ip(request)
    buckets = [(f"pwd_reset_ip:{ip}", "password_reset")]
    await _check_rate_limits(database, buckets)
    _validate_password_strength(body.new_password)

    parts = body.token.strip().split("_")
    if len(parts) < 3 or parts[0] != "prt":
        await _record_rate_limit_failure(database, buckets)
        raise AuthenticationRequired("invalid or expired password reset token")

    token_prefix = parts[1]
    row = (
        await session.execute(
            select(PasswordResetToken).where(PasswordResetToken.token_prefix == token_prefix)
        )
    ).scalar_one_or_none()
    now = dt.datetime.now(tz=UTC)
    if (
        row is None
        or row.used_at is not None
        or row.revoked_at is not None
        or not verify_token(body.token.strip(), row.token_prefix, row.token_hash)
    ):
        await _record_rate_limit_failure(database, buckets)
        raise AuthenticationRequired("invalid or expired password reset token")

    exp = row.expires_at.replace(tzinfo=UTC) if row.expires_at.tzinfo is None else row.expires_at
    if exp <= now:
        await _record_rate_limit_failure(database, buckets)
        raise AuthenticationRequired("password reset token has expired")

    user = (
        await session.execute(select(User).where(User.id == row.user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthenticationRequired("target user account is not active")

    row.used_at = now
    user.password_hash = await hash_password_async(body.new_password)

    active_sessions = (
        await session.execute(
            select(UserSession).where(
                UserSession.user_id == user.id,
                UserSession.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    for s_row in active_sessions:
        s_row.revoked_at = now
        s_row.revoked_reason = "password_reset"

    session.add(
        AuditLog(
            id=new_id("aud"),
            org_id=row.org_id,
            actor_kind="user",
            actor_id=user.id,
            actor_display=user.display_name,
            action="auth.password.reset",
            action_level="L3",
            resource_kind="user",
            resource_id=user.id,
            outcome="success",
            details={"reset_token_id": row.id, "revoked_sessions": len(active_sessions)},
            ip_address=ip,
            user_agent=_user_agent(request),
            occurred_at=now,
        )
    )
    return {"status": "password_reset_complete", "user_id": user.id}


# --------------------------------------------------------------------------- tenant user management


def _validate_assignable_roles(auth: AuthContext, role_keys: list[str]) -> list[str]:
    if not role_keys:
        raise ValidationFailed("at least one role_key must be specified")
    unknown = [k for k in role_keys if k not in SYSTEM_ROLES]
    if unknown:
        raise ValidationFailed(
            f"unknown role_key(s): {', '.join(unknown)}",
            details={"unknown_roles": unknown, "available_roles": sorted(SYSTEM_ROLES)},
        )
    # Only the explicit platform-admin role may delegate platform-wide administration or the
    # wildcard/L5 well-manager role. A permission wildcard such as `**` on a tenant role is not itself
    # proof of platform-admin authority: that distinction must not be erased by permission matching.
    is_platform_admin = "admin" in auth.principal.role_keys and permission_granted(
        auth.principal, "system.egress_policy.manage"
    )
    for key in role_keys:
        if key in {"admin", "well_manager"} and not is_platform_admin:
            raise PermissionDenied(
                "tenant administrators cannot grant platform-wide or wildcard automation roles",
                details={"forbidden_role": key},
            )
    return list(dict.fromkeys(role_keys))


@router.get("/auth/users", summary="List users and role memberships in the caller's organization")
async def list_org_users(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("user.read"))],
) -> dict[str, Any]:
    org_id = auth.org_id or ""
    memberships = (
        await session.execute(select(Membership).where(Membership.org_id == org_id))
    ).scalars().all()
    user_ids = sorted({m.user_id for m in memberships})
    if not user_ids:
        return {"items": [], "total": 0}
    users = (
        await session.execute(select(User).where(User.id.in_(user_ids)).order_by(User.email))
    ).scalars().all()
    roles_by_user: dict[str, list[str]] = {}
    for m in memberships:
        roles_by_user.setdefault(m.user_id, []).append(m.role_key)
    items = [
        {
            "id": u.id,
            "email": u.email,
            "display_name": u.display_name,
            "job_title": u.job_title,
            "is_active": u.is_active,
            "role_keys": sorted(set(roles_by_user.get(u.id, []))),
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]
    return {"items": items, "total": len(items)}


@router.post("/auth/users", status_code=201, summary="Provision a user and assign roles in the caller's organization")
async def create_org_user(
    body: UserCreateRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("user.manage"))],
) -> dict[str, Any]:
    _validate_password_strength(body.password)
    role_keys = _validate_assignable_roles(auth, body.role_keys)
    org_id = auth.org_id or ""
    email_norm = body.email.strip().lower()

    existing = (
        await session.execute(select(User).where(func.lower(User.email) == email_norm))
    ).scalar_one_or_none()
    if existing is not None:
        raise Conflict(f"a user with email {email_norm!r} already exists")

    now = dt.datetime.now(tz=UTC)
    user = User(
        id=new_id("usr"),
        email=email_norm,
        display_name=body.display_name.strip(),
        job_title=body.job_title,
        password_hash=await hash_password_async(body.password),
        unit_system=body.unit_system or "field_us",
        is_active=True,
    )
    session.add(user)
    await session.flush()

    for rk in role_keys:
        session.add(
            Membership(
                id=new_id("mbr"),
                org_id=org_id,
                user_id=user.id,
                role_key=rk,
                scope_kind="org",
                granted_by=auth.principal.id,
            )
        )

    session.add(
        AuditLog(
            id=new_id("aud"),
            org_id=org_id,
            actor_kind="user",
            actor_id=auth.principal.id,
            actor_display=auth.principal.display_name,
            action="auth.user.create",
            action_level="L3",
            resource_kind="user",
            resource_id=user.id,
            outcome="success",
            details={"email": user.email, "role_keys": role_keys},
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
            occurred_at=now,
        )
    )
    await session.flush()
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "job_title": user.job_title,
        "is_active": user.is_active,
        "org_id": org_id,
        "role_keys": role_keys,
    }


@router.patch("/auth/users/{user_id}", summary="Update a user's roles or active status in the caller's organization")
async def update_org_user(
    user_id: str,
    body: UserUpdateRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("user.manage"))],
) -> dict[str, Any]:
    org_id = auth.org_id or ""
    existing_mbrs = (
        await session.execute(
            select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
        )
    ).scalars().all()
    if not existing_mbrs:
        raise NotFound(f"user {user_id!r} not found in this organization")

    user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise NotFound(f"user {user_id!r} not found")

    now = dt.datetime.now(tz=UTC)
    if body.display_name is not None:
        user.display_name = body.display_name.strip()
    if body.job_title is not None:
        user.job_title = body.job_title

    if body.role_keys is not None:
        new_roles = _validate_assignable_roles(auth, body.role_keys)
        for m in existing_mbrs:
            await session.delete(m)
        await session.flush()
        for rk in new_roles:
            session.add(
                Membership(
                    id=new_id("mbr"),
                    org_id=org_id,
                    user_id=user.id,
                    role_key=rk,
                    scope_kind="org",
                    granted_by=auth.principal.id,
                )
            )
    else:
        new_roles = sorted({m.role_key for m in existing_mbrs})

    if body.is_active is not None:
        if body.is_active is False:
            if user.id == auth.principal.id:
                raise PermissionDenied("cannot deactivate your own account through tenant user management")
            # `User.is_active`, sessions and API tokens are global to an identity. An organization
            # administrator may not use a tenant-scoped endpoint to disable a shared account or revoke
            # credentials used in another organization's membership.
            other_org_membership = (
                await session.execute(
                    select(Membership.id)
                    .where(Membership.user_id == user.id, Membership.org_id != org_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if other_org_membership is not None:
                raise PermissionDenied(
                    "cannot deactivate a user who belongs to another organization",
                    details={"reason": "cross_organization_account"},
                )
        user.is_active = body.is_active
        if not body.is_active:
            # Immediately revoke all active sessions and API tokens for this now-disabled identity.
            sessions_to_revoke = (
                await session.execute(
                    select(UserSession).where(
                        UserSession.user_id == user.id,
                        UserSession.revoked_at.is_(None),
                    )
                )
            ).scalars().all()
            for s_row in sessions_to_revoke:
                s_row.revoked_at = now
                s_row.revoked_reason = "user_deactivated"
            tokens_to_revoke = (
                await session.execute(
                    select(ApiToken).where(
                        ApiToken.user_id == user.id,
                        ApiToken.revoked_at.is_(None),
                    )
                )
            ).scalars().all()
            for t_row in tokens_to_revoke:
                t_row.revoked_at = now

    session.add(
        AuditLog(
            id=new_id("aud"),
            org_id=org_id,
            actor_kind="user",
            actor_id=auth.principal.id,
            actor_display=auth.principal.display_name,
            action="auth.user.update",
            action_level="L3",
            resource_kind="user",
            resource_id=user.id,
            outcome="success",
            details={"is_active": user.is_active, "role_keys": new_roles},
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
            occurred_at=now,
        )
    )
    await session.flush()
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "job_title": user.job_title,
        "is_active": user.is_active,
        "org_id": org_id,
        "role_keys": new_roles,
    }


# --------------------------------------------------------------------------- API token lifecycle


def _principal_may_delegate_scope(principal: Principal, scope: str) -> bool:
    """A token scope may narrow a principal, but a wildcard may not broaden one."""
    if "*" in scope:
        held = set(principal.permissions)
        if scope not in held and not ({"*", "**"} & held):
            return False
        scoped_grants = set(principal.scopes)
        return not principal.scopes or scope in scoped_grants or bool({"*", "**"} & scoped_grants)
    return permission_granted(principal, scope)


def _token_metadata_out(row: ApiToken) -> dict[str, Any]:
    """Serialize API token metadata without ever exposing `token_hash` or the clear secret."""
    return {
        "id": row.id,
        "org_id": row.org_id,
        "user_id": row.user_id,
        "name": row.name,
        "token_prefix": row.token_prefix,
        "scopes": list(row.scopes or []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "is_active": row.revoked_at is None
        and (
            row.expires_at is None
            or (
                row.expires_at.replace(tzinfo=UTC)
                if row.expires_at.tzinfo is None
                else row.expires_at
            )
            > dt.datetime.now(tz=UTC)
        ),
    }


@router.get("/auth/tokens", summary="List API token metadata in the caller's organization (no secret material)")
async def list_api_tokens(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(current_auth)],
) -> dict[str, Any]:
    org_id = auth.org_id or ""
    stmt = select(ApiToken).where(ApiToken.org_id == org_id)
    if not permission_granted(auth.principal, "user.read"):
        stmt = stmt.where(ApiToken.user_id == auth.principal.id)
    rows = (await session.execute(stmt.order_by(ApiToken.created_at.desc()))).scalars().all()
    return {"items": [_token_metadata_out(r) for r in rows], "total": len(rows)}


@router.post(
    "/auth/tokens",
    status_code=201,
    summary="Mint a scoped API token (clear secret returned once; only digest stored)",
)
async def create_api_token(
    body: ApiTokenCreateRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    database: Annotated[Database, Depends(get_database)],
    auth: Annotated[AuthContext, Depends(current_auth)],
) -> dict[str, Any]:
    org_id = auth.org_id or ""
    if not org_id:
        raise PermissionDenied("an organization context is required to mint an API token")

    ip = _client_ip(request)
    await _check_rate_limits(database, [(f"token_create:{auth.principal.id}:{ip}", "token_create")])

    target_user_id = body.user_id or auth.principal.id
    if target_user_id != auth.principal.id and not permission_granted(auth.principal, "user.manage"):
        raise PermissionDenied("only user administrators may mint tokens on behalf of another user")

    target_user = (
        await session.execute(select(User).where(User.id == target_user_id))
    ).scalar_one_or_none()
    if target_user is None or not target_user.is_active:
        raise NotFound(f"active user {target_user_id!r} not found")

    mbr_rows = (
        await session.execute(
            select(Membership).where(
                Membership.org_id == org_id,
                Membership.user_id == target_user.id,
            )
        )
    ).scalars().all()
    if not mbr_rows:
        raise NotFound(f"user {target_user_id!r} has no membership in this organization")

    target_roles = [SYSTEM_ROLES[m.role_key] for m in mbr_rows if m.role_key in SYSTEM_ROLES]
    target_principal = principal_from_roles(
        principal_id=target_user.id,
        org_id=org_id,
        roles=target_roles,
        display_name=target_user.display_name,
    )

    # Every requested scope must be held by BOTH the caller and the token owner; wildcard or
    # system-wide egress scopes cannot be minted by a tenant admin who lacks them.
    requested_scopes = list(dict.fromkeys(s.strip() for s in body.scopes if s.strip()))
    if not requested_scopes:
        raise ValidationFailed("at least one explicit API token scope must be specified")
    for scope_pat in requested_scopes:
        if scope_pat in {"*", "**"} and not permission_granted(auth.principal, "system.egress_policy.manage"):
            raise PermissionDenied(
                f"cannot mint API token with unrestricted wildcard scope {scope_pat!r}",
                details={"forbidden_scope": scope_pat},
            )
        if not _principal_may_delegate_scope(auth.principal, scope_pat):
            raise PermissionDenied(
                f"caller does not hold requested token scope {scope_pat!r}",
                details={"forbidden_scope": scope_pat},
            )
        if not _principal_may_delegate_scope(target_principal, scope_pat):
            raise PermissionDenied(
                f"target user does not hold requested token scope {scope_pat!r}",
                details={"forbidden_scope": scope_pat},
            )

    now = dt.datetime.now(tz=UTC)
    expires_at = (
        now + dt.timedelta(days=body.expires_in_days)
        if body.expires_in_days is not None
        else None
    )
    raw_token, token_prefix, token_hash = new_api_token(prefix="dk")
    row = ApiToken(
        id=new_id("tok"),
        org_id=org_id,
        user_id=target_user.id,
        name=body.name.strip(),
        token_prefix=token_prefix,
        token_hash=token_hash,
        scopes=requested_scopes,
        expires_at=expires_at,
    )
    session.add(row)
    session.add(
        AuditLog(
            id=new_id("aud"),
            org_id=org_id,
            actor_kind="user",
            actor_id=auth.principal.id,
            actor_display=auth.principal.display_name,
            action="auth.token.create",
            action_level="L2",
            resource_kind="api_token",
            resource_id=row.id,
            outcome="success",
            details={
                "token_prefix": token_prefix,
                "scopes": requested_scopes,
                "user_id": target_user.id,
            },
            ip_address=ip,
            user_agent=_user_agent(request),
            occurred_at=now,
        )
    )
    await session.flush()
    out = _token_metadata_out(row)
    out["token"] = raw_token  # Shown once on creation; never persisted in clear text.
    return out


@router.delete("/auth/tokens/{token_id}", summary="Revoke an API token immediately")
async def revoke_api_token(
    token_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(current_auth)],
) -> dict[str, Any]:
    org_id = auth.org_id or ""
    row = (
        await session.execute(
            select(ApiToken).where(ApiToken.id == token_id, ApiToken.org_id == org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound(f"api token {token_id!r} not found")
    if row.user_id != auth.principal.id and not permission_granted(auth.principal, "user.manage"):
        raise PermissionDenied("cannot revoke another user's API token without user.manage permission")

    now = dt.datetime.now(tz=UTC)
    if row.revoked_at is None:
        row.revoked_at = now

    session.add(
        AuditLog(
            id=new_id("aud"),
            org_id=org_id,
            actor_kind="user",
            actor_id=auth.principal.id,
            actor_display=auth.principal.display_name,
            action="auth.token.revoke",
            action_level="L2",
            resource_kind="api_token",
            resource_id=row.id,
            outcome="success",
            details={"token_prefix": row.token_prefix},
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
            occurred_at=now,
        )
    )
    await session.flush()
    return _token_metadata_out(row)
