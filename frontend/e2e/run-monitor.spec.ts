/**
 * Journey 4 — a real run, watched in the browser.
 *
 * This is the first journey that starts work on the server from the UI and then reports what the
 * server did. It runs the published definition against the seeded well, so the run executes the
 * platform's real nodes: the engineering context is built, the well state is read, NPT is computed
 * from the recorded operations, and the human approval node stops the run.
 *
 * Why the assertions are shaped the way they are:
 *
 *   * every status, node name and count is compared with what the API returns for the same run, so a
 *     screen that invents a status cannot pass;
 *   * a page reload is part of the journey, because a run waiting for a human must survive it — that
 *     state lives in the database, not in React;
 *   * the approval is decided by a different identity than the one that started the run, which is the
 *     platform's separation-of-duties rule and is enforced by the server, not by the button;
 *   * the failure journey uses the seeded `failing-node-demo` workflow, so the failed state is a real
 *     node failure rather than a mocked error.
 */

import { apiGet, apiPost, fixtures, selectRole, test, expect, waitForLoaded } from './fixtures'
import type { Page } from '@playwright/test'

type RunRow = {
  id: string
  workflow_id: string
  workflow_key: string | null
  version: number | null
  status: string
  trigger_type: string
  project_id: string | null
  well_id: string | null
  wellbore_id: string | null
  section_id: string | null
  operation_id: string | null
  inputs: Record<string, unknown>
  outputs: Record<string, unknown>
  step_count: number
  error: string | null
  error_node_id: string | null
  cursor_node_id: string | null
  pending_approval_id: string | null
  is_dry_run: boolean
  initiated_by: string | null
  approved_by: string | null
}

type NodeRun = {
  id: string
  node_id: string
  node_type: string
  node_name: string | null
  status: string
  attempt: number | null
  outputs: Record<string, unknown> | null
  error: Record<string, unknown> | null
  engine_run_id: string | null
}

type Approval = {
  id: string
  run_id: string | null
  node_id: string | null
  title: string
  description: string | null
  action_level: string
  required_role: string | null
  risk_notes: string | null
  evidence_refs: string[]
  conditions: string[]
  status: string
  requested_by: string | null
  decided_by: string | null
  decision_note: string | null
}

type RunEnvelope = {
  run: RunRow
  workflow: { id: string; key: string; name: string; status: string } | null
  node_runs: NodeRun[]
  artifacts: Array<Record<string, unknown>>
  events: Array<{ id: string; seq: number; type: string; node_id: string | null; message: string | null }>
  pending_approval?: Approval | null
  resumable: boolean
}

function runEnvelope(request: Parameters<typeof apiGet>[1], runId: string, role: Parameters<typeof apiGet>[2] = 'engineer') {
  return apiGet<RunEnvelope>(request, `/runs/${runId}`, role)
}

async function startRunFromStudio(page: Page, workflowId: string, wellId: string): Promise<string> {
  await page.goto(`/workflows?workflow=${workflowId}`)
  await waitForLoaded(page)
  // The identity selector is part of the shell, so the page has to be open before it can be used.
  await selectRole(page, 'supervisor')
  await page.getByLabel('Run context').selectOption(wellId)
  await page.getByRole('button', { name: 'Start run' }).click()
  await expect(page).toHaveURL(/\/runs\?run=/)
  const runId = new URL(page.url()).searchParams.get('run')
  expect(runId, 'the studio must navigate to the run it started').toBeTruthy()
  return runId as string
}

