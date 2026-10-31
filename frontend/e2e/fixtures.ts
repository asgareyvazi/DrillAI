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
import { expect, test as base, type APIRequestContext, type Frame, type Page, type Request, type TestInfo } from '@playwright/test'
import {
  classifyNetworkFailure,
  evaluateFailureOracle,
  safeNetworkFailureEvidence,
  safeFailureText,
  safeFailureUrl,
  type AbortEvidence,
  type FailureAllowance,
  type FailedResponse,
  type NetworkFailure,
  type NavigationSupersessionEvidence,
} from './failure-oracle'
import type { E2EFixtures } from './global-setup'

export type { FailedResponse, FailureAllowance, NetworkFailure } from './failure-oracle'

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
  // The remaining catalogued roles. They are here because the switcher offers the server's
  // `development_presets`, and a journey that cannot name a role cannot check that it is offered.
  integrityEngineer: 'integrity_engineer',
  dataManager: 'data_manager',
  auditor: 'auditor',
  // The decider in the approval journeys: a different principal than the one that starts the run,
  // because the server refuses a decision taken by the requester.
  wellManager: 'well_manager',
  admin: 'admin',
} as const

export type RoleKey = keyof typeof ROLES

/**
 * Every role the backend catalogues, in the order the development presets list them.
 *
 * A journey walks this list rather than a subset it happens to use: the defect this guards against is
 * a frontend list that had drifted from the server's, leaving catalogued roles unreachable from the
 * interface. A role added to the backend shows up here as a failing journey, which is the point.
 */
export const CATALOGUED_ROLES: RoleKey[] = [
  'viewer',
  'engineer',
  'supervisor',
  'integrityEngineer',
  'wellManager',
  'dataManager',
  'auditor',
  'admin',
]

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

/**
 * The request context the API helpers take.
 *
 * A spec that writes its own helper needs the type of that first argument. Naming it here keeps
 * every spec from re-deriving it — and from re-deriving it wrongly, which is what an index into
 * `Parameters<typeof apiGet>` does: index 0 is the request context, index 1 is the path.
 */
export type ApiRequest = Parameters<typeof apiGet>[0]

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
 * A mutation that a journey expects to be *refused*.
 *
 * `apiPost`/`apiPatch` assert success because a journey that expects a write to work should fail loudly
 * when it does not. This one is the opposite: it sends the request and returns what the server said,
 * including the taxonomy code — which is how a journey proves that a refusal came from the backend
 * rather than from a button the frontend chose to disable.
 */
export async function apiAttempt(
  request: APIRequestContext,
  method: 'POST' | 'PATCH',
  pathname: string,
  options: { body?: unknown; role?: RoleKey; headers?: Record<string, string> } = {},
): Promise<{ status: number; code: string | null; retryable: boolean | null; message: string | null }> {
  const response = await request.fetch(`/api/v1${pathname}`, {
    method,
    headers: { 'X-Dev-Roles': ROLES[options.role ?? 'engineer'], ...(options.headers ?? {}) },
    data: options.body,
  })
  const payload = (await response.json().catch(() => null)) as
    | { error?: { code?: string; retryable?: boolean; message?: string } }
    | null
  return {
    status: response.status(),
    code: payload?.error?.code ?? null,
    retryable: payload?.error?.retryable ?? null,
    message: payload?.error?.message ?? null,
  }
}

export async function apiPatch<T>(
  request: APIRequestContext,
  pathname: string,
  body: unknown,
  role: RoleKey = 'engineer',
): Promise<T> {
  const response = await request.patch(`/api/v1${pathname}`, {
    headers: { 'X-Dev-Roles': ROLES[role] },
    data: body,
  })
  expect(response.ok(), `PATCH /api/v1${pathname} -> ${response.status()}`).toBeTruthy()
  return (await response.json()) as T
}

const ABORT_EVIDENCE_BINDING = '__drillaiReportE2EAbort'

interface FailureOracleState {
  consoleErrors: string[]
  failedResponses: FailedResponse[]
  networkFailures: NetworkFailure[]
  allowances: FailureAllowance[]
  applicationOrigin: string
  abortEvidence: Map<string, AbortEvidence>
}

type AbortEvidencePayload = AbortEvidence

interface FailedRequestTrace {
  request: Request
  record: NetworkFailure
  sequence: number
  failureSequence: number
}

const oracleByResponses = new WeakMap<FailedResponse[], FailureOracleState>()

