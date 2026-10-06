"""Fixtures for the telemetry suites: the same two-tenant fabric every other suite uses.

The telemetry layer is the first place in the platform where two organizations' *measurements* live
side by side, so the two-tenant fixture is not ceremony here — it is the environment every identity and
isolation claim is made in.
"""

from __future__ import annotations

from tests.fixtures.fabric import DDR_BYTES, Fabric, Tenant, fabric

__all__ = ["DDR_BYTES", "Fabric", "Tenant", "fabric"]
