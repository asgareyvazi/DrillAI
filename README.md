# DrillAI — Well Engineering Intelligence Platform

Extensible platform foundation for well engineering (drilling first, full well lifecycle in scope:
planning → design → drilling → completion → intervention → integrity → P&A → lessons learned).

The platform core is **Engineering Context + Digital Well Twin + Data Fabric + Engineering Engines +
Workflow Runtime**. The LLM is one replaceable component behind an adapter: it never performs
engineering arithmetic, and it never invents engineering values.

## Repository layout

| Path | Contents |
| --- | --- |
| `backend/` | Python 3.11 service: domain models, engines, data fabric, workflow runtime, AI/LLM abstraction, FastAPI |
| `scripts/` | Operator/repository tooling (`smoke_e2e.py` end-to-end smoke check) |
| `frontend/` | Reserved for the React/TypeScript client — **not implemented yet** (see Status) |
| `docs/` | Reserved for architecture documents and ADRs — **not written yet** (see Status) |
| `ops/` | Reserved for deployment assets (Compose profiles, images) — **not written yet** |

## Quickstart (backend)

```bash
cd backend
python3.11 -m venv .venv
./.venv/bin/pip install -e ".[dev]"

# configuration is environment-driven (prefix DRILLAI_), see src/drillai/core/config.py
DRILLAI_DATABASE_URL="sqlite+aiosqlite:///./drillai.db" ./.venv/bin/python -m alembic upgrade head
DRILLAI_DATABASE_URL="sqlite+aiosqlite:///./drillai.db" ./.venv/bin/uvicorn --factory \
    "drillai.api.app:create_app" --host 0.0.0.0 --port 8080
```

PostgreSQL is the intended production database (`postgresql+asyncpg://…`); SQLite is supported for
local development and for the test suite.

## Verification

```bash
cd backend
./.venv/bin/python -m pytest                 # full suite
./.venv/bin/python -m ruff check .           # lint (also runnable from the repository root)
./.venv/bin/python -m alembic check          # "No new upgrade operations detected" == zero model drift

cd ..
backend/.venv/bin/python scripts/smoke_e2e.py   # end-to-end smoke, no network access required
```

`scripts/smoke_e2e.py` walks the real pipeline on in-memory SQLite: context → ingestion → evidence →
digital twin → RAG → workflow runtime → engineering engine → LLM router → recommendation, including
the L4 approval (suspend/resume) path.

## Status

Implemented and covered by tests (backend):

- 90-table domain/data model with a zero-drift Alembic baseline migration.
- Unit/dimension registry (121 units, 33 dimensions) with explicit, testable conversions.
- 14 engineering engines (trajectory minimum-curvature, wellbore capacity, hydraulics, torque & drag,
  MSE, kill sheet, API 5C3 tubulars, barrier envelope, well schematic, offset similarity, readiness,
  twin reconciliation, optimisation sweep, Pareto) plus an engine registry and execution service.
- Digital Well Twin with planned/actual/current/historical/predicted/recommended state kinds,
  revisions, snapshots and change history.
- Data fabric: PDF/Excel/Word/CSV/DDR ingestion with extraction records, page/region provenance,
  confidence and validation status; evidence links; local and in-memory blob stores.
- Context builder (single source of truth for agents, skills, workflows, engines, prompts) with
  purpose-scoped bundles, plus retrieval (lexical/hybrid/metadata) with scope-aware filters.
- Workflow runtime: node registry across 9 families, expressions, run events, pauses, per-node
  human approvals, resume, and recommendation/artifact persistence.
- Security: 33 catalogued actions with L0–L5 action levels, RBAC roles, separation of duties,
  fail-closed authorisation for unregistered actions, token authentication and secret references.
- AI layer: provider abstraction, model registry, capability/cost/privacy routing, LLM call audit,
  tool registry and agent/skill registries (agents capped at L2).
- FastAPI service (assets/context/documents/evidence/twin/workflows/runs/registry/platform) with
  request IDs, structured errors, CORS, i18n/RTL locale plumbing and observability hooks.

Not implemented yet (do not assume otherwise):

- **No frontend.** The React/TypeScript workflow editor and the §41 UI acceptance walkthrough are
  not built; the API is exercised by the API test suite and the smoke script instead.
- **No architecture documents or ADRs** in `docs/` yet, and no `ops/` deployment assets.
- The WebSocket run-event stream endpoint exists but is **verified manually only** — the automated
  stream test was removed because it hung the suite rather than test the stream.
- Integration adapters for messaging (Telegram/WhatsApp/email), WITSML/ETP and vector databases are
  configuration-shaped boundaries; outbound delivery, live WITSML/ETP streaming and pgvector-backed
  retrieval are not exercised by the test suite.
