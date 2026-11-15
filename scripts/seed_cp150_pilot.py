#!/usr/bin/env python
"""Create exactly two clearly-labelled synthetic wells via the isolated product API.

This seed refuses non-isolated database/blob paths, refuses an existing database, and never resets or
reuses records. It creates only a project, two wells, and one wellbore per well through the same API
routes the product exposes; daily-report records are created later by the real browser journey.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from typing import Any
from urllib.parse import unquote

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTEND_ROOT = REPO_ROOT / "frontend"
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

PROJECT_NAME = "CP150 Isolated Two-Well Synthetic Pilot"
ORG_SLUG = "cp150-synthetic-pilot"
WELLS = (
    {
        "name": "CP150-SYNTHETIC-WELL-A",
        "wellbore_name": "CP150 Synthetic main bore A",
    },
    {
        "name": "CP150-SYNTHETIC-WELL-B",
        "wellbore_name": "CP150 Synthetic main bore B",
    },
)


class SeedError(RuntimeError):
    """The isolated two-well API seed did not meet its fixture contract."""


def _isolated_paths(database_url: str, blob_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix) or "?" in database_url or "#" in database_url:
        raise SeedError("CP150 requires a plain isolated SQLite database URL")
    raw_path = unquote(database_url[len(prefix) :])
    candidate = pathlib.Path(os.path.abspath(raw_path))
    frontend_root = FRONTEND_ROOT.resolve()
    candidate_blob_dir = pathlib.Path(os.path.abspath(blob_dir))
    if candidate.resolve() != candidate or candidate_blob_dir.resolve() != candidate_blob_dir:
        raise SeedError("CP150 refuses symlinked database or blob paths")
    database_path = candidate
    data_dir = database_path.parent
    expected_blob_dir = data_dir / "blobs"
    marker_path = data_dir / ".cp150-run.json"
    output_marker_path = data_dir / ".cp150-output-run.json"
    run_id = os.environ.get("DRILLAI_CP150_RUN_ID", "")
    invocation_token = os.environ.get("DRILLAI_CP150_INVOCATION_TOKEN", "")
    try:
        marker = None if marker_path.is_symlink() else json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        marker = None
    try:
        output_marker = (
            None
            if output_marker_path.is_symlink()
            else json.loads(output_marker_path.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError):
        output_marker = None
    if (
        not database_path.is_absolute()
        or database_path.name != "e2e.db"
        or data_dir.name == ".e2e"
        or not data_dir.name.startswith(".e2e-cp150-")
        or data_dir.parent != frontend_root
        or not data_dir.is_dir()
        or data_dir.is_symlink()
        or database_path.exists()
        or not run_id
        or not invocation_token
        or not isinstance(marker, dict)
        or marker.get("run_id") != run_id
        or marker.get("invocation_token") != invocation_token
        or marker.get("scope") != "CP150 data directory"
        or not isinstance(output_marker, dict)
        or output_marker.get("run_id") != run_id
        or output_marker.get("invocation_token") != invocation_token
        or output_marker.get("scope") != "CP150 output directory"
        or {entry.name for entry in data_dir.iterdir()} != {".cp150-run.json", ".cp150-output-run.json"}
    ):
        raise SeedError("CP150 refuses anything except its fresh, invocation-owned data directory")
    if candidate_blob_dir != expected_blob_dir or candidate_blob_dir.exists():
        raise SeedError("CP150 requires a fresh blobs directory beside its isolated e2e.db")
    return database_path, candidate_blob_dir


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
        response = await self._client.request(method, path.lstrip("/"), json=json_body, headers=headers)
        if response.status_code not in expect:
            raise SeedError(f"{method} {path} returned HTTP {response.status_code}")
        return response.json() if response.content else {}


async def seed(database_url: str, blob_dir: pathlib.Path) -> dict[str, Any]:
    if os.environ.get("DRILLAI_ENVIRONMENT", "").strip().lower() == "production":
        raise SeedError("Refusing to create synthetic CP150 data in a production environment")
    _database_path, resolved_blob_dir = _isolated_paths(database_url, blob_dir)

    os.environ.update(
        {
            "DRILLAI_ENVIRONMENT": "development",
            "DRILLAI_AUTH_ENABLED": "false",
            "DRILLAI_DATABASE_URL": database_url,
            "DRILLAI_BLOB_BACKEND": "filesystem",
            "DRILLAI_BLOB_ROOT": str(resolved_blob_dir),
            "DRILLAI_SCHEDULER_ENABLED": "false",
            "DRILLAI_NOTIFICATION_WORKER_ENABLED": "false",
            "DRILLAI_CONNECTOR_WORKER_ENABLED": "false",
            "DRILLAI_DEV_ORG_SLUG": ORG_SLUG,
            "DRILLAI_LOG_JSON": "false",
        }
    )

    from drillai.core.config import get_settings, reset_settings_cache

    reset_settings_cache()
    application = None
    try:
        from drillai.api.app import _warm_registries, create_app
        from drillai.db.models import Base

        application = create_app(get_settings())
        async with application.state.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        application.state.catalogues = _warm_registries()

        import httpx

        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://cp150-seed.local/api/v1/",
            headers={"X-Dev-Roles": "drilling_supervisor,admin,engineer"},
            timeout=30.0,
        ) as client:
            api = Api(client)
            projects = await api.call("GET", "/projects")
            if any(row.get("name") == PROJECT_NAME for row in projects.get("items", [])):
                raise SeedError("the CP150 project already exists; refusing to reuse data")
            project = await api.call(
                "POST",
                "/projects",
                json_body={
                    "name": PROJECT_NAME,
                    "code": "CP150-2W-SYN",
                    "operator": "CP150 synthetic test fixture",
                    "phase": "integration-test",
                    "description": (
                        "Isolated CP150 browser/API/DB fixture. Names and later report values are synthetic; "
                        "this is not field data, a drilling plan, or engineering acceptance."
                    ),
                },
                headers={"Idempotency-Key": "cp150-two-well-pilot-v1-project"},
            )

            seeded_wells: list[dict[str, str]] = []
            for index, definition in enumerate(WELLS, start=1):
                wells = await api.call("GET", f"/wells?q={definition['name']}&limit=500")
                if any(row.get("name") == definition["name"] for row in wells.get("items", [])):
                    raise SeedError(f"{definition['name']} already exists; refusing to reuse data")
                well = await api.call(
                    "POST",
                    "/wells",
                    json_body={
                        "project_id": project["id"],
                        "name": definition["name"],
                        "well_type": "development_producer",
                        "operator": "CP150 synthetic test fixture",
                        "elevation_datum": "msl",
                        "objectives": (
                            f"Synthetic CP150 UI-scope test well {index}; not a field drilling plan or site instruction."
                        ),
                    },
                    headers={"Idempotency-Key": f"cp150-two-well-pilot-v1-well-{index}"},
                )
                wellbores = await api.call("GET", f"/wells/{well['id']}/wellbores")
                if any(row.get("name") == definition["wellbore_name"] for row in wellbores.get("items", [])):
                    raise SeedError(f"{definition['wellbore_name']} already exists; refusing to reuse data")
                wellbore = await api.call(
                    "POST",
                    f"/wells/{well['id']}/wellbores",
                    json_body={
                        "name": definition["wellbore_name"],
                        "purpose": "original",
                        "sequence": 1,
                        # No planned/actual depth values are invented for this UI-scope fixture.
                        "datum": "rkb",
                    },
                    headers={"Idempotency-Key": f"cp150-two-well-pilot-v1-wellbore-{index}"},
                )
                seeded_wells.append(
                    {
                        "well_id": well["id"],
                        "well_name": definition["name"],
                        "wellbore_id": wellbore["id"],
                        "wellbore_name": definition["wellbore_name"],
                    }
                )

            if (
                len(seeded_wells) != 2
                or len({row["well_id"] for row in seeded_wells}) != 2
                or len({row["wellbore_id"] for row in seeded_wells}) != 2
            ):
                raise SeedError("the CP150 seed must produce exactly two distinct well/wellbore scopes")
            return {
                "database_url": database_url,
                "classification": "synthetic",
                "fixture_id": "cp150-two-well-pilot-v1",
                "org_slug": ORG_SLUG,
                "project_id": project["id"],
                "project_name": PROJECT_NAME,
                "wells": seeded_wells,
                "reset_scope": (
                    "No reset or reuse; only one synthetic project, two wells, and their wellbores were "
                    "created through local product API routes in a fresh isolated SQLite database."
                ),
            }
    finally:
        if application is not None:
            await application.state.database.dispose()
        reset_settings_cache()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True, help="Fresh isolated CP150 SQLite database URL")
    parser.add_argument("--blob-dir", required=True, help="Fresh blobs directory beside the isolated e2e.db")
    args = parser.parse_args(argv)
    result = asyncio.run(seed(args.database_url, pathlib.Path(args.blob_dir)))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
