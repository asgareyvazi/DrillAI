# Master mission — engine registry namespace assurance

**Status: PARTIAL** — this is one source-level assurance improvement, not completion of the 14-engine audit or independent/field validation.

## Defect and correction

`EngineRegistry.register()` previously overwrote aliases and allowed an alias to shadow a registered canonical engine key, or a subsequent canonical key to be shadowed by an earlier alias. `get()` resolves aliases first, so a collision could silently route a requested engineering calculation to the wrong engine. Registration now validates canonical keys and all aliases against the existing and proposed namespace before making any changes. Duplicate aliases and self-aliases are also rejected. Iterable aliases are materialized before mutation; a failed registration leaves the registry unchanged. Existing shipped aliases are retained.

## Evidence and limits

`backend/tests/engines/test_registry_contracts.py::test_registration_rejects_namespace_collisions_atomically` covers canonical/alias collisions in both directions, alias collision after an initially valid alias, repeated and self aliases, generator input, rollback and successful lookups. Focused local Linux selection across registry contracts, safety engines and CP142 independent calculation assurance: **45 passed**, Ruff passed. One test was added to the reviewed backend inventory; no expectations were removed. After the registry fix and inventory/README synchronization, the local full SQLite backend suite passed **1,781**, with 15 reviewed PostgreSQL-only skips; the default inventory verifier passed. Full 14-engine independent calculations, PostgreSQL-enabled execution on this revision, native Windows, release CI and field acceptance remain separately required. `well_control.kill_sheet` and `tubulars.api_5c3` remain `needs_field_validation`.
