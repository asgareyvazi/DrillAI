"""Tenant-scoped authoring, review, decision and export for structured engineering products."""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, get_db, require
from drillai.core.audit import record_audit
from drillai.core.errors import ValidationFailed
from drillai.core.idempotency import complete, replay_or_reserve
from drillai.db.models import ApprovalRequest, Artifact, ArtifactVersion
from drillai.documents.scope import DocumentScope
from drillai.engineering.exports import product_export_payload, render_product_json, render_product_markdown
from drillai.engineering.products import ProductPayloadError, parse_product_payload
from drillai.engineering.service import EngineeringProductService
from drillai.security.actions import authorize

router = APIRouter(tags=["engineering-products"])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EngineeringProductCreate(_StrictModel):
    kind: Literal["program", "procedure"]
    title: str = Field(min_length=1, max_length=300)
    code: str | None = Field(default=None, max_length=120)
    project_id: str | None = Field(default=None, max_length=64)
    wellbore_id: str | None = Field(default=None, max_length=64)
    section_id: str | None = Field(default=None, max_length=64)
    operation_id: str | None = Field(default=None, max_length=64)
    payload: dict[str, Any]
    linked_program_version_id: str | None = Field(default=None, max_length=64)


class EngineeringProductRevision(_StrictModel):
    expected_current_version: int = Field(ge=1)
    change_reason: str = Field(min_length=1, max_length=2000)
    payload: dict[str, Any]
    linked_program_version_id: str | None = Field(default=None, max_length=64)


class EngineeringProductReview(_StrictModel):
    notes: str = Field(min_length=1, max_length=4000)


class EngineeringProductDecision(_StrictModel):
    approval_id: str = Field(min_length=1, max_length=64)
    decision: Literal["approved", "rejected"]
    note: str = Field(min_length=1, max_length=4000)


def _service(session: AsyncSession, auth: AuthContext) -> EngineeringProductService:
    return EngineeringProductService(session, auth.org_id or "")


def _parse_payload(kind: str, payload: dict[str, Any]):
    try:
        return parse_product_payload(kind, payload)
    except ProductPayloadError as exc:
        raise ValidationFailed(str(exc), details={"issues": exc.issues}) from exc


def _artifact_json(artifact: Artifact) -> dict[str, Any]:
    return {
        "id": artifact.id,
        "org_id": artifact.org_id,
        "kind": artifact.kind,
        "title": artifact.title,
        "code": artifact.code,
        "status": artifact.status,
        "current_version": artifact.current_version,
        "owner": artifact.owner,
        "project_id": artifact.project_id,
        "well_id": artifact.well_id,
        "wellbore_id": artifact.wellbore_id,
        "section_id": artifact.section_id,
        "operation_id": artifact.operation_id,
        "domain_pack": artifact.domain_pack,
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
        "updated_at": artifact.updated_at.isoformat() if artifact.updated_at else None,
    }


def _version_json(version: ArtifactVersion) -> dict[str, Any]:
    return {
        "id": version.id,
        "artifact_id": version.artifact_id,
        "version": version.version,
        "state": version.state,
        "schema_key": version.schema_key,
        "schema_version": version.schema_version,
        "payload": version.payload or {},
        "authored_by": version.authored_by,
        "authored_at": version.authored_at.isoformat() if version.authored_at else None,
        "reviewed_by": version.reviewed_by,
        "reviewed_at": version.reviewed_at.isoformat() if version.reviewed_at else None,
        "approved_by": version.approved_by,
        "approved_at": version.approved_at.isoformat() if version.approved_at else None,
        "approval_request_id": version.approval_request_id,
        "change_reason": version.change_reason,
        "supersedes_version_id": version.supersedes_version_id,
        "content_hash": version.content_hash,
        "validation_summary": version.validation_summary or {},
        "engine_run_ids": version.engine_run_ids or [],
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }


def _approval_json(approval: ApprovalRequest) -> dict[str, Any]:
    return {
        "id": approval.id,
        "kind": approval.kind,
        "subject_kind": approval.subject_kind,
        "subject_id": approval.subject_id,
        "status": approval.status,
        "title": approval.title,
        "required_role": approval.required_role,
        "requested_by": approval.requested_by,
        "requested_at": approval.requested_at.isoformat() if approval.requested_at else None,
        "request_payload": approval.request_payload or {},
        "decided_by": approval.decided_by,
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "decision_note": approval.decision_note,
    }


