/**
 * Journey 2 — the operational record: what happened on the well, and what each claim rests on.
 *
 * The spec walks the path an engineer does after a daily report has been promoted: the cockpit, the
 * operations workspace, an operation's drawer with the document it came from, the events with their
 * NPT charges, and the merged timeline. Every number and identifier is read from the API inside the
 * test, so the assertions fail when the product stops reporting the real value — and they can never
 * pass because a value was typed into the spec.
 *
 * The three claims this journey refuses to let collapse:
 *
 * * a plan is not an actual (`operation_class`, and the hours belong to the matching column);
 * * an event's kind is not its NPT category (`is_npt`, `npt_category`, `npt_hours`);
 * * a cause's basis travels with the cause (`cause_basis`).
 */

import { apiGet, expect, test, wellPath } from './fixtures'

type OperationPage = {
  items: {
    id: string
    name: string
    sequence: number
    kind: string
    status: string
    operation_class: string
    is_planned: boolean
    actual_duration_hours: number | null
    planned_duration_hours: number | null
    data_quality: string | null
    source_kind: string | null
    source_document_id: string | null
    allowed_transitions: string[]
  }[]
  total: number
}

type EventPage = {
  items: {
    id: string
    title: string
    kind: string
    status: string
    is_npt: boolean
    npt_category: string | null
    npt_hours: number | null
    cause_basis: string
    allowed_transitions: string[]
  }[]
  total: number
}

type TimelinePage = {
  entries: { kind: string; id: string; document_id: string | null }[]
  count: number
  kinds_available: string[]
  next_cursor: string | null
}

