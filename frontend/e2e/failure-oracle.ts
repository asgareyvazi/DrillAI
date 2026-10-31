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
const SENSITIVE_PATH_LABEL = /^(?:tokens?|secrets?|passwords?|passphrases?|csrf|credentials?|authorizations?|auth|api[_-]?keys?|access[_-]?keys?|private[_-]?keys?|sessions?|cookies?|jwts?|signatures?|sigs?|codes?|assertions?|reset|password[-_]reset|reset[-_]token|invite[-_]code|magic[-_]link)$/i
const SENSITIVE_PATH_INLINE_VALUE = /^(?:token|secret|password|passphrase|csrf|credential|authorization|api[_-]?key|access[_-]?key|private[_-]?key|session|cookie|jwt|signature|sig|code|assertion)[=:].+$/i
const SENSITIVE_PATH_PREFIX_VALUE = /^(?:token|secret|password|passphrase|csrf|credential|authorization|api[_-]?key|access[_-]?key|private[_-]?key|session|cookie|jwt|signature|sig|code|assertion)[_-][A-Za-z0-9._~-]+$/i
const ENCODED_PATH_SEPARATOR = /%(?:25)*(?:2f|5c)/i
const JWT_PATH_VALUE = /^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$/

function parsedTarget(pathname: string, query: string): { pathSegments: string[]; queryEntries: Array<[string, string]> } | null {
  if (
    !pathname.startsWith('/api/v1/') ||
    pathname.includes('//') ||
    pathname.includes('\\') ||
    pathname.includes('?') ||
    pathname.includes('#') ||
    ENCODED_PATH_SEPARATOR.test(pathname)
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

function allowanceMatches(
  entry: FailureAllowance,
  failure: ObservedFailure,
  applicationOrigin: string,
): boolean {
  if (entry.kind !== failure.kind || entry.method !== failure.method) return false
  let url: URL
  try {
    url = new URL(failure.url)
  } catch {
    return false
  }
  if (url.origin !== applicationOrigin) return false
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

export function safeFailureUrl(rawUrl: string): string {
  try {
    const url = new URL(rawUrl)
    // URL.href retains userinfo unless explicitly cleared; never put URL credentials into an oracle
    // finding, attachment, or CI log.
    url.username = ''
    url.password = ''
    url.hash = ''
    const pathSegments = url.pathname.split('/')
    let redactNextPathSegment = false
    let pathChanged = false
    for (let index = 0; index < pathSegments.length; index += 1) {
      const segment = pathSegments[index] ?? ''
      if (!segment) continue
      let decoded = segment
      let encodedSeparator = false
      // Percent-encoded separators can be nested more than once. Each successful decode shortens the
      // segment, so the original length is a hard upper bound without an arbitrary depth cutoff.
      for (let decodePass = 0; decodePass <= segment.length; decodePass += 1) {
        if (ENCODED_PATH_SEPARATOR.test(decoded)) {
          encodedSeparator = true
          break
        }
        try {
          const next = decodeURIComponent(decoded)
          if (next === decoded) break
          decoded = next
        } catch {
          // Keep malformed public route text, but never let it interfere with checks on other segments.
          break
        }
      }
      if (encodedSeparator || decoded.includes('/') || decoded.includes('\\')) {
        // Encoded path separators make segment boundaries ambiguous; discard the remainder rather
        // than risk logging a secret that the server may interpret as the next path parameter.
        pathSegments.fill('%5Bredacted%5D', index)
        pathChanged = true
        break
      }
      if (redactNextPathSegment) {
        pathSegments[index] = '%5Bredacted%5D'
        pathChanged = true
        // A redacted segment can itself be a credential label (for example password/reset/token).
        // Keep following the chain so masking the route word does not expose the value after it.
        redactNextPathSegment = SENSITIVE_PATH_LABEL.test(decoded)
        continue
      }
      const sensitiveLabel = SENSITIVE_PATH_LABEL.test(decoded)
      if (
        !sensitiveLabel && (
          SENSITIVE_PATH_INLINE_VALUE.test(decoded) ||
          SENSITIVE_PATH_PREFIX_VALUE.test(decoded) ||
          JWT_PATH_VALUE.test(decoded)
        )
      ) {
        pathSegments[index] = '%5Bredacted%5D'
        pathChanged = true
        continue
      }
      if (sensitiveLabel) redactNextPathSegment = true
    }
    if (pathChanged) url.pathname = pathSegments.join('/')
    const sensitiveQueryKey = /(token|secret|password|passphrase|csrf|credential|authorization|auth|api[_-]?key|access[_-]?key|private[_-]?key|session|cookie|jwt|signature|(?:^|[_-])sig(?:$|[_-])|(?:^|[_-])code(?:$|[_-])|assertion)/i
    for (const key of [...url.searchParams.keys()]) {
      if (sensitiveQueryKey.test(key)) url.searchParams.set(key, '[redacted]')
    }
    return url.href
  } catch {
    return '<invalid-url>'
  }
}

export function safeFailureText(rawText: string): string {
  const sensitiveKey = '(?:token|secret|password|passphrase|csrf|credential|authorization|auth|api[_-]?key|access[_-]?key|private[_-]?key|session|cookie|jwt|signature|(?:^|[_-])sig(?:$|[_-])|(?:^|[_-])code(?:$|[_-])|assertion)'
  let safe = rawText.replace(/https?:\/\/[^\s"'<>]+/g, (candidate) => safeFailureUrl(candidate))
  safe = safe.replace(/\b(Bearer|Basic)\s+[^\s"'<>]+/gi, '$1 [redacted]')
  safe = safe.replace(
    new RegExp(`([?&]${sensitiveKey}=)[^&#\\s]*`, 'gi'),
    '$1[redacted]',
  )
  // Values written as JSON, quoted assignments, or plain header/log assignments are all possible in
  // exception text. Consume the entire quoted value (including spaces), and consume an unquoted value
  // to the next field delimiter or line break rather than leaving the tail of a multiword secret.
  safe = safe.replace(
    new RegExp(
      `(["']?${sensitiveKey}["']?\\s*[:=]\\s*)(?:"(?:\\\\.|[^"\\\\])*"|'(?:\\\\.|[^'\\\\])*'|[^\\r\\n,;&}]+)`,
      'gi',
    ),
    '$1[redacted]',
  )
  return safe
}

export function safeNetworkFailureEvidence(
  failure: NetworkFailure,
): Pick<NetworkFailure, 'method' | 'url' | 'errorText' | 'resourceType' | 'signalReason' | 'disposition'> {
  return {
    method: safeFailureText(failure.method),
    url: safeFailureUrl(failure.url),
    errorText: safeFailureText(failure.errorText),
    resourceType: safeFailureText(failure.resourceType),
    signalReason: failure.signalReason === undefined ? undefined : safeFailureText(failure.signalReason),
    disposition: failure.disposition,
  }
}

function safeFailureLabel(failure: ObservedFailure): string {
  const url = safeFailureUrl(failure.url)
  return failure.kind === 'http'
    ? `${safeFailureText(failure.method)} ${url} -> HTTP ${failure.status}`
    : `${safeFailureText(failure.method)} ${url} -> ${safeFailureText(failure.errorText)} (${safeFailureText(failure.resourceType)})`
}

/**
 * Evaluate observations one-to-one. A single response cannot be consumed by overlapping tolerances;
 * an explicit maxMatches is the only way one tolerance can account for more than one failure.
 */
function canonicalApplicationOrigin(value: unknown): string | null {
  if (typeof value !== 'string') return null
  try {
    const url = new URL(value)
    if (
      !['http:', 'https:'].includes(url.protocol) ||
      url.origin !== value ||
      url.username !== '' ||
      url.password !== '' ||
      url.pathname !== '/' ||
      url.search !== '' ||
      url.hash !== ''
    ) return null
    return url.origin
  } catch {
    return null
  }
}

export function evaluateFailureOracle(
  responses: readonly FailedResponse[],
  networkFailures: readonly NetworkFailure[],
  allowances: readonly FailureAllowance[],
  expectedApplicationOrigin: string,
): FailureOracleResult {
  const findings = validateFailureAllowances(allowances)
  const applicationOrigin = canonicalApplicationOrigin(expectedApplicationOrigin)
  if (!applicationOrigin) findings.push('expected application origin must be a canonical absolute HTTP(S) origin')
  const safeAllowances: readonly FailureAllowance[] = Array.isArray(allowances) ? allowances : []
  const matchedCounts = safeAllowances.map(() => 0)
  if (findings.length > 0 || applicationOrigin === null) return { findings, matchedCounts }

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
        return usedMatches !== undefined && usedMatches < maxMatches && allowanceMatches(allowance, failure, applicationOrigin)
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