async def _audit(
    session: AsyncSession,
    request: Request,
    auth: AuthContext,
    *,
    action: str,
    permission: str,
    artifact: Artifact,
    version: ArtifactVersion | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    await record_audit(
        session,
        org_id=auth.org_id or "",
        principal=auth.principal,
        action=action,
        resource_kind="engineering_artifact_version" if version else "engineering_artifact",
        resource_id=version.id if version else artifact.id,
        permission=permission,
        request_id=getattr(request.state, "request_id", None),
        project_id=artifact.project_id,
        well_id=artifact.well_id,
        before=before,
        after=after,
        details={"artifact_id": artifact.id, "version_id": version.id if version else None, **(details or {})},
    )


@router.get("/wells/{well_id}/engineering-products", summary="List versioned programs and procedures for a well")
async def list_engineering_products(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("artifact.read"))],
    kind: Literal["program", "procedure"] | None = Query(default=None),
) -> dict[str, Any]:
    authorize(auth.principal, "artifact.read", context={"well_id": well_id})
    rows = await _service(session, auth).list_for_well(well_id, kind=kind)
    return {
        "items": [
            {"artifact": _artifact_json(artifact), "current_version": _version_json(version) if version else None}
            for artifact, version in rows
        ],
        "total": len(rows),
    }


@router.post(
    "/wells/{well_id}/engineering-products",
    summary="Create a structured, scoped drilling-program or controlled-procedure draft",
    status_code=201,
)
async def create_engineering_product(
    well_id: str,
    payload: EngineeringProductCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("artifact.draft"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "artifact.draft", context={"well_id": well_id, "kind": payload.kind})
    product_model = _parse_payload(payload.kind, payload.payload)
    org_id = auth.org_id or ""
    replay_scope = f"engineering_product.create:{well_id}"
    request_body = payload.model_dump(mode="json")
    replay = await replay_or_reserve(
        session,
        org_id=org_id,
        key=idempotency_key,
        scope=replay_scope,
        payload=request_body,
    )
    if replay is not None:
        return replay

    artifact, version = await _service(session, auth).create(
        kind=payload.kind,
        title=payload.title,
        code=payload.code,
        scope=DocumentScope(
            project_id=payload.project_id,
            well_id=well_id,
            wellbore_id=payload.wellbore_id,
            section_id=payload.section_id,
            operation_id=payload.operation_id,
        ),
        payload_model=product_model,
        linked_program_version_id=payload.linked_program_version_id,
        actor_id=auth.principal.id,
    )
    response = {"artifact": _artifact_json(artifact), "version": _version_json(version)}
    await _audit(
        session,
        request,
        auth,
        action="artifact.draft",
        permission="artifact.draft",
        artifact=artifact,
        version=version,
        after={"state": version.state, "content_hash": version.content_hash},
        details={"operation": "create", "schema_key": version.schema_key, "schema_version": version.schema_version},
    )
    await complete(session, org_id=org_id, key=idempotency_key, scope=replay_scope, response=response)
    return response


@router.get("/engineering-products/{artifact_id}", summary="Read a tenant-scoped engineering product and current revision")
async def get_engineering_product(
    artifact_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("artifact.read"))],
) -> dict[str, Any]:
    authorize(auth.principal, "artifact.read", context={"artifact_id": artifact_id})
    service = _service(session, auth)
    artifact = await service.get(artifact_id)
    version = await service.get_current_version(artifact)
    linked_snapshot, linked_state = await service.linked_program_state(version)
    approval = await service.approval_for_version(version)
    return {
        "artifact": _artifact_json(artifact),
        "current_version": _version_json(version),
        "linked_program": linked_snapshot,
        "linked_program_current_state": linked_state,
        "approval_request": _approval_json(approval) if approval else None,
    }


@router.get("/engineering-products/{artifact_id}/versions", summary="List immutable revision history")
async def list_engineering_product_versions(
    artifact_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("artifact.read"))],
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    authorize(auth.principal, "artifact.read", context={"artifact_id": artifact_id})
    service = _service(session, auth)
    artifact = await service.get(artifact_id)
    versions = await service.list_versions(artifact, limit=limit)
    return {"items": [_version_json(version) for version in versions], "total": len(versions)}


