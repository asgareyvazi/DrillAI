"""What a well *is*, what may change about it, and what may not.

Identity is not a display concern. A well is referred to by five different things and they are not
interchangeable: the internal ``id`` (immutable, the anchor every document, operation, engine run and
workflow run points at), the canonical ``name``, the operator's ``uwi``, a regulatory ``api_number``
and its association with a project and field. This module fixes which of those a caller may edit, so
that a rename is a rename and never an identity change.

The rules, and why:

* ``id``, ``org_id``, ``project_id`` are **immutable**. A well that moves between projects would
  disagree with the records that hang off it: documents, operations and evidence each carry their own
  ``project_id``, so the historical trail would claim a project the well no longer belongs to. Moving
  a well between fields *within* its project is allowed and validated.
* ``status`` is **not editable here**. It moves only through the life-cycle service, which checks the
  transition and records who made it; allowing it as an ordinary field would let a form edit bypass
  the process rules entirely.
* ``twin_state`` is **not editable**. It is twin-owned — ``twin/service.py`` writes it when a snapshot
  is taken — so accepting it from a client would let the asset API overwrite the twin's own view of
  the well.
* ``rig_id`` is editable but goes through :meth:`AssetService.assign_rig`, because assigning a rig
  also has to keep ``Rig.current_well_id`` consistent; it is not a free foreign key.
* ``uwi`` and ``api_number`` are identifiers, not prose: they are trimmed, and internal whitespace is
  collapsed, so that the same identifier typed with a stray space is not a second well.

Nothing here validates the *format* of a UWI or an API number beyond structure. Those formats are
jurisdictional — a 14-digit US API number, a Canadian DLS location and a North Sea designation have
nothing in common — and a regex that rejects a valid identifier from one regulator is worse than no
regex at all. Uniqueness within the organization is what actually prevents duplicates, and that is
enforced by the database.
"""

from __future__ import annotations

import re

from drillai.core.errors import ValidationFailed

__all__ = [
    "FIELD_EDITABLE_FIELDS",
    "FIELD_MANAGED_FIELDS",
    "IMMUTABLE_WELL_FIELDS",
    "SECTION_DERIVED_FIELDS",
    "SECTION_EDITABLE_FIELDS",
    "SECTION_MANAGED_FIELDS",
    "WELLBORE_EDITABLE_FIELDS",
    "WELLBORE_IMMUTABLE_FIELDS",
    "WELLBORE_MANAGED_FIELDS",
    "WELL_EDITABLE_FIELDS",
    "WELL_MANAGED_FIELDS",
    "normalize_identifier",
    "normalize_name",
]

#: Fields a ``PATCH /wells/{id}`` may change. Anything not named here is refused by name rather than
#: ignored, so a client that tries to change ``project_id`` is told why instead of silently no-op'ing.
WELL_EDITABLE_FIELDS: tuple[str, ...] = (
    "name",
    "uwi",
    "api_number",
    "field_id",
    "well_type",
    "operator",
    "is_offshore",
    "surface_lat",
    "surface_lon",
    "kb_elevation_si",
    "ground_elevation_si",
    "water_depth_si",
    "elevation_datum",
    "slot",
    "pad_name",
    "total_depth_planned_si",
    "spud_date",
    "release_date",
    "objectives",
    "target_formations",
    "tags",
)

#: Named separately because a caller asking for one of these deserves an explanation, not a 422 that
#: lists the editable set and leaves them guessing.
IMMUTABLE_WELL_FIELDS: tuple[str, ...] = (
    "id",
    "org_id",
    "project_id",
    "status",
    "twin_state",
    "rig_id",
    "created_at",
    "updated_at",
)

#: A well's fields that another part of the domain owns, with the door that owns them.
WELL_MANAGED_FIELDS: dict[str, str] = {
    "status": "the well lifecycle (POST /wells/{well_id}/lifecycle)",
    "rig_id": "rig assignment (POST /wells/{well_id}/rig), which also keeps the rig's own current-well pointer true",
    "twin_state": "the twin, which writes it when a snapshot is taken (POST /wells/{well_id}/twin/aspects)",
    "attributes": "no master data lives here; this API gives every permanent field its own column",
}

#: Master-data fields on a field (the geographical unit), all of them ordinary edits except ``status``,
#: which is managed so that retiring a field is a recorded act rather than a stray form value.
FIELD_EDITABLE_FIELDS: tuple[str, ...] = (
    "name",
    "country",
    "basin",
    "water_depth_si",
    "centroid_lat",
    "centroid_lon",
    "notes",
    "aliases",
)

