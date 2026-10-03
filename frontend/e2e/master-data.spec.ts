/**
 * The asset chain, end to end, against the real product.
 *
 * This spec walks the journey the mission names: **Project → Field → Well → Wellbore → 12¼" section**,
 * then renames the well, moves it to another field, moves its life cycle, drills a sidetrack off the
 * original hole, activates it and adds a section to it — checking after every step that the record's
 * identity and its history are still intact.
 *
 * Nothing here is mocked or intercepted. The writes go through the real FastAPI service and the real
 * database the E2E stack seeded; the browser reads what that service actually returns. Two things
 * follow from that, and they are the reason the spec is written this way:
 *
 * * **The refusals are the backend's.** The negative journeys ask the API directly, with a role header,
 *   and assert the HTTP status and the error's taxonomy code. Disabling a button in the UI is not a
 *   security control, and a test that proved a disabled button would prove nothing about the server.
 * * **The provenance check is about identity, not about names.** The document attached to the well is
 *   re-read after the rename; it must still point at the same well id, and the well must still be the
 *   same row. A rename that changed an id would silently orphan every document, operation, engine run
 *   and twin aspect that references the well, and no screen would say a word.
 *
 * The data this spec creates is its own — a fresh project, field and well with a suffixed name — so it
 * never mutates the seeded fixtures the other journeys depend on.
 */

import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect } from '@playwright/test'
import { apiAttempt, apiGet, apiPatch, apiPost, seededIds, test } from './fixtures'

const here = path.dirname(fileURLToPath(import.meta.url))
const uploadPath = path.join(here, 'assets', 'synthetic_ddr_upload.txt')

interface WellRow {
  id: string
  name: string
  field_id: string | null
  status: string
  well_type: string
  updated_at: string
  allowed_transitions?: string[]
}

interface WellboreRow {
  id: string
  name: string
  sequence: number
  purpose: string
  parent_wellbore_id: string | null
  is_active: boolean
  status: string
  allowed_transitions?: string[]
}

interface SectionRow {
  id: string
  name: string
  sequence: number
  kind: string
  planned_bottom_md_si: number | null
  actual_bottom_md_si: number | null
  current_md_si: number | null
  is_planned_only: boolean
  semantics: Record<string, string>
}

interface Structure {
  well: WellRow & { active_wellbore_id: string | null }
  wellbores: Array<WellboreRow & { sections: SectionRow[]; counts: Record<string, number> }>
  counts: Record<string, number>
}

/** A suffix that is unique per run, so a rerun does not collide with the previous run's rows. */
function stamp(): string {
  return `${Date.now().toString(36)}`
}

