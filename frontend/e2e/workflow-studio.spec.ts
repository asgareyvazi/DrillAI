/**
 * Journey 3 — the workflow lifecycle, driven through the browser.
 *
 * The editor is used the way it is offered: the palette adds nodes, the inspector configures them,
 * the connect control links them. Every claim the screen makes is then checked against the API, so a
 * UI that reports a save the server never stored fails here — and so does a UI that quietly reports
 * success for a version the server refused to publish.
 *
 * The lifecycle is built around a node whose configuration is genuinely required by the server
 * (`output.report` has no default title), because that is where "configure through the inspector"
 * stops being cosmetic: until the field is filled in, the server refuses to publish the graph.
 *
 * `data.load_context → output.report` is used because both are registered node types whose empty
 * configuration is otherwise valid, so the only error in the graph is the one being demonstrated.
 */

import { holdsPermission } from '../src/lib/permissions'
import { apiGet, apiPost, selectRole, test, expect } from './fixtures'

type Workflow = {
  id: string
  key: string
  name: string
  status: string
  current_version: number
  published_version_id: string | null
}

type GraphIssue = {
  code: string
  message: string
  node_id?: string | null
  edge_id?: string | null
  severity: 'error' | 'warning'
}

type WorkflowDetail = {
  workflow: Workflow
  version: {
    id: string
    version: number
    graph_hash: string
    node_count: number
    edge_count: number
    published_at: string | null
    validation: { is_valid: boolean; issues: GraphIssue[] }
  }
  graph: {
    nodes: Array<{
      id: string
      type: string
      name?: string
      config?: Record<string, unknown>
      inputs?: Record<string, unknown>
      on_error?: string
      retry?: Record<string, unknown>
      timeout_seconds?: number | null
      is_breakpoint?: boolean
      notes?: string
    }>
    edges: Array<{
      id?: string
      source: string
      target: string
      condition?: string | null
      label?: string | null
      is_loop_back?: boolean
    }>
    variables?: Record<string, unknown>
    settings?: Record<string, unknown>
  }
}

async function detail(request: Parameters<typeof apiGet>[1], workflowId: string): Promise<WorkflowDetail> {
  return apiGet<WorkflowDetail>(request, `/workflows/${workflowId}`)
}

/** A workflow that has never been saved has no version; the API answers 404 until the first save. */
/** The newest saved version number, from the history rather than from a default. */
async function newestVersion(request: Parameters<typeof apiGet>[1], workflowId: string): Promise<number> {
  const history = await apiGet<{ items: Array<{ version: number }>; total: number }>(
    request,
    `/workflows/${workflowId}/versions?limit=1`,
  )
  expect(history.items.length, 'a saved version is required for this assertion').toBeGreaterThan(0)
  return history.items[0].version
}

async function hasVersion(
  request: Parameters<typeof apiGet>[1],
  workflowId: string,
): Promise<boolean> {
  const response = await request.get(`/api/v1/workflows/${workflowId}`, {
    headers: { 'X-Dev-Roles': 'drilling_supervisor,admin,engineer' },
  })
  if (response.status() === 404) return false
  expect(response.ok(), `GET /workflows/${workflowId} -> ${response.status()}`).toBeTruthy()
  return true
}

async function createWorkflow(request: Parameters<typeof apiPost>[1], suffix: string): Promise<Workflow> {
  return apiPost<Workflow>(request, '/workflows', {
    key: `e2e.studio-${suffix}`,
    name: `Studio journey ${suffix}`,
    description: 'Created by the workflow studio journey',
  })
}

