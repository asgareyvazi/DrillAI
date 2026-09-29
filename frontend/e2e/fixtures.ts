/**
 * Shared helpers for the end-to-end suite.
 *
 * Everything the specs need comes from two places: the fixture identifiers the seed produced
 * (`.e2e/fixtures.json`) and the running API. Assertions therefore use real identifiers and real
 * values, never copies pasted into the test — a test that hard-codes "8.5" would pass even if the
 * product stopped computing it.
 */

import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test as base, type APIRequestContext, type Locator, type Page } from '@playwright/test'
import type { E2EFixtures } from './global-setup'

const here = path.dirname(fileURLToPath(import.meta.url))
const FIXTURES_PATH = path.join(path.resolve(here, '..'), '.e2e', 'fixtures.json')

/**
 * The seeded workflow and well, as the server under test reports them.
 *
 * The main suite can read `.e2e/fixtures.json` because its API is the one the runner seeded. The
 * bundles that run beside it — the fault-injection stack and the authentication-enabled stack — have
 * their own databases, so their journeys ask the API they are talking to instead of trusting a file
 * that belongs to a different server.
 */
export async function seededIds(
  request: APIRequestContext,
): Promise<{ workflowId: string; wellId: string }> {
  const workflows = await apiGet<{ items: { id: string; name: string }[] }>(request, '/workflows', 'engineer')
  const wells = await apiGet<{ items: { id: string; name: string }[] }>(request, '/wells', 'engineer')
  const workflow = workflows.items.find((row) => row.name === 'Daily Drilling Intelligence')
  const well = wells.items.find((row) => row.name.startsWith('SYNTH-DEMO-01'))
  expect(workflow, 'the seed must have produced the drilling workflow').toBeTruthy()
  expect(well, 'the seed must have produced the synthetic well').toBeTruthy()
  return { workflowId: workflow?.id as string, wellId: well?.id as string }
}

/** Development identity presets. The E2E API runs with authentication disabled; see start-api.mjs. */
export const ROLES = {
  engineer: 'engineer',
  viewer: 'viewer',
  supervisor: 'drilling_supervisor',
  // The decider in the approval journeys: a different principal than the one that starts the run,
  // because the server refuses a decision taken by the requester.
  wellManager: 'well_manager',
  admin: 'admin',
} as const

export type RoleKey = keyof typeof ROLES

let cached: E2EFixtures | null = null

export function fixtures(): E2EFixtures {
  if (cached === null) {
    cached = JSON.parse(readFileSync(FIXTURES_PATH, 'utf8')) as E2EFixtures
  }
  return cached
}

/** The well every well-scoped journey uses. */
export function wellPath(suffix = ''): string {
  return `/wells/${fixtures().well_id}${suffix}`
}

/** Read a value straight from the API so a spec can compare the UI with the backend, not with itself. */
export async function apiGet<T>(
  request: APIRequestContext,
  pathname: string,
  role: RoleKey = 'engineer',
): Promise<T> {
  const response = await request.get(`/api/v1${pathname}`, {
    headers: { 'X-Dev-Roles': ROLES[role] },
  })
  expect(response.ok(), `GET /api/v1${pathname} -> ${response.status()}`).toBeTruthy()
  return (await response.json()) as T
}

export async function apiPost<T>(
  request: APIRequestContext,
  pathname: string,
  body: unknown,
  role: RoleKey = 'engineer',
): Promise<T> {
  const response = await request.post(`/api/v1${pathname}`, {
    headers: { 'X-Dev-Roles': ROLES[role] },
    data: body,
  })
  expect(response.ok(), `POST /api/v1${pathname} -> ${response.status()}`).toBeTruthy()
  return (await response.json()) as T
}

/**
 * The suite's own `test`, extended with the two things every journey needs.
 *
 * `consoleErrors` exists because a page that renders its values while throwing in the background is
 * not working; collecting the list in a fixture means a spec has to assert on it rather than being
 * able to ignore it. `wellId` comes from the seed rather than from a literal, so the journey still
 * breaks if the seeder stops producing a well a user can open.
 */