function isAbortEvidencePayload(value: unknown): value is AbortEvidencePayload {
  if (typeof value !== 'object' || value === null) return false
  const candidate = value as Record<string, unknown>
  return typeof candidate.requestId === 'string' && candidate.requestId.length > 0 &&
    typeof candidate.url === 'string' && typeof candidate.method === 'string' &&
    typeof candidate.reasonName === 'string'
}

function navigationForRequest(
  request: Request,
  sequence: number,
  failureSequence: number,
  latestNavigationByFrame: Map<Frame, Request>,
  requestSequences: WeakMap<Request, number>,
  committedNavigations: WeakSet<Request>,
  requestFrames: WeakMap<Request, Frame>,
): NavigationSupersessionEvidence | undefined {
  const failedFrame = requestFrames.get(request)
  if (!failedFrame) return undefined
  const successor = latestNavigationByFrame.get(failedFrame)
  if (!successor || successor === request) return undefined
  const successorSequence = requestSequences.get(successor)
  return {
    startedAfterOriginalRequest: successorSequence !== undefined && successorSequence > sequence,
    startedBeforeFailure: successorSequence !== undefined && successorSequence < failureSequence,
    committedInSameFrame: committedNavigations.has(successor) && requestFrames.get(successor) === failedFrame,
  }
}

