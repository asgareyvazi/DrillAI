"""The asset service: the one place asset state is created, changed and governed.

Every mutation in this module does the same five things in the same order, which is the reason it is
a service and not four routers:

1. **resolve and scope** — the row is loaded by ``(id, org_id)``, so another tenant's id is a 404 and
   never a successful read;
2. **check the domain rule** — vocabulary, lineage, geometry, uniqueness, life-cycle edge;
3. **persist** — through the ORM, so ``updated_at`` and the change hooks behave as everywhere else;
4. **record the governance ledger** — ``AuditLog`` with actor, action, level, permission decision and
   the before/after picture (``core/audit.py``);
5. **record the domain change** — a ``ChangeRecord`` through the twin service, which is what the well
   timeline reads, so a rename or a status change appears where the platform already shows history.

The alternative — a router that adds a row and returns it — is what this module replaces, and the
audit table it fills had been empty since the schema was created.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.assets.identity import (
    FIELD_EDITABLE_FIELDS,
    FIELD_MANAGED_FIELDS,
    IMMUTABLE_WELL_FIELDS,
    SECTION_DERIVED_FIELDS,
    SECTION_EDITABLE_FIELDS,
    SECTION_MANAGED_FIELDS,
    WELL_EDITABLE_FIELDS,
    WELL_MANAGED_FIELDS,
    WELLBORE_EDITABLE_FIELDS,
    WELLBORE_IMMUTABLE_FIELDS,
    WELLBORE_MANAGED_FIELDS,
    normalize_identifier,
    normalize_name,
)
from drillai.assets.lifecycle import (
    plan_section_transition,
    plan_well_transition,
    plan_wellbore_transition,
)
from drillai.assets.vocabulary import (
    DATUMS,
    DEFAULT_ELEVATION_DATUM,
    DEFAULT_SECTION_KIND,
    DEFAULT_WELL_TYPE,
    DEFAULT_WELLBORE_PURPOSE,
    SECTION_AS_DRILLED_FIELDS,
    SECTION_KINDS,
    SECTION_STATUSES,
    WELL_STATUSES,
    WELL_TYPES,
    WELLBORE_PURPOSES,
    WELLBORE_STATUSES,
    canonical_choice,
)
from drillai.core.audit import record_audit, snapshot
from drillai.core.errors import Conflict, NotFound, ValidationFailed
from drillai.core.ids import new_id
from drillai.db.models import (
    Document,
    EvidenceLink,
    Field,
    Operation,
    Project,
    Rig,
    TwinAspect,
    Well,
    Wellbore,
    WellSection,
)
from drillai.security.actions import Principal
from drillai.twin.service import ChangeEvent, TwinService

__all__ = ["AssetService"]

#: Purposes that describe a hole branching off or entering an existing hole. They require a parent;
#: ``original`` must not have one. This is what keeps lineage structured instead of guessed from the
#: wellbore's name.
DERIVED_PURPOSES = frozenset({"sidetrack", "bypass", "reentry"})


class AssetService:
    """Governed reads and writes for projects, fields, rigs, wells, wellbores and sections."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        org_id: str,
        principal: Principal | None = None,
        request_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.session = session
        self.org_id = org_id
        self.principal = principal
        self.request_id = request_id
        self.ip_address = ip_address
        self.user_agent = user_agent

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
        """Both the stored column and the client's ISO string mean the same instant; compare as one."""
        if value is None:
            return None
        return value.astimezone(dt.UTC) if value.tzinfo else value.replace(tzinfo=dt.UTC)

    def _require_fresh(self, row: Any, expected_updated_at: dt.datetime | None, *, label: str) -> None:
        """Refuse a write that was composed against a version of the row that has since changed.

        Master-data forms are read, thought about, then saved. Between those moments somebody else may
        have moved the well to another field or corrected its rig, and last-write-wins would discard
        that edit without anyone noticing. A caller that sends the ``updated_at`` it read gets a
        conflict instead; a caller that sends nothing keeps the old behaviour.
        """
        if expected_updated_at is None:
            return
        current = self._as_utc(getattr(row, "updated_at", None))
        if current != self._as_utc(expected_updated_at):
            raise Conflict(
                f"the {label} was changed by someone else since you read it",
                details={
                    "resource": label,
                    "id": row.id,
                    "expected_updated_at": expected_updated_at.isoformat(),
                    "current_updated_at": current.isoformat() if current else None,
                },
            )

    def _twin(self) -> TwinService:
        return TwinService(self.session, org_id=self.org_id)

    async def _row(self, model: type, row_id: str, label: str) -> Any:
        row = (
            await self.session.execute(select(model).where(model.id == row_id, model.org_id == self.org_id))
        ).scalar_one_or_none()
        if row is None:
            raise NotFound(f"{label} {row_id!r} not found")
        return row

    async def _audit(
        self,
        *,
        action: str,
        resource_kind: str,
        resource_id: str | None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
        project_id: str | None = None,
        well_id: str | None = None,
    ) -> None:
        await record_audit(
            self.session,
            org_id=self.org_id,
            action=action,
            resource_kind=resource_kind,
            resource_id=resource_id,
            principal=self.principal,
            before=before,
            after=after,
            details=details,
            project_id=project_id,
            well_id=well_id,
            request_id=self.request_id,
            trace_id=self.request_id,
            ip_address=self.ip_address,
            user_agent=self.user_agent,
        )

    async def _change(
        self,
        *,
        subject_kind: str,
        subject_id: str,
        change_type: str,
        field_paths: list[str],
        before: dict[str, Any],
        after: dict[str, Any],
        well_id: str,
        project_id: str | None,
        reason: str | None = None,
    ) -> None:
        """Record the change where the platform already keeps well history (the timeline's source)."""
        await self._twin().record_change(
            well_id,
            ChangeEvent(
                subject_kind=subject_kind,
                subject_id=subject_id,
                change_type=change_type,
                field_paths=field_paths,
                before=before,
                after=after,
                reason=reason,
                actor_kind="user" if self.principal else "system",
                actor_id=self.principal.id if self.principal else None,
                source="api",
                source_ref=self.request_id,
            ),
            project_id=project_id,
            trace_id=self.request_id,
        )

    def _refuse_unknown_fields(
        self,
        payload: dict[str, Any],
        *,
        model: str,
        editable: tuple[str, ...],
        immutable: tuple[str, ...] = (),
        managed: dict[str, str] | None = None,
    ) -> None:
        """Refuse a field the caller tried to set, by name, and say who owns it.

        Silently ignoring an unknown key is worse than refusing it: a client that sends ``status`` to a
        PATCH endpoint would be told the edit succeeded while nothing moved, and would render a status
        the server never stored. The three refusals are deliberately distinguishable — a field that
        identifies the record, a field another part of the domain owns, and a field that does not exist
        — because "422: not editable" is not an answer a caller can act on.
        """
        editable_set = set(editable)
        unknown = [key for key in payload if key not in editable_set]
        if not unknown:
            return
        owned = managed or {}
        # A field another part of the domain owns is reported as *owned*, not as unchangeable: "status
        # identifies the well" would be false, and it would send the caller looking for a different
        # well instead of the life-cycle endpoint.
        managed_hit = {key: owned[key] for key in unknown if key in owned}
        if managed_hit:
            first = sorted(managed_hit)[0]
            raise ValidationFailed(
                f"{first} is not edited through the master-data API; it is owned by {managed_hit[first]}",
                details={"field": model, "managed": managed_hit, "editable": sorted(editable_set)},
            )
        immutable_hit = [key for key in unknown if key in immutable]
        if immutable_hit:
            raise ValidationFailed(
                f"these fields identify the {model} and cannot be changed: {', '.join(sorted(immutable_hit))}",
                details={
                    "field": model,
                    "immutable": sorted(immutable_hit),
                    "editable": sorted(editable_set),
                },
            )
        raise ValidationFailed(
            f"unknown {model} fields: {', '.join(sorted(unknown))}",
            details={"field": model, "unknown": sorted(unknown), "editable": sorted(editable_set)},
        )

    # ------------------------------------------------------------------ projects

    async def get_project(self, project_id: str) -> Project:
        return await self._row(Project, project_id, "project")

    async def project_well_count(self, project_id: str) -> int:
        return (
            await self.session.execute(
                select(func.count())
                .select_from(Well)
                .where(Well.project_id == project_id, Well.org_id == self.org_id)
            )
        ).scalar_one()

    async def create_project(
        self,
        *,
        name: str,
        code: str | None = None,
        operator: str | None = None,
        country: str | None = None,
        basin: str | None = None,
        phase: str | None = None,
        description: str | None = None,
    ) -> Project:
        clean_name = normalize_name(name, field="name", max_length=200)
        project = Project(
            id=new_id("prj"),
            org_id=self.org_id,
            name=clean_name,
            code=code,
            operator=operator,
            country=country,
            basin=basin,
            phase=phase,
            description=description,
            status="active",
        )
        self.session.add(project)
        await self.session.flush()
        await self._audit(
            action="project.create",
            resource_kind="project",
            resource_id=project.id,
            after=snapshot(project, ("id", "name", "code", "operator", "country", "basin", "status")),
            project_id=project.id,
        )
        return project

    #: A project's own master data. Deliberately short: its container role is identity, so widening
    #: what a caller may rename never touches the ids that documents and wells already point at.
    PROJECT_EDITABLE_FIELDS: tuple[str, ...] = (
        "name",
        "code",
        "operator",
        "country",
        "basin",
        "phase",
        "description",
    )

    async def update_project(
        self, project_id: str, payload: dict[str, Any], *, expected_updated_at: dt.datetime | None = None
    ) -> Project:
        project = await self.get_project(project_id)
        self._require_fresh(project, expected_updated_at, label="project")
        self._refuse_unknown_fields(
            payload,
            model="project",
            editable=self.PROJECT_EDITABLE_FIELDS,
            immutable=("id", "org_id", "status", "datum_policy", "created_at", "updated_at"),
        )
        before = snapshot(project, self.PROJECT_EDITABLE_FIELDS)
        for key, value in payload.items():
            setattr(
                project, key, normalize_name(value, field=key, max_length=200) if key == "name" else value
            )
        await self.session.flush()
        after = snapshot(project, self.PROJECT_EDITABLE_FIELDS)
        changed = sorted(key for key in self.PROJECT_EDITABLE_FIELDS if before.get(key) != after.get(key))
        if changed:
            await self._audit(
                action="project.update",
                resource_kind="project",
                resource_id=project.id,
                before={key: before.get(key) for key in changed},
                after={key: after.get(key) for key in changed},
                details={"changed": changed},
                project_id=project.id,
            )
        return project

    # ------------------------------------------------------------------ rigs

    async def list_rigs(self, *, limit: int = 200, offset: int = 0) -> tuple[list[Rig], int]:
        """The rig register, so a caller can name a real rig instead of typing a free-text string."""
        stmt = select(Rig).where(Rig.org_id == self.org_id)
        total = (await self.session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        rows = (
            (await self.session.execute(stmt.order_by(Rig.name).limit(limit).offset(offset))).scalars().all()
        )
        return list(rows), total

    # ------------------------------------------------------------------ fields

    async def list_fields(
        self,
        *,
        project_id: str | None = None,
        query: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[Field], int]:
        """Fields, searchable by canonical name *and* alias — which is what an alias is for.

        The alias match reads the JSON array as text, so it is a scan rather than an index lookup. On a
        register that holds tens of fields per project that is the right trade; a deployment that ever
        holds thousands would want the alias in its own table, and this is the line that says so.
        """
        stmt = select(Field).where(Field.org_id == self.org_id)
        if project_id:
            stmt = stmt.where(Field.project_id == project_id)
        if query:
            needle = f"%{query.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(Field.name).like(needle),
                    func.lower(cast(Field.aliases, Text)).like(needle),
                )
            )
        total = (await self.session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        rows = (
            (await self.session.execute(stmt.order_by(Field.name).limit(limit).offset(offset)))
            .scalars()
            .all()
        )
        return list(rows), total

    async def get_field(self, field_id: str) -> Field:
        return await self._row(Field, field_id, "field")

    async def _require_field_in_project(self, field_id: str | None, project_id: str) -> Field | None:
        """A field may only be attached to a well in the field's own project.

        This is the invariant that a foreign key alone cannot express: the column exists, but nothing
        stopped a well in project A from naming a field in project B.
        """
        if field_id is None:
            return None
        field = await self._row(Field, field_id, "field")
        if field.project_id != project_id:
            raise ValidationFailed(
                "the field belongs to a different project",
                details={
                    "field_id": field_id,
                    "field_project_id": field.project_id,
                    "well_project_id": project_id,
                },
            )
        return field

    async def create_field(
        self,
        *,
        project_id: str,
        name: str,
        country: str | None = None,
        basin: str | None = None,
        water_depth_si: float | None = None,
        centroid_lat: float | None = None,
        centroid_lon: float | None = None,
        notes: str | None = None,
        aliases: list[str] | None = None,
    ) -> Field:
        await self.get_project(project_id)
        clean_name = normalize_name(name, field="name", max_length=200)
        existing = (
            await self.session.execute(
                select(Field).where(
                    Field.org_id == self.org_id,
                    Field.project_id == project_id,
                    func.lower(Field.name) == clean_name.lower(),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise Conflict(
                f"field {clean_name!r} already exists in this project",
                details={"field": "name", "value": clean_name, "existing_id": existing.id},
            )
        field = Field(
            id=new_id("fld"),
            org_id=self.org_id,
            project_id=project_id,
            name=clean_name,
            country=country,
            basin=basin,
            water_depth_si=water_depth_si,
            centroid_lat=centroid_lat,
            centroid_lon=centroid_lon,
            notes=notes,
            aliases=list(aliases or []),
            status="active",
        )
        self.session.add(field)
        await self.session.flush()
        await self._audit(
            action="field.create",
            resource_kind="field",
            resource_id=field.id,
            after=snapshot(field, ("id", "project_id", "name", "country", "basin", "status")),
            project_id=project_id,
        )
        return field

    async def update_field(
        self, field_id: str, payload: dict[str, Any], *, expected_updated_at: dt.datetime | None = None
    ) -> Field:
        field = await self.get_field(field_id)
        self._require_fresh(field, expected_updated_at, label="field")
        editable = FIELD_EDITABLE_FIELDS
        self._refuse_unknown_fields(
            payload,
            model="field",
            editable=FIELD_EDITABLE_FIELDS,
            immutable=("id", "org_id", "project_id", "created_at", "updated_at"),
            managed=FIELD_MANAGED_FIELDS,
        )
        before = snapshot(field, editable)
        if "name" in payload:
            clean = normalize_name(payload["name"], field="name", max_length=200)
            clash = (
                await self.session.execute(
                    select(Field).where(
                        Field.org_id == self.org_id,
                        Field.project_id == field.project_id,
                        func.lower(Field.name) == clean.lower(),
                        Field.id != field.id,
                    )
                )
            ).scalar_one_or_none()
            if clash is not None:
                raise Conflict(
                    f"field {clean!r} already exists in this project",
                    details={"field": "name", "value": clean, "existing_id": clash.id},
                )
            field.name = clean
        for key, value in payload.items():
            if key == "name":
                continue
            setattr(field, key, value)
        await self.session.flush()
        after = snapshot(field, editable)
        changed = sorted(key for key in editable if before.get(key) != after.get(key))
        if changed:
            await self._audit(
                action="field.update",
                resource_kind="field",
                resource_id=field.id,
                before=before,
                after=after,
                details={"changed": changed},
                project_id=field.project_id,
            )
        return field

    # ------------------------------------------------------------------ wells

    async def list_wells(
        self,
        *,
        project_id: str | None = None,
        field_id: str | None = None,
        status: str | None = None,
        well_type: str | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Well], int]:
        """Wells in this organization, filtered in the database.

        Filtering belongs here rather than in the browser for the reason pagination exists at all: a
        client that filters what it was handed shows an incomplete list with the confidence of a
        complete one. The identifiers are searchable too — ``uwi`` and ``api_number`` are how an
        engineer often refers to a well in terms its canonical name does not contain.
        """
        stmt = select(Well).where(Well.org_id == self.org_id)
        if project_id:
            stmt = stmt.where(Well.project_id == project_id)
        if field_id:
            stmt = stmt.where(Well.field_id == field_id)
        if status:
            stmt = stmt.where(Well.status == status)
        if well_type:
            stmt = stmt.where(Well.well_type == well_type)
        if query:
            needle = f"%{query.strip()}%"
            stmt = stmt.where(
                or_(
                    Well.name.ilike(needle),
                    Well.uwi.ilike(needle),
                    Well.api_number.ilike(needle),
                    Well.operator.ilike(needle),
                )
            )
        total = (await self.session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        rows = (
            (await self.session.execute(stmt.order_by(Well.name).limit(limit).offset(offset))).scalars().all()
        )
        return list(rows), total

    async def get_well(self, well_id: str) -> Well:
        return await self._row(Well, well_id, "well")

    async def _require_unique_well_identity(
        self,
        *,
        project_id: str,
        name: str | None = None,
        uwi: str | None = None,
        api_number: str | None = None,
        exclude_id: str | None = None,
    ) -> None:
        """Refuse a duplicate well identity with a *domain* error rather than a database traceback.

        The old path let the unique constraint on ``(project_id, name)`` fire inside the session, which
        surfaced as an unhandled ``IntegrityError`` — a 500 for what is plainly a client mistake. The
        checks here produce the 409 the UI can render; the constraints remain as the backstop for two
        requests racing to insert the same identity.
        """

        async def clash(stmt) -> Well | None:
            if exclude_id:
                stmt = stmt.where(Well.id != exclude_id)
            return (await self.session.execute(stmt)).scalar_one_or_none()

        if name is not None:
            found = await clash(
                select(Well).where(
                    Well.org_id == self.org_id,
                    Well.project_id == project_id,
                    func.lower(Well.name) == name.lower(),
                )
            )
            if found is not None:
                raise Conflict(
                    f"well {name!r} already exists in this project",
                    details={"field": "name", "value": name, "existing_id": found.id},
                )
        if uwi is not None:
            found = await clash(select(Well).where(Well.org_id == self.org_id, Well.uwi == uwi))
            if found is not None:
                raise Conflict(
                    f"UWI {uwi!r} is already registered to another well",
                    details={"field": "uwi", "value": uwi, "existing_id": found.id},
                )
        if api_number is not None:
            found = await clash(select(Well).where(Well.org_id == self.org_id, Well.api_number == api_number))
            if found is not None:
                raise Conflict(
                    f"API number {api_number!r} is already registered to another well",
                    details={"field": "api_number", "value": api_number, "existing_id": found.id},
                )

    async def create_well(
        self,
        *,
        project_id: str,
        name: str,
        well_type: str | None = None,
        uwi: str | None = None,
        api_number: str | None = None,
        field_id: str | None = None,
        operator: str | None = None,
        is_offshore: bool = False,
        elevation_datum: str | None = None,
        total_depth_planned_si: float | None = None,
        spud_date: dt.datetime | None = None,
        objectives: str | None = None,
        tags: list[str] | None = None,
        rig_id: str | None = None,
        surface_lat: float | None = None,
        surface_lon: float | None = None,
        kb_elevation_si: float | None = None,
        ground_elevation_si: float | None = None,
        water_depth_si: float | None = None,
        slot: str | None = None,
        pad_name: str | None = None,
        target_formations: list[str] | None = None,
    ) -> Well:
        await self.get_project(project_id)
        await self._require_field_in_project(field_id, project_id)
        clean_name = normalize_name(name, field="name", max_length=200)
        clean_uwi = normalize_identifier(uwi, field="uwi", max_length=80)
        clean_api = normalize_identifier(api_number, field="api_number", max_length=40)
        await self._require_unique_well_identity(
            project_id=project_id, name=clean_name, uwi=clean_uwi, api_number=clean_api
        )
        if total_depth_planned_si is not None and total_depth_planned_si <= 0:
            raise ValidationFailed(
                "total_depth_planned_si must be greater than zero",
                details={"field": "total_depth_planned_si", "value": total_depth_planned_si},
            )
        rig: Rig | None = None
        if rig_id is not None:
            rig = await self._row(Rig, rig_id, "rig")

        well = Well(
            id=new_id("wel"),
            org_id=self.org_id,
            project_id=project_id,
            field_id=field_id,
            name=clean_name,
            uwi=clean_uwi,
            api_number=clean_api,
            well_type=canonical_choice(well_type, WELL_TYPES, field="well_type", default=DEFAULT_WELL_TYPE),
            operator=operator,
            is_offshore=is_offshore,
            elevation_datum=canonical_choice(
                elevation_datum, DATUMS, field="elevation_datum", default=DEFAULT_ELEVATION_DATUM
            ),
            status="planned",
            total_depth_planned_si=total_depth_planned_si,
            spud_date=spud_date,
            objectives=objectives,
            tags=list(tags or []),
            target_formations=list(target_formations or []),
            surface_lat=surface_lat,
            surface_lon=surface_lon,
            kb_elevation_si=kb_elevation_si,
            ground_elevation_si=ground_elevation_si,
            water_depth_si=water_depth_si,
            slot=slot,
            pad_name=pad_name,
            rig_id=rig_id,
        )
        self.session.add(well)
        try:
            await self.session.flush()
        except IntegrityError as exc:  # pragma: no cover - the pre-checks above normally catch this
            raise Conflict(
                "a well with this identity already exists",
                details={"field": "name", "value": clean_name, "database_error": str(exc.orig)},
            ) from exc
        if rig is not None:
            rig.current_well_id = well.id
        await self._audit(
            action="well.create",
            resource_kind="well",
            resource_id=well.id,
            after=snapshot(well, (*WELL_EDITABLE_FIELDS, "id", "project_id", "status", "rig_id")),
            project_id=project_id,
            well_id=well.id,
        )
        await self._change(
            subject_kind="well",
            subject_id=well.id,
            change_type="created",
            field_paths=["well"],
            before={},
            after={"name": well.name, "well_type": well.well_type, "status": well.status},
            well_id=well.id,
            project_id=project_id,
            reason="well created",
        )
        return well

    async def update_well(
        self,
        well_id: str,
        payload: dict[str, Any],
        *,
        reason: str | None = None,
        expected_updated_at: dt.datetime | None = None,
    ) -> Well:
        well = await self.get_well(well_id)
        self._require_fresh(well, expected_updated_at, label="well")
        self._refuse_unknown_fields(
            payload,
            model="well",
            editable=WELL_EDITABLE_FIELDS,
            immutable=IMMUTABLE_WELL_FIELDS,
            managed=WELL_MANAGED_FIELDS,
        )
        before = snapshot(well, WELL_EDITABLE_FIELDS)

        clean: dict[str, Any] = {}
        for key, value in payload.items():
            if key == "name":
                clean[key] = normalize_name(value, field="name", max_length=200)
            elif key == "uwi":
                clean[key] = normalize_identifier(value, field="uwi", max_length=80)
            elif key == "api_number":
                clean[key] = normalize_identifier(value, field="api_number", max_length=40)
            elif key == "well_type":
                clean[key] = canonical_choice(value, WELL_TYPES, field="well_type", default=DEFAULT_WELL_TYPE)
            elif key == "elevation_datum":
                clean[key] = canonical_choice(
                    value, DATUMS, field="elevation_datum", default=DEFAULT_ELEVATION_DATUM
                )
            elif key == "total_depth_planned_si":
                if value is not None and value <= 0:
                    raise ValidationFailed(
                        "total_depth_planned_si must be greater than zero",
                        details={"field": "total_depth_planned_si", "value": value},
                    )
                clean[key] = value
            else:
                clean[key] = value

        await self._require_unique_well_identity(
            project_id=well.project_id,
            name=clean.get("name"),
            uwi=clean.get("uwi"),
            api_number=clean.get("api_number"),
            exclude_id=well.id,
        )
        if "field_id" in clean:
            await self._require_field_in_project(clean["field_id"], well.project_id)

        for key, value in clean.items():
            setattr(well, key, value)
        await self.session.flush()
        after = snapshot(well, WELL_EDITABLE_FIELDS)
        changed = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
        if not changed:
            # An edit that changes nothing is not an error, but it is not a change either: recording it
            # would fill the ledger with no-ops and make the trail harder to read.
            return well
        details = {
            "changed": changed,
            "before": {key: before.get(key) for key in changed},
            "after": {key: after.get(key) for key in changed},
            # Recorded even when absent: a reader can then tell "no reason was given" from "the reason
            # was not recorded", which are different statements about the same edit.
            "reason": reason,
        }
        await self._audit(
            action="well.update",
            resource_kind="well",
            resource_id=well.id,
            before={key: before.get(key) for key in changed},
            after={key: after.get(key) for key in changed},
            details=details,
            project_id=well.project_id,
            well_id=well.id,
        )
        await self._change(
            subject_kind="well",
            subject_id=well.id,
            change_type="updated",
            field_paths=changed,
            before={key: before.get(key) for key in changed},
            after={key: after.get(key) for key in changed},
            well_id=well.id,
            project_id=well.project_id,
            reason=reason,
        )
        return well

    async def transition_well(self, well_id: str, target: str, *, reason: str | None = None) -> Well:
        well = await self.get_well(well_id)
        requested = canonical_choice(target, WELL_STATUSES, field="status")
        plan = plan_well_transition(well_id=well.id, current=well.status, requested=requested)
        before_status = well.status
        well.status = plan.to_status
        await self.session.flush()
        await self._audit(
            action="well.lifecycle",
            resource_kind="well",
            resource_id=well.id,
            before={"status": before_status},
            after={"status": well.status},
            details={"reason": reason},
            project_id=well.project_id,
            well_id=well.id,
        )
        await self._change(
            subject_kind="well",
            subject_id=well.id,
            change_type="lifecycle",
            field_paths=["status"],
            before={"status": before_status},
            after={"status": well.status},
            well_id=well.id,
            project_id=well.project_id,
            reason=reason,
        )
        return well

    async def assign_rig(self, well_id: str, rig_id: str | None, *, reason: str | None = None) -> Well:
        """Attach or detach a rig, keeping ``Rig.current_well_id`` consistent in both directions.

        A rig has one current well, and that pointer is the reason this is not a plain column write: a
        well could previously name a rig that had never been told about it, leaving the two records
        disagreeing about where the rig was.
        """
        well = await self.get_well(well_id)
        previous_rig_id = well.rig_id
        if rig_id is None:
            well.rig_id = None
            if previous_rig_id:
                previous = (
                    await self.session.execute(
                        select(Rig).where(Rig.id == previous_rig_id, Rig.org_id == self.org_id)
                    )
                ).scalar_one_or_none()
                if previous is not None and previous.current_well_id == well.id:
                    previous.current_well_id = None
        else:
            rig = await self._row(Rig, rig_id, "rig")
            conflict = (
                (
                    await self.session.execute(
                        select(Well).where(
                            Well.org_id == self.org_id, Well.rig_id == rig.id, Well.id != well.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            if conflict:
                raise Conflict(
                    f"rig {rig.name!r} is already assigned to another well",
                    details={"rig_id": rig.id, "well_ids": [row.id for row in conflict]},
                )
            well.rig_id = rig.id
            rig.current_well_id = well.id
        await self.session.flush()
        if previous_rig_id != well.rig_id:
            await self._audit(
                action="well.assign_rig",
                resource_kind="well",
                resource_id=well.id,
                before={"rig_id": previous_rig_id},
                after={"rig_id": well.rig_id},
                details={"reason": reason},
                well_id=well.id,
                project_id=well.project_id,
            )
            await self._change(
                subject_kind="well",
                subject_id=well.id,
                change_type="updated",
                field_paths=["rig_id"],
                before={"rig_id": previous_rig_id},
                after={"rig_id": well.rig_id},
                well_id=well.id,
                project_id=well.project_id,
                reason=reason or "rig assignment",
            )
        return well

    # ------------------------------------------------------------------ wellbores

    async def list_wellbores(self, well_id: str) -> list[Wellbore]:
        await self.get_well(well_id)
        rows = (
            (
                await self.session.execute(
                    select(Wellbore)
                    .where(Wellbore.well_id == well_id, Wellbore.org_id == self.org_id)
                    .order_by(Wellbore.sequence)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def get_wellbore(self, wellbore_id: str) -> Wellbore:
        return await self._row(Wellbore, wellbore_id, "wellbore")

    async def _validate_lineage(
        self,
        *,
        well_id: str,
        parent_wellbore_id: str | None,
        purpose: str,
        exclude_id: str | None = None,
    ) -> Wellbore | None:
        """Structured lineage: a parent must exist, belong to the same well, and not create a cycle.

        The parent is never inferred from the wellbore's name. ``ST-1`` is a label an operator chose;
        the relation is data, and it is checked here so that three impossible shapes cannot enter the
        database: a wellbore parented to itself, a wellbore parented to a hole in a different well, and
        a parent chain that loops.
        """
        if parent_wellbore_id is not None:
            if exclude_id is not None and parent_wellbore_id == exclude_id:
                raise ValidationFailed(
                    "a wellbore cannot be its own parent",
                    details={"wellbore_id": exclude_id, "parent_wellbore_id": parent_wellbore_id},
                )
            if purpose == "original":
                # A hole that was drilled from another hole is not the original hole. This is the
                # mirror of the rule below: `original` is defined by having no parent, so naming one
                # would leave the well with a hole whose stated purpose contradicts its lineage.
                raise ValidationFailed(
                    "an original wellbore cannot have a parent",
                    details={
                        "field": "purpose",
                        "purpose": purpose,
                        "parent_wellbore_id": parent_wellbore_id,
                    },
                )
            parent = await self._row(Wellbore, parent_wellbore_id, "parent wellbore")
            if parent.well_id != well_id:
                raise ValidationFailed(
                    "the parent wellbore belongs to a different well",
                    details={
                        "parent_wellbore_id": parent.id,
                        "parent_well_id": parent.well_id,
                        "well_id": well_id,
                    },
                )
            # Walk up the chain: reaching the wellbore being edited means the edit would create a loop.
            seen: set[str] = set()
            cursor: str | None = parent.parent_wellbore_id
            while cursor is not None:
                if cursor in seen:  # pre-existing loop in the data; refuse to extend it
                    raise ValidationFailed(
                        "the parent chain already contains a loop",
                        details={"parent_wellbore_id": parent.id, "repeated_at": cursor},
                    )
                if exclude_id is not None and cursor == exclude_id:
                    raise ValidationFailed(
                        "that parent would make the lineage circular",
                        details={"wellbore_id": exclude_id, "parent_wellbore_id": parent_wellbore_id},
                    )
                seen.add(cursor)
                ancestor = (
                    await self.session.execute(
                        select(Wellbore).where(Wellbore.id == cursor, Wellbore.org_id == self.org_id)
                    )
                ).scalar_one_or_none()
                cursor = ancestor.parent_wellbore_id if ancestor else None
            return parent

        if purpose in DERIVED_PURPOSES:
            raise ValidationFailed(
                f"a {purpose} wellbore must name the wellbore it was drilled from",
                details={
                    "field": "parent_wellbore_id",
                    "purpose": purpose,
                    "derived_purposes": sorted(DERIVED_PURPOSES),
                },
            )
        # `original` with no parent is the definition of the original hole, which is why it is not
        # refused here; the remaining purposes (reamed, pilot, contingency) describe a hole that stands
        # on its own and may or may not name where it came from.
        return None

    async def create_wellbore(
        self,
        well_id: str,
        *,
        name: str,
        purpose: str | None = None,
        sequence: int | None = None,
        parent_wellbore_id: str | None = None,
        planned_td_md_si: float | None = None,
        planned_td_tvd_si: float | None = None,
        kickoff_md_si: float | None = None,
        datum: str | None = None,
    ) -> Wellbore:
        well = await self.get_well(well_id)
        clean_name = normalize_name(name, field="name", max_length=120)
        clean_purpose = canonical_choice(
            purpose, WELLBORE_PURPOSES, field="purpose", default=DEFAULT_WELLBORE_PURPOSE
        )
        await self._validate_lineage(
            well_id=well.id, parent_wellbore_id=parent_wellbore_id, purpose=clean_purpose
        )

        existing = await self.list_wellbores(well_id)
        if sequence is None:
            # The old default was `1` for every wellbore, so a well with two wellbores had two rows
            # claiming position one. Choosing the next free position is what the caller meant.
            sequence = max((row.sequence for row in existing), default=0) + 1
        elif sequence < 1:
            raise ValidationFailed(
                "sequence must be at least 1", details={"field": "sequence", "value": sequence}
            )
        elif any(row.sequence == sequence for row in existing):
            raise ValidationFailed(
                f"this well already has a wellbore at position {sequence}",
                details={
                    "field": "sequence",
                    "value": sequence,
                    "used": sorted(row.sequence for row in existing),
                },
            )

        for label, value in (
            ("planned_td_md_si", planned_td_md_si),
            ("planned_td_tvd_si", planned_td_tvd_si),
        ):
            if value is not None and value <= 0:
                raise ValidationFailed(
                    f"{label} must be greater than zero", details={"field": label, "value": value}
                )

        # Exactly one wellbore per well is active. The first one to exist is the one being drilled;
        # anything added later — a sidetrack above all — becomes active only by an explicit call, so
        # that "which hole is current?" is always something a person decided.
        becomes_active = not any(row.is_active for row in existing)
        wellbore = Wellbore(
            id=new_id("wlb"),
            org_id=self.org_id,
            well_id=well.id,
            name=clean_name,
            purpose=clean_purpose,
            sequence=sequence,
            parent_wellbore_id=parent_wellbore_id,
            status="planned",
            planned_td_md_si=planned_td_md_si,
            planned_td_tvd_si=planned_td_tvd_si,
            kickoff_md_si=kickoff_md_si,
            datum=canonical_choice(datum, DATUMS, field="datum", default="rkb"),
            is_active=becomes_active,
        )
        self.session.add(wellbore)
        try:
            await self.session.flush()
        except IntegrityError as exc:  # pragma: no cover - the checks above normally catch this
            raise Conflict(
                "this well already has a wellbore at that position",
                details={"field": "sequence", "value": sequence, "database_error": str(exc.orig)},
            ) from exc
        await self._audit(
            action="wellbore.create",
            resource_kind="wellbore",
            resource_id=wellbore.id,
            after=snapshot(
                wellbore,
                ("id", "well_id", "name", "purpose", "sequence", "parent_wellbore_id", "status", "is_active"),
            ),
            well_id=well.id,
            project_id=well.project_id,
        )
        await self._change(
            subject_kind="wellbore",
            subject_id=wellbore.id,
            change_type="created",
            field_paths=["name", "purpose", "sequence", "parent_wellbore_id"],
            before={},
            after={
                "name": wellbore.name,
                "purpose": wellbore.purpose,
                "sequence": wellbore.sequence,
                "parent_wellbore_id": wellbore.parent_wellbore_id,
            },
            well_id=well.id,
            project_id=well.project_id,
            reason="wellbore created",
        )
        return wellbore

    async def update_wellbore(
        self, wellbore_id: str, payload: dict[str, Any], *, expected_updated_at: dt.datetime | None = None
    ) -> Wellbore:
        wellbore = await self.get_wellbore(wellbore_id)
        self._require_fresh(wellbore, expected_updated_at, label="wellbore")
        self._refuse_unknown_fields(
            payload,
            model="wellbore",
            editable=WELLBORE_EDITABLE_FIELDS,
            immutable=WELLBORE_IMMUTABLE_FIELDS,
            managed=WELLBORE_MANAGED_FIELDS,
        )
        before = snapshot(wellbore, WELLBORE_EDITABLE_FIELDS)
        clean: dict[str, Any] = {}
        for key, value in payload.items():
            if key == "name":
                clean[key] = normalize_name(value, field="name", max_length=120)
            elif key == "purpose":
                clean[key] = canonical_choice(
                    value, WELLBORE_PURPOSES, field="purpose", default=DEFAULT_WELLBORE_PURPOSE
                )
            elif key == "datum":
                clean[key] = canonical_choice(value, DATUMS, field="datum", default="rkb")
            else:
                clean[key] = value
        purpose = clean.get("purpose", wellbore.purpose)
        parent = clean.get("parent_wellbore_id", wellbore.parent_wellbore_id)
        await self._validate_lineage(
            well_id=wellbore.well_id, parent_wellbore_id=parent, purpose=purpose, exclude_id=wellbore.id
        )
        for label in (
            "planned_td_md_si",
            "planned_td_tvd_si",
            "kickoff_md_si",
            "actual_td_md_si",
            "actual_td_tvd_si",
        ):
            value = clean.get(label, getattr(wellbore, label))
            if value is not None and value <= 0:
                raise ValidationFailed(
                    f"{label} must be greater than zero", details={"field": label, "value": value}
                )
        for key, value in clean.items():
            setattr(wellbore, key, value)
        await self.session.flush()
        after = snapshot(wellbore, WELLBORE_EDITABLE_FIELDS)
        changed = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
        if not changed:
            return wellbore
        well = await self.get_well(wellbore.well_id)
        await self._audit(
            action="wellbore.update",
            resource_kind="wellbore",
            resource_id=wellbore.id,
            before={key: before.get(key) for key in changed},
            after={key: after.get(key) for key in changed},
            details={"changed": changed},
            well_id=wellbore.well_id,
            project_id=well.project_id,
        )
        await self._change(
            subject_kind="wellbore",
            subject_id=wellbore.id,
            change_type="updated",
            field_paths=changed,
            before={key: before.get(key) for key in changed},
            after={key: after.get(key) for key in changed},
            well_id=wellbore.well_id,
            project_id=well.project_id,
        )
        return wellbore

    async def activate_wellbore(self, wellbore_id: str, *, reason: str | None = None) -> Wellbore:
        """Make one wellbore the active hole, and no more than one.

        The deactivation is part of the same transaction as the activation: a failure between the two
        would leave a well with two active holes or none, and the second is indistinguishable from a
        well nobody is drilling.
        """
        wellbore = await self.get_wellbore(wellbore_id)
        well = await self.get_well(wellbore.well_id)
        if wellbore.status == "abandoned":
            raise ValidationFailed(
                "an abandoned wellbore cannot be made active",
                details={"wellbore_id": wellbore.id, "status": wellbore.status},
            )
        previously_active = [
            row
            for row in await self.list_wellbores(wellbore.well_id)
            if row.is_active and row.id != wellbore.id
        ]
        for row in previously_active:
            row.is_active = False
        wellbore.is_active = True
        await self.session.flush()
        await self._audit(
            action="wellbore.activate",
            resource_kind="wellbore",
            resource_id=wellbore.id,
            before={"active_wellbore_ids": [row.id for row in previously_active]},
            after={"active_wellbore_ids": [wellbore.id]},
            details={"reason": reason},
            well_id=wellbore.well_id,
            project_id=well.project_id,
        )
        await self._change(
            subject_kind="well",
            subject_id=wellbore.well_id,
            change_type="updated",
            field_paths=["active_wellbore"],
            before={"active_wellbore_id": previously_active[0].id if previously_active else None},
            after={"active_wellbore_id": wellbore.id},
            well_id=wellbore.well_id,
            project_id=well.project_id,
            reason=reason or "active wellbore changed",
        )
        return wellbore

    async def transition_wellbore(
        self, wellbore_id: str, target: str, *, reason: str | None = None
    ) -> Wellbore:
        wellbore = await self.get_wellbore(wellbore_id)
        requested = canonical_choice(target, WELLBORE_STATUSES, field="status")
        plan = plan_wellbore_transition(wellbore_id=wellbore.id, current=wellbore.status, requested=requested)
        before_status = wellbore.status
        wellbore.status = plan.to_status
        # An abandoned hole is not the hole being drilled: leaving it active would make the cockpit
        # report current depth against a wellbore nobody is working on.
        if plan.to_status == "abandoned" and wellbore.is_active:
            wellbore.is_active = False
        await self.session.flush()
        well = await self.get_well(wellbore.well_id)
        await self._audit(
            action="wellbore.lifecycle",
            resource_kind="wellbore",
            resource_id=wellbore.id,
            before={"status": before_status},
            after={"status": wellbore.status},
            details={"reason": reason},
            well_id=wellbore.well_id,
            project_id=well.project_id,
        )
        await self._change(
            subject_kind="wellbore",
            subject_id=wellbore.id,
            change_type="lifecycle",
            field_paths=["status"],
            before={"status": before_status},
            after={"status": wellbore.status},
            well_id=wellbore.well_id,
            project_id=well.project_id,
            reason=reason,
        )
        return wellbore

    # ------------------------------------------------------------------ sections

    async def list_sections(self, wellbore_id: str) -> list[WellSection]:
        await self.get_wellbore(wellbore_id)
        rows = (
            (
                await self.session.execute(
                    select(WellSection)
                    .where(WellSection.wellbore_id == wellbore_id, WellSection.org_id == self.org_id)
                    .order_by(WellSection.sequence)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def get_section(self, section_id: str) -> WellSection:
        return await self._row(WellSection, section_id, "section")

    def _validate_geometry(self, values: dict[str, Any]) -> None:
        """Structural only: plan above its own bottom, actual above its own bottom, current inside.

        Deliberately *not* checked here: whether the actual sits inside the plan, or whether an actual
        top matches a revised plan. Those are engineering judgements, and a validator that rejected
        them would be asserting a drilling programme it cannot know.
        """
        planned_top, planned_bottom = values.get("planned_top_md_si"), values.get("planned_bottom_md_si")
        if planned_top is not None and planned_bottom is not None and planned_bottom <= planned_top:
            raise ValidationFailed(
                "planned_bottom_md_si must be deeper than planned_top_md_si",
                details={"field": "planned_bottom_md_si", "top": planned_top, "bottom": planned_bottom},
            )
        actual_top, actual_bottom = values.get("actual_top_md_si"), values.get("actual_bottom_md_si")
        if actual_top is not None and actual_bottom is not None and actual_bottom <= actual_top:
            raise ValidationFailed(
                "actual_bottom_md_si must be deeper than actual_top_md_si",
                details={"field": "actual_bottom_md_si", "top": actual_top, "bottom": actual_bottom},
            )
        current = values.get("current_md_si")
        section_top = actual_top if actual_top is not None else planned_top
        if current is not None and section_top is not None and current < section_top:
            raise ValidationFailed(
                "current_md_si cannot be above the section's own top",
                details={"field": "current_md_si", "value": current, "section_top_md_si": section_top},
            )

    async def create_section(
        self,
        wellbore_id: str,
        *,
        sequence: int,
        name: str,
        kind: str | None = None,
        hole_diameter_si: float | None = None,
        hole_diameter_nominal: str | None = None,
        planned_top_md_si: float | None = None,
        planned_bottom_md_si: float | None = None,
        actual_top_md_si: float | None = None,
        actual_bottom_md_si: float | None = None,
        current_md_si: float | None = None,
    ) -> WellSection:
        wellbore = await self.get_wellbore(wellbore_id)
        clean_name = normalize_name(name, field="name", max_length=120)
        if sequence < 1:
            raise ValidationFailed(
                "sequence must be at least 1", details={"field": "sequence", "value": sequence}
            )
        existing = await self.list_sections(wellbore_id)
        if any(row.sequence == sequence for row in existing):
            raise ValidationFailed(
                f"this wellbore already has a section at position {sequence}",
                details={
                    "field": "sequence",
                    "value": sequence,
                    "used": sorted(row.sequence for row in existing),
                },
            )
        values = {
            "planned_top_md_si": planned_top_md_si,
            "planned_bottom_md_si": planned_bottom_md_si,
            "actual_top_md_si": actual_top_md_si,
            "actual_bottom_md_si": actual_bottom_md_si,
            "current_md_si": current_md_si,
        }
        self._validate_geometry(values)
        if hole_diameter_si is not None and hole_diameter_si <= 0:
            raise ValidationFailed(
                "hole_diameter_si must be greater than zero",
                details={"field": "hole_diameter_si", "value": hole_diameter_si},
            )
        section = WellSection(
            id=new_id("sec"),
            org_id=self.org_id,
            wellbore_id=wellbore.id,
            sequence=sequence,
            name=clean_name,
            kind=canonical_choice(kind, SECTION_KINDS, field="kind", default=DEFAULT_SECTION_KIND),
            status="planned",
            hole_diameter_si=hole_diameter_si,
            hole_diameter_nominal=hole_diameter_nominal,
            planned_top_md_si=planned_top_md_si,
            planned_bottom_md_si=planned_bottom_md_si,
            actual_top_md_si=actual_top_md_si,
            actual_bottom_md_si=actual_bottom_md_si,
            current_md_si=current_md_si,
            # Derived, never asserted: a section is planned-only when the record holds no as-drilled
            # depth at all. A caller that sends the flag is refused by name rather than obeyed.
            is_planned_only=not any(values[key] is not None for key in SECTION_AS_DRILLED_FIELDS),
        )
        self.session.add(section)
        try:
            await self.session.flush()
        except IntegrityError as exc:  # pragma: no cover - the checks above normally catch this
            raise Conflict(
                "this wellbore already has a section at that position",
                details={"field": "sequence", "value": sequence, "database_error": str(exc.orig)},
            ) from exc
        well = await self.get_well(wellbore.well_id)
        await self._audit(
            action="section.create",
            resource_kind="section",
            resource_id=section.id,
            after=snapshot(
                section, ("id", "wellbore_id", "sequence", "name", "kind", "status", "is_planned_only")
            ),
            well_id=wellbore.well_id,
            project_id=well.project_id,
        )
        await self._change(
            subject_kind="section",
            subject_id=section.id,
            change_type="created",
            field_paths=["sequence", "name", "kind"],
            before={},
            after={"sequence": section.sequence, "name": section.name, "kind": section.kind},
            well_id=wellbore.well_id,
            project_id=well.project_id,
            reason="section created",
        )
        return section

    async def update_section(
        self, section_id: str, payload: dict[str, Any], *, expected_updated_at: dt.datetime | None = None
    ) -> WellSection:
        section = await self.get_section(section_id)
        self._require_fresh(section, expected_updated_at, label="section")
        self._refuse_unknown_fields(
            payload,
            model="section",
            editable=SECTION_EDITABLE_FIELDS,
            immutable=("id", "wellbore_id", "org_id", "sequence", "created_at", "updated_at"),
            managed={
                **SECTION_MANAGED_FIELDS,
                **dict.fromkeys(SECTION_DERIVED_FIELDS, "the service, from the recorded as-drilled depths"),
            },
        )
        before = snapshot(section, SECTION_EDITABLE_FIELDS)
        clean: dict[str, Any] = {}
        for key, value in payload.items():
            if key == "name":
                clean[key] = normalize_name(value, field="name", max_length=120)
            elif key == "kind":
                clean[key] = canonical_choice(
                    value, SECTION_KINDS, field="kind", default=DEFAULT_SECTION_KIND
                )
            else:
                clean[key] = value

        # A recorded measurement is not a plan to be overwritten. Clearing actual geometry with an
        # ordinary edit would erase the record of what was drilled, which is the one thing a section
        # cannot reconstruct.
        recorded = ("actual_top_md_si", "actual_bottom_md_si", "current_md_si")
        erased = [
            key
            for key in recorded
            if key in clean and clean[key] is None and getattr(section, key) is not None
        ]
        if erased:
            raise ValidationFailed(
                "recorded as-drilled geometry cannot be cleared by an edit",
                details={"field": erased[0], "cleared": erased},
            )

        merged = {key: clean.get(key, getattr(section, key)) for key in SECTION_EDITABLE_FIELDS}
        self._validate_geometry(merged)
        # A plan revision and an as-drilled record are different acts with different consequences, and
        # one of them can invalidate work that hangs off the other. The edit is allowed either way —
        # plans really are revised — but a plan revised *after* the hole was drilled is recorded as
        # such instead of passing for an edit that happened before anyone drilled.
        plan_fields = {"planned_top_md_si", "planned_bottom_md_si"}
        revising_plan = bool(clean.keys() & plan_fields)
        had_as_drilled = any(getattr(section, key) is not None for key in SECTION_AS_DRILLED_FIELDS)
        for key in ("hole_diameter_si", "casing_od_si", "mud_weight_si"):
            value = merged.get(key)
            if value is not None and value <= 0:
                raise ValidationFailed(
                    f"{key} must be greater than zero", details={"field": key, "value": value}
                )
        for key, value in clean.items():
            setattr(section, key, value)
        section.is_planned_only = not any(
            getattr(section, key) is not None for key in SECTION_AS_DRILLED_FIELDS
        )
        await self.session.flush()
        after = snapshot(section, SECTION_EDITABLE_FIELDS)
        changed = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
        if not changed:
            return section
        details: dict[str, Any] = {"changed": changed}
        if revising_plan and had_as_drilled:
            details["plan_revised_with_as_drilled_geometry"] = True
        wellbore = await self.get_wellbore(section.wellbore_id)
        well = await self.get_well(wellbore.well_id)
        await self._audit(
            action="section.update",
            resource_kind="section",
            resource_id=section.id,
            before={key: before.get(key) for key in changed},
            after={key: after.get(key) for key in changed},
            details=details,
            well_id=wellbore.well_id,
            project_id=well.project_id,
        )
        await self._change(
            subject_kind="section",
            subject_id=section.id,
            change_type="updated",
            field_paths=changed,
            before={key: before.get(key) for key in changed},
            after={key: after.get(key) for key in changed},
            well_id=wellbore.well_id,
            project_id=well.project_id,
        )
        return section

    async def transition_section(
        self, section_id: str, target: str, *, reason: str | None = None
    ) -> WellSection:
        section = await self.get_section(section_id)
        requested = canonical_choice(target, SECTION_STATUSES, field="status")
        plan = plan_section_transition(section_id=section.id, current=section.status, requested=requested)
        if plan.to_status == "drilled" and section.actual_bottom_md_si is None:
            # "Drilled" without a recorded bottom would be a claim about hole geometry that no record
            # supports, and every reader of the section would then have to guess how deep it went.
            raise ValidationFailed(
                "a section cannot be recorded as drilled without its as-drilled bottom; "
                "record actual_bottom_md_si first",
                details={
                    "field": "actual_bottom_md_si",
                    "section_id": section.id,
                    "current_status": section.status,
                    "recorded": {key: getattr(section, key) for key in SECTION_AS_DRILLED_FIELDS},
                },
            )
        before_status = section.status
        section.status = plan.to_status
        # `is_planned_only` answers a question about the record, not about the hole: it follows the
        # as-drilled depths and nothing else, so a status change never rewrites it.
        section.is_planned_only = not any(
            getattr(section, key) is not None for key in SECTION_AS_DRILLED_FIELDS
        )
        await self.session.flush()
        wellbore = await self.get_wellbore(section.wellbore_id)
        well = await self.get_well(wellbore.well_id)
        await self._audit(
            action="section.lifecycle",
            resource_kind="section",
            resource_id=section.id,
            before={"status": before_status},
            after={"status": section.status},
            details={"reason": reason},
            well_id=wellbore.well_id,
            project_id=well.project_id,
        )
        await self._change(
            subject_kind="section",
            subject_id=section.id,
            change_type="lifecycle",
            field_paths=["status"],
            before={"status": before_status},
            after={"status": section.status},
            well_id=wellbore.well_id,
            project_id=well.project_id,
            reason=reason,
        )
        return section

    # ------------------------------------------------------------------ history and integrity

    async def wellbore_lineage(self, wellbore_id: str) -> list[Wellbore]:
        """The wellbore's ancestry, oldest first, ending with the wellbore itself.

        Lineage is read from ``parent_wellbore_id`` and never from names: ``ST-1`` is a label an
        operator chose and two wells may both have one. The walk is bounded by the set of visited ids,
        so a loop that somehow reached the database reports a lineage that stops rather than hanging.
        """
        wellbore = await self.get_wellbore(wellbore_id)
        chain = [wellbore]
        seen = {wellbore.id}
        cursor = wellbore.parent_wellbore_id
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            parent = await self._row(Wellbore, cursor, "parent wellbore")
            chain.append(parent)
            cursor = parent.parent_wellbore_id
        chain.reverse()
        return chain

    async def well_context_counts(self, well_id: str) -> dict[str, Any]:
        """How much context hangs off this well, per wellbore — one grouped query per record kind.

        The counts are what makes "the identity spine is intact" checkable from the screen rather than
        only in a test: a rename that had silently detached documents would show as a well with no
        documents and wellbores with none either. Records with no wellbore are the well's own.
        """
        await self.get_well(well_id)
        sources = (
            ("documents", Document),
            ("operations", Operation),
            ("evidence", EvidenceLink),
            ("twin_aspects", TwinAspect),
        )
        counts: dict[str, Any] = {}
        for label, model in sources:
            # Documents, operations and twin aspects can be scoped to a wellbore, a section or the well
            # itself; evidence links are only ever well-scoped. Grouping once per kind keeps this at
            # four queries no matter how many wellbores and sections the well has.
            if hasattr(model, "wellbore_id"):
                stmt = (
                    select(model.wellbore_id, func.count())
                    .where(model.org_id == self.org_id, model.well_id == well_id)
                    .group_by(model.wellbore_id)
                )
                by_wellbore: dict[str, int] = {}
                well_level = 0
                for wellbore_id, count in (await self.session.execute(stmt)).all():
                    if wellbore_id is None:
                        well_level = int(count)
                    else:
                        by_wellbore[str(wellbore_id)] = int(count)
                counts[label] = {
                    "total": well_level + sum(by_wellbore.values()),
                    "well": well_level,
                    "by_wellbore": by_wellbore,
                }
            else:
                total = (
                    await self.session.execute(
                        select(func.count())
                        .select_from(model)
                        .where(model.org_id == self.org_id, model.well_id == well_id)
                    )
                ).scalar_one()
                counts[label] = {"total": int(total), "well": int(total), "by_wellbore": {}}
        return counts

    async def well_history(self, well_id: str, *, limit: int = 50) -> list[Any]:
        """The governance ledger for one well, newest first — what the master-data Audit tab shows."""
        from drillai.db.models import AuditLog

        await self.get_well(well_id)
        rows = (
            (
                await self.session.execute(
                    select(AuditLog)
                    .where(AuditLog.org_id == self.org_id, AuditLog.well_id == well_id)
                    .order_by(AuditLog.occurred_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)
