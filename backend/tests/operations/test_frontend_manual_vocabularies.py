"""Keep manually maintained React choices aligned with the backend vocabulary contract."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from drillai.db.models.operations import (
    EVENT_KINDS,
    EVENT_SEVERITIES,
    EVENT_STATUSES,
    NPT_CATEGORIES,
    OPERATION_CLASSES,
    OPERATION_KINDS,
    OPERATION_STATUSES,
)
from drillai.drilling.npt import NOT_NPT_CATEGORY

FRONTEND = Path(__file__).resolve().parents[3] / "frontend/src/pages/wells/OperationsWorkspace.tsx"


def _frontend_array(name: str) -> tuple[str, ...]:
    source = FRONTEND.read_text(encoding="utf-8")
    match = re.search(rf"const {re.escape(name)}\s*=\s*(\[[^\]]*\])", source)
    assert match is not None, f"frontend choice list {name} is missing"
    values = ast.literal_eval(match.group(1))
    assert isinstance(values, list) and all(isinstance(item, str) for item in values)
    return tuple(values)


def test_frontend_operation_event_and_npt_choices_match_backend_contracts() -> None:
    assert _frontend_array("MANUAL_OPERATION_KINDS") == OPERATION_KINDS
    assert _frontend_array("MANUAL_EVENT_KINDS") == EVENT_KINDS
    assert _frontend_array("MANUAL_NPT_CATEGORIES") == tuple(
        category for category in NPT_CATEGORIES if category != NOT_NPT_CATEGORY
    )
    assert _frontend_array("OPERATION_CLASS_FILTERS") == OPERATION_CLASSES
    assert _frontend_array("OPERATION_STATUS_FILTERS") == OPERATION_STATUSES
    assert _frontend_array("EVENT_STATUS_FILTERS") == EVENT_STATUSES
    assert _frontend_array("MANUAL_EVENT_SEVERITIES") == EVENT_SEVERITIES
    assert _frontend_array("MANUAL_ACTUAL_OPERATION_STATUSES") == ("in_progress", "completed")
    assert set(_frontend_array("MANUAL_ACTUAL_OPERATION_STATUSES")).issubset(set(OPERATION_STATUSES))