async function assertFailureOracle(state: FailureOracleState, testInfo: TestInfo): Promise<void> {
  // Let the browser-to-Node binding and request lifecycle events already queued by the test drain.
  await new Promise<void>((resolve) => setTimeout(resolve, 0))
  const responseResult = evaluateFailureOracle(
    state.failedResponses,
    state.networkFailures,
    state.allowances,
    state.applicationOrigin,
  )
  const consoleErrors = appConsoleErrors(state.consoleErrors).map(safeFailureText)
  if (consoleErrors.length > 0 || responseResult.findings.length > 0) {
    const evidence = {
      project: testInfo.project.name,
      test: testInfo.title,
      consoleErrors,
      failures: responseResult.findings,
      httpResponses: state.failedResponses.filter((response) => response.status >= 400).map((response) => ({
        method: response.method,
        url: safeFailureUrl(response.url),
        status: response.status,
      })),
      networkFailures: state.networkFailures
        .filter((failure) => failure.disposition === 'unexpected')
        .map(safeNetworkFailureEvidence),
    }
    await testInfo.attach('request-failure-oracle.json', {
      body: JSON.stringify(evidence, null, 2),
      contentType: 'application/json',
    })
  }
  expect(consoleErrors, `application console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  expect(responseResult.findings, 'every HTTP or transport failure must be explicitly accounted for').toEqual([])
}

/**
 * The suite's test fixture owns the wire oracle for every journey, including specs that do not
 * explicitly destructure the arrays. All pages in the test context are observed; a `context.newPage()`
 * cannot escape the verdict. Browser failures are classified from request lifecycle evidence, never
 * from Chromium's platform-specific console wording.
 */
export const test = base.extend<{
  consoleErrors: string[]
  failedResponses: FailedResponse[]
  networkFailures: NetworkFailure[]
  wellId: string
  _failureOracle: FailureOracleState
}>({
  _failureOracle: [
    async ({ page, context }, use, testInfo) => {
      if (!context.pages().includes(page)) throw new Error('the fixture page must belong to its observed context')
      const baseURL = testInfo.project.use.baseURL
      if (typeof baseURL !== 'string') throw new Error('the failure oracle requires an explicit project baseURL')
      const applicationOrigin = new URL(baseURL).origin
      const state: FailureOracleState = {
        consoleErrors: [],
        failedResponses: [],
        networkFailures: [],
        allowances: [],
        applicationOrigin,
        abortEvidence: new Map(),
      }
      oracleByResponses.set(state.failedResponses, state)

      let lifecycleSequence = 0
      const requestSequences = new WeakMap<Request, number>()
      const requestFrames = new WeakMap<Request, Frame>()
      const latestNavigationByFrame = new Map<Frame, Request>()
      const committedNavigations = new WeakSet<Request>()
      const failedTraces: FailedRequestTrace[] = []
      const updateTraceDisposition = (trace: FailedRequestTrace) => {
        const evidence = trace.record.requestId
          ? state.abortEvidence.get(trace.record.requestId)
          : undefined
        if (evidence && evidence.url === trace.record.url && evidence.method === trace.record.method) {
          trace.record.signalReason = evidence.reasonName
        }
        const navigationEvidence = navigationForRequest(
          trace.request,
          trace.sequence,
          trace.failureSequence,
          latestNavigationByFrame,
          requestSequences,
          committedNavigations,
          requestFrames,
        )
        trace.record.disposition = classifyNetworkFailure(trace.record, evidence, navigationEvidence)
      }

      await context.exposeBinding(ABORT_EVIDENCE_BINDING, (_source, payload: unknown) => {
        if (typeof payload === 'object' && payload !== null && (payload as { kind?: unknown }).kind === 'flush') return true
        if (isAbortEvidencePayload(payload)) {
          state.abortEvidence.set(payload.requestId, payload)
          for (const trace of failedTraces) {
            if (trace.record.requestId === payload.requestId) updateTraceDisposition(trace)
          }
          return true
        }
        return false
      })
      await context.addInitScript((bindingName: string) => {
        const originalFetch = window.fetch.bind(window)
        const documentTag = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
        let requestSequence = 0
        window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
          const sourceUrl = input instanceof Request ? input.url : String(input)
          const target = new URL(sourceUrl, window.location.href)
          const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase()
          // The E2E servers use Vite's same-origin `/api/v1` proxy. The marker is test-only and adds
          // no cross-origin preflight; it lets requestfailed be joined to the *same* fetch's signal.
          if (target.origin !== window.location.origin || !target.pathname.startsWith('/api/v1/')) {
            return originalFetch(input, init)
          }
          const requestId = `e2e-${documentTag}-${++requestSequence}`
          const headers = new Headers(input instanceof Request ? input.headers : undefined)
          if (init?.headers) new Headers(init.headers).forEach((value, key) => headers.set(key, value))
          headers.set('x-drillai-e2e-request-id', requestId)
          const nextInit: RequestInit = { ...(init ?? {}), headers }
          const signal = nextInit.signal ?? (input instanceof Request ? input.signal : undefined)
          if (signal) {
            const reportAbort = () => {
              const reason: unknown = signal.reason
              const reasonName = typeof reason === 'object' && reason !== null && 'name' in reason &&
                typeof (reason as { name?: unknown }).name === 'string'
                ? (reason as { name: string }).name
                : 'UnknownAbortReason'
              const binding = (window as typeof window & Record<string, unknown>)[bindingName]
              if (typeof binding === 'function') {
                void Promise.resolve(binding({ requestId, url: target.href, method, reasonName })).catch(() => undefined)
              }
            }
            if (signal.aborted) reportAbort()
            else signal.addEventListener('abort', reportAbort, { once: true })
          }
          return originalFetch(input, nextInit)
        }
      }, ABORT_EVIDENCE_BINDING)


      const collectPageErrors = (observedPage: Page) => {
        observedPage.on('console', (message) => {
          if (message.type() === 'error') state.consoleErrors.push(message.text())
        })
        observedPage.on('pageerror', (error) => state.consoleErrors.push(error.message))
        observedPage.on('framenavigated', (frame) => {
          const latest = latestNavigationByFrame.get(frame)
          if (latest && frame.url() === latest.url()) {
            committedNavigations.add(latest)
            for (const trace of failedTraces) {
              if (requestFrames.get(trace.request) === frame) updateTraceDisposition(trace)
            }
          }
        })
      }
      for (const openPage of context.pages()) collectPageErrors(openPage)
      context.on('page', collectPageErrors)

      context.on('request', (request) => {
        lifecycleSequence += 1
        requestSequences.set(request, lifecycleSequence)
        try {
          const frame = request.frame()
          requestFrames.set(request, frame)
          if (request.isNavigationRequest()) {
            latestNavigationByFrame.set(frame, request)
            for (const trace of failedTraces) {
              if (requestFrames.get(trace.request) === frame) updateTraceDisposition(trace)
            }
          }
        } catch {
          // A detached-frame request has no same-frame supersession evidence and remains unexpected.
        }
      })
      context.on('response', (response) => {
        if (response.status() >= 400) {
          state.failedResponses.push({
            kind: 'http',
            status: response.status(),
            method: response.request().method(),
            url: response.url(),
          })
        }
      })
      context.on('requestfailed', (request) => {
        lifecycleSequence += 1
        const failureSequence = lifecycleSequence
        const record: NetworkFailure = {
          kind: 'network',
          method: request.method(),
          url: request.url(),
          errorText: request.failure()?.errorText ?? 'unknown network failure',
          resourceType: request.resourceType(),
          isNavigationRequest: request.isNavigationRequest(),
          disposition: 'unexpected',
        }
        const requestId = request.headers()['x-drillai-e2e-request-id']
        if (requestId) record.requestId = requestId
        const sequence = requestSequences.get(request) ?? failureSequence
        state.networkFailures.push(record)
        const trace = { request, record, sequence, failureSequence }
        failedTraces.push(trace)
        updateTraceDisposition(trace)
      })

      await use(state)
      // An awaited binding call is a per-page event-loop fence: abort reports issued before the end
      // of the body must reach Node before the request failures are classified.
      for (const observedPage of context.pages()) {
        if (observedPage.isClosed()) continue
        try {
          await observedPage.evaluate(async (bindingName: string) => {
            const binding = (window as typeof window & Record<string, unknown>)[bindingName]
            if (typeof binding === 'function') await binding({ kind: 'flush' })
          }, ABORT_EVIDENCE_BINDING)
        } catch {
          // A page closed or began navigating after the body; absence of abort evidence is not benign.
        }
      }
      await new Promise<void>((resolve) => setTimeout(resolve, 0))
      for (const trace of failedTraces) updateTraceDisposition(trace)
      await assertFailureOracle(state, testInfo)
    },
    { auto: true },
  ],
  consoleErrors: async ({ _failureOracle }, use) => use(_failureOracle.consoleErrors),
  failedResponses: async ({ _failureOracle }, use) => use(_failureOracle.failedResponses),
  networkFailures: async ({ _failureOracle }, use) => use(_failureOracle.networkFailures),
  // Playwright requires the first argument to be a destructuring pattern, and this fixture has no
  // dependencies; the empty pattern is the library's documented form.
  // eslint-disable-next-line no-empty-pattern
  wellId: async ({}, use) => {
    await use(fixtures().well_id)
  },
})

export { expect } from '@playwright/test'
export type { Page } from '@playwright/test'

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
 *
 * The control is found by its test handle, not by its label: the label is translated, so a journey
 * that switches identity while the interface is Persian — which is a thing a Persian user does — would
 * fail to find the control at all with a label lookup. The handle is the same in both locales.
 */
export async function selectRole(page: Page, role: RoleKey): Promise<void> {
  const select = page.getByTestId('shell-role-switch')
  const value = ROLES[role]
  await select.selectOption(value)
  await expect(select).toHaveValue(value)
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
 *
 * The line itself is not a reliable oracle for *what* failed: Chromium words a cancelled request as
 * `net::ERR_ABORTED` on one platform and as a dropped connection on another, and the application
 * shell cancels its own background reads whenever the reader navigates. A journey that needs to know
 * whether a particular call failed asserts on `failedResponses` instead, which is the response and
 * therefore the same on every platform.
 */
export function appConsoleErrors(errors: string[]): string[] {
  return errors.filter((text) => !/Failed to load resource/.test(text))
}

/**
 * Assert this journey's console and wire observations and register its narrow, justified tolerances.
 *
 * The `auto: true` oracle fixture repeats the verdict after the test body. This immediate check gives
 * the journey a local failure message; the teardown check ensures a test cannot omit this call or
 * stop collecting responses/request failures and thereby pass silently.
 */
export function expectNoFailures(
  consoleErrors: string[],
  failedResponses: FailedResponse[],
  options: { allowed?: readonly FailureAllowance[] } = {},
): void {
  const state = oracleByResponses.get(failedResponses)
  if (!state) throw new Error('expectNoFailures must receive the shared Playwright fixture arrays')
  if (state.consoleErrors !== consoleErrors) {
    throw new Error('consoleErrors must come from the shared Playwright fixture')
  }
  state.allowances.push(...(options.allowed ?? []))
  const result = evaluateFailureOracle(
    state.failedResponses,
    [],
    state.allowances,
    state.applicationOrigin,
  )
  const safeConsoleErrors = appConsoleErrors(consoleErrors).map(safeFailureText)
  expect(
    safeConsoleErrors,
    `application console errors: ${safeConsoleErrors.join(' | ')}`,
  ).toEqual([])
  expect(result.findings, 'every HTTP or transport failure must be explicitly accounted for').toEqual([])
}