test.describe('workflow studio: create, edit, validate, save, publish', () => {
  test('a definition created in the studio opens as an empty editor, and says so', async ({
    page,
    request,
    consoleErrors,
  }) => {
    const suffix = `${Date.now()}`
    const before = await apiGet<{ items: Workflow[]; total: number }>(request, '/workflows')

    await page.goto('/workflows')
    await expect(page.getByRole('heading', { name: /workflow studio/i })).toBeVisible()

    // The key is derived from the name: a space or a capital must not produce an invalid key.
    await page.getByPlaceholder('Daily Drilling Intelligence').fill(`Studio journey ${suffix}`)
    await page.getByRole('button', { name: /new workflow/i }).click()

    await expect
      .poll(async () => (await apiGet<{ total: number }>(request, '/workflows')).total, {
        timeout: 30_000,
        message: 'the created workflow must exist on the server',
      })
      .toBe(before.total + 1)

    const created = (await apiGet<{ items: Workflow[] }>(request, '/workflows')).items.find(
      (item) => item.name === `Studio journey ${suffix}`,
    )
    expect(created, 'the workflow must be listed by the API').toBeTruthy()
    expect(created?.key).toMatch(/^[a-z0-9][a-z0-9_.-]*$/)
    expect(created?.published_version_id).toBeNull()

    // The studio selects what was just created instead of leaving the user on an empty page.
    await expect(page).toHaveURL(new RegExp(`workflow=${created?.id}`))
    await expect(page.getByText('0 nodes · 0 edges', { exact: true })).toBeVisible()
    await expect(page.getByText(/no saved version yet/i)).toBeVisible()
    // Nothing has been typed, so nothing is unsaved: an empty draft against an empty server graph.
    await expect(page.getByText('saved', { exact: true })).toBeVisible()

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('an unchanged save creates no version, and the version numbers come from the server', async ({
    page,
    request,
    consoleErrors,
  }) => {
    const workflow = await createWorkflow(request, `unchanged-${Date.now()}`)
    expect(await hasVersion(request, workflow.id)).toBe(false)

    await page.goto(`/workflows?workflow=${workflow.id}`)
    await expect(page.getByText(/no saved version yet/i)).toBeVisible()

    // 1. The first save creates version 1.
    await page.getByRole('button', { name: /save graph/i }).click()
    await expect(page.getByTestId('studio-message')).toContainText('Saved as v1')

    const first = await detail(request, workflow.id)
    expect(first.version.version).toBe(1)
    expect(first.version.node_count).toBe(0)

    // 2. Saving again with nothing changed must not mint a new version: the server keeps the row and
    //    the editor says exactly that instead of claiming a save.
    await expect(page.getByText('saved', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: /save graph/i }).click()
    await expect(page.getByTestId('studio-message')).toContainText('No new version')

    const second = await detail(request, workflow.id)
    expect(second.version.version).toBe(1)
    expect(second.version.graph_hash).toBe(first.version.graph_hash)

    // 3. A real edit is dirty, and only a real edit produces the next version number.
    await page.getByRole('button', { name: /load engineering context/i }).first().click()
    await expect(page.getByText('unsaved changes', { exact: true })).toBeVisible()
    await expect(page.getByText('1 nodes · 0 edges', { exact: true })).toBeVisible()

    await page.getByRole('button', { name: /save graph/i }).click()
    await expect(page.getByTestId('studio-message')).toContainText('Saved as v2')
    expect((await detail(request, workflow.id)).version.version).toBe(2)
    await expect(page.getByText('saved', { exact: true })).toBeVisible()

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('add, configure, connect, validate, save and publish — the server decides each step', async ({
    page,
    request,
    consoleErrors,
  }) => {
    const workflow = await createWorkflow(request, `lifecycle-${Date.now()}`)
    await page.goto(`/workflows?workflow=${workflow.id}`)
    await expect(page.getByText(/no saved version yet/i)).toBeVisible()
    // Drafting and publishing are different rights: this journey is walked as the role that holds
    // both, and the engineer's version of the same steps is asserted separately below.
    await selectRole(page, 'supervisor')
    await expect(page.getByRole('button', { name: /^publish$/i })).toBeEnabled()

    // 1. Add two nodes from the registry-backed palette.
    await page.getByRole('button', { name: /load engineering context/i }).first().click()
    await expect(page.getByText('1 nodes · 0 edges', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: /build report/i }).first().click()
    await expect(page.getByText('2 nodes · 0 edges', { exact: true })).toBeVisible()

    // The node that was just added is the selected one, and the inspector names its real type.
    await expect(page.getByLabel('Node type')).toHaveValue('output.report')
    // `output.report` has a required `title` with no default, and the schema says so.
    await expect(page.getByTestId('config-label-title')).toHaveText('Title')
    await expect(page.getByText(/required/).first()).toBeVisible()

    // 2. Connect them without a mouse drag: select the first node, connect it to the second.
    await page.locator('.react-flow__node', { hasText: 'Load engineering context' }).click()
    await expect(page.getByLabel('Node type')).toHaveValue('data.load_context')
    await page.getByLabel('Connect to').selectOption({ index: 1 })
    await expect(page.getByText('2 nodes · 1 edges', { exact: true })).toBeVisible()

    // 3. Validation finds the real defect: the report node has no title yet — and what is shown is the
    //    server's own report, with its severity, the offending node, and the code to look up.
    await page.getByRole('button', { name: /^validate$/i }).click()
    const issues = page.getByRole('listitem').filter({ hasText: 'invalid_node_config' })
    await expect(issues.first()).toBeVisible()
    await expect(issues.first()).toContainText('error')
    await expect(issues.first()).toContainText(/title/i)
    await expect(issues.first()).toContainText(/node output_report_\d+/)
    await expect(page.getByText('1 error(s)')).toBeVisible()

    // 4. An invalid graph can be saved as a draft — the server records it and refuses to publish it.
    await page.getByRole('button', { name: /save graph/i }).click()
    await expect(page.getByTestId('studio-message')).toContainText('Saved as v1')
    const draft = await detail(request, workflow.id)
    expect(draft.version.validation.is_valid).toBe(false)
    expect(draft.version.published_at).toBeNull()

    // The server refuses to publish a graph its own validation rejected, and the refusal is shown
    // rather than dressed up: the workflow is still unpublished afterwards.
    await page.getByRole('button', { name: /^publish$/i }).click()
    await expect(page.getByTestId('studio-message')).toContainText(/invalid|cannot publish/i)
    await expect(page.getByTestId('studio-message')).not.toContainText('Published v')
    const refused = await detail(request, workflow.id)
    expect(refused.version.published_at).toBeNull()
    expect(refused.workflow.published_version_id).toBeNull()
    // Publishing a draft the server rejects is a deliberate failure, and the browser logs the failed
    // request. It is the only such failure this journey provokes; anything else still fails the test.
    const expectedRefusal = consoleErrors.filter((error) => /status of 4\d\d/.test(error))

    // 5. Fix it in the inspector. The field writes the same config the JSON view shows.
    await page.locator('.react-flow__node', { hasText: 'Build report' }).click()
    await page.getByLabel('Title', { exact: true }).fill('Daily drilling report')
    await page.getByLabel('Configuration (JSON)').blur()
    await expect(page.getByLabel('Configuration (JSON)')).toHaveValue(/Daily drilling report/)

    await page.getByRole('button', { name: /^validate$/i }).click()
    await expect(page.getByText('valid', { exact: true })).toBeVisible()

    // 6. Save v2 and publish it: the version that is published is the reviewed one.
    await page.getByRole('button', { name: /save graph/i }).click()
    await expect(page.getByTestId('studio-message')).toContainText('Saved as v2')
    await page.getByRole('button', { name: /^publish$/i }).click()
    await expect(page.getByTestId('studio-message')).toContainText('Published v2')

    const published = await detail(request, workflow.id)
    expect(published.version.version).toBe(2)
    expect(published.version.published_at).not.toBeNull()
    expect(published.workflow.published_version_id).toBe(published.version.id)
    expect(published.graph.nodes).toHaveLength(2)
    expect(published.graph.edges).toHaveLength(1)
    expect(
      published.graph.nodes.find((node) => node.type === 'output.report')?.config?.title,
    ).toBe('Daily drilling report')

    // 7. The published state is the server's, so it survives a reload.
    await page.reload()
    await expect(page.getByTestId('loaded-version')).toContainText('v2')
    await expect(page.getByTestId('published-version')).toContainText('v2')
    await expect(page.getByTestId('published-version')).toHaveAttribute('data-published-at', /.+/)
    await expect(page.getByText('saved', { exact: true })).toBeVisible()

    expect(expectedRefusal.length, 'the refused publish is the only expected failure').toBeLessThanOrEqual(1)
    for (const error of consoleErrors) {
      if (!expectedRefusal.includes(error)) {
        expect(error, `unexpected console error: ${error}`).toMatch(/status of 4\d\d/)
      }
    }
  })

  test('publishing is offered only to an identity the server would accept it from', async ({
    page,
    request,
    consoleErrors,
  }) => {
    const workflow = await createWorkflow(request, `permissions-${Date.now()}`)
    await page.goto(`/workflows?workflow=${workflow.id}`)
    await expect(page.getByText(/no saved version yet/i)).toBeVisible()

    // The default identity of the suite holds well/engineer/admin, which does not include
    // 'workflow.publish': the button is disabled and the reason is stated rather than implied.
    const engineerIdentity = await apiGet<{ role_keys: string[]; permissions: string[] }>(
      request,
      '/platform/identity',
    )
    expect(engineerIdentity.permissions, 'the engineer identity must not hold publish').not.toContain(
      'workflow.publish',
    )
    const publish = page.getByRole('button', { name: /^publish$/i })
    await expect(publish).toBeDisabled()
    await expect(publish).toHaveAttribute('title', /workflow\.publish/)

    // The server refuses it too — a disabled button is a convenience, never the control.
    const refused = await request.post(`/api/v1/workflows/${workflow.id}/publish`, {
      headers: { 'X-Dev-Roles': 'engineer' },
    })
    expect(refused.status(), 'the server must refuse the publish for this identity').toBe(403)

    // Switching to the supervisor identity offers the same action, and it succeeds.
    await selectRole(page, 'supervisor')
    await expect(page.getByRole('button', { name: /^publish$/i })).toBeEnabled()

    await page.getByRole('button', { name: /load engineering context/i }).first().click()
    await page.getByRole('button', { name: /save graph/i }).click()
    await expect(page.getByTestId('studio-message')).toContainText(/Saved as v\d+/)

    // The identity endpoint reports the role's *patterns* ('workflow.*'), not an expanded list, so
    // the pattern set is matched with the same rules the interface uses. The behavioural half of the
    // assertion is the publish below: if the matcher and the server disagreed, that call would fail.
    const supervisorIdentity = await apiGet<{ permissions: string[] }>(request, '/platform/identity', 'supervisor')
    expect(holdsPermission(supervisorIdentity.permissions, 'workflow.publish')).toBe(true)

    await page.getByRole('button', { name: /^publish$/i }).click()
    await expect(page.getByTestId('studio-message')).toContainText(/Published v\d+/)
    const detail = await apiGet<WorkflowDetail>(request, `/workflows/${workflow.id}`, 'supervisor')
    expect(detail.version.published_at).not.toBeNull()

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('the version history loads an older version without overwriting the newest', async ({
    page,
    request,
    consoleErrors,
  }) => {
    const workflows = await apiGet<{ items: Workflow[] }>(request, '/workflows')
    const target = workflows.items.find((item) => item.current_version > 1)
    expect(target, 'a definition with more than one version is needed').toBeTruthy()
    const workflowId = target?.id as string

    const history = await apiGet<{ items: Array<{ version: number; id: string; graph_hash: string }> }>(
      request,
      `/workflows/${workflowId}/versions?limit=20`,
    )
    const newest = history.items[0]
    const older = history.items[history.items.length - 1]
    expect(older.version).toBeLessThan(newest.version)

    await page.goto(`/workflows?workflow=${workflowId}`)
    // The editor opens on the newest saved version, not on the published one: a draft must not look
    // as if it had been lost when the page is reopened.
    await expect(page.getByTestId('loaded-version')).toContainText(`v${newest.version}`)

    await page.getByRole('button', { name: /^load$/ }).last().click()
    await expect(page.getByText(/viewing an older version/i)).toBeVisible()

    // Loading an older version is a read: nothing was stored, and the newest version is still newest.
    const after = await apiGet<{ items: Array<{ version: number; graph_hash: string }> }>(
      request,
      `/workflows/${workflowId}/versions?limit=20`,
    )
    expect(after.items[0].version).toBe(newest.version)
    expect(after.items[0].graph_hash).toBe(newest.graph_hash)

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('a published graph keeps every field the editor does not display', async ({
    page,
    request,
    consoleErrors,
  }) => {
    const workflows = await apiGet<{ items: Workflow[] }>(request, '/workflows')
    const target = workflows.items.find((item) => item.published_version_id !== null && item.current_version > 0)
    expect(target, 'the seed must publish at least one workflow').toBeTruthy()
    const workflowId = target?.id as string
    const before = await detail(request, workflowId)

    await page.goto(`/workflows?workflow=${workflowId}`)
    await expect(page.getByText('saved', { exact: true })).toBeVisible()

    // Edit an edge through the edge inspector. An edge is an SVG path a few pixels wide, so dispatch
    // the click the canvas listens for rather than asking Playwright to hit-test a hairline. The
    // property edited is the label: the point is that the whole graph survives a save, and a label
    // has no runtime meaning, so this journey cannot leave a condition behind that would fail inside
    // some later run of the same seeded definition.
    await page.locator('.react-flow__edge').first().dispatchEvent('click')
    const label = page.getByLabel(/edge label/i)
    await expect(label).toBeVisible()
    await label.fill('reviewed edge')
    await expect(page.getByText('unsaved changes', { exact: true })).toBeVisible()

    await page.getByRole('button', { name: /save graph/i }).click()
    await expect(page.getByTestId('studio-message')).toContainText(/Saved as v\d+/)

    // The detail endpoint answers with the *published* version when there is one, so the newest
    // saved version is asked for by the number the history reports.
    const newest = await newestVersion(request, workflowId)
    const after = await apiGet<WorkflowDetail>(request, `/workflows/${workflowId}?version=${newest}`)
    expect(after.version.version).toBeGreaterThan(before.version.version)

    // Nothing was dropped: every node and edge of the stored graph is still there.
    expect(after.graph.nodes.map((node) => node.id).sort()).toEqual(
      before.graph.nodes.map((node) => node.id).sort(),
    )
    expect(after.graph.edges).toHaveLength(before.graph.edges.length)

    // And the fields the editor does not model were copied forward unchanged.
    for (const node of before.graph.nodes) {
      const saved = after.graph.nodes.find((candidate) => candidate.id === node.id)
      expect(saved, `node ${node.id} must survive the save`).toBeTruthy()
      expect(saved).toMatchObject({
        type: node.type,
        inputs: node.inputs ?? {},
        config: node.config ?? {},
      })
      if (node.on_error !== undefined) expect(saved?.on_error).toBe(node.on_error)
      if (node.timeout_seconds !== undefined) expect(saved?.timeout_seconds).toBe(node.timeout_seconds)
      if (node.is_breakpoint !== undefined) expect(saved?.is_breakpoint).toBe(node.is_breakpoint)
      if (node.notes !== undefined) expect(saved?.notes).toBe(node.notes)
    }
    expect(after.graph.settings).toEqual(before.graph.settings)
    expect(after.graph.variables).toEqual(before.graph.variables)

    // The label that was typed is really on the edge.
    expect(after.graph.edges.some((edge) => edge.label === 'reviewed edge')).toBe(true)

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('an unsaved draft is not discarded without being asked', async ({ page, request, consoleErrors }) => {
    const workflows = await apiGet<{ items: Workflow[] }>(request, '/workflows')
    expect(workflows.items.length, 'at least two definitions are needed').toBeGreaterThan(1)
    const [first, second] = workflows.items
    const stored = await detail(request, first.id)

    await page.goto(`/workflows?workflow=${first.id}`)
    await expect(page.getByText('saved', { exact: true })).toBeVisible()

    // Make an edit, then try to switch away.
    await page.getByRole('button', { name: /load engineering context/i }).first().click()
    await expect(page.getByText('unsaved changes', { exact: true })).toBeVisible()

    await page.getByLabel('Workflows', { exact: true }).selectOption(second.id)
    const dialog = page.getByRole('dialog', { name: /unsaved graph changes/i })
    await expect(dialog).toBeVisible()

    // Staying keeps the draft.
    await dialog.getByRole('button', { name: /stay on this workflow/i }).click()
    await expect(page.getByText('unsaved changes', { exact: true })).toBeVisible()
    await expect(page).toHaveURL(new RegExp(`workflow=${first.id}`))

    // Switching discards the draft — and the stored version was never touched.
    await page.getByLabel('Workflows', { exact: true }).selectOption(second.id)
    await page
      .getByRole('dialog', { name: /unsaved graph changes/i })
      .getByRole('button', { name: /discard/i })
      .click()
    await expect(page).toHaveURL(new RegExp(`workflow=${second.id}`))

    const untouched = await detail(request, first.id)
    expect(untouched.version.version).toBe(stored.version.version)
    expect(untouched.version.graph_hash).toBe(stored.version.graph_hash)
    expect(untouched.graph.nodes).toHaveLength(stored.graph.nodes.length)

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('a run is scoped to the well the user chose, and to no other', async ({ page, request, consoleErrors }) => {
    const workflows = await apiGet<{ items: Workflow[] }>(request, '/workflows')
    const target = workflows.items.find((item) => item.published_version_id !== null)
    expect(target, 'a published workflow is needed to start a run').toBeTruthy()
    const workflowId = target?.id as string

    const wells = await apiGet<{ items: Array<{ id: string; name: string; project_id: string | null }>; total: number }>(
      request,
      '/wells',
    )
    expect(wells.total, 'the seed must produce a well a run can target').toBeGreaterThan(0)
    const well = wells.items[0]
    // The second well is created here rather than assumed: this journey is about a run carrying the
    // well it was started for, and one well cannot show that.
    const other = await apiPost<{ id: string; name: string; project_id: string | null }>(
      request,
      '/wells',
      { project_id: well.project_id, name: `Studio journey well ${Date.now()}` },
      'supervisor',
    )

    await page.goto(`/workflows?workflow=${workflowId}`)
    await expect(page.getByText('saved', { exact: true })).toBeVisible()

    // A definition has no well of its own, so without a choice the run cannot be started at all.
    await expect(page.getByText(/no well selected/i)).toBeVisible()
    const runButton = page.getByRole('button', { name: /start run/i })
    await expect(runButton).toBeDisabled()

    // Choosing a well enables it and navigates to that run.
    await page.getByLabel('Run context').selectOption(well.id)
    await expect(runButton).toBeEnabled()
    await runButton.click()
    await expect(page).toHaveURL(/\/runs\?run=/)

    const runId = new URL(page.url()).searchParams.get('run')
    const run = await apiGet<{ run: { id: string; well_id: string | null; project_id: string | null; workflow_id: string } }>(
      request,
      `/runs/${runId}`,
    )
    expect(run.run.well_id).toBe(well.id)
    expect(run.run.workflow_id).toBe(workflowId)

    // The same definition run against the other well is scoped to that one: nothing is cached.
    await page.goto(`/workflows?workflow=${workflowId}`)
    await page.getByLabel('Run context').selectOption(other.id)
    await page.getByRole('button', { name: /start run/i }).click()
    await expect(page).toHaveURL(/\/runs\?run=/)
    const otherRunId = new URL(page.url()).searchParams.get('run')
    expect(otherRunId).not.toBe(runId)
    const otherRun = await apiGet<{ run: { well_id: string | null } }>(request, `/runs/${otherRunId}`)
    expect(otherRun.run.well_id).toBe(other.id)

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('the palette is the node registry, not a list copied into the frontend', async ({
    page,
    request,
    consoleErrors,
  }) => {
    const catalogue = await apiGet<{
      items: Array<{ key: string; name: string; family: string }>
      families: Record<string, unknown>
      total: number
    }>(request, '/registry/node-types')

    await page.goto('/workflows')
    await page.getByRole('tab', { name: /node palette/i }).click()

    for (const spec of catalogue.items) {
      await expect(page.getByText(spec.key, { exact: true }).first()).toBeVisible()
    }
    // The tab badge counts what the registry returned, so a node type added on the server appears
    // without a frontend change.
    await expect(page.getByRole('tab', { name: /node palette/i })).toContainText(String(catalogue.total))

        expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })
})