@router.get("/engineering-products/{artifact_id}/versions/{version_id}", summary="Read one exact product revision")
async def get_engineering_product_version(
    artifact_id: str,
    version_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("artifact.read"))],
) -> dict[str, Any]:
    authorize(auth.principal, "artifact.read", context={"artifact_id": artifact_id, "version_id": version_id})
    service = _service(session, auth)
    artifact = await service.get(artifact_id)
    version = await service.get_version(artifact, version_id)
    linked_snapshot, linked_state = await service.linked_program_state(version)
    approval = await service.approval_for_version(version)
    return {
        "artifact": _artifact_json(artifact),
        "version": _version_json(version),
        "linked_program": linked_snapshot,
        "linked_program_current_state": linked_state,
        "approval_request": _approval_json(approval) if approval else None,
    }


@router.post("/engineering-products/{artifact_id}/versions", summary="Create a compare-and-swap revision", status_code=201)
async def revise_engineering_product(
    artifact_id: str,
    payload: EngineeringProductRevision,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("artifact.draft"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "artifact.draft", context={"artifact_id": artifact_id})
    service = _service(session, auth)
    artifact = await service.get(artifact_id)
    product_model = _parse_payload(artifact.kind, payload.payload)
    org_id = auth.org_id or ""
    replay_scope = f"engineering_product.revise:{artifact_id}"
    request_body = payload.model_dump(mode="json")
    replay = await replay_or_reserve(
        session,
        org_id=org_id,
        key=idempotency_key,
        scope=replay_scope,
        payload=request_body,
    )
    if replay is not None:
        return replay
    before_version = artifact.current_version
    artifact, version = await service.revise(
        artifact_id=artifact_id,
        expected_current_version=payload.expected_current_version,
        payload_model=product_model,
        linked_program_version_id=payload.linked_program_version_id,
        linked_program_was_supplied="linked_program_version_id" in payload.model_fields_set,
        change_reason=payload.change_reason,
        actor_id=auth.principal.id,
    )
    response = {"artifact": _artifact_json(artifact), "version": _version_json(version)}
    await _audit(
        session,
        request,
        auth,
        action="artifact.draft",
        permission="artifact.draft",
        artifact=artifact,
        version=version,
        before={"current_version": before_version},
        after={"current_version": artifact.current_version, "state": version.state, "content_hash": version.content_hash},
        details={"operation": "revise", "change_reason": version.change_reason},
    )
    await complete(session, org_id=org_id, key=idempotency_key, scope=replay_scope, response=response)
    return response


@router.post(
    "/engineering-products/{artifact_id}/versions/{version_id}/validate",
    summary="Run deterministic validation against one immutable revision",
)
async def validate_engineering_product(
    artifact_id: str,
    version_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("artifact.draft"))],
) -> dict[str, Any]:
    authorize(auth.principal, "artifact.validate", context={"artifact_id": artifact_id, "version_id": version_id})
    service = _service(session, auth)
    artifact = await service.get(artifact_id)
    version = await service.get_version(artifact, version_id)
    validation = await service.validate(artifact, version, actor_id=auth.principal.id)
    await _audit(
        session,
        request,
        auth,
        action="artifact.validate",
        permission="artifact.draft",
        artifact=artifact,
        version=version,
        after={"validation_state": validation["state"], "content_hash": validation["content_hash"]},
        details={"finding_count": len(validation.get("findings") or []), "ruleset": validation.get("ruleset")},
    )
    return {"artifact": _artifact_json(artifact), "version": _version_json(version), "validation": validation}


@router.post(
    "/engineering-products/{artifact_id}/versions/{version_id}/submit",
    summary="Submit a current validated draft for independent review",
    status_code=202,
)
async def submit_engineering_product(
    artifact_id: str,
    version_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("action:artifact.submit"))],
) -> dict[str, Any]:
    authorize(auth.principal, "artifact.submit", context={"artifact_id": artifact_id, "version_id": version_id})
    service = _service(session, auth)
    artifact = await service.get(artifact_id, for_update=True)
    version = await service.get_version(artifact, version_id)
    approval = await service.submit(artifact, version, actor_id=auth.principal.id)
    await _audit(
        session,
        request,
        auth,
        action="artifact.submit",
        permission="action:artifact.submit",
        artifact=artifact,
        version=version,
        before={"state": "draft"},
        after={"state": version.state, "approval_request_id": approval.id},
        details={"content_hash": version.content_hash},
    )
    return {"artifact": _artifact_json(artifact), "version": _version_json(version), "approval_request": _approval_json(approval)}


