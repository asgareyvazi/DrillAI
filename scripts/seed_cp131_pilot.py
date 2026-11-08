#!/usr/bin/env python
"""Idempotently seed the CP131 synthetic pilot hierarchy through the real API.

This script never resets or deletes a database. It creates the schema when necessary and then uses
the real API to create or reuse only the project, well, wellbore and section named below. It never
writes operational measurements, a survey, a DDR, or an engineering result. The browser journey
creates those through the same real product flows used by an operator.

Example (use a fresh, explicitly isolated path)::

    backend/.venv/bin/python scripts/seed_cp131_pilot.py \
        --database-url sqlite+aiosqlite:///./frontend/.e2e-cp131-local/e2e.db \
        --blob-dir frontend/.e2e-cp131-local/blobs

No data in the repository's existing ``frontend/.e2e`` directory is used or changed by this script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

PROJECT_NAME = "CP131 Synthetic Pilot Project"
WELL_NAME = "CP131-SYNTHETIC-PILOT-01"
WELLBORE_NAME = "CP131 Synthetic main bore"
SECTION_NAME = 'CP131 synthetic 12-1/4" surface section'


class SeedError(RuntimeError):
    """A scoped pilot seed step failed; no reset or deletion is attempted."""


class Api:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def call(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expect: tuple[int, ...] = (200, 201),
    ) -> dict[str, Any]:
        response = await self._client.request(
            method,
            path,
            json=json_body,
            headers=headers,
        )
        if response.status_code not in expect:
            raise SeedError(f"{method} {path} -> {response.status_code}\n{response.text[:1200]}")
        if not response.content:
            return {}
        return response.json()


def _unique_named(rows: list[dict[str, Any]], name: str, *, kind: str) -> dict[str, Any] | None:
    matches = [row for row in rows if row.get("name") == name]
    if len(matches) > 1:
        raise SeedError(f"expected at most one existing {kind} named {name!r}; found {len(matches)}")
    return matches[0] if matches else None


def _idempotency_key(kind: str) -> str:
    return f"cp131-synthetic-pilot-v1-{kind}"


async def seed(database_url: str, blob_dir: pathlib.Path) -> dict[str, Any]:
    if os.environ.get("DRILLAI_ENVIRONMENT", "").strip().lower() == "production":
        raise SeedError("Refusing to create synthetic pilot data in a production environment")

    os.environ["DRILLAI_ENVIRONMENT"] = "development"
    os.environ["DRILLAI_AUTH_ENABLED"] = "false"
    os.environ["DRILLAI_DATABASE_URL"] = database_url
    os.environ["DRILLAI_BLOB_BACKEND"] = "filesystem"
    os.environ["DRILLAI_BLOB_ROOT"] = str(blob_dir.resolve())
    os.environ["DRILLAI_SCHEDULER_ENABLED"] = "false"
    os.environ["DRILLAI_LOG_JSON"] = "false"

    from drillai.core.config import get_settings, reset_settings_cache

    reset_settings_cache()
    settings = get_settings()

    from drillai.api.app import _warm_registries, create_app
    from drillai.db.models import Base

    application = create_app(settings)
    async with application.state.database.engine.begin() as connection:
        # Safe on an initialized dev database; this is not a drop/reset and adds no fixture rows.
        await connection.run_sync(Base.metadata.create_all)
    application.state.catalogues = _warm_registries()

    import httpx

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://cp131-seed.local/api/v1",
        headers={"X-Dev-Roles": "drilling_supervisor,admin,engineer"},
        timeout=60.0,
    ) as client:
        try:
            api = Api(client)
            projects_response = await api.call("GET", "/projects")
            project = _unique_named(projects_response.get("items", []), PROJECT_NAME, kind="project")
            if project is None:
                project = await api.call(
                    "POST",
                    "/projects",
                    json_body={
                        "name": PROJECT_NAME,
                        "code": "CP131-SYN",
                        "operator": "DrillAI (synthetic only)",
                        "country": "NO",
                        "phase": "drilling",
                        "description": (
                            "Isolated CP131 browser-pilot fixture. All values are synthetic and "
                            "are not field data, qualified engineering inputs, or site instructions."
                        ),
                    },
                    headers={"Idempotency-Key": _idempotency_key("project")},
                )

            wells_response = await api.call("GET", "/wells?q=CP131-SYNTHETIC-PILOT-01&limit=500")
            well = _unique_named(wells_response.get("items", []), WELL_NAME, kind="well")
            if well is not None and well.get("project_id") != project["id"]:
                raise SeedError(
                    f"existing pilot well {well['id']} belongs to a different project; refusing to duplicate it"
                )
            if well is None:
                well = await api.call(
                    "POST",
                    "/wells",
                    json_body={
                        "project_id": project["id"],
                        "name": WELL_NAME,
                        "well_type": "development_producer",
                        "operator": "DrillAI (synthetic only)",
                        "elevation_datum": "rkb",
                        "objectives": (
                            "Synthetic integration-pilot objective only; not a field drilling plan."
                        ),
                    },
                    headers={"Idempotency-Key": _idempotency_key("well")},
                )

            wellbores_response = await api.call("GET", f"/wells/{well['id']}/wellbores")
            wellbore = _unique_named(wellbores_response.get("items", []), WELLBORE_NAME, kind="wellbore")
            if wellbore is not None and wellbore.get("well_id") != well["id"]:
                raise SeedError("existing synthetic pilot wellbore is attached to a different well")
            if wellbore is None:
                wellbore = await api.call(
                    "POST",
                    f"/wells/{well['id']}/wellbores",
                    json_body={
                        "name": WELLBORE_NAME,
                        "purpose": "original",
                        "sequence": 1,
                        "planned_td_md_si": 1500.0,
                        "planned_td_tvd_si": 1490.0,
                        "datum": "rkb",
                    },
                    headers={"Idempotency-Key": _idempotency_key("wellbore")},
                )

            sections_response = await api.call("GET", f"/wellbores/{wellbore['id']}/sections")
            section = _unique_named(sections_response.get("items", []), SECTION_NAME, kind="section")
            if section is not None and section.get("wellbore_id") != wellbore["id"]:
                raise SeedError("existing synthetic pilot section is attached to a different wellbore")
            if section is None:
                section = await api.call(
                    "POST",
                    f"/wellbores/{wellbore['id']}/sections",
                    json_body={
                        "sequence": 1,
                        "name": SECTION_NAME,
                        "kind": "surface",
                        "hole_diameter_nominal": '12-1/4"',
                        "hole_diameter_si": 0.31115,
                        "planned_top_md_si": 0.0,
                        "planned_bottom_md_si": 1500.0,
                    },
                    headers={"Idempotency-Key": _idempotency_key("section")},
                )

            return {
                "classification": "synthetic",
                "fixture_id": "cp131-synthetic-pilot-v1",
                "project_id": project["id"],
                "well_id": well["id"],
                "well_name": WELL_NAME,
                "wellbore_id": wellbore["id"],
                "wellbore_name": WELLBORE_NAME,
                "section_id": section["id"],
                "section_name": SECTION_NAME,
                "survey_id": None,
                "survey_note": "Created through the browser journey, not by the master-data seed.",
                "reset_scope": "No reset performed; API seed only creates or reuses these exact named objects.",
            }
        finally:
            await application.state.database.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DRILLAI_DATABASE_URL", "sqlite+aiosqlite:///./.data/cp131.db"),
        help="Already-initialized isolated development database; no schema or data reset is performed.",
    )
    parser.add_argument(
        "--blob-dir",
        default=".data/blobs",
        help="Filesystem document store path for the same database.",
    )
    args = parser.parse_args()
    result = asyncio.run(seed(args.database_url, pathlib.Path(args.blob_dir)))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
