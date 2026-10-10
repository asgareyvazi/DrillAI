/**
 * The API boundary: query building, identity headers, and the classification of every way a request
 * can fail.
 *
 * Every screen depends on this file turning a failed request into something a user can act on: what
 * kind of failure it was, a message, the platform's error code, the request id for support, and
 * whether retrying could help. Those are the properties tested here — against the platform's real
 * error envelope and against real transport behaviour, not against an invented contract.
 *
 * The failure modes are deliberately *not* collapsed: a 403, a 404, a 409, a timeout, a body that is
 * not JSON, and a request the caller cancelled all have to come out of this file as different things,
 * because the interface says something different for each and one of them (a cancellation) must not
 * be reported as a failure at all.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ApiError,
  DEFAULT_TIMEOUT_MS,
  api,
  canRetry,
  isAbortError,
  request,
  setIdentity,
  shouldRetryRequest,
} from './client'

function jsonResponse(body: unknown, init: { status?: number; headers?: Record<string, string> } = {}) {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
  })
}

function envelope(
  status: number,
  error: Record<string, unknown>,
  headers: Record<string, string> = {},
): Response {
  return jsonResponse({ error }, { status, headers })
}

const fetchMock = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  setIdentity({ token: null, devRoles: 'engineer' })
  fetchMock.mockReset()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

export function abortedRejection(): Promise<never> {
  return Promise.reject(new DOMException('aborted', 'AbortError'))
}

describe('request: transport', () => {
  it('prefixes the API base path and serialises JSON bodies', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }))
    await api.post('/wells', { name: 'NF-12' })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/wells')
    expect(init.method).toBe('POST')
    expect(init.body).toBe('{"name":"NF-12"}')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
  })

  it('drops empty query values and repeats array parameters', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}))
    await api.get('/wells', {
      query: {
        project_id: 'prj_1',
        limit: 25,
        offset: 0,
        kinds: ['operation', 'event'],
        blank: '',
        missing: undefined,
        nil: null,
      },
    })

    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toBe('/api/v1/wells?project_id=prj_1&limit=25&offset=0&kinds=operation&kinds=event')
  })

  it('prefers a bearer token over the development role header', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}))
    setIdentity({ token: 'tok_123', devRoles: 'engineer' })
    await api.get('/wells')

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const headers = init.headers as Record<string, string>
    expect(headers.Authorization).toBe('Bearer tok_123')
    expect(headers['X-Dev-Roles']).toBeUndefined()
  })

  it('sends the development role header only when no token is configured', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}))
    setIdentity({ token: null, devRoles: 'viewer' })
    await api.get('/wells')

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect((init.headers as Record<string, string>)['X-Dev-Roles']).toBe('viewer')
  })

  it('returns undefined for an empty 204 response instead of throwing on JSON parse', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }))
    await expect(request('/workflows/wfl_1')).resolves.toBeUndefined()
  })

  it('sends a FormData body without setting the content type by hand', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ document: { id: 'doc_1' } }))
    const form = new FormData()
    form.append('file', new Blob(['x']), 'x.txt')
    await api.upload('/documents', form)

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/documents')
    expect(init.body).toBe(form)
    expect((init.headers as Record<string, string>)['Content-Type']).toBeUndefined()
  })
})

describe('request: the platform error envelope', () => {
  it('maps the envelope, including code, details, request id and retryable', async () => {
    fetchMock.mockResolvedValue(
      envelope(
        403,
        {
          code: 'security.permission_denied',
          message: "missing permission 'workflow.publish'",
          retryable: false,
          details: { permission: 'workflow.publish', role_keys: ['engineer'] },
        },
        { 'x-request-id': 'req_abc' },
      ),
    )

    const error = (await api.get('/workflows/wfl_1').catch((caught: unknown) => caught)) as ApiError
    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(403)
    expect(error.code).toBe('security.permission_denied')
    expect(error.message).toContain('workflow.publish')
    expect(error.details).toEqual({ permission: 'workflow.publish', role_keys: ['engineer'] })
    expect(error.requestId).toBe('req_abc')
    expect(error.retryable).toBe(false)
    expect(error.kind).toBe('forbidden')
    expect(error.path).toBe('/workflows/wfl_1')
  })

  it('prefers the envelope trace id over the response header when both are present', async () => {
    fetchMock.mockResolvedValue(
      envelope(
        500,
        { code: 'platform.internal_error', message: 'internal error', retryable: false, trace_id: 'req_body' },
        { 'x-request-id': 'req_header' },
      ),
    )
    const error = (await api.get('/x').catch((caught: unknown) => caught)) as ApiError
    expect(error.requestId).toBe('req_body')
  })

  it('maps a FastAPI validation error to a validation ApiError with its details', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        { detail: { code: 'platform.validation_failed', message: 'unknown report kind', details: { known: ['npt'] } } },
        { status: 422 },
      ),
    )
    const error = (await api.get('/reports/nope').catch((caught: unknown) => caught)) as ApiError
    expect(error.code).toBe('platform.validation_failed')
    expect(error.isValidation).toBe(true)
    expect(error.details).toEqual({ known: ['npt'] })
  })

  it('reads a bare string detail as the message', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'Not authenticated' }, { status: 401 }))
    const error = (await api.get('/x').catch((caught: unknown) => caught)) as ApiError
    expect(error.message).toBe('Not authenticated')
    expect(error.isUnauthenticated).toBe(true)
  })

  it('falls back to the HTTP status when the body carries no error contract', async () => {
    fetchMock.mockResolvedValue(new Response('', { status: 500 }))
    const error = (await api.get('/health').catch((caught: unknown) => caught)) as ApiError
    expect(error.code).toBe('http_500')
    expect(error.status).toBe(500)
    expect(error.isServer).toBe(true)
  })

  it('keeps a non-JSON error body bounded instead of failing on the parse', async () => {
    fetchMock.mockResolvedValue(
      new Response('<html>gateway</html>'.padEnd(1000, 'x'), {
        status: 502,
        headers: { 'Content-Type': 'text/html' },
      }),
    )
    const error = (await api.get('/health').catch((caught: unknown) => caught)) as ApiError
    expect(error.status).toBe(502)
    expect(error.isServer).toBe(true)
    expect(String(error.details.preview)).toHaveLength(401) // 400 characters + the ellipsis
    expect(error.contentType).toContain('text/html')
  })
})

describe('request: classification', () => {
  const cases: Array<[number, string, ApiError['kind']]> = [
    [400, 'invalid_request', 'invalid_request'],
    [401, 'unauthenticated', 'unauthenticated'],
    [403, 'forbidden', 'forbidden'],
    [404, 'not_found', 'not_found'],
    [409, 'conflict', 'conflict'],
    [422, 'validation', 'validation'],
    [500, 'server', 'server'],
    [503, 'server', 'server'],
  ]

  for (const [status, kind, expected] of cases) {
    it(`classifies ${status} as ${kind}`, async () => {
      fetchMock.mockResolvedValue(envelope(status, { code: kind, message: `${status} from the server` }))
      const error = (await api.get('/x').catch((caught: unknown) => caught)) as ApiError
      expect(error.status).toBe(status)
      expect(error.kind).toBe(expected)
      expect(error.summary).toContain(`${expected} /x`)
    })
  }

  it('keeps 401 and 403 apart, because the user has to do different things about them', async () => {
    fetchMock.mockResolvedValueOnce(envelope(401, { code: 'security.authentication_required', message: 'no' }))
    const unauthorized = (await api.get('/x').catch((caught: unknown) => caught)) as ApiError
    fetchMock.mockResolvedValueOnce(envelope(403, { code: 'security.permission_denied', message: 'no' }))
    const forbidden = (await api.get('/x').catch((caught: unknown) => caught)) as ApiError

    expect(unauthorized.isUnauthenticated).toBe(true)
    expect(unauthorized.isForbidden).toBe(false)
    expect(forbidden.isForbidden).toBe(true)
    expect(forbidden.isUnauthenticated).toBe(false)
  })

  it('treats the platform approval contract as a conflict that needs a human', async () => {
    fetchMock.mockResolvedValue(
      envelope(409, { code: 'security.approval_required', message: 'approval needed', retryable: false }),
    )
    const error = (await api.post('/workflows/wfl_1/run').catch((caught: unknown) => caught)) as ApiError
    expect(error.isConflict).toBe(true)
    expect(error.isApprovalRequired).toBe(true)
  })

  it('does not call every conflict an approval', async () => {
    fetchMock.mockResolvedValue(
      envelope(409, { code: 'platform.conflict', message: 'the approval has not been decided yet' }),
    )
    const error = (await api.post('/runs/run_1/resume').catch((caught: unknown) => caught)) as ApiError
    expect(error.isConflict).toBe(true)
    expect(error.isApprovalRequired).toBe(false)
  })

  it('preserves the backend retryable flag on a status that is not otherwise retryable', async () => {
    fetchMock.mockResolvedValue(envelope(429, { code: 'platform.rate_limited', message: 'slow down', retryable: true }))
    const error = (await api.get('/x').catch((caught: unknown) => caught)) as ApiError
    expect(error.retryable).toBe(true)
    expect(error.isRetryable).toBe(true)
  })
})

describe('request: a response that is not the contract', () => {
  it('reports a 200 that is not JSON as a protocol failure, never as an outage', async () => {
    fetchMock.mockResolvedValue(
      new Response('{"broken": ', { status: 200, headers: { 'Content-Type': 'application/json' } }),
    )
    const error = (await api.get('/runs/run_1').catch((caught: unknown) => caught)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.kind).toBe('malformed')
    expect(error.isMalformed).toBe(true)
    expect(error.isNetwork).toBe(false)
    expect(error.status).toBe(200)
    expect(error.code).toBe('protocol.malformed_response')
    expect(error.details.preview).toBe('{"broken": ')
    expect(error.isRetryable).toBe(false)
  })

  it('reports a well-formed JSON payload of the wrong shape as a protocol failure', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ something_else: [] }))
    const validate = (payload: unknown) =>
      typeof payload === 'object' && payload !== null && 'run' in payload
        ? null
        : 'expected an object with a "run" property'
    const error = (await api.get('/runs/run_1', { validate }).catch((caught: unknown) => caught)) as ApiError

    expect(error.kind).toBe('malformed')
    expect(error.details.issue).toContain('"run"')
    expect(error.isNetwork).toBe(false)
  })

  it('never returns an unvalidated payload for a guarded read', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: 'not a list' }))
    const error = (await api
      .get('/wells', { validate: () => 'expected a paged response with an "items" array' })
      .catch((caught: unknown) => caught)) as ApiError
    expect(error.isMalformed).toBe(true)
  })
})

describe('request: transport failures', () => {
  it('reports a connection failure as a network error with no invented status', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    const error = (await api.get('/wells').catch((caught: unknown) => caught)) as ApiError

    expect(error.status).toBe(0)
    expect(error.kind).toBe('network')
    expect(error.isNetwork).toBe(true)
    expect(error.isRetryable).toBe(true)
    expect(error.details.cause).toBe('Failed to fetch')
  })

  it('turns a request that never answers into a timeout, and aborts the fetch', async () => {
    vi.useFakeTimers()
    let seenSignal: AbortSignal | undefined
    fetchMock.mockImplementation((_url: string, init: RequestInit) => {
      seenSignal = init.signal ?? undefined
      return new Promise((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
      })
    })

    const pending = api.get('/slow', { timeoutMs: 1_000 })
    const settled = pending.catch((caught: unknown) => caught)
    await vi.advanceTimersByTimeAsync(1_001)
    const error = (await settled) as ApiError

    expect(error.kind).toBe('timeout')
    expect(error.isTimeout).toBe(true)
    expect(error.isNetwork).toBe(false)
    expect(error.code).toBe('network.timeout')
    expect(error.status).toBe(0)
    expect(error.details.timeout_ms).toBe(1_000)
    expect(error.isRetryable).toBe(true)
    expect(seenSignal?.aborted).toBe(true) // the request really was cancelled, not merely abandoned
  })

  it('uses the configured deadline when the caller does not name one', async () => {
    vi.useFakeTimers()
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
        }),
    )
    const settled = api.get('/slow').catch((caught: unknown) => caught)
    await vi.advanceTimersByTimeAsync(DEFAULT_TIMEOUT_MS - 1)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(2)
    const error = (await settled) as ApiError
    expect(error.isTimeout).toBe(true)
    expect(error.details.timeout_ms).toBe(DEFAULT_TIMEOUT_MS)
  })

  it('lets a caller opt out of the deadline for a genuinely long call', async () => {
    vi.useFakeTimers()
    let signal: AbortSignal | undefined
    fetchMock.mockImplementation((_url: string, init: RequestInit) => {
      signal = init.signal ?? undefined
      return new Promise((resolve) => setTimeout(() => resolve(jsonResponse({ ok: true })), 5 * DEFAULT_TIMEOUT_MS))
    })
    const settled = api.get('/engine-run', { timeoutMs: null })
    await vi.advanceTimersByTimeAsync(DEFAULT_TIMEOUT_MS + 1_000)
    expect(signal?.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(5 * DEFAULT_TIMEOUT_MS)
    await expect(settled).resolves.toEqual({ ok: true })
  })
})

describe('request: cancellation', () => {
  it('passes a caller abort through instead of dressing it up as an outage', async () => {
    const controller = new AbortController()
    let seenSignal: AbortSignal | undefined
    fetchMock.mockImplementation((_url: string, init: RequestInit) => {
      seenSignal = init.signal ?? undefined
      return new Promise((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
      })
    })

    const settled = api.get('/runs/run_a', { signal: controller.signal }).catch((caught: unknown) => caught)
    controller.abort()
    const error = await settled

    expect(isAbortError(error)).toBe(true)
    expect(error).not.toBeInstanceOf(ApiError)
    expect((error as Error).message).not.toMatch(/unreachable/i)
    expect(seenSignal?.aborted).toBe(true)
  })

  it('aborts before the first byte when the caller has already cancelled', async () => {
    const controller = new AbortController()
    controller.abort()
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          if (init.signal?.aborted) reject(new DOMException('aborted', 'AbortError'))
        }),
    )
    const error = await api.get('/runs/run_a', { signal: controller.signal }).catch((caught: unknown) => caught)
    expect(isAbortError(error)).toBe(true)
    expect(error).not.toBeInstanceOf(ApiError)
  })

  it('keeps a caller abort distinct from a deadline that fired on the same request', async () => {
    vi.useFakeTimers()
    const controller = new AbortController()
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
        }),
    )
    const settled = api
      .get('/runs/run_a', { signal: controller.signal, timeoutMs: 5_000 })
      .catch((caught: unknown) => caught)
    controller.abort()
    const error = await settled
    expect(error).not.toBeInstanceOf(ApiError)

    // and the deadline of that request can no longer fire at all
    await vi.advanceTimersByTimeAsync(10_000)
    expect(isAbortError(error)).toBe(true)
  })

  it('exposes an explicit cancellation for the rare caller that wants to show one', () => {
    const cancelled = ApiError.cancelled('/uploads/doc_1')
    expect(cancelled.kind).toBe('cancelled')
    expect(cancelled.isRetryable).toBe(false)
    expect(cancelled.path).toBe('/uploads/doc_1')
  })
})

describe('retry policy', () => {
  it('retries transport failures and 5xx, and only those', () => {
    const retryable = [new ApiError(0, 'network.unreachable', 'x'), new ApiError(0, 'network.timeout', 'x'), new ApiError(503, 'http_503', 'x')]
    for (const error of retryable) {
      expect(shouldRetryRequest(0, error), error.summary).toBe(true)
      expect(canRetry(error)).toBe(true)
    }

    const notRetryable = [
      new ApiError(400, 'platform.invalid', 'x'),
      new ApiError(401, 'security.authentication_required', 'x'),
      new ApiError(403, 'security.permission_denied', 'x'),
      new ApiError(404, 'platform.not_found', 'x'),
      new ApiError(409, 'platform.conflict', 'x'),
      new ApiError(422, 'platform.validation_failed', 'x'),
      new ApiError(200, 'protocol.malformed_response', 'x', {}, null, false, { kind: 'malformed' }),
    ]
    for (const error of notRetryable) {
      expect(shouldRetryRequest(0, error), error.summary).toBe(false)
      expect(canRetry(error), error.summary).toBe(false)
    }
  })

  it('stops after two automatic attempts, whatever the failure', () => {
    const timeout = new ApiError(0, 'network.timeout', 'x', {}, null, true, { kind: 'timeout' })
    expect(shouldRetryRequest(0, timeout)).toBe(true)
    expect(shouldRetryRequest(1, timeout)).toBe(true)
    expect(shouldRetryRequest(2, timeout)).toBe(false)
  })

  it('does not retry an abort', () => {
    const abort = new DOMException('aborted', 'AbortError')
    expect(shouldRetryRequest(0, abort)).toBe(false)
    expect(canRetry(abort)).toBe(false)
  })
})
