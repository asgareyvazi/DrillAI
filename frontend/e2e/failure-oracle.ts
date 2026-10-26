/**
 * Runtime semantics for the Playwright failure oracle.
 *
 * This module intentionally has no Playwright imports: the same matcher used by the browser fixture
 * is exercised directly by Vitest. An allowance is an exact HTTP action, a canonical API route,
 * an exact query shape, and one exact failure condition. A response can consume at most one
 * allowance; ambiguous rules are errors, not a first-match-wins escape hatch.
 */

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

interface FailureTarget {
  /** Exact API pathname. `:name` is one non-empty route segment, never a prefix or glob. */
  pathname: string
  /** Exact query shape, including the leading `?`, or the empty string for no query. */
  query: string
}

export interface HttpFailureAllowance extends FailureTarget {
  kind: 'http'
  method: HttpMethod
  status: number
  reason: string
  /** Required maximum number of observations this one explicit contract can account for. */
  maxMatches: number
}

export interface NetworkFailureAllowance extends FailureTarget {
  kind: 'network'
  method: HttpMethod
  /** Exact Chromium transport code, e.g. `net::ERR_INTERNET_DISCONNECTED`. */
  errorText: string
  /** Required for deliberately timed-out requests; caller/navigation cancellations are not allowances. */
  signalReason?: 'TimeoutError'
  reason: string
  /** Required maximum number of observations this one explicit contract can account for. */
  maxMatches: number
}

export type FailureAllowance = HttpFailureAllowance | NetworkFailureAllowance

export interface FailedResponse {
  kind: 'http'
  status: number
  method: string
  url: string
}

export interface AbortEvidence {
  requestId: string
  url: string
  method: string
  reasonName: string
}

export interface NavigationSupersessionEvidence {
  /** The successor document request started after the request that later failed. */
  startedAfterOriginalRequest: boolean
  /** The successor request started before the browser reported the original request's cancellation. */
  startedBeforeFailure: boolean
  /** The successor request committed a navigation in the same frame. */
  committedInSameFrame: boolean
}

export type NetworkDisposition = 'caller_abort' | 'superseded_navigation' | 'unexpected'

export interface NetworkFailure {
  kind: 'network'
  method: string
  url: string
  errorText: string
  resourceType: string
  isNavigationRequest: boolean
  requestId?: string
  /** The abort signal name captured at the exact fetch that carried requestId. */
  signalReason?: string
  disposition: NetworkDisposition
}

export type ObservedFailure = FailedResponse | NetworkFailure

export interface FailureOracleResult {
  /** Human-readable findings; empty means every observation is accounted for. */
  findings: string[]
  /** Number of observations assigned to each allowance, in declaration order. */
  matchedCounts: number[]
}

