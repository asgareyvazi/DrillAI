"""Tenant-scoped, append-only lifecycle for versioned drilling programs and procedures."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.errors import Conflict, NotFound, ValidationFailed
from drillai.db.base import utc_now
from drillai.db.models import (
    ApprovalRequest,
    Artifact,
    ArtifactVersion,
    Document,
    EngineRun,
    RawArtifact,
    ValidationFinding,
)
from drillai.documents.access import document_not_quarantined_condition
from drillai.documents.scope import DocumentScope, DocumentScopeResolver, validate_source_scope
from drillai.engineering.products import (
    SCHEMA_KEYS,
    normalize_payload,
    product_validation_findings,
    validation_state,
)


class EngineeringProductService:
    """Application services for structured program/procedure authoring and human control gates."""

    def __init__(self, session: AsyncSession, org_id: str) -> None:
        if not org_id:
            raise NotFound("the authenticated principal has no organisation scope")
        self.session = session
        self.org_id = org_id

    async def list_for_well(self, well_id: str, *, kind: str | None = None) -> list[tuple[Artifact, ArtifactVersion | None]]:
        scope = await DocumentScopeResolver(self.session, org_id=self.org_id).resolve(
            DocumentScope(well_id=well_id)
        )
        statement = select(Artifact).where(
            Artifact.org_id == self.org_id,
            Artifact.well_id == scope.well_id,
            Artifact.kind.in_(("program", "procedure")),
        )
        if kind is not None:
            statement = statement.where(Artifact.kind == kind)
        rows = (await self.session.execute(statement.order_by(Artifact.updated_at.desc(), Artifact.id))).scalars().all()
        result: list[tuple[Artifact, ArtifactVersion | None]] = []
        for artifact in rows:
            version = await self._version_number(artifact.id, artifact.current_version)
            result.append((artifact, version))
        return result

    async def get(self, artifact_id: str, *, for_update: bool = False) -> Artifact:
        statement = select(Artifact).where(
            Artifact.id == artifact_id,
            Artifact.org_id == self.org_id,
            Artifact.kind.in_(("program", "procedure")),
        )
        if for_update:
            statement = statement.with_for_update()
        artifact = (await self.session.execute(statement)).scalar_one_or_none()
        if artifact is None:
            raise NotFound(f"engineering artifact {artifact_id!r} not found")
        return artifact

    async def get_version(self, artifact: Artifact, version_id: str) -> ArtifactVersion:
        row = (
            await self.session.execute(
                select(ArtifactVersion).where(
                    ArtifactVersion.id == version_id,
                    ArtifactVersion.artifact_id == artifact.id,
                    ArtifactVersion.org_id == self.org_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound(f"version {version_id!r} not found on artifact {artifact.id!r}")
        return row

    async def get_current_version(self, artifact: Artifact) -> ArtifactVersion:
        version = await self._version_number(artifact.id, artifact.current_version)
        if version is None:
            raise NotFound(f"current version {artifact.current_version} is missing for artifact {artifact.id!r}")
        return version

    async def list_versions(self, artifact: Artifact, *, limit: int = 100) -> list[ArtifactVersion]:
        rows = (
            await self.session.execute(
                select(ArtifactVersion)
                .where(
                    ArtifactVersion.artifact_id == artifact.id,
                    ArtifactVersion.org_id == self.org_id,
                )
                .order_by(ArtifactVersion.version.desc())
                .limit(max(1, min(limit, 200)))
            )
        ).scalars().all()
        return list(rows)

    async def create(
        self,
        *,
        kind: str,
        title: str,
        code: str | None,
        scope: DocumentScope,
        payload_model: Any,
        linked_program_version_id: str | None,
        actor_id: str,
    ) -> tuple[Artifact, ArtifactVersion]:
        if kind not in SCHEMA_KEYS:
            raise ValidationFailed("only drilling programs and controlled procedures are authorable here")
        title_clean = title.strip()
        code_clean = code.strip() if isinstance(code, str) and code.strip() else None
        if not title_clean or len(title_clean) > 300:
            raise ValidationFailed("a non-empty title of at most 300 characters is required")
        resolved = await DocumentScopeResolver(self.session, org_id=self.org_id).resolve(scope)
        if resolved.well_id is None:
            raise ValidationFailed("engineering products require an explicit well scope")
        if code_clean:
            existing_code = (
                await self.session.execute(
                    select(Artifact.id).where(Artifact.org_id == self.org_id, Artifact.code == code_clean)
                )
            ).scalar_one_or_none()
            if existing_code is not None:
                raise Conflict("an engineering artifact with this organization-scoped code already exists", details={"code": code_clean})
        schema_key, schema_version = SCHEMA_KEYS[kind]
        now = utc_now()
        normalized = normalize_payload(payload_model, actor_id=actor_id, authored_at=now)
        await self._validate_product_scope(kind, normalized, resolved)
        source_lineage = await self._resolve_source_lineage(normalized, resolved)
        program_link = await self._resolve_program_link(
            linked_program_version_id,
            expected_well_id=resolved.well_id,
            expected_project_id=resolved.project_id,
        ) if kind == "procedure" and linked_program_version_id else None
        if kind == "program" and linked_program_version_id is not None:
            raise ValidationFailed("a drilling program cannot reference another program revision")
        digest = self._content_hash(
            schema_key=schema_key,
            schema_version=schema_version,
            payload=normalized,
            source_lineage=source_lineage,
            linked_program=program_link,
        )
        artifact = Artifact(
            org_id=self.org_id,
            project_id=resolved.project_id,
            well_id=resolved.well_id,
            wellbore_id=resolved.wellbore_id,
            section_id=resolved.section_id,
            operation_id=resolved.operation_id,
            kind=kind,
            title=title_clean,
            code=code_clean,
            status="draft",
            current_version=1,
            owner=actor_id,
            domain_pack="drilling_engineering",
            tags=[],
            is_template=False,
            is_demo_fixture=False,
        )
        self.session.add(artifact)
        await self.session.flush()
        version = ArtifactVersion(
            org_id=self.org_id,
            artifact_id=artifact.id,
            version=1,
            state="draft",
            schema_key=schema_key,
            schema_version=schema_version,
            payload=normalized,
            authored_by=actor_id,
            authored_at=now,
            change_reason="Initial draft",
            content_hash=digest,
            validation_summary={
                "source_lineage": source_lineage,
                "linked_program": program_link,
                "validation": None,
            },
            engine_run_ids=[item["id"] for item in source_lineage if item.get("kind") == "engine_run"],
        )
        self.session.add(version)
        await self.session.flush()
        return artifact, version

    async def revise(
        self,
        *,
        artifact_id: str,
        expected_current_version: int,
        payload_model: Any,
        linked_program_version_id: str | None,
        linked_program_was_supplied: bool,
        change_reason: str,
        actor_id: str,
    ) -> tuple[Artifact, ArtifactVersion]:
        artifact = await self.get(artifact_id, for_update=True)
        current = await self.get_current_version(artifact)
        if artifact.current_version != expected_current_version:
            raise Conflict(
                "artifact changed since this draft was read",
                details={"expected_current_version": expected_current_version, "actual_current_version": artifact.current_version},
            )
        if current.state == "in_review":
            raise Conflict("reject the submitted revision before creating another one", details={"version_state": current.state})
        if artifact.status == "archived":
            raise Conflict("archived engineering artifacts cannot be revised")
        if not change_reason.strip():
            raise ValidationFailed("a change reason is required for every new revision")
        resolved_scope = DocumentScope(
            project_id=artifact.project_id,
            well_id=artifact.well_id,
            wellbore_id=artifact.wellbore_id,
            section_id=artifact.section_id,
            operation_id=artifact.operation_id,
        )
        now = utc_now()
        normalized = normalize_payload(payload_model, actor_id=actor_id, authored_at=now)
        await self._validate_product_scope(artifact.kind, normalized, resolved_scope)
        source_lineage = await self._resolve_source_lineage(normalized, resolved_scope)
        program_link = None
        if artifact.kind == "procedure":
            prior_summary = dict(current.validation_summary or {})
            prior_link = prior_summary.get("linked_program") or {}
            if linked_program_was_supplied:
                selected_program_version_id = linked_program_version_id
            else:
                selected_program_version_id = prior_link.get("version_id")
            program_link = await self._resolve_program_link(
                selected_program_version_id,
                expected_well_id=artifact.well_id,
                expected_project_id=artifact.project_id,
            ) if selected_program_version_id else None
        elif linked_program_was_supplied and linked_program_version_id is not None:
            raise ValidationFailed("a drilling program cannot reference another program revision")
        schema_key, schema_version = SCHEMA_KEYS[artifact.kind]
        digest = self._content_hash(
            schema_key=schema_key,
            schema_version=schema_version,
            payload=normalized,
            source_lineage=source_lineage,
            linked_program=program_link,
        )
        next_number = artifact.current_version + 1
        result = await self.session.execute(
            update(Artifact)
            .where(
                Artifact.id == artifact.id,
                Artifact.org_id == self.org_id,
                Artifact.current_version == expected_current_version,
            )
            .values(current_version=next_number, status="draft", updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise Conflict("another author created a revision first", details={"expected_current_version": expected_current_version})
        artifact.current_version = next_number
        artifact.status = "draft"
        artifact.updated_at = now
        if current.state == "draft":
            current.state = "superseded"
        version = ArtifactVersion(
            org_id=self.org_id,
            artifact_id=artifact.id,
            version=next_number,
            state="draft",
            schema_key=schema_key,
            schema_version=schema_version,
            payload=normalized,
            authored_by=actor_id,
            authored_at=now,
            change_reason=change_reason.strip(),
            supersedes_version_id=current.id,
            content_hash=digest,
            validation_summary={
                "source_lineage": source_lineage,
                "linked_program": program_link,
                "validation": None,
            },
            engine_run_ids=[item["id"] for item in source_lineage if item.get("kind") == "engine_run"],
        )
        self.session.add(version)
        await self.session.flush()
        return artifact, version

    async def verify_revision_integrity(self, artifact: Artifact, version: ArtifactVersion) -> dict[str, Any]:
        """Re-resolve tenant-scoped lineage and verify the immutable payload fingerprint before export."""
        expected_schema = SCHEMA_KEYS.get(artifact.kind)
        if expected_schema != (version.schema_key, version.schema_version):
            raise Conflict("stored revision schema does not match the registered product schema")
        summary = dict(version.validation_summary or {})
        scope = DocumentScope(
            project_id=artifact.project_id,
            well_id=artifact.well_id,
            wellbore_id=artifact.wellbore_id,
            section_id=artifact.section_id,
            operation_id=artifact.operation_id,
        )
        current_lineage = await self._resolve_source_lineage(version.payload or {}, scope)
        if current_lineage != (summary.get("source_lineage") or []):
            raise Conflict("the revision source lineage changed; refusing to export an unverified snapshot")
        expected_hash = self._content_hash(
            schema_key=version.schema_key,
            schema_version=version.schema_version,
            payload=version.payload or {},
            source_lineage=current_lineage,
            linked_program=summary.get("linked_program"),
        )
        if not version.content_hash or expected_hash != version.content_hash:
            raise Conflict("stored revision content does not match its immutable content hash")
        linked_snapshot = summary.get("linked_program")
        linked_program_state = None
        if isinstance(linked_snapshot, dict):
            linked_version = await self._get_linked_program_version(linked_snapshot)
            linked_program_state = linked_version.state if linked_version else "missing"
        return {
            "source_lineage": current_lineage,
            "linked_program": linked_snapshot,
            "linked_program_state": linked_program_state,
            "content_hash": expected_hash,
        }

    async def validate(self, artifact: Artifact, version: ArtifactVersion, *, actor_id: str) -> dict[str, Any]:
        """Validate one immutable revision against its exact source and registered-engine snapshots."""
        if artifact.current_version != version.version or version.state != "draft":
            raise Conflict("only the current draft revision can be validated", details={"version": version.version, "state": version.state})
        summary = dict(version.validation_summary or {})
        scope = DocumentScope(
            project_id=artifact.project_id,
            well_id=artifact.well_id,
            wellbore_id=artifact.wellbore_id,
            section_id=artifact.section_id,
            operation_id=artifact.operation_id,
        )
        source_lineage = await self._resolve_source_lineage(version.payload or {}, scope)
        stored_lineage = summary.get("source_lineage") or []
        if source_lineage != stored_lineage:
            raise Conflict("the revision source lineage changed; create a new revision and validate it again")
        linked_snapshot = summary.get("linked_program")
        linked_state: str | None = None
        if linked_snapshot:
            linked_version = await self._get_linked_program_version(linked_snapshot)
            linked_state = linked_version.state if linked_version else "missing"
        engine_findings, engine_snapshots = await self._engine_validation_data(artifact, version, source_lineage)
        attached_engine_findings = await self.engine_findings(version)
        attached_engine_fingerprint = self._engine_row_fingerprint(attached_engine_findings)
        engine_findings.extend(
            {
                "rule_key": row.rule_key,
                "severity": row.severity,
                "message": row.message or "A linked registered-engine finding requires review.",
                "details": row.details or {},
            }
            for row in attached_engine_findings
        )
        if not version.engine_run_ids and not attached_engine_findings:
            engine_findings.append(
                {
                    "rule_key": "registered_engine_result_missing",
                    "severity": "warning",
                    "message": "No registered-engine run is attached to this revision; engineering calculations have not been measured by this validation endpoint.",
                    "details": {},
                }
            )
        findings = product_validation_findings(
            artifact.kind,
            version.payload or {},
            linked_program_state=linked_state,
            engine_findings=engine_findings,
        )
        expected_hash = self._content_hash(
            schema_key=version.schema_key,
            schema_version=version.schema_version,
            payload=version.payload or {},
            source_lineage=source_lineage,
            linked_program=linked_snapshot,
        )
        if expected_hash != version.content_hash:
            raise Conflict("stored revision content does not match its immutable content hash")
        state = validation_state(findings)
        now = utc_now()
        validation = {
            "state": state,
            "validated_at": now.isoformat(),
            "validated_by": actor_id,
            "content_hash": version.content_hash,
            "ruleset": f"drillai.{artifact.kind}-structure.v1",
            "engine_finding_count": len(engine_findings),
            "engine_run_snapshots": engine_snapshots,
            "attached_engine_findings_fingerprint": attached_engine_fingerprint,
            "linked_program_state": linked_state,
            "field_acceptance": "not_established",
            "findings": [item.to_dict() for item in findings],
        }

        previous = summary.get("validation")
        if (
            isinstance(previous, dict)
            and previous.get("content_hash") == validation["content_hash"]
            and previous.get("engine_finding_count") == validation["engine_finding_count"]
            and previous.get("engine_run_snapshots") == validation["engine_run_snapshots"]
            and previous.get("attached_engine_findings_fingerprint") == validation["attached_engine_findings_fingerprint"]
            and previous.get("linked_program_state") == linked_state
            and previous.get("findings") == validation["findings"]
        ):
            return previous

        finding_rows: list[ValidationFinding] = []
        for item in findings:
            rule_key = f"{item.code}:{item.path}" if item.path else item.code
            finding = ValidationFinding(
                org_id=self.org_id,
                artifact_version_id=version.id,
                well_id=artifact.well_id,
                wellbore_id=artifact.wellbore_id,
                section_id=artifact.section_id,
                operation_id=artifact.operation_id,
                subject_kind="artifact_version",
                subject_id=version.id,
                rule_key=rule_key[:120],
                rule_version=1,
                status={"error": "failed", "warning": "warning", "info": "passed"}[item.severity],
                severity=item.severity,
                title=item.code.replace("_", " ").title()[:300],
                message=item.message,
                details={"code": item.code, "path": item.path, **item.details},
                computed_at=now,
            )
            finding_rows.append(finding)
            self.session.add(finding)
        version.validation_summary = {**summary, "validation": validation}
        await self.session.flush()
        validation["finding_ids"] = [row.id for row in finding_rows]
        version.validation_summary = {**summary, "validation": validation}
        await self.session.flush()
        return validation

    async def _engine_validation_data(
        self,
        artifact: Artifact,
        version: ArtifactVersion,
        source_lineage: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Use only referenced persisted EngineRun results; never recalculate or invent outputs."""
        refs = {
            item["id"]: item
            for item in source_lineage
            if item.get("kind") == "engine_run" and isinstance(item.get("id"), str)
        }
        if sorted(refs) != sorted(version.engine_run_ids or []):
            raise Conflict("the revision engine-run references do not match its source-lineage snapshot")
        findings: list[dict[str, Any]] = []
        snapshots: list[dict[str, Any]] = []
        for engine_run_id, reference in sorted(refs.items()):
            run = (
                await self.session.execute(
                    select(EngineRun).where(
                        EngineRun.id == engine_run_id,
                        EngineRun.org_id == self.org_id,
                    )
                )
            ).scalar_one_or_none()
            if run is None:
                findings.append(
                    {
                        "rule_key": "registered_engine_run_missing",
                        "severity": "error",
                        "message": "The referenced registered-engine run is no longer available in this organization.",
                        "details": {"engine_run_id": engine_run_id},
                    }
                )
                snapshots.append({"id": engine_run_id, "status": "missing"})
                continue
            validate_source_scope(
                run,
                project_id=artifact.project_id,
                well_id=artifact.well_id,
                wellbore_id=artifact.wellbore_id,
                section_id=artifact.section_id,
                operation_id=artifact.operation_id,
            )
            run_fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "status": run.status,
                        "outputs_hash": run.outputs_hash,
                        "is_feasible": run.is_feasible,
                        "constraint_violations": run.constraint_violations or [],
                        "warnings": run.warnings or [],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            snapshots.append(
                {
                    "id": run.id,
                    "status": run.status,
                    "outputs_hash": run.outputs_hash,
                    "result_fingerprint": run_fingerprint,
                }
            )
            if reference.get("outputs_hash") != run.outputs_hash:
                findings.append(
                    {
                        "rule_key": "registered_engine_output_hash_changed",
                        "severity": "error",
                        "message": "The referenced engine output hash differs from the source-lineage snapshot.",
                        "details": {"engine_run_id": run.id, "expected_hash": reference.get("outputs_hash"), "actual_hash": run.outputs_hash},
                    }
                )
            if run.status not in {"succeeded", "succeeded_with_violations"}:
                findings.append(
                    {
                        "rule_key": "registered_engine_run_not_successful",
                        "severity": "error",
                        "message": "The referenced registered-engine run is not in a successful state.",
                        "details": {"engine_run_id": run.id, "status": run.status},
                    }
                )
            violations = list(run.constraint_violations or [])
            error_count = 0
            for index, violation in enumerate(violations):
                severity = violation.get("severity") if isinstance(violation, dict) else None
                if severity not in {"info", "warning", "error"}:
                    severity = "warning"
                if severity == "error":
                    error_count += 1
                name = violation.get("name", f"constraint_{index + 1}") if isinstance(violation, dict) else f"constraint_{index + 1}"
                message = violation.get("message") if isinstance(violation, dict) else None
                findings.append(
                    {
                        "rule_key": f"engine.{run.engine_key}.{name}",
                        "severity": severity,
                        "message": message or "The registered engine reported a constraint finding.",
                        "details": {
                            "engine_run_id": run.id,
                            "engine_key": run.engine_key,
                            "engine_version": run.engine_version,
                            "outputs_hash": run.outputs_hash,
                            "violation": violation,
                        },
                    }
                )
            if run.is_feasible is False and error_count == 0:
                findings.append(
                    {
                        "rule_key": "registered_engine_infeasible_without_error_detail",
                        "severity": "error",
                        "message": "The registered engine marked this result infeasible but supplied no error-severity constraint detail.",
                        "details": {"engine_run_id": run.id, "outputs_hash": run.outputs_hash},
                    }
                )
            for index, warning in enumerate(run.warnings or []):
                findings.append(
                    {
                        "rule_key": f"engine.{run.engine_key}.warning.{index + 1}",
                        "severity": "warning",
                        "message": str(warning),
                        "details": {"engine_run_id": run.id, "engine_version": run.engine_version, "outputs_hash": run.outputs_hash},
                    }
                )
        return findings, snapshots

    @staticmethod
    def _engine_row_fingerprint(rows: list[ValidationFinding]) -> str:
        material = [
            {
                "id": row.id,
                "rule_key": row.rule_key,
                "status": row.status,
                "severity": row.severity,
                "message": row.message,
                "details": row.details or {},
                "engine_run_id": row.engine_run_id,
            }
            for row in rows
        ]
        return hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()

    async def ensure_validation_current(self, artifact: Artifact, version: ArtifactVersion) -> dict[str, Any]:
        summary = dict(version.validation_summary or {})
        validation = summary.get("validation")
        if not isinstance(validation, dict):
            raise Conflict("validate this exact revision before submission or approval")
        scope = DocumentScope(
            project_id=artifact.project_id,
            well_id=artifact.well_id,
            wellbore_id=artifact.wellbore_id,
            section_id=artifact.section_id,
            operation_id=artifact.operation_id,
        )
        current_lineage = await self._resolve_source_lineage(version.payload or {}, scope)
        expected_hash = self._content_hash(
            schema_key=version.schema_key,
            schema_version=version.schema_version,
            payload=version.payload or {},
            source_lineage=current_lineage,
            linked_program=summary.get("linked_program"),
        )
        linked_snapshot = summary.get("linked_program")
        current_linked_state = None
        if linked_snapshot:
            linked_version = await self._get_linked_program_version(linked_snapshot)
            current_linked_state = linked_version.state if linked_version else "missing"
        current_engine_findings, current_engine_snapshots = await self._engine_validation_data(
            artifact, version, current_lineage
        )
        attached_engine_findings = await self.engine_findings(version)
        current_engine_findings.extend(
            {
                "rule_key": row.rule_key,
                "severity": row.severity,
                "message": row.message,
                "details": row.details,
            }
            for row in attached_engine_findings
        )
        if not version.engine_run_ids and not attached_engine_findings:
            current_engine_findings.append(
                {
                    "rule_key": "registered_engine_result_missing",
                    "severity": "warning",
                    "message": "No registered-engine run is attached to this revision.",
                    "details": {},
                }
            )
        attached_fingerprint = self._engine_row_fingerprint(attached_engine_findings)
        if (
            expected_hash != version.content_hash
            or validation.get("content_hash") != version.content_hash
            or validation.get("engine_finding_count") != len(current_engine_findings)
            or validation.get("engine_run_snapshots") != current_engine_snapshots
            or validation.get("attached_engine_findings_fingerprint") != attached_fingerprint
            or validation.get("linked_program_state") != current_linked_state
        ):
            raise Conflict(
                "the revision, source lineage, linked program or registered-engine results changed after validation; validate again",
                details={
                    "validated_content_hash": validation.get("content_hash"),
                    "current_content_hash": version.content_hash,
                    "source_lineage_matches_hash": expected_hash == version.content_hash,
                    "validated_engine_finding_count": validation.get("engine_finding_count"),
                    "current_engine_finding_count": len(current_engine_findings),
                    "validated_linked_program_state": validation.get("linked_program_state"),
                    "current_linked_program_state": current_linked_state,
                },
            )
        if validation.get("state") == "blocked":
            raise ValidationFailed(
                "this revision has blocking validation findings",
                details={"findings": validation.get("findings", [])},
            )
        if version.state not in {"draft", "in_review"}:
            raise Conflict("only a draft or submitted revision can use this lifecycle action", details={"state": version.state})
        return validation

    async def submit(self, artifact: Artifact, version: ArtifactVersion, *, actor_id: str) -> ApprovalRequest:
        await self.ensure_validation_current(artifact, version)
        if artifact.current_version != version.version or version.state != "draft":
            raise Conflict("only the current draft revision can be submitted", details={"current_version": artifact.current_version, "version": version.version, "state": version.state})
        now = utc_now()
        request = ApprovalRequest(
            org_id=self.org_id,
            kind="engineering_artifact",
            subject_kind="engineering_artifact_version",
            subject_id=version.id,
            project_id=artifact.project_id,
            well_id=artifact.well_id,
            title=f"Review {artifact.kind}: {artifact.title} v{version.version}"[:300],
            description="Independent human review and approval of an immutable engineering artifact revision.",
            request_payload={
                "artifact_id": artifact.id,
                "artifact_version_id": version.id,
                "version": version.version,
                "content_hash": version.content_hash,
                "schema_key": version.schema_key,
                "schema_version": version.schema_version,
            },
            proposed_action="Approve or reject this exact version; later revisions require a separate review.",
            action_level="L4",
            required_role="drilling_supervisor",
            evidence_refs=[
                item.get("id")
                for item in ((version.validation_summary or {}).get("source_lineage") or [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ],
            requested_by=actor_id,
            requested_by_kind="user",
            requested_at=now,
            status="pending",
        )
        version_update = await self.session.execute(
            update(ArtifactVersion)
            .where(
                ArtifactVersion.id == version.id,
                ArtifactVersion.org_id == self.org_id,
                ArtifactVersion.artifact_id == artifact.id,
                ArtifactVersion.state == "draft",
            )
            .values(state="in_review", reviewed_by=None, reviewed_at=None)
            .execution_options(synchronize_session=False)
        )
        if version_update.rowcount != 1:
            raise Conflict("another request submitted this revision first")
        artifact_update = await self.session.execute(
            update(Artifact)
            .where(
                Artifact.id == artifact.id,
                Artifact.org_id == self.org_id,
                Artifact.current_version == version.version,
                Artifact.status == "draft",
            )
            .values(status="in_review", updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if artifact_update.rowcount != 1:
            raise Conflict("the artifact changed before submission could be recorded")
        version.state = "in_review"
        version.reviewed_by = None
        version.reviewed_at = None
        artifact.status = "in_review"
        artifact.updated_at = now
        self.session.add(request)
        await self.session.flush()
        return request

    async def review(
        self,
        artifact: Artifact,
        version: ArtifactVersion,
        *,
        actor_id: str,
        notes: str,
    ) -> ApprovalRequest:
        await self.ensure_validation_current(artifact, version)
        if artifact.current_version != version.version or version.state != "in_review":
            raise Conflict("only the current submitted revision can be independently reviewed", details={"state": version.state})
        approval = await self._pending_approval(version.id, for_update=True)
        if approval is None:
            raise Conflict("the revision has no pending approval request")
        if actor_id in {version.authored_by, approval.requested_by}:
            raise Conflict("the author/requester cannot perform the independent review", details={"author_id": version.authored_by})
        review_notes = notes.strip()
        if not review_notes:
            raise ValidationFailed("independent review notes are required")
        now = utc_now()
        result = await self.session.execute(
            update(ArtifactVersion)
            .where(
                ArtifactVersion.id == version.id,
                ArtifactVersion.org_id == self.org_id,
                ArtifactVersion.artifact_id == artifact.id,
                ArtifactVersion.state == "in_review",
                ArtifactVersion.reviewed_by.is_(None),
                ArtifactVersion.reviewed_at.is_(None),
            )
            .values(reviewed_by=actor_id, reviewed_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise Conflict("another independent reviewer already reviewed this revision")
        version.reviewed_by = actor_id
        version.reviewed_at = now
        approval.request_payload = {
            **(approval.request_payload or {}),
            "review": {"reviewed_by": actor_id, "reviewed_at": now.isoformat(), "notes": review_notes},
        }
        await self.session.flush()
        return approval

    async def decide(
        self,
        artifact: Artifact,
        version: ArtifactVersion,
        *,
        approval_id: str,
        decision: Literal["approved", "rejected"],
        actor_id: str,
        note: str,
    ) -> ApprovalRequest:
        await self.ensure_validation_current(artifact, version)
        if artifact.current_version != version.version or version.state != "in_review":
            raise Conflict("only the current submitted revision can be decided", details={"state": version.state})
        approval = await self._pending_approval(version.id, for_update=True)
        if approval is None or approval.id != approval_id:
            raise NotFound(f"pending approval {approval_id!r} not found for this revision")
        if approval.status != "pending":
            raise Conflict("approval request has already been decided", details={"status": approval.status})
        review = (approval.request_payload or {}).get("review") or {}
        if version.reviewed_by is None or version.reviewed_at is None or review.get("reviewed_by") != version.reviewed_by:
            raise Conflict("an independent review must be recorded before approval or rejection")
        if actor_id in {version.authored_by, approval.requested_by, version.reviewed_by}:
            raise Conflict("the author, submitter and reviewer must not decide their own review", details={"author_id": version.authored_by, "reviewer_id": version.reviewed_by})
        decision_note = note.strip()
        if not decision_note:
            raise ValidationFailed("a written approval or rejection decision is required")
        now = utc_now()
        approval_update = await self.session.execute(
            update(ApprovalRequest)
            .where(
                ApprovalRequest.id == approval.id,
                ApprovalRequest.org_id == self.org_id,
                ApprovalRequest.status == "pending",
            )
            .values(status=decision, decided_by=actor_id, decided_at=now, decision_note=decision_note)
            .execution_options(synchronize_session=False)
        )
        if approval_update.rowcount != 1:
            raise Conflict("approval request was decided concurrently")
        version_values: dict[str, Any] = {"state": decision}
        if decision == "approved":
            version_values.update(approved_by=actor_id, approved_at=now, approval_request_id=approval.id)
        version_update = await self.session.execute(
            update(ArtifactVersion)
            .where(
                ArtifactVersion.id == version.id,
                ArtifactVersion.org_id == self.org_id,
                ArtifactVersion.artifact_id == artifact.id,
                ArtifactVersion.state == "in_review",
                ArtifactVersion.reviewed_by == version.reviewed_by,
                ArtifactVersion.reviewed_at.is_not(None),
            )
            .values(**version_values)
            .execution_options(synchronize_session=False)
        )
        if version_update.rowcount != 1:
            raise Conflict("the submitted revision changed before the decision could be recorded")
        artifact_update = await self.session.execute(
            update(Artifact)
            .where(
                Artifact.id == artifact.id,
                Artifact.org_id == self.org_id,
                Artifact.current_version == version.version,
                Artifact.status == "in_review",
            )
            .values(status=decision, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if artifact_update.rowcount != 1:
            raise Conflict("the artifact changed before the decision could be recorded")
        approval.status = decision
        approval.decided_by = actor_id
        approval.decided_at = now
        approval.decision_note = decision_note
        version.state = decision
        if decision == "approved":
            prior_approved = list(
                (
                    await self.session.execute(
                        select(ArtifactVersion).where(
                            ArtifactVersion.org_id == self.org_id,
                            ArtifactVersion.artifact_id == artifact.id,
                            ArtifactVersion.state == "approved",
                            ArtifactVersion.id != version.id,
                        ).with_for_update()
                    )
                ).scalars().all()
            )
            for old in prior_approved:
                # Only lifecycle metadata changes. The approved payload and content hash are never edited.
                old.state = "superseded"
            version.approved_by = actor_id
            version.approved_at = now
            version.approval_request_id = approval.id
        artifact.status = decision
        artifact.updated_at = now
        await self.session.flush()
        return approval

    async def linked_program_state(self, version: ArtifactVersion) -> tuple[dict[str, Any] | None, str | None]:
        snapshot = (version.validation_summary or {}).get("linked_program")
        if not isinstance(snapshot, dict):
            return None, None
        linked_version = await self._get_linked_program_version(snapshot)
        return snapshot, linked_version.state if linked_version else "missing"

    async def approval_for_version(self, version: ArtifactVersion) -> ApprovalRequest | None:
        """Return the latest review/decision request attached to this exact immutable revision."""
        return (
            await self.session.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.org_id == self.org_id,
                    ApprovalRequest.kind == "engineering_artifact",
                    ApprovalRequest.subject_kind == "engineering_artifact_version",
                    ApprovalRequest.subject_id == version.id,
                )
                .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def engine_findings(self, version: ArtifactVersion) -> list[ValidationFinding]:
        return list(
            (
                await self.session.execute(
                    select(ValidationFinding).where(
                        ValidationFinding.org_id == self.org_id,
                        ValidationFinding.artifact_version_id == version.id,
                        ValidationFinding.engine_run_id.is_not(None),
                    ).order_by(ValidationFinding.created_at, ValidationFinding.id)
                )
            ).scalars().all()
        )

    async def _version_number(self, artifact_id: str, version_number: int) -> ArtifactVersion | None:
        return (
            await self.session.execute(
                select(ArtifactVersion).where(
                    ArtifactVersion.org_id == self.org_id,
                    ArtifactVersion.artifact_id == artifact_id,
                    ArtifactVersion.version == version_number,
                )
            )
        ).scalar_one_or_none()

    async def _pending_approval(self, version_id: str, *, for_update: bool = False) -> ApprovalRequest | None:
        statement = select(ApprovalRequest).where(
            ApprovalRequest.org_id == self.org_id,
            ApprovalRequest.kind == "engineering_artifact",
            ApprovalRequest.subject_kind == "engineering_artifact_version",
            ApprovalRequest.subject_id == version_id,
            ApprovalRequest.status == "pending",
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def _engine_finding_count(self, version_id: str) -> int:
        findings = await self.engine_findings_for_version(version_id)
        return len(findings)

    async def engine_findings_for_version(self, version_id: str) -> list[ValidationFinding]:
        return list(
            (
                await self.session.execute(
                    select(ValidationFinding).where(
                        ValidationFinding.org_id == self.org_id,
                        ValidationFinding.artifact_version_id == version_id,
                        ValidationFinding.engine_run_id.is_not(None),
                    )
                )
            ).scalars().all()
        )

    async def _resolve_program_link(
        self,
        version_id: str | None,
        *,
        expected_well_id: str | None,
        expected_project_id: str | None,
    ) -> dict[str, Any] | None:
        if version_id is None:
            return None
        linked = (
            await self.session.execute(
                select(ArtifactVersion, Artifact)
                .join(Artifact, Artifact.id == ArtifactVersion.artifact_id)
                .where(
                    ArtifactVersion.id == version_id,
                    ArtifactVersion.org_id == self.org_id,
                    Artifact.org_id == self.org_id,
                    Artifact.kind == "program",
                )
            )
        ).one_or_none()
        if linked is None:
            raise NotFound(f"approved drilling-program revision {version_id!r} not found")
        linked_version, linked_artifact = linked
        if linked_version.state != "approved":
            raise Conflict(
                "a controlled procedure can link only to an approved drilling-program revision",
                details={"version_id": linked_version.id, "state": linked_version.state},
            )
        if linked_artifact.well_id and expected_well_id != linked_artifact.well_id:
            raise ValidationFailed("the linked program belongs to a different well", details={"program_well_id": linked_artifact.well_id, "procedure_well_id": expected_well_id})
        if linked_artifact.project_id and expected_project_id != linked_artifact.project_id:
            raise ValidationFailed("the linked program belongs to a different project", details={"program_project_id": linked_artifact.project_id, "procedure_project_id": expected_project_id})
        return {
            "artifact_id": linked_artifact.id,
            "version_id": linked_version.id,
            "version": linked_version.version,
            "title": linked_artifact.title,
            "code": linked_artifact.code,
            "content_hash": linked_version.content_hash,
            "state_at_link": linked_version.state,
            "linked_at": utc_now().isoformat(),
        }

    async def _get_linked_program_version(self, snapshot: dict[str, Any]) -> ArtifactVersion | None:
        version_id = snapshot.get("version_id")
        artifact_id = snapshot.get("artifact_id")
        if not isinstance(version_id, str) or not isinstance(artifact_id, str):
            return None
        result = (
            await self.session.execute(
                select(ArtifactVersion)
                .join(Artifact, Artifact.id == ArtifactVersion.artifact_id)
                .where(
                    ArtifactVersion.id == version_id,
                    ArtifactVersion.artifact_id == artifact_id,
                    ArtifactVersion.org_id == self.org_id,
                    Artifact.org_id == self.org_id,
                    Artifact.kind == "program",
                )
            )
        ).scalar_one_or_none()
        if result is None or result.content_hash != snapshot.get("content_hash"):
            return None
        return result

    async def _validate_product_scope(self, kind: str, payload: dict[str, Any], scope: Any) -> None:
        """Resolve every explicit well-section reference; never trust client-supplied IDs by shape."""
        if kind != "program":
            return
        resolver = DocumentScopeResolver(self.session, org_id=self.org_id)
        seen: set[str] = set()
        for section in payload.get("sections") or []:
            section_id = section.get("section_id") if isinstance(section, dict) else None
            if not section_id:
                continue
            if section_id in seen:
                raise ValidationFailed("a drilling-program revision cannot repeat a well-section identity", details={"section_id": section_id})
            seen.add(section_id)
            if scope.section_id and section_id != scope.section_id:
                raise ValidationFailed(
                    "a section-scoped engineering artifact cannot reference a different section",
                    details={"artifact_section_id": scope.section_id, "payload_section_id": section_id},
                )
            await resolver.resolve(
                DocumentScope(
                    project_id=scope.project_id,
                    well_id=scope.well_id,
                    wellbore_id=scope.wellbore_id,
                    section_id=section_id,
                )
            )

    async def _resolve_source_lineage(
        self,
        payload: dict[str, Any],
        scope: DocumentScope,
    ) -> list[dict[str, Any]]:
        refs: set[tuple[str, str]] = set()

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                kind = value.get("kind")
                identifier = value.get("id")
                if kind in {"document", "engine_run"} and isinstance(identifier, str):
                    refs.add((kind, identifier))
                source_kind = value.get("source_kind")
                source_ref = value.get("source_ref")
                if source_kind == "source_document" and isinstance(source_ref, str):
                    refs.add(("document", source_ref))
                elif source_kind == "engine_run" and isinstance(source_ref, str):
                    refs.add(("engine_run", source_ref))
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(payload)
        resolved: list[dict[str, Any]] = []
        for kind, identifier in sorted(refs):
            if kind == "document":
                match = (
                    await self.session.execute(
                        select(Document, RawArtifact)
                        .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
                        .where(
                            Document.id == identifier,
                            Document.org_id == self.org_id,
                            document_not_quarantined_condition(Document, RawArtifact),
                        )
                    )
                ).one_or_none()
                if match is None:
                    raise NotFound(f"source document {identifier!r} not found or quarantined")
                document, raw = match
                if raw is None:
                    raise ValidationFailed("a metadata-only document cannot be used as engineering evidence", details={"document_id": identifier})
                validate_source_scope(
                    document,
                    project_id=scope.project_id,
                    well_id=scope.well_id,
                    wellbore_id=scope.wellbore_id,
                    section_id=scope.section_id,
                    operation_id=scope.operation_id,
                )
                resolved.append(
                    {
                        "kind": "document",
                        "id": document.id,
                        "raw_artifact_id": raw.id,
                        "sha256": raw.sha256,
                        "scope": {
                            "project_id": document.project_id,
                            "well_id": document.well_id,
                            "wellbore_id": document.wellbore_id,
                            "section_id": document.section_id,
                            "operation_id": document.operation_id,
                        },
                    }
                )
            else:
                row = (
                    await self.session.execute(
                        select(EngineRun).where(EngineRun.id == identifier, EngineRun.org_id == self.org_id)
                    )
                ).scalar_one_or_none()
                if row is None:
                    raise NotFound(f"source engine run {identifier!r} not found")
                if row.status != "succeeded":
                    raise ValidationFailed("only successful registered-engine runs can support an authored quantity", details={"engine_run_id": identifier, "status": row.status})
                validate_source_scope(
                    row,
                    project_id=scope.project_id,
                    well_id=scope.well_id,
                    wellbore_id=scope.wellbore_id,
                    section_id=scope.section_id,
                    operation_id=scope.operation_id,
                )
                resolved.append(
                    {
                        "kind": "engine_run",
                        "id": row.id,
                        "engine_key": row.engine_key,
                        "engine_version": row.engine_version,
                        "outputs_hash": row.outputs_hash,
                        "scope": {
                            "project_id": row.project_id,
                            "well_id": row.well_id,
                            "wellbore_id": row.wellbore_id,
                            "section_id": row.section_id,
                            "operation_id": row.operation_id,
                        },
                    }
                )
        return resolved

    @staticmethod
    def _content_hash(
        *,
        schema_key: str,
        schema_version: int,
        payload: dict[str, Any],
        source_lineage: list[dict[str, Any]],
        linked_program: dict[str, Any] | None,
    ) -> str:
        canonical = json.dumps(
            {
                "schema_key": schema_key,
                "schema_version": schema_version,
                "payload": payload,
                "source_lineage": source_lineage,
                "linked_program": linked_program,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
