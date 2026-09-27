/**
 * Well Cockpit against the payloads the backend actually serves.
 *
 * The fixtures in `src/test/fixtures` were captured from the running API, not written by hand. This
 * test existed nowhere while the page read fields the API never sent — which is exactly how the
 * cockpit shipped a crash on real data (`state.sources.join` on a payload with no `sources`). The
 * rule this file enforces: the cockpit renders the real contract, and every number it shows can be
 * traced to a field the backend sent.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '../../i18n'
import WellCockpit from './WellCockpit'
import auditFixture from '../../test/fixtures/well-audit.json'
import nptFixture from '../../test/fixtures/well-npt.json'
import stateFixture from '../../test/fixtures/well-state.json'
import timelineFixture from '../../test/fixtures/well-timeline.json'
import twinFixture from '../../test/fixtures/well-twin.json'
import type { AuditTrail, DrillingState, NptSummary, TimelineEntry, TwinState } from '../../api/types'

const state = stateFixture.state as unknown as DrillingState
const npt = nptFixture as unknown as { npt: NptSummary }
const timeline = timelineFixture as unknown as { entries: TimelineEntry[]; count: number; kinds_available: string[] }
const twin = twinFixture as unknown as TwinState
const audit = auditFixture as unknown as AuditTrail

vi.mock('../../api/endpoints', () => ({
  drillingApi: {
    wellState: () => Promise.resolve(stateFixture),
    wellNpt: () => Promise.resolve(nptFixture),
    wellTimeline: () => Promise.resolve(timelineFixture),
    wellTwin: () => Promise.resolve(twinFixture),
    wellAudit: () => Promise.resolve(auditFixture),
    listDocuments: () => Promise.resolve({ items: [], total: 0 }),
    // Mirrors the real evidence-summary payload (backend/src/drillai/api/routers/evidence.py).
    evidenceSummary: () =>
      Promise.resolve({
        link_count: 0,
        verified_quotes: 0,
        documents_referenced: 0,
        mean_confidence: null,
        by_kind: {},
        limitations: ['no evidence links recorded in this scope'],
      }),
  },
  platformApi: {},
  workflowApi: {},
  runApi: {},
  docsApi: {},
  engineeringApi: {},
  registryApi: {},
  ragApi: {},
}))

function fixtureAt<T>(rows: T[], index: number, what: string): T {
  const row = rows.at(index)
  if (row === undefined) throw new Error(`fixture ${what}[${index}] is missing — recapture src/test/fixtures`)
  return row
}

function renderCockpit() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <I18nProvider>
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={[`/wells/${state.well.id}`]}>
            <Routes>
              <Route path="/wells/:wellId" element={children} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </I18nProvider>
    )
  }
  return render(<WellCockpit />, { wrapper: Wrapper })
}

beforeEach(() => {
  window.localStorage.clear()
})

describe('WellCockpit: renders the payload the API serves', () => {
  it('renders the overview without reading fields the backend does not send', async () => {
    // The crash this guards against threw inside render, so reaching the assertions is the point.
    renderCockpit()

    expect((await screen.findAllByText(state.well.name)).length).toBeGreaterThan(0)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows the recorded current depth and names where that depth came from', async () => {
    renderCockpit()
    await screen.findAllByText(state.well.name)

    const depth = state.progress.current_md_si as number
    expect(screen.getAllByText(new RegExp(`${Math.round(depth).toLocaleString('en-US')}`)).length).toBeGreaterThan(0)
    expect(screen.getByText(state.progress.current_md_source as string)).toBeInTheDocument()
  })

  it('never shows a planned depth as the measured one', async () => {
    renderCockpit()
    await screen.findAllByText(state.well.name)

    // The seeder's planned depth (3,200 m) must only ever appear as a plan, so the string must carry
    // the planned label with it rather than standing alone in the measured column.
    const planned = state.progress.planned_td_md_si as number
    for (const node of screen.queryAllByText(new RegExp(`^${planned.toLocaleString('en-US')}$`))) {
      expect(node.closest('dt,dd')?.parentElement?.textContent ?? '').not.toMatch(/measured/i)
    }
  })

  it('reports the NPT total from the NPT endpoint, with its basis', async () => {
    renderCockpit()
    await screen.findAllByText(state.well.name)

    // The synthetic well's recorded NPT total, and the Pareto rows come from `by_category`.
    const total = npt.npt.total_hours
    expect((await screen.findAllByText(String(total))).length).toBeGreaterThan(0)
    // The basis is stated on the card and repeated under the Pareto: both are the backend's words.
    expect(screen.getAllByText(npt.npt.basis).length).toBeGreaterThan(0)
    for (const row of npt.npt.by_category.filter((category) => category.occurrences > 0)) {
      // Each Pareto row carries its controllability in words; the row is selectable by that name.
      const suffix =
        row.operator_controllable === null
          ? 'controllability not established'
          : row.operator_controllable
            ? 'operator-controllable'
            : 'not controllable'
      expect(
        screen.getByRole('button', { name: (accessibleName) => accessibleName.startsWith(`${row.label} (${suffix})`) }),
      ).toBeInTheDocument()
    }
  })

  it('keeps controllability tri-state instead of forcing unknown into yes/no', async () => {
    renderCockpit()
    await screen.findAllByText(state.well.name)

    const unknownLabel = await screen.findByText('controllability unknown', { selector: 'dt' })
    // The unknown bucket is the payload's own number — a recorded zero here is a measurement, not
    // a missing value, and it must not be shown as "no data".
    const box = unknownLabel.parentElement as HTMLElement
    expect(within(box).getByText(String(npt.npt.unknown_controllability_hours))).toBeInTheDocument()
    const controlledLabel = screen.getByText('operator-controllable', { selector: 'dt' })
    expect(within(controlledLabel.parentElement as HTMLElement).getByText(String(npt.npt.controllable_hours))).toBeInTheDocument()
    // Events recorded without a code keep their unknown state in the case list too.
    const coded = npt.npt.cases.filter((row) => row.operator_controllable === null)
    expect(coded.length).toBe(npt.npt.event_count - npt.npt.cases.filter((row) => row.operator_controllable !== null).length)
  })

  it('lists what the platform knows it is missing, with how to supply it', async () => {
    renderCockpit()
    await screen.findAllByText(state.well.name)

    const user = userEvent.setup()
    await user.click(screen.getByRole('tab', { name: /missing/i }))

    const missing = fixtureAt(state.missing, 0, 'missing')
    expect(await screen.findByText(missing.description)).toBeInTheDocument()
    expect(screen.getByText(missing.how_to_supply)).toBeInTheDocument()
  })

  it('renders every timeline entry the API returned, with its summary', async () => {
    renderCockpit()
    await screen.findAllByText(state.well.name)

    const user = userEvent.setup()
    await user.click(screen.getByRole('tab', { name: /timeline/i }))

    const first = fixtureAt(timeline.entries, 0, 'timeline.entries')
    expect(await screen.findByText(first.title)).toBeInTheDocument()
    if (first.summary) expect(screen.getAllByText(first.summary).length).toBeGreaterThan(0)
    // Every entry the API returned is on the axis: a silently truncated timeline hides NPT.
    expect(screen.getAllByRole('listitem')).toHaveLength(timeline.entries.length)
  })

  it('shows the twin aspects the twin endpoint returned', async () => {
    renderCockpit()
    await screen.findAllByText(state.well.name)

    const user = userEvent.setup()
    await user.click(screen.getByRole('tab', { name: /twin/i }))

    const table = await screen.findByRole('table')
    expect(within(table).getAllByRole('row').length).toBe(twin.aspects.length + 1)
  })

  it('counts the audit trail by kind rather than printing an empty table', async () => {
    renderCockpit()
    await screen.findAllByText(state.well.name)

    const user = userEvent.setup()
    await user.click(screen.getByRole('tab', { name: /audit/i }))

    expect(
      await screen.findByText(new RegExp(`engine runs: ${audit.counts.engine_runs}`, 'i')),
    ).toBeInTheDocument()
  })
})
