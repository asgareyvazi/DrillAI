"""Synthetic Daily Drilling Report fixtures — **explicitly labelled synthetic**.

These documents are *not* field data. They exist so that the DDR → structured engineering data
pipeline, the well cockpit, the NPT intelligence and the end-to-end scenario can be exercised
without pretending fabricated numbers came from a rig.

Every fixture declares ``SYNTHETIC`` in its header text, and the ingesting test asserts that the
label survives into the stored document metadata. Nothing in this module is imported by
production code: ``drillai.drilling`` never depends on it.
"""

from __future__ import annotations

#: A DDR-shaped CSV: header rows, an operations/activities table, a drilling-parameters block and
#: two problems (one of which is NPT with a duration). Column names are the ones the extractors
#: recognise, so this exercises the real extraction path rather than a shortcut.
SYNTHETIC_DDR_CSV = b"""Daily Drilling Report - SYNTHETIC TEST DATA - Well NF-12
Report No,RPT-NF12-014
Date,2026-03-15
Well,NF-12
Rig,RIG-SYNTH-7
Section,8-1/2 in
Operations,Activity,Code,Duration (h),Depth (m),Description
1,Drilling ahead 8-1/2 in section,DRL,8.5,2410,Drilling ahead from 2350 m to 2410 m at 120 rpm
2,Made connection,CONN,0.5,2410,Connection at 2410 m
3,Stuck pipe while POOH,STUCK,6.0,2408,Stuck pipe at 2408 m; jarring attempted; worked free after 6 hours
4,Lost circulation,LC,2.5,2412,Lost circulation 15 bbl/hr; LCM pill pumped
5,Circulating and conditioning mud,CIRC,1.0,2412,Bottoms up and mud conditioning
Drilling Parameters,Value
WOB (klbf),18.5
RPM (rpm),120
Flow Rate (l/min),2100
SPP (psi),2450
Torque (ft-lbf),8200
Hookload (klbf),245
ROP (m/h),9.4
Mud Weight (ppg),12.4
Depth (m),2412
"""

#: A narrative DDR in plain text: no table, so the extractors fall back to label/value parsing.
#: Used to prove the pipeline degrades honestly instead of inventing rows.
SYNTHETIC_DDR_TEXT = b"""Daily Drilling Report - SYNTHETIC TEST DATA - Well NF-12
Report No: RPT-NF12-015
Date: 2026-03-16
Well: NF-12
Rig: RIG-SYNTH-7

Operations
Drilling ahead 8-1/2 in section from 2412 m to 2460 m at 120 rpm. Made 4 connections.
Torque fluctuated between 7800 and 9600 ft-lbf. Circulated bottoms up at 2460 m.

Drilling Parameters
WOB: 19.0 klbf
RPM: 125 rpm
Flow Rate: 2150 l/min
SPP: 2510 psi
Mud Weight: 12.5 ppg
Depth: 2460 m

Problems
Lost circulation 12 bbl/hr at 2455 m; LCM sweep pumped; losses reduced to seepage.
"""

#: Documents that are not DDRs, used to assert the processor refuses to promote them.
SYNTHETIC_PROGRAM_TEXT = b"""Well Drilling Program - SYNTHETIC TEST DATA - Well NF-12
Section: 8-1/2 in
Planned mud weight window: 12.2 - 12.8 ppg
Planned TD: 2860 m MD
Procedure: drill to section TD, run 7 in liner.
"""

SYNTHETIC_FIXTURES: dict[str, bytes] = {
    "ddr_csv": SYNTHETIC_DDR_CSV,
    "ddr_text": SYNTHETIC_DDR_TEXT,
    "program": SYNTHETIC_PROGRAM_TEXT,
}

#: Every fixture must carry this marker so synthetic data can never be mistaken for field data.
SYNTHETIC_MARKER = "SYNTHETIC TEST DATA"


def fixture(name: str) -> bytes:
    """Return a named synthetic fixture, failing loudly on a typo."""
    try:
        return SYNTHETIC_FIXTURES[name]
    except KeyError as exc:  # pragma: no cover - programming error
        raise KeyError(f"unknown synthetic fixture {name!r}; known: {sorted(SYNTHETIC_FIXTURES)}") from exc