test.describe('the asset chain', () => {
  test('creates, renames, moves, drills a sidetrack: identity and history survive', async ({ request, page }) => {
    const tag = stamp()

    // 1. Project → Field. The project is created as the well manager, which is the role that holds
    //    `project.write`; the rest of the chain only needs `well.*`, held by an engineer.
    const project = await apiPost<{ id: string; name: string }>(
      request,
      '/projects',
      { name: `E2E Asset Project ${tag}`, code: `E2E-${tag}`.toUpperCase(), phase: 'engineering' },
      'wellManager',
    )
    const field = await apiPost<{ id: string; name: string }>(
      request,
      '/fields',
      { project_id: project.id, name: `E2E Field ${tag}`, aliases: [`E2E Alias ${tag}`] },
      'engineer',
    )
    const secondField = await apiPost<{ id: string }>(
      request,
      '/fields',
      { project_id: project.id, name: `E2E Field Two ${tag}` },
      'engineer',
    )

    // 2. Well → Wellbore → the 12¼" section.
    const well = await apiPost<WellRow>(
      request,
      '/wells',
      {
        project_id: project.id,
        field_id: field.id,
        name: `E2E-${tag}-01`,
        uwi: `E2E-${tag}-UWI`,
        well_type: 'development_producer',
        operator: 'E2E Operator',
        total_depth_planned_si: 3200,
      },
      'engineer',
    )
    expect(well.id).toMatch(/^wel_/)

    const wellbore = await apiPost<WellboreRow>(
      request,
      `/wells/${well.id}/wellbores`,
      { name: 'Main bore', purpose: 'original', planned_td_md_si: 3200 },
      'engineer',
    )
    const section = await apiPost<SectionRow>(
      request,
      `/wellbores/${wellbore.id}/sections`,
      {
        sequence: 1,
        name: '12-1/4" section',
        kind: 'intermediate',
        hole_diameter_nominal: '12-1/4"',
        planned_top_md_si: 900,
        planned_bottom_md_si: 3200,
      },
      'engineer',
    )
    // The plan is recorded; nothing has been drilled. The API says so in its own words, and the plan's
    // 3200 m is not offered anywhere as a current depth.
    expect(section.planned_bottom_md_si).toBe(3200)
    expect(section.current_md_si).toBeNull()
    expect(section.is_planned_only).toBe(true)
    expect(section.semantics.planned_bottom_md_si).toBe('plan')
    expect(section.semantics.current_md_si).toBe('progress')

    // 3. A document attached to the well, so the rename has something real to preserve.
    // The bytes carry the run's tag. The ingestion pipeline de-duplicates by content hash — one blob,
    // one document, across every well (correct, and proven by its own tests) — so uploading the
    // fixture byte-for-byte would return the *seeded* document attached to the seeded well. The
    // trailer makes this a genuinely new document for this well.
    const uniqueBytes = Buffer.concat([
      readFileSync(uploadPath),
      Buffer.from(`\n# e2e run ${tag}\n`, 'utf8'),
    ])
    const upload = await request.post(`/api/v1/documents`, {
      headers: { 'X-Dev-Roles': 'engineer' },
      multipart: {
        file: {
          name: 'synthetic_ddr_upload.txt',
          mimeType: 'text/plain',
          buffer: uniqueBytes,
        },
        well_id: well.id,
        wellbore_id: wellbore.id,
        section_id: section.id,
        doc_type: 'ddr',
        title: `E2E DDR ${tag}`,
      },
    })
    expect(upload.ok(), `upload -> ${upload.status()}`).toBeTruthy()
    const document = (await upload.json()) as { document: { id: string; well_id: string | null } }
    expect(document.document.well_id).toBe(well.id)

    // 4. Rename. The id must not move, and the name must actually change.
    const renamed = await apiPatch<WellRow>(
      request,
      `/wells/${well.id}`,
      { name: `E2E-${tag}-01 (renamed)`, reason: 'e2e rename' },
      'engineer',
    )
    expect(renamed.id).toBe(well.id)
    expect(renamed.name).toBe(`E2E-${tag}-01 (renamed)`)

    // 5. Move to another field, in the same project.
    const moved = await apiPatch<WellRow>(
      request,
      `/wells/${well.id}`,
      { field_id: secondField.id, reason: 'e2e field move' },
      'engineer',
    )
    expect(moved.id).toBe(well.id)
    expect(moved.field_id).toBe(secondField.id)

    // 6. Life cycle. Only the states the server offers may be requested.
    const allowed = renamed.allowed_transitions ?? []
    expect(allowed).toContain('permitting')
    const transitioned = await apiPost<WellRow>(
      request,
      `/wells/${well.id}/lifecycle`,
      { target: 'permitting', reason: 'e2e transition' },
      'engineer',
    )
    expect(transitioned.status).toBe('permitting')
    expect(transitioned.id).toBe(well.id)

    // 7. A sidetrack off the original hole, with its parent recorded.
    const sidetrack = await apiPost<WellboreRow>(
      request,
      `/wells/${well.id}/wellbores`,
      { name: 'Sidetrack ST-1', purpose: 'sidetrack', parent_wellbore_id: wellbore.id, planned_td_md_si: 3400 },
      'engineer',
    )
    expect(sidetrack.parent_wellbore_id).toBe(wellbore.id)
    expect(sidetrack.sequence).toBe(2)

    const lineage = await apiGet<{ items: WellboreRow[]; total: number; root_id: string }>(
      request,
      `/wellbores/${sidetrack.id}/lineage`,
      'engineer',
    )
    expect(lineage.total).toBe(2)
    expect(lineage.items[0]?.id).toBe(wellbore.id)
    expect(lineage.items[1]?.id).toBe(sidetrack.id)
    expect(lineage.root_id).toBe(wellbore.id)

    // 8. Activate the sidetrack: exactly one hole is active afterwards.
    await apiPost(request, `/wellbores/${sidetrack.id}/activate`, {}, 'engineer')
    const structure = await apiGet<Structure>(request, `/wells/${well.id}/structure`, 'engineer')
    expect(structure.well.active_wellbore_id).toBe(sidetrack.id)
    expect(structure.wellbores.filter((hole) => hole.is_active).map((hole) => hole.id)).toEqual([sidetrack.id])

    // 9. A section in the sidetrack.
    await apiPost(
      request,
      `/wellbores/${sidetrack.id}/sections`,
      { sequence: 1, name: '8-1/2" sidetrack section', kind: 'production', planned_bottom_md_si: 3400 },
      'engineer',
    )
    const after = await apiGet<Structure>(request, `/wells/${well.id}/structure`, 'engineer')
    const sidetrackAfter = after.wellbores.find((hole) => hole.id === sidetrack.id)
    expect(sidetrackAfter?.sections).toHaveLength(1)
    // The original hole keeps its own section: the sidetrack did not replace it.
    const originalAfter = after.wellbores.find((hole) => hole.id === wellbore.id)
    expect(originalAfter?.sections).toHaveLength(1)

    // 10. Provenance: the document still points at the same well, and the well still exists as one row.
    const documentAfter = await apiGet<{ document: { id: string; well_id: string | null } }>(
      request,
      `/documents/${document.document.id}`,
      'engineer',
    )
    expect(documentAfter.document.well_id).toBe(well.id)
    expect(after.counts.documents).toBe(1)

    // 11. And the ledger recorded the edits rather than the rename erasing them.
    const ledger = await apiGet<{ items: Array<{ action: string; details: { changed?: string[] } }> }>(
      request,
      `/wells/${well.id}/audit-log`,
      'engineer',
    )
    const rename = ledger.items.find((row) => row.action === 'well.update' && row.details.changed?.includes('name'))
    expect(rename, 'the rename must be in the governance ledger').toBeTruthy()

    // 12. The same well, read through the browser, with the rename visible and the plan never
    //     presented as the current depth.
    await page.goto(`/master-data?well=${well.id}&tab=sections`)
    await expect(page.getByRole('heading', { level: 2, name: `E2E-${tag}-01 (renamed)` })).toBeVisible()
    // The workspace starts on the active hole, which is the sidetrack after step 8.
    await expect(page.getByText('8-1/2" sidetrack section')).toBeVisible()
    // The section carries a plan of 3400 m and no as-drilled or current depth: the current-depth cell
    // says so, and does not borrow the plan's number.
    const currentCell = page.getByTestId(/^current-/).first()
    await expect(currentCell).toContainText(/no current depth recorded/i)
    await expect(currentCell).not.toContainText('3400')

    // The ledger tab shows the governance entries, read from the same API.
    await page.goto(`/master-data?well=${well.id}&tab=ledger`)
    await expect(page.getByText('well.update').first()).toBeVisible()
  })

  test('a refusal is the server refusing, not a button being hidden', async ({ request }) => {
    const tag = stamp()
    const project = await apiPost<{ id: string }>(
      request,
      '/projects',
      { name: `E2E Refusal Project ${tag}` },
      'wellManager',
    )
    const well = await apiPost<WellRow>(
      request,
      '/wells',
      { project_id: project.id, name: `E2E-${tag}-02`, well_type: 'development_producer' },
      'engineer',
    )

    // A viewer may read a well and may not edit one. The refusal is a 403 with the taxonomy code, so a
    // client can distinguish "you may not" from "that does not exist" and from "the server is down".
    const denied = await apiAttempt(request, 'PATCH', `/wells/${well.id}`, {
      role: 'viewer',
      body: { name: 'should not be applied' },
    })
    expect(denied.status).toBe(403)
    expect(denied.code).toBe('security.permission_denied')

    // The write really did not happen.
    const reread = await apiGet<WellRow>(request, `/wells/${well.id}`, 'viewer')
    expect(reread.name).toBe(`E2E-${tag}-02`)

    // A stale write is refused as a conflict *and* the caller is told what to re-read. `retryable` is
    // false because repeating the same request would conflict again: the client must re-read first.
    await apiPatch<WellRow>(request, `/wells/${well.id}`, { name: `E2E-${tag}-02 first` }, 'engineer')
    const stale = await apiAttempt(request, 'PATCH', `/wells/${well.id}`, {
      body: { name: `E2E-${tag}-02 stale`, expected_updated_at: well.updated_at },
    })
    expect(stale.status).toBe(409)
    expect(stale.code).toBe('platform.conflict')
    expect(stale.retryable).toBe(false)
    expect(stale.message ?? '').toMatch(/changed|version|updated/i)

    // The stale write did not overwrite the winner.
    const winner = await apiGet<WellRow>(request, `/wells/${well.id}`, 'engineer')
    expect(winner.name).toBe(`E2E-${tag}-02 first`)

    // Series, cycles and cross-well parents are refused by the domain, not by the form.
    const other = await apiPost<WellRow>(
      request,
      '/wells',
      { project_id: project.id, name: `E2E-${tag}-03`, well_type: 'development_producer' },
      'engineer',
    )
    const first = await apiPost<WellboreRow>(
      request,
      `/wells/${well.id}/wellbores`,
      { name: 'Main bore', purpose: 'original' },
      'engineer',
    )
    const second = await apiPost<WellboreRow>(
      request,
      `/wells/${well.id}/wellbores`,
      { name: 'ST-1', purpose: 'sidetrack', parent_wellbore_id: first.id },
      'engineer',
    )
    const foreign = await apiPost<WellboreRow>(
      request,
      `/wells/${other.id}/wellbores`,
      { name: 'Other bore', purpose: 'original' },
      'engineer',
    )

    const selfParent = await apiAttempt(request, 'PATCH', `/wellbores/${second.id}`, {
      body: { parent_wellbore_id: second.id },
    })
    expect(selfParent.status, 'a wellbore cannot be its own parent').toBeLessThan(500)
    expect(selfParent.status).toBeGreaterThanOrEqual(400)

    const crossWell = await apiAttempt(request, 'PATCH', `/wellbores/${second.id}`, {
      body: { parent_wellbore_id: foreign.id },
    })
    expect(crossWell.status, 'a parent must belong to the same well').toBeGreaterThanOrEqual(400)
    expect(crossWell.status).toBeLessThan(500)

    const cycle = await apiAttempt(request, 'PATCH', `/wellbores/${first.id}`, {
      body: { parent_wellbore_id: second.id },
    })
    expect(cycle.status, 'lineage must not contain a cycle').toBeGreaterThanOrEqual(400)
    expect(cycle.status).toBeLessThan(500)

    // And the lineage was not damaged by the refused attempts.
    const intact = await apiGet<{ items: WellboreRow[] }>(request, `/wellbores/${second.id}/lineage`, 'engineer')
    expect(intact.items.map((row) => row.id)).toEqual([first.id, second.id])
  })

  test('a well created through the interface is a well the API serves', async ({ page, request }) => {
    const tag = stamp()
    const project = await apiPost<{ id: string; name: string }>(
      request,
      '/projects',
      { name: `E2E UI Project ${tag}` },
      'wellManager',
    )
    const field = await apiPost<{ id: string }>(
      request,
      '/fields',
      { project_id: project.id, name: `E2E UI Field ${tag}` },
      'engineer',
    )

    await page.goto(`/master-data?project=${project.id}&field=${field.id}`)
    await page.getByTestId('master-new-well').click()

    const form = page.getByTestId('well-create-form')
    await expect(form).toBeVisible()
    await form.getByRole('textbox', { name: /^Name/ }).fill(`E2E-${tag}-UI`)
    // The project is chosen in the form; the field comes from the scope the page was opened in.
    await form.getByRole('combobox', { name: 'Field', exact: true }).selectOption({ label: `E2E UI Field ${tag}` })
    await form.getByRole('textbox', { name: /^UWI/ }).fill(`E2E-${tag}-UI-UWI`)
    await form.getByTestId('well-create-submit').click()

    // The workspace switches to the well it created — its own id, read back from the response.
    await expect(page).toHaveURL(/well=wel_/)
    const wellId = new URL(page.url()).searchParams.get('well') as string
    const created = await apiGet<WellRow>(request, `/wells/${wellId}`, 'engineer')
    expect(created.name).toBe(`E2E-${tag}-UI`)
    // The server canonicalises regulator identifiers to upper case on write (one spelling of one well),
    // so the journey asserts the canonical form rather than the casing that was typed.
    expect(created.uwi).toBe(`E2E-${tag}-UI-UWI`.toUpperCase())
    // Created planned: the form never sends a status, so the life cycle starts where the server says.
    expect(created.status).toBe('planned')

    // The seed is untouched: the journey's well is a new row, not an edit of a fixture.
    const seeded = await seededIds(request)
    expect(seeded.wellId).not.toBe(wellId)
  })
})