test.describe('the operational workspace built from the real records', () => {
  test('opens from the cockpit and lists the operations the API serves', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const operations = await apiGet<OperationPage>(
      request,
      `/operations?well_id=${wellId}&limit=20`,
    )
    expect(operations.total, 'the seeded daily report must have been promoted').toBeGreaterThan(0)

    await page.goto(wellPath('/operations'))
    await expect(page.getByRole('heading', { level: 1 })).toContainText('Operations Workspace')

    // Every operation the API served is on the page, by name.
    for (const row of operations.items) {
      await expect(page.getByText(row.name, { exact: true })).toBeVisible()
    }
    await expect(page.getByText(`${operations.total} records matching the current filter`)).toBeVisible()

    // And the class column distinguishes a plan from an actual, as the API does.
    for (const row of operations.items) {
      const line = page.getByRole('row').filter({ hasText: row.name })
      await expect(line.getByText(row.is_planned ? 'Plan' : 'Actual')).toBeVisible()
    }
    expect(consoleErrors).toEqual([])
  })

  test('shows an operation’s actual hours in the actual column and the document behind it', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const operations = await apiGet<OperationPage>(
      request,
      `/operations?well_id=${wellId}&limit=20`,
    )
    const recorded = operations.items.find(
      (row) => !row.is_planned && row.actual_duration_hours !== null,
    )
    expect(recorded, 'the promotion must produce at least one actual operation').toBeTruthy()
    const source = recorded?.source_document_id as string
    expect(source, 'a promoted operation must name its source document').toBeTruthy()

    // The drawer is a deep link: opening the URL with the record selected renders it directly.
    await page.goto(wellPath(`/operations?tab=operations&operation=${recorded?.id}`))
    const drawer = page.getByRole('dialog')
    await expect(drawer).toBeVisible()
    await expect(drawer.getByText(recorded?.name as string)).toBeVisible()
    // The definition list pairs each label with its value; a bare number would match half the drawer.
    const detail = (label: string) =>
      drawer.getByText(label, { exact: true }).locator('xpath=following-sibling::dd')
    await expect(detail('Sequence')).toHaveText(String(recorded?.sequence))
    await expect(detail('Actual hours')).toHaveText(String(recorded?.actual_duration_hours))

    // Provenance: the drawer links back to the document the record was promoted from, and the link
    // really opens that document's workspace rather than a page that happens to mention it.
    const link = drawer.getByRole('link', { name: source })
    await expect(link).toBeVisible()
    await link.click()
    await expect(page).toHaveURL(new RegExp(`/wells/${wellId}/documents\\?document=${source}`))
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
    expect(consoleErrors).toEqual([])
  })

  test('offers the transitions the server allows and says so when a record is terminal', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const operations = await apiGet<OperationPage>(
      request,
      `/operations?well_id=${wellId}&limit=20`,
    )
    const terminal = operations.items.find((row) => row.allowed_transitions.length === 0)
    expect(terminal, 'the seeded promotion completes its operations').toBeTruthy()

    await page.goto(wellPath(`/operations?tab=operations&operation=${terminal?.id}`))
    const drawer = page.getByRole('dialog')
    await expect(
      drawer.getByText('This record has reached a terminal state; no further transition is offered.'),
    ).toBeVisible()
    // Nothing is offered for it — not a disabled row of buttons, nothing.
    await expect(drawer.getByRole('button', { name: /^Move to / })).toHaveCount(0)

    // A record the server *does* offer transitions for renders exactly those buttons.
    const open = operations.items.find((row) => row.allowed_transitions.length > 0)
    if (open) {
      await page.goto(wellPath(`/operations?tab=operations&operation=${open.id}`))
      for (const status of open.allowed_transitions) {
        const label = status.charAt(0).toUpperCase() + status.slice(1)
        await expect(
          page.getByRole('dialog').getByRole('button', { name: `Move to ${label}` }),
        ).toBeVisible()
      }
    }
    expect(consoleErrors).toEqual([])
  })

  test('charges NPT from the event’s own hours and keeps the cause’s basis with the cause', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const events = await apiGet<EventPage>(request, `/events?well_id=${wellId}&limit=20`)
    expect(events.total).toBeGreaterThan(0)

    await page.goto(wellPath('/operations?tab=events'))
    for (const row of events.items) {
      const line = page.getByRole('row').filter({ hasText: row.title })
      await expect(line).toBeVisible()
      if (row.is_npt) {
        // The charge the API booked, in the row — the category's label and the hours.
        const category = (row.npt_category ?? '').replace(/_/g, ' ')
        const label = category.charAt(0).toUpperCase() + category.slice(1)
        await expect(line.getByText(`${label} · ${row.npt_hours} h`)).toBeVisible()
      } else {
        await expect(line.getByText('not charged')).toBeVisible()
      }
      // The basis of the cause is on the row whether or not a cause exists.
      const basis = { recorded: 'cause recorded', inferred: 'cause inferred', unknown: 'cause unknown' }[
        row.cause_basis as 'recorded' | 'inferred' | 'unknown'
      ]
      await expect(line.getByText(basis)).toBeVisible()
    }

    // The event drawer is a deep link too, and states where the event was recorded.
    const first = events.items[0]
    await page.goto(wellPath(`/operations?tab=events&event=${first.id}`))
    const drawer = page.getByRole('dialog')
    await expect(drawer.getByText(first.title)).toBeVisible()
    expect(consoleErrors).toEqual([])
  })

  test('merges the timeline and deep-links an entry to its document', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    // The workspace pages the timeline at 100 entries per read, so the assertion asks the API for the
    // same page the screen asked for rather than for a different one.
    const timeline = await apiGet<TimelinePage>(request, `/wells/${wellId}/timeline?limit=100`)
    expect(timeline.entries.length).toBeGreaterThan(0)

    await page.goto(wellPath('/operations?tab=timeline'))
    const list = page.getByTestId('timeline-entries')
    await expect(list.getByRole('listitem')).toHaveCount(timeline.entries.length)

    // Every kind the server says it can return has a filter; none that it does not.
    for (const kind of timeline.kinds_available) {
      await expect(page.getByTestId(`timeline-kind-${kind}`)).toBeVisible()
    }
    await expect(page.getByTestId('timeline-kind-kpi')).toHaveCount(0)

    if (timeline.next_cursor) {
      await expect(page.getByText('A full page came back: more entries follow.')).toBeVisible()
    } else {
      await expect(page.getByText('End of the timeline.')).toBeVisible()
    }

    const fromDocument = timeline.entries.find((entry) => entry.document_id !== null)
    expect(fromDocument, 'the seeded day has a document behind it').toBeTruthy()
    const source = fromDocument?.document_id as string
    // The link inside the entry's own item points at the source document, and following it lands on
    // that document's workspace by id — a deep link that names the record, not a generic page.
    const item = list.getByRole('listitem').filter({ hasText: fromDocument?.title as string }).first()
    const link = item.getByRole('link', { name: 'Source document' })
    await expect(link).toHaveAttribute('href', new RegExp(`document=${source}`))
    await link.click()
    await expect(page).toHaveURL(new RegExp(`/wells/${wellId}/documents\\?document=${source}`))
    expect(consoleErrors).toEqual([])
  })
})