@router.post(
    "/engineering-products/{artifact_id}/versions/{version_id}/review",
    summary="Record a distinct human review with written notes",
)
async def review_engineering_product(
    artifact_id: str,
    version_id: str,
    payload: EngineeringProductReview,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("action:artifact.review"))],
) -> dict[str, Any]:
    authorize(auth.principal, "artifact.review", context={"artifact_id": artifact_id, "version_id": version_id})
    service = _service(session, auth)
    artifact = await service.get(artifact_id, for_update=True)
    version = await service.get_version(artifact, version_id)
    approval = await service.review(artifact, version, actor_id=auth.principal.id, notes=payload.notes)
    await _audit(
        session,
        request,
        auth,
        action="artifact.review",
        permission="action:artifact.review",
        artifact=artifact,
        version=version,
        after={"reviewed_by": version.reviewed_by, "reviewed_at": version.reviewed_at.isoformat() if version.reviewed_at else None},
        details={"approval_request_id": approval.id, "content_hash": version.content_hash, "notes": payload.notes.strip()},
    )
    return {"artifact": _artifact_json(artifact), "version": _version_json(version), "approval_request": _approval_json(approval)}


@router.post(
    "/engineering-products/{artifact_id}/versions/{version_id}/decision",
    summary="Approve or reject after an independent review",
)
async def decide_engineering_product(
    artifact_id: str,
    version_id: str,
    payload: EngineeringProductDecision,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("action:artifact.approve"))],
) -> dict[str, Any]:
    action = "artifact.approve" if payload.decision == "approved" else "artifact.reject"
    authorize(
        auth.principal,
        action,
        approval_id=payload.approval_id,
        context={"artifact_id": artifact_id, "version_id": version_id},
    )
    service = _service(session, auth)
    artifact = await service.get(artifact_id, for_update=True)
    version = await service.get_version(artifact, version_id)
    approval = await service.decide(
        artifact,
        version,
        approval_id=payload.approval_id,
        decision=payload.decision,
        actor_id=auth.principal.id,
        note=payload.note,
    )
    await _audit(
        session,
        request,
        auth,
        action=action,
        permission="action:artifact.approve",
        artifact=artifact,
        version=version,
        before={"state": "in_review", "approval_status": "pending"},
        after={"state": version.state, "approval_status": approval.status},
        details={"approval_request_id": approval.id, "content_hash": version.content_hash, "decision_note": payload.note.strip()},
    )
    return {"artifact": _artifact_json(artifact), "version": _version_json(version), "approval_request": _approval_json(approval)}


@router.get(
    "/engineering-products/{artifact_id}/versions/{version_id}/exports/{export_format}",
    summary="Export a fingerprint-verified version as deterministic JSON or Markdown",
    response_class=Response,
)
async def export_engineering_product(
    artifact_id: str,
    version_id: str,
    export_format: Literal["json", "markdown"],
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("artifact.export"))],
) -> Response:
    authorize(auth.principal, "artifact.export", context={"artifact_id": artifact_id, "version_id": version_id})
    service = _service(session, auth)
    artifact = await service.get(artifact_id)
    version = await service.get_version(artifact, version_id)
    integrity = await service.verify_revision_integrity(artifact, version)
    export_payload = product_export_payload(
        artifact,
        version,
        linked_program_current_state=integrity["linked_program_state"],
    )
    if export_format == "json":
        content = render_product_json(export_payload)
        media_type = "application/json; charset=utf-8"
        suffix = "json"
    else:
        content = render_product_markdown(export_payload)
        media_type = "text/markdown; charset=utf-8"
        suffix = "md"
    digest = hashlib.sha256(content).hexdigest()
    await _audit(
        session,
        request,
        auth,
        action="artifact.export",
        permission="artifact.export",
        artifact=artifact,
        version=version,
        details={
            "format": export_format,
            "content_hash": version.content_hash,
            "export_sha256": digest,
            "bytes": len(content),
            "linked_program_current_state": integrity["linked_program_state"],
        },
    )
    filename = f"{artifact.kind}-revision-{version.version}.{suffix}"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "ETag": f'"{digest}"',
        },
    )


__all__ = ["router"]