FIELD_MANAGED_FIELDS: dict[str, str] = {
    "status": "the field lifecycle status; a field is retired deliberately, not by a form field",
    "geo_meta": "no master data lives here; the location columns above carry it",
}

WELLBORE_MANAGED_FIELDS: dict[str, str] = {
    "status": "the wellbore lifecycle (POST /wellbores/{wellbore_id}/lifecycle)",
    "is_active": "hole activation (POST /wellbores/{wellbore_id}/activate), which enforces one active hole per well",
    "attributes": "no master data lives here",
}

#: Fields that identify a wellbore. ``well_id`` is here rather than in the managed map because moving a
#: hole to another well is not an edit at all: a different hole is a new wellbore naming its parent.
WELLBORE_IMMUTABLE_FIELDS: tuple[str, ...] = ("id", "org_id", "well_id", "sequence", "created_at", "updated_at")

WELLBORE_EDITABLE_FIELDS: tuple[str, ...] = (
    "name",
    "purpose",
    "kickoff_md_si",
    "planned_td_md_si",
    "planned_td_tvd_si",
    "actual_td_md_si",
    "actual_td_tvd_si",
    "datum",
    "parent_wellbore_id",
)

SECTION_EDITABLE_FIELDS: tuple[str, ...] = (
    "name",
    "kind",
    "hole_diameter_si",
    "hole_diameter_nominal",
    "planned_top_md_si",
    "planned_bottom_md_si",
    "actual_top_md_si",
    "actual_bottom_md_si",
    "current_md_si",
    "casing_od_si",
    "casing_od_nominal",
    "casing_weight_si",
    "casing_grade",
    "casing_connection",
    "casing_top_md_si",
    "casing_shoe_md_si",
    "cement_top_md_si",
    "cement_planned_top_md_si",
    "mud_weight_si",
    "mud_weight_min_si",
    "mud_weight_max_si",
    "pore_pressure_gradient_si",
    "fracture_gradient_si",
    "collapse_gradient_si",
    "lot_fit_equivalent_mw_si",
    "pressure_source",
    "notes",
)

#: ``is_planned_only`` describes the *record*: whether everything known about the section's geometry is
#: still a plan. It is recomputed from the as-drilled depths on every write, because a flag a client can
#: set independently is a flag a client can set wrongly — and a section that claims to be drilled while
#: carrying only planned depths is exactly the misrepresentation the depth rules exist to prevent.
SECTION_DERIVED_FIELDS: tuple[str, ...] = ("is_planned_only",)

#: Fields whose owner is another part of the domain. They are named in the refusal so a caller is told
#: which door to use instead of being handed a list of the editable set.
SECTION_MANAGED_FIELDS: dict[str, str] = {
    "status": "the section lifecycle (POST /wellbores/{wellbore_id}/sections/{section_id}/lifecycle)",
    "attributes": "the section's free-form notes field (this column carries no master data)",
}

_WHITESPACE = re.compile(r"\s+")


def normalize_identifier(value: str | None, *, field: str, max_length: int) -> str | None:
    """Trim and collapse an identifier so two spellings of one value cannot become two records.

    ``None`` and the empty string both mean "not recorded" and are returned as ``None``: an operator
    who clears a UWI field has removed the identifier, which is different from recording the string
    ``""``. Uppercasing is deliberate — UWI and API identifiers are case-insensitive in practice, and
    normalising here means the uniqueness constraint sees one value.
    """
    if value is None:
        return None
    stripped = _WHITESPACE.sub(" ", value.strip())
    if not stripped:
        return None
    if len(stripped) > max_length:
        raise ValidationFailed(
            f"{field} must be at most {max_length} characters",
            details={"field": field, "length": len(stripped), "max_length": max_length},
        )
    return stripped.upper()


def normalize_name(value: str | None, *, field: str, max_length: int) -> str:
    """A display name: whitespace collapsed, but **not** upper-cased — casing is the operator's."""
    if value is None:
        raise ValidationFailed(f"{field} is required", details={"field": field})
    stripped = _WHITESPACE.sub(" ", value.strip())
    if not stripped:
        raise ValidationFailed(f"{field} is required", details={"field": field})
    if len(stripped) > max_length:
        raise ValidationFailed(
            f"{field} must be at most {max_length} characters",
            details={"field": field, "length": len(stripped), "max_length": max_length},
        )
    return stripped