const METHODS = new Set<HttpMethod>(['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
const ALLOWED_HTTP_FAILURE_STATUSES = new Set([400, 401, 403, 404, 409, 422, 428, 429, 500, 502, 503, 504])
const ROUTE_PARAMETER = /^:[A-Za-z][A-Za-z0-9_]*$/
const ROUTE_SEGMENT = /^[A-Za-z0-9._~-]+$/
const NETWORK_ERROR = /^net::ERR_[A-Z0-9_]+$/

function parsedTarget(pathname: string, query: string): { pathSegments: string[]; queryEntries: Array<[string, string]> } | null {
  if (
    !pathname.startsWith('/api/v1/') ||
    pathname.includes('//') ||
    pathname.includes('\\') ||
    pathname.includes('?') ||
    pathname.includes('#') ||
    /%2f|%5c/i.test(pathname)
  ) return null

  const pathSegments = pathname.slice(1).split('/')
  if (pathSegments.some((segment) => !segment || segment === '.' || segment === '..' || (!ROUTE_PARAMETER.test(segment) && !ROUTE_SEGMENT.test(segment)))) {
    return null
  }
  if (query !== '' && (!query.startsWith('?') || query.includes('#') || query.includes('*'))) return null

  if (query === '?') return null
  const params = new URLSearchParams(query ? query.slice(1) : '')
  const queryEntries = [...params.entries()]
  if (queryEntries.some(([key, value]) => !key || key.includes('*') || value.includes('*') || ROUTE_PARAMETER.test(value))) return null
  return { pathSegments, queryEntries }
}

function allowanceValidationErrors(value: unknown, index: number): string[] {
  const prefix = `allowance ${index}`
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return [`${prefix} must be a structured object`]
  }
  const entry = value as Record<string, unknown>
  const errors: string[] = []
  const httpKeys = new Set(['kind', 'method', 'pathname', 'query', 'status', 'reason', 'maxMatches'])
  const networkKeys = new Set(['kind', 'method', 'pathname', 'query', 'errorText', 'signalReason', 'reason', 'maxMatches'])
  const permittedKeys = entry.kind === 'http' ? httpKeys : networkKeys
  for (const key of Object.keys(entry)) {
    if (!permittedKeys.has(key)) errors.push(`${prefix} contains unsupported field ${key}`)
  }
  if (entry.kind !== 'http' && entry.kind !== 'network') errors.push(`${prefix} needs kind "http" or "network"`)
  if (typeof entry.method !== 'string' || !METHODS.has(entry.method as HttpMethod)) {
    errors.push(`${prefix} needs one exact HTTP method`)
  }
  if (typeof entry.pathname !== 'string' || typeof entry.query !== 'string') {
    errors.push(`${prefix} needs a canonical pathname and exact query string`)
  } else if (!parsedTarget(entry.pathname, entry.query)) {
    errors.push(`${prefix} has a blank, wildcard, non-API, or non-canonical target`)
  }
  if (typeof entry.reason !== 'string' || entry.reason.trim().length < 20) {
    errors.push(`${prefix} needs a meaningful reason of at least 20 non-whitespace characters`)
  }
  if (!Number.isSafeInteger(entry.maxMatches) || Number(entry.maxMatches) < 1) {
    errors.push(`${prefix} needs a positive safe-integer maxMatches bound`)
  }
  if (entry.kind === 'http') {
    if (!Number.isInteger(entry.status) || !ALLOWED_HTTP_FAILURE_STATUSES.has(Number(entry.status))) {
      errors.push(`${prefix} needs one explicitly listed HTTP failure status`)
    }
  } else if (entry.kind === 'network') {
    if (typeof entry.errorText !== 'string' || !NETWORK_ERROR.test(entry.errorText)) {
      errors.push(`${prefix} needs one exact Chromium network error code`)
    }
    if (entry.signalReason !== undefined && entry.signalReason !== 'TimeoutError') {
      errors.push(`${prefix} has an unsupported signal reason`)
    }
    if (entry.errorText === 'net::ERR_ABORTED' && entry.signalReason !== 'TimeoutError') {
      errors.push(`${prefix} may allow ERR_ABORTED only when the same request carries a TimeoutError signal`)
    }
  }
  return errors
}

/** Reject wildcard, blank, broad-prefix, unscoped, or otherwise non-meaningful tolerances at runtime. */
export function validateFailureAllowances(allowances: readonly FailureAllowance[]): string[] {
  if (!Array.isArray(allowances)) return ['allowances must be an array']
  return allowances.flatMap((entry, index) => allowanceValidationErrors(entry, index))
}

function matchesSegments(pattern: string[], actual: string[]): boolean {
  return pattern.length === actual.length && pattern.every((segment, index) => {
    const value = actual[index]
    if (value === undefined || value === '' || value === '.' || value === '..') return false
    return ROUTE_PARAMETER.test(segment) ? ROUTE_SEGMENT.test(value) : segment === value
  })
}

function matchesQuery(pattern: Array<[string, string]>, actual: Array<[string, string]>): boolean {
  if (pattern.length !== actual.length) return false
  const remaining = [...actual]
  for (const [key, expected] of pattern) {
    const index = remaining.findIndex(([actualKey, actualValue]) => actualKey === key && actualValue === expected)
    if (index < 0) return false
    remaining.splice(index, 1)
  }
  return remaining.length === 0
}

function allowanceMatches(entry: FailureAllowance, failure: ObservedFailure): boolean {
  if (entry.kind !== failure.kind || entry.method !== failure.method) return false
  let url: URL
  try {
    url = new URL(failure.url)
  } catch {
    return false
  }
  const target = parsedTarget(entry.pathname, entry.query)
  if (!target) return false
  const actualSegments = url.pathname.slice(1).split('/')
  const actualQuery = [...new URLSearchParams(url.search).entries()]
  if (!matchesSegments(target.pathSegments, actualSegments) || !matchesQuery(target.queryEntries, actualQuery)) return false

  if (entry.kind === 'http') {
    return failure.kind === 'http' && failure.status === entry.status
  }
  return failure.kind === 'network' && failure.errorText === entry.errorText &&
    (entry.signalReason === undefined || failure.signalReason === entry.signalReason)
}

/**
 * Match network request failures against lifecycle evidence. Chromium's `ERR_ABORTED` string alone
 * is never sufficient: the request must carry the unique test-only ID and the same fetch signal must
 * have emitted a caller `AbortError`; or a newer same-frame navigation must have started before the
 * failure and actually committed.
 */
export function classifyNetworkFailure(
  failure: Pick<NetworkFailure, 'method' | 'url' | 'errorText' | 'requestId'>,
  abortEvidence?: AbortEvidence,
  navigationEvidence?: NavigationSupersessionEvidence,
): NetworkDisposition {
  if (failure.errorText !== 'net::ERR_ABORTED') return 'unexpected'
  if (
    abortEvidence && failure.requestId && abortEvidence.requestId === failure.requestId &&
    abortEvidence.method === failure.method && abortEvidence.url === failure.url &&
    abortEvidence.reasonName === 'AbortError'
  ) return 'caller_abort'
  if (
    navigationEvidence?.startedAfterOriginalRequest === true &&
    navigationEvidence.startedBeforeFailure === true &&
    navigationEvidence.committedInSameFrame === true
  ) return 'superseded_navigation'
  return 'unexpected'
}

function safeFailureLabel(failure: ObservedFailure): string {
  const url = new URL(failure.url)
  for (const key of [...url.searchParams.keys()]) {
    if (/(token|secret|password|csrf|credential|authorization)/i.test(key)) url.searchParams.set(key, '[redacted]')
  }
  return failure.kind === 'http'
    ? `${failure.method} ${url.href} -> HTTP ${failure.status}`
    : `${failure.method} ${url.href} -> ${failure.errorText} (${failure.resourceType})`
}

/**
 * Evaluate observations one-to-one. A single response cannot be consumed by overlapping tolerances;
 * an explicit maxMatches is the only way one tolerance can account for more than one failure.
 */
export function evaluateFailureOracle(
  responses: readonly FailedResponse[],
  networkFailures: readonly NetworkFailure[],
  allowances: readonly FailureAllowance[],
): FailureOracleResult {
  const findings = validateFailureAllowances(allowances)
  const safeAllowances: readonly FailureAllowance[] = Array.isArray(allowances) ? allowances : []
  const matchedCounts = safeAllowances.map(() => 0)
  if (findings.length > 0) return { findings, matchedCounts }

  const observations: ObservedFailure[] = [
    ...responses.filter((failure) => failure.status >= 400),
    ...networkFailures.filter((failure) => failure.disposition === 'unexpected'),
  ]

  for (const failure of observations) {
    const matching = safeAllowances
      .map((allowance, index) => ({ allowance, index }))
      .filter(({ allowance, index }) => {
        const maxMatches = allowance.maxMatches
        const usedMatches = matchedCounts[index]
        return usedMatches !== undefined && usedMatches < maxMatches && allowanceMatches(allowance, failure)
      })
    if (matching.length === 0) {
      findings.push(`unprovoked ${safeFailureLabel(failure)}`)
    } else if (matching.length > 1) {
      findings.push(`ambiguous tolerances (${matching.map(({ index }) => index).join(', ')}) for ${safeFailureLabel(failure)}`)
    } else {
      const matched = matching[0]
      if (matched === undefined) {
        findings.push('internal oracle invariant: one matching tolerance disappeared')
      } else {
        matchedCounts[matched.index] = (matchedCounts[matched.index] ?? 0) + 1
      }
    }
  }
  return { findings, matchedCounts }
}
