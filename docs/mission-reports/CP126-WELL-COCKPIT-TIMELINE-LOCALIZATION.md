# CP126 — Well Cockpit Timeline Localization

## Disposition

**CP126 software/localization scope: DONE.** English and Persian timeline context labels and known timeline-kind enum values now use the typed catalogues. Source record titles and summaries remain source text and are not rewritten.

| Item | Status | Evidence / boundary |
| --- | --- | --- |
| Localize timeline context, depth/NPT labels and known timeline-kind values | **DONE (software)** | English and Persian catalogue keys cover the timeline subtitle, kind label, depth qualifier, NPT labels, and operation/event/document/engine/recommendation/twin-change/approval kinds. |
| Preserve RTL Persian and isolate technical measurements | **DONE (software test evidence)** | The new Persian render test checks `dir="rtl"`, Persian labels, localized enum values, and `bdi`-isolated LTR numeric/unit strings. |
| Preserve source-produced entry titles and summaries | **DONE (provenance preserved)** | They are source record content, not UI labels. They remain unchanged and retain their provenance meaning. |

## Implementation

The Cockpit timeline previously hard-coded its summary, the “kinds” prefix, depth phrase and `NPT` labels in English, and displayed known kind values through an English-only humanizer. Those user-interface labels now come from the typed English/Persian catalogues. Known enum values are translated using explicit keys; an unknown kind still uses the existing neutral humanizer rather than being assigned an invented meaning. The list of available kinds uses bidi-isolated labels. Measurements and standardized unit tokens remain in an LTR-isolated span while the Persian page direction remains RTL; duration formatting receives the active locale.

No event timestamps, source values, title text, summaries, units, or engineering calculations were changed.

## Verification

- From `frontend/`: `npm test -- --run src/pages/wells/WellCockpit.test.tsx` — **10 passed**.
- Full frontend unit suite: `npm test -- --run` — **391 passed across 39 files**.
- `npm run typecheck` and `npm run lint` passed.
- `npm run build` passed; Vite emitted the existing warning about a minified chunk larger than 500 kB. No build failure occurred.
- No E2E browser run was made against existing ignored `.e2e*` data. Exact-final-SHA Linux/native-Windows browser certification remains a CP132 gate.

## Limits

This localizes the Well Cockpit timeline only; it does not claim that every Cockpit or Operations Workspace string is localized. The visible source entry title and summary may remain in the language of the source record. Persian text/RTL behavior is covered by component-level rendering, not a field-user language-acceptance study. The full backend suite and final SHA certification remain pending.