test.describe('run monitor: a run that stops for a human decision', () => {
  test('a published definition runs on a real well and stops at its approval gate', async ({
    page,
    request,
    consoleErrors,
    wellId,
  }) => {
    const { workflow_id } = fixtures()
    const runId = await startRunFromStudio(page, workflow_id, wellId)

    // The server ran the graph inline and parked at the gate; the monitor must show that state, not a
    // spinner and not a success.
    const envelope = await runEnvelope(request, runId, 'supervisor')
    expect(envelope.run.status).toBe('waiting_approval')
    expect(envelope.resumable).toBe(true)
    await expect(page.getByTestId('run-status')).toHaveText(/waiting approval/i)
    await expect(page.getByTestId('run-awaiting-approval')).toBeVisible()

    // The run's own scope, from the API: the well the studio was told to act on.
    const scope = page.getByTestId('run-scope')
    await expect(scope).toContainText(wellId)
    expect(envelope.run.well_id).toBe(wellId)

    // Every node execution the server recorded is shown, with the server's own names and statuses.
    for (const nodeRun of envelope.node_runs) {
      const label = nodeRun.node_name ?? nodeRun.node_id
      await expect(page.getByText(label, { exact: true })).toBeVisible()
    }
    const waitingNode = envelope.node_runs.find((row) => row.status === 'waiting_approval')
    expect(waitingNode, 'the gate node must have a waiting row').toBeTruthy()

    // The approval card carries what the approver decides on.
    const card = page.getByTestId('approval-card')
    const approval = envelope.pending_approval as Approval
    expect(approval).toBeTruthy()
    await expect(card).toContainText(approval.title)
    await expect(card).toContainText(String(approval.description))
    await expect(card).toContainText(approval.action_level)
    if (approval.risk_notes) await expect(card).toContainText(approval.risk_notes)
    // Resuming an undecided approval is refused by the server, and the UI does not offer it.
    await expect(page.getByTestId('resume-run')).toBeDisabled()

    // A reload must land in the same state: it comes from the database, not from the browser.
    await page.reload()
    await waitForLoaded(page)
    await expect(page.getByTestId('run-status')).toHaveText(/waiting approval/i)
    await expect(page.getByTestId('approval-card')).toBeVisible()

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('a decision taken by another identity resumes the run and is recorded with its note', async ({
    page,
    request,
    consoleErrors,
    wellId,
  }) => {
    const { workflow_id } = fixtures()
    const runId = await startRunFromStudio(page, workflow_id, wellId)

    // The requester is the supervisor; the decider must be someone else. Switching identity in the
    // app is a real switch: the requests after this carry the new roles.
    await selectRole(page, 'wellManager')
    await page.reload()
    await waitForLoaded(page)
    await expect(page.getByTestId('approval-card')).toBeVisible()

    await page.getByTestId('approval-note').fill('Reviewed the NPT attribution for this well.')
    await page.getByTestId('approval-conditions').fill('Re-issue the summary if NPT changes.')
    await page.getByTestId('approval-approve').click()

    await expect(page.getByTestId('run-status')).toHaveText(/succeeded/i)
    const envelope = await runEnvelope(request, runId, 'wellManager')
    expect(envelope.run.status).toBe('succeeded')
    expect(envelope.run.approved_by).toBeTruthy()
    expect(envelope.resumable).toBe(false)
    expect(envelope.pending_approval ?? null).toBeNull()

    // The nodes that were skipped while waiting really ran afterwards.
    const byId = new Map(envelope.node_runs.map((row) => [row.node_id, row]))
    const previouslySkipped = [...byId.values()].filter((row) => row.status === 'skipped')
    expect(previouslySkipped).toEqual([])
    expect([...byId.values()].every((row) => row.status === 'succeeded')).toBe(true)

    // The decision is on the record with the note and the condition, and reading the approval back by
    // id returns the same thing the inbox lists — the decision was stored, not just rendered.
    const approvals = await apiGet<{ items: Approval[] }>(request, '/approvals?status=any', 'wellManager')
    const decidingRecord = approvals.items.find((item) => item.run_id === runId)
    expect(decidingRecord, 'the decision must be in the approval record').toBeTruthy()
    expect(decidingRecord?.status).toBe('approved')
    expect(decidingRecord?.decision_note).toBe('Reviewed the NPT attribution for this well.')
    expect(decidingRecord?.conditions).toEqual(['Re-issue the summary if NPT changes.'])
    expect(decidingRecord?.decided_by).toBeTruthy()
    // Separation of duties, as the server recorded it: the decider is not the requester.
    expect(decidingRecord?.requested_by).not.toBe(decidingRecord?.decided_by)

    const reread = await apiGet<{ approval: Approval }>(
      request,
      `/approvals/${decidingRecord?.id}`,
      'wellManager',
    )
    expect(reread.approval.decision_note).toBe('Reviewed the NPT attribution for this well.')
    expect(reread.approval.conditions).toEqual(['Re-issue the summary if NPT changes.'])

    // Reload: the completed run stays completed, and the decision it went through is still on the
    // page — the note and the conditions are part of the run's record, not only of the inbox.
    await page.reload()
    await waitForLoaded(page)
    await expect(page.getByTestId('run-status')).toHaveText(/succeeded/i)
    // Nothing is waiting for a decision any more — asserted on the action, not on the card, because
    // the decision itself is shown as a (read-only) approval card inside the record panel below.
    await expect(page.getByTestId('approval-approve')).toHaveCount(0)

    const recordPanel = page.getByTestId('approval-record')
    await expect(recordPanel).toBeVisible()
    await expect(recordPanel).toContainText('Reviewed the NPT attribution for this well.')
    await expect(recordPanel).toContainText('Re-issue the summary if NPT changes.')
    await expect(recordPanel).toContainText(String(decidingRecord?.decided_by ?? ''))
    // A decided approval is not offered for a second decision.
    await expect(recordPanel.getByTestId('approval-approve')).toHaveCount(0)

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('a rejection cancels the run instead of resuming it', async ({ page, request, consoleErrors, wellId }) => {
    const { workflow_id } = fixtures()
    const runId = await startRunFromStudio(page, workflow_id, wellId)

    await selectRole(page, 'wellManager')
    await page.reload()
    await waitForLoaded(page)
    await page.getByTestId('approval-note').fill('The plan does not match the morning report.')
    await page.getByTestId('approval-reject').click()

    await expect(page.getByTestId('run-status')).toHaveText(/cancelled/i)
    const envelope = await runEnvelope(request, runId, 'wellManager')
    expect(envelope.run.status).toBe('cancelled')
    expect(envelope.resumable).toBe(false)
    // The run's error names the rejection, and no node ran after the gate.
    expect(envelope.run.error).toMatch(/rejected/i)
    const byId = new Map(envelope.node_runs.map((row) => [row.node_id, row]))
    expect(byId.get('daily_report')?.status ?? 'skipped').toBe('skipped')

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })
})

test.describe('run monitor: the approval inbox', () => {
  test('lists what is waiting and can be widened to the decisions already taken', async ({
    page,
    request,
    consoleErrors,
  }) => {
    const waiting = await apiGet<{ items: Array<{ id: string; title: string }>; total: number }>(
      request,
      '/approvals?status=pending',
      'wellManager',
    )
    await page.goto('/runs')
    await waitForLoaded(page)
    await page.getByRole('tab', { name: /approvals/i }).click()

    if (waiting.total > 0) {
      for (const approval of waiting.items) {
        await expect(page.getByTestId('approval-card').filter({ hasText: approval.title })).toBeVisible()
      }
    } else {
      await expect(page.getByText(/no approval is waiting/i)).toBeVisible()
    }

    // Widening the filter to every decision must show decided ones and never lose the pending one.
    await page.getByLabel('Approval status').selectOption('any')
    const decided = await apiGet<{ items: Array<{ decision_note: string | null }> }>(
      request,
      '/approvals?status=any',
      'wellManager',
    )
    const withNote = decided.items.find((row) => row.decision_note)
    if (withNote) {
      await expect(page.getByText(String(withNote.decision_note))).toBeVisible()
    }

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })
})

test.describe('run monitor: the run list and a real failure', () => {
  test('the list shows the runs the API returns, with their own scope and status', async ({
    page,
    request,
    consoleErrors,
  }) => {
    const listed = await apiGet<{ items: RunRow[]; total: number }>(request, '/runs?limit=50', 'supervisor')
    expect(listed.total).toBeGreaterThan(0)

    await page.goto('/runs')
    await waitForLoaded(page)
    const table = page.getByRole('table')
    for (const run of listed.items.slice(0, 5)) {
      await expect(table.getByText(run.id)).toBeVisible()
      if (run.well_id) await expect(table.getByText(run.well_id).first()).toBeVisible()
    }
    // The run count in the tab comes from the API's `total`, not from the row count of one page.
    await expect(page.getByRole('tab', { name: 'Runs' })).toContainText(String(listed.total))

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('a run that fails says which node failed and why', async ({ page, request, consoleErrors, wellId }) => {
    const { failing_workflow_id } = fixtures()
    const started = await apiPost<RunRow>(
      request,
      `/workflows/${failing_workflow_id}/runs`,
      { well_id: wellId, trigger_type: 'manual' },
      'supervisor',
    )
    expect(started.status).toBe('failed')

    await page.goto(`/runs?run=${started.id}`)
    await waitForLoaded(page)

    const envelope = await runEnvelope(request, started.id, 'supervisor')
    expect(envelope.run.status).toBe('failed')
    expect(envelope.run.error_node_id).toBeTruthy()

    await expect(page.getByTestId('run-status')).toHaveText(/failed/i)
    await expect(page.getByTestId('run-error')).toBeVisible()
    await expect(page.getByTestId('run-error')).toContainText(String(envelope.run.error_node_id))

    // The failing node is listed with a failed status; the nodes after it did not run.
    const failed = envelope.node_runs.find((row) => row.status === 'failed')
    expect(failed, 'the seeded failing workflow must fail a node').toBeTruthy()
    await expect(page.getByText(failed?.node_name ?? (failed?.node_id as string), { exact: true })).toBeVisible()
    await expect(page.getByText('Failed', { exact: true }).first()).toBeVisible()

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })
})
