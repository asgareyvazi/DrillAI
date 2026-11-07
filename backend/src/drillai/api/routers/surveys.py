"""First-class survey import, review, calculation, immutable revisions and export."""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, get_db, require
from drillai.core.idempotency import complete, replay_or_reserve
from drillai.drilling.surveys import SurveyCreateRequest, SurveyImportRequest, SurveyService
from drillai.security.actions import authorize

router = APIRouter(tags=["surveys"])


def _service(session: AsyncSession, auth: AuthContext, request: Request) -> SurveyService:
    return SurveyService(
        session,
        org_id=auth.org_id or "",
        principal=auth.principal,
        request_id=getattr(request.state, "request_id", None),
    )


def _engine_authorize(auth: AuthContext, *, well_id: str, wellbore_id: str) -> None:
    """Survey calculation persists the same governed engine run as the generic engine runner."""
    authorize(
        auth.principal,
        "engine.run",
        context={"well_id": well_id, "wellbore_id": wellbore_id, "engine_key": "trajectory.minimum_curvature"},
    )


@router.get("/wells/{well_id}/surveys", summary="List wellbore trajectory revisions")
async def list_surveys(
    well_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("survey.read"))],
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    authorize(auth.principal, "survey.read", context={"well_id": well_id})
    return await _service(session, auth, request).list_trajectories(well_id, limit=limit, offset=offset)


@router.get("/wells/{well_id}/surveys/{trajectory_id}", summary="Read one trajectory revision")
async def get_survey(
    well_id: str,
    trajectory_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("survey.read"))],
) -> dict[str, Any]:
    authorize(auth.principal, "survey.read", context={"well_id": well_id, "trajectory_id": trajectory_id})
    return await _service(session, auth, request).get_trajectory(well_id, trajectory_id)


@router.get(
    "/wells/{well_id}/surveys/{trajectory_id}/compare/{baseline_id}",
    summary="Compare revisions at exact measured depths",
)
async def compare_surveys(
    well_id: str,
    trajectory_id: str,
    baseline_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("survey.read"))],
) -> dict[str, Any]:
    authorize(
        auth.principal,
        "survey.read",
        context={"well_id": well_id, "trajectory_id": trajectory_id, "baseline_id": baseline_id},
    )
    return await _service(session, auth, request).compare(well_id, trajectory_id, baseline_id)


@router.post("/wells/{well_id}/surveys", summary="Create or revise a manual trajectory", status_code=201)
async def create_survey(
    well_id: str,
    payload: SurveyCreateRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("survey.write"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    action = "survey.revise" if payload.base_trajectory_id is not None else "survey.create"
    authorize(
        auth.principal,
        action,
        context={"well_id": well_id, "wellbore_id": payload.wellbore_id, "kind": payload.kind},
    )
    _engine_authorize(auth, well_id=well_id, wellbore_id=payload.wellbore_id)
    org_id = auth.org_id or ""
    scope = f"survey.manual:{well_id}:{payload.wellbore_id}:{payload.name}:{payload.kind}"
    body = payload.model_dump(mode="json")
    if (replayed := await replay_or_reserve(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)) is not None:
        return replayed
    response = await _service(session, auth, request).create_manual(well_id, payload)
    await complete(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)
    return response


@router.post(
    "/wells/{well_id}/surveys/imports",
    summary="Calculate a trajectory from explicitly reviewed survey records",
    status_code=201,
)
async def import_survey(
    well_id: str,
    payload: SurveyImportRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("survey.write"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(
        auth.principal,
        "survey.import",
        context={
            "well_id": well_id,
            "wellbore_id": payload.wellbore_id,
            "source_document_id": payload.source_document_id,
            "kind": payload.kind,
        },
    )
    if payload.base_trajectory_id is not None:
        authorize(
            auth.principal,
            "survey.revise",
            context={"well_id": well_id, "wellbore_id": payload.wellbore_id, "kind": payload.kind},
        )
    _engine_authorize(auth, well_id=well_id, wellbore_id=payload.wellbore_id)
    org_id = auth.org_id or ""
    scope = f"survey.import:{well_id}:{payload.wellbore_id}:{payload.name}:{payload.kind}"
    body = payload.model_dump(mode="json")
    if (replayed := await replay_or_reserve(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)) is not None:
        return replayed
    response = await _service(session, auth, request).import_document(well_id, payload)
    await complete(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)
    return response


@router.get(
    "/wells/{well_id}/surveys/{trajectory_id}/export.csv",
    summary="Download a provenance-preserving trajectory CSV",
    response_class=Response,
)
async def export_survey_csv(
    well_id: str,
    trajectory_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("survey.export"))],
) -> Response:
    authorize(auth.principal, "survey.export", context={"well_id": well_id, "trajectory_id": trajectory_id})
    data, metadata = await _service(session, auth, request).export_csv(well_id, trajectory_id)
    safe_id = trajectory_id if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", trajectory_id) else "survey"
    version = metadata.get("version")
    version_text = str(version) if isinstance(version, int) and version > 0 else "revision"
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="trajectory-{safe_id}-v{version_text}.csv"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "ETag": f'"{metadata["sha256"]}"',
        },
    )
