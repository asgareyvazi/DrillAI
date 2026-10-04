"""Harness for the document/evidence domain tests.

These tests need something ``tests/api/conftest.py`` deliberately does not provide: **two
organizations at once, with bearer tokens**. The shared harness runs with authentication disabled and
acts as one development organization, which is the right default — most API tests are about one
tenant's contract. Scope safety is not that kind of test. "This tenant cannot reach that tenant's
data" cannot be expressed with one identity, and the cross-organization binding reproduced before
CP8 (an upload naming another organization's wellbore) would be invisible to a single-tenant fixture.

So the app is built here from the same ``create_app`` factory, with ``DRILLAI_AUTH_ENABLED=true`` and
two real ``ApiToken`` rows. The hierarchy each tenant owns is created **through the API**, not
inserted, so the fixture cannot accidentally construct a shape the API would refuse to.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest_asyncio


@dataclass
class Tenant:
    """One organization, its credential, and the asset hierarchy it created through the API."""

    slug: str
    token: str
    project_id: str = ""
    well_id: str = ""
    wellbore_id: str = ""
    section_id: str = ""
    extra_well_ids: list[str] = field(default_factory=list)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@dataclass
class Fabric:
    """The client plus the tenants, so a test can say ``fabric.alpha.well_id``.

    ``app`` is carried as well because some tests need to arrange a row the API does not yet serve
    (an operation, before the operations endpoints exist) — the application's own session factory is
    the only honest way to write it, since a hand-written INSERT could omit a NOT NULL column that the
    model would have filled.
    """

    http: httpx.AsyncClient
    alpha: Tenant
    bravo: Tenant
    app: object

    def session(self):
        return self.app.state.database.session_factory()

    def org_id(self, tenant: Tenant) -> str:
        return f"org_{tenant.slug}"

    def tenant(self, slug: str) -> Tenant:
        return {"alpha": self.alpha, "bravo": self.bravo}[slug]

    async def create_tree(self, tenant: Tenant, *, name_prefix: str | None = None) -> None:
        """project → well → wellbore → section for one tenant, entirely through the API."""
        prefix = (name_prefix or tenant.slug).upper()
        project = await self.http.post(
            "/api/v1/projects",
            json={"name": f"{tenant.slug} project"},
            headers=tenant.headers,
        )
        assert project.status_code == 201, project.text
        tenant.project_id = project.json()["id"]

        well = await self.http.post(
            "/api/v1/wells",
            json={
                "project_id": tenant.project_id,
                "name": f"{prefix}-1",
                "well_type": "development_producer",
            },
            headers=tenant.headers,
        )
        assert well.status_code == 201, well.text
        tenant.well_id = well.json()["id"]

        wellbore = await self.http.post(
            f"/api/v1/wells/{tenant.well_id}/wellbores",
            json={"name": "Main bore", "purpose": "original", "planned_td_md_si": 3000.0},
            headers=tenant.headers,
        )
        assert wellbore.status_code == 201, wellbore.text
        tenant.wellbore_id = wellbore.json()["id"]

        section = await self.http.post(
            f"/api/v1/wellbores/{tenant.wellbore_id}/sections",
            json={"sequence": 1, "name": '12-1/4" section', "kind": "surface"},
            headers=tenant.headers,
        )
        assert section.status_code == 201, section.text
        tenant.section_id = section.json()["id"]

    async def add_well(self, tenant: Tenant, name: str) -> str:
        response = await self.http.post(
            "/api/v1/wells",
            json={
                "project_id": tenant.project_id,
                "name": name,
                "well_type": "development_producer",
            },
            headers=tenant.headers,
        )
        assert response.status_code == 201, response.text
        tenant.extra_well_ids.append(response.json()["id"])
        return response.json()["id"]

    async def add_wellbore(self, tenant: Tenant, well_id: str, name: str) -> str:
        response = await self.http.post(
            f"/api/v1/wells/{well_id}/wellbores",
            json={"name": name, "purpose": "original", "planned_td_md_si": 3200.0},
            headers=tenant.headers,
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    async def add_section(self, tenant: Tenant, wellbore_id: str, name: str) -> str:
        response = await self.http.post(
            f"/api/v1/wellbores/{wellbore_id}/sections",
            json={"sequence": 1, "name": name, "kind": "intermediate"},
            headers=tenant.headers,
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    async def upload(
        self,
        tenant: Tenant,
        payload: bytes,
        *,
        filename: str = "report.txt",
        content_type: str = "text/plain",
        **fields: str,
    ) -> httpx.Response:
        return await self.http.post(
            "/api/v1/documents",
            files={"file": (filename, io.BytesIO(payload), content_type)},
            data={key: value for key, value in fields.items() if value is not None},
            headers=tenant.headers,
        )


#: A two-operation daily report that the text extractors can genuinely read: a header, an operation
#: table and a labelled parameter line. Written as bytes rather than generated so that what the
#: extractors are expected to find is visible in the test that asserts it.
DDR_BYTES = (
    b"DAILY DRILLING REPORT\n"
    b"Well: ALPHA-1\n"
    b"Report date: 2026-03-15\n"
    b"\n"
    b"Operation | Dur (h) | MD (m) | Comment\n"
    b"1 | Drilling ahead | 8.5 | 2410 | drilled from 2350 m to 2410 m\n"
    b"2 | Made connection | 0.5 | 2410 | connection at 2410 m\n"
    b"\n"
    b"Mud weight: 9.2 ppg\n"
)


@pytest_asyncio.fixture
async def fabric(tmp_path, monkeypatch) -> AsyncIterator[Fabric]:
    from drillai.api.app import _warm_registries, create_app
    from drillai.core.config import get_settings, reset_settings_cache
    from drillai.db.models import (
        ApiToken,
        Base,
        Membership,
        Organization,
        Project,
        User,
    )
    from drillai.security.passwords import hash_password, new_api_token

    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "true")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/fabric.db")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    reset_settings_cache()
    application = create_app(get_settings())
    async with application.state.database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    application.state.catalogues = _warm_registries()

    tokens: dict[str, str] = {}
    async with application.state.database.session_factory() as session:
        for slug in ("alpha", "bravo"):
            organization = Organization(id=f"org_{slug}", slug=slug, name=slug.title())
            user = User(
                id=f"usr_{slug}",
                email=f"{slug}@example.com",
                display_name=slug.title(),
                password_hash=hash_password("correct-horse-battery-staple"),
            )
            session.add_all([organization, user])
            await session.flush()
            session.add(
                Membership(
                    id=f"mem_{slug}",
                    user_id=user.id,
                    org_id=organization.id,
                    role_key="well_manager",
                )
            )
            token, prefix, digest = new_api_token()
            session.add(
                ApiToken(
                    id=f"tok_{slug}",
                    org_id=organization.id,
                    user_id=user.id,
                    name=f"{slug} token",
                    token_prefix=prefix,
                    token_hash=digest,
                )
            )
            session.add(
                Project(
                    id=f"prj_{slug}",
                    org_id=organization.id,
                    name=f"{slug} project",
                    status="active",
                    datum_policy="rkb",
                )
            )
            tokens[slug] = token
        await session.commit()

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        alpha = Tenant(slug="alpha", token=tokens["alpha"])
        bravo = Tenant(slug="bravo", token=tokens["bravo"])
        result = Fabric(http=http, alpha=alpha, bravo=bravo, app=application)
        for tenant in (alpha, bravo):
            await result.create_tree(tenant)
        yield result
    await application.state.database.dispose()
    reset_settings_cache()
