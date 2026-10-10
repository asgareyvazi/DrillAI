"""Drilling intelligence domain.

This package turns the platform foundation (data fabric, digital well twin, engineering
engines, workflow runtime) into the things a drilling team actually asks for:

* :mod:`drillai.drilling.state` — where are we, what is happening, how are we performing
* :mod:`drillai.drilling.npt` — non-productive time derived from *recorded events*, never invented
* :mod:`drillai.drilling.timeline` — one merged, evidence-linked well timeline
* :mod:`drillai.drilling.ddr` — Daily Drilling Report → structured operations/events/twin state
* :mod:`drillai.drilling.advisor` — the Operations Advisor answer contract
* :mod:`drillai.drilling.reporting` — report data contracts (facts / calculations / evidence / …)
* :mod:`drillai.drilling.dependencies` — the engineering dependency graph behind "what went stale?"

Everything here is a *service over recorded data*. No module in this package computes an
engineering quantity: those come from :mod:`drillai.engines`, which are deterministic and
versioned. When data is missing the services say so explicitly instead of estimating.
"""

from __future__ import annotations

__all__ = [
    "advisor",
    "ddr",
    "dependencies",
    "npt",
    "reporting",
    "state",
    "timeline",
]
