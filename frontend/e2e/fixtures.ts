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

/** Development identity presets. The E2E API runs with authentication disabled; see start-api.mjs. */
export const ROLES = {
  engineer: 'engineer',
  viewer: 'viewer',
  supervisor: 'drilling_supervisor',
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

/** Switch the development identity the UI sends with each request. */
export async function selectRole(page: Page, role: RoleKey): Promise<void> {
  const select = page.getByLabel('Acting as')
  await select.selectOption(role)
  await expect(select).toHaveValue(role)
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
