"""The asset domain: identity, lifecycle, lineage and the services that govern them.

The database models describe *shape*; this package describes *meaning and rules*. Everything that
decides whether an asset change is allowed — which well types exist, which lifecycle transitions
are legal, what a wellbore's parent may be, which fields a caller may edit — lives here, so a router
can stay a thin translation between HTTP and the domain.

The audit trail for every mutation is produced here too (``drillai.core.audit``), which is why an
asset write is never a bare ``session.add``.
"""

from __future__ import annotations

from drillai.assets.lifecycle import (
    SECTION_TRANSITIONS,
    WELL_TRANSITIONS,
    WELLBORE_TRANSITIONS,
    TransitionPlan,
    allowed_transitions,
    plan_section_transition,
    plan_well_transition,
    plan_wellbore_transition,
)
from drillai.assets.service import AssetService
from drillai.assets.vocabulary import (
    DEFAULT_ELEVATION_DATUM,
    DEFAULT_SECTION_KIND,
    DEFAULT_WELL_TYPE,
    DEFAULT_WELLBORE_PURPOSE,
    SECTION_STATUSES,
    WELLBORE_STATUSES,
    canonical_choice,
)

__all__ = [
    "DEFAULT_ELEVATION_DATUM",
    "DEFAULT_SECTION_KIND",
    "DEFAULT_WELLBORE_PURPOSE",
    "DEFAULT_WELL_TYPE",
    "SECTION_STATUSES",
    "SECTION_TRANSITIONS",
    "WELLBORE_STATUSES",
    "WELLBORE_TRANSITIONS",
    "WELL_TRANSITIONS",
    "AssetService",
    "TransitionPlan",
    "allowed_transitions",
    "canonical_choice",
    "plan_section_transition",
    "plan_well_transition",
    "plan_wellbore_transition",
]