export const test = base.extend<{ consoleErrors: string[]; wellId: string }>({
  consoleErrors: async ({ page }, use) => {
    const errors: string[] = []
    page.on('console', (message) => {
      if (message.type() === 'error') errors.push(message.text())
    })
    page.on('pageerror', (error) => errors.push(error.message))
    await use(errors)
  },
  // Playwright requires the first argument to be a destructuring pattern, and this fixture has no
  // dependencies; the empty pattern is the library's documented form.
  // eslint-disable-next-line no-empty-pattern
  wellId: async ({}, use) => {
    await use(fixtures().well_id)
  },
})

export { expect } from '@playwright/test'

/**
 * Wait until a page has finished its first data load.
 *
 * Every screen renders a `status` region while a query is in flight and an `alert` when it fails, so
 * "settled" means: no status region and no alert region left. A spec that skipped this would assert
 * against the loading state and pass for the wrong reason.
 */
export async function waitForLoaded(page: Page): Promise<void> {
  await page.getByRole('heading', { level: 1 }).first().waitFor({ state: 'visible' })
  await expect(page.getByRole('status').filter({ hasText: 'Loading' })).toHaveCount(0)
}

/**
 * Switch the development identity the UI sends with each request.
 *
 * The option values are the *dev role strings* the app will send as `X-Dev-Roles`, which is what
 * `ROLES` maps a short role name to — 'supervisor' is the identity `drilling_supervisor`, not a role
 * literally called 'supervisor'.
 */
export async function selectRole(page: Page, role: RoleKey): Promise<void> {
  const select = page.getByLabel('Acting as')
  const value = ROLES[role]
  await select.selectOption(value)
  await expect(select).toHaveValue(value)
}

/** Switch the display unit system. */
export async function selectUnits(page: Page, units: 'si' | 'oilfield'): Promise<void> {
  const select = page.getByLabel(/Units|Detail/)
  await select.selectOption(units)
}

/**
 * The label in front of a value, as the cockpit renders it: the label and value live in the same
 * element, so the assertion can stay close to what a user reads.
 */
export function cardWithText(page: Page, text: string): Locator {
  return page.locator('div').filter({ hasText: text }).last()
}

/**
 * Start a run of a workflow from the studio, the way an operator does, and return its id.
 *
 * The studio owns the identity switch to the supervisor (the role that may run a workflow), the well
 * it is run against, and the navigation to the run: a journey that started a run some other way would
 * be testing a path no user has.
 */
export async function startRunFromStudio(page: Page, workflowId: string, wellId: string): Promise<string> {
  await page.goto(`/workflows?workflow=${workflowId}`)
  await waitForLoaded(page)
  await selectRole(page, 'supervisor')
  await page.getByLabel('Run context').selectOption(wellId)
  await page.getByRole('button', { name: 'Start run' }).click()
  await expect(page).toHaveURL(/\/runs\?run=/)
  const runId = new URL(page.url()).searchParams.get('run')
  expect(runId, 'the studio must navigate to the run it started').toBeTruthy()
  return runId as string
}

/**
 * Approve a run's gate so a journey leaves no pending approval behind.
 *
 * The suite shares one seeded database, and an approval left pending is not inert: it appears in the
 * inbox the next journey asserts on. A test that changes shared state cleans up after itself.
 */
export async function clearPendingApproval(
  request: APIRequestContext,
  runId: string,
  note: string,
): Promise<void> {
  const envelope = await apiGet<{ run: { status: string }; pending_approval?: { id: string } | null }>(
    request,
    `/runs/${runId}`,
    'supervisor',
  )
  const pending = envelope.pending_approval
  if (envelope.run.status !== 'waiting_approval' || !pending) return
  await apiPost(
    request,
    `/approvals/${pending.id}/decide`,
    { decision: 'approved', note, resume: true },
    'wellManager',
  )
}

/**
 * The console errors the *application* produced.
 *
 * A browser logs every response with a failing status, and every aborted connection, as a console
 * error by itself — on the stacks that produce those on purpose, that line is the harness talking,
 * not the product. Anything else (a React error, an unhandled rejection, a thrown exception) still
 * counts, which is what the check is for.
 */
export function appConsoleErrors(errors: string[]): string[] {
  return errors.filter((text) => !/Failed to load resource/.test(text))
}
