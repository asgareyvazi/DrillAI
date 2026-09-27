/**
 * The API boundary: query building, identity headers and error normalisation.
 *
 * Every screen depends on this file turning a failed request into something a user can act on: a
 * message, the platform's error code, the request id for support, and whether retrying could help.
 * Those are the properties tested here — against the platform's real error envelope, not an invented
 * one.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, request, setIdentity } from './client'

function jsonResponse(body: unknown, init: { status?: number; headers?: Record<string, string> } = {}) {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
  })
}

const fetchMock = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  setIdentity({ token: null, devRoles: 'engineer' })
  fetchMock.mockReset()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

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
      query: { project_id: 'prj_1', limit: 25, offset: 0, kinds: ['operation', 'event'], blank: '', missing: undefined, nil: null },
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
})

describe('request: error normalisation', () => {
  it('maps the platform error envelope, including the request id', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          error: {
            code: 'security.permission_denied',
            message: "missing permission 'workflow.publish'",
            retryable: false,
            details: { permission: 'workflow.publish', role_keys: ['engineer'] },
          },
        },
        { status: 403, headers: { 'x-request-id': 'req_abc' } },
      ),
    )

    const error = await api.get('/workflows/wfl_1').catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(403)
    expect(apiError.code).toBe('security.permission_denied')
    expect(apiError.message).toContain('workflow.publish')
    expect(apiError.details).toEqual({ permission: 'workflow.publish', role_keys: ['engineer'] })
    expect(apiError.requestId).toBe('req_abc')
    expect(apiError.retryable).toBe(false)
    expect(apiError.isUnauthorized).toBe(true)
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

  it('treats 409 as "approval required" rather than a generic conflict', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ error: { code: 'security.approval_required', message: 'approval needed', retryable: false } }, { status: 409 }),
    )
    const error = (await api.post('/workflows/wfl_1/run').catch((caught: unknown) => caught)) as ApiError
    expect(error.isApprovalRequired).toBe(true)
  })

  it('keeps a malformed body usable instead of throwing a parse error', async () => {
    fetchMock.mockResolvedValue(new Response('<html>gateway</html>', { status: 502 }))
    const error = (await api.get('/health').catch((caught: unknown) => caught)) as ApiError
    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(502)
  })

  it('falls back to the HTTP status code when the body carries no error contract', async () => {
    fetchMock.mockResolvedValue(new Response('', { status: 500 }))
    const error = (await api.get('/health').catch((caught: unknown) => caught)) as ApiError
    expect(error.code).toBe('http_500')
    expect(error.status).toBe(500)
  })

  it('reports a transport failure as a network error, distinct from an HTTP error', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    const error = (await api.get('/wells').catch((caught: unknown) => caught)) as ApiError
    expect(error.status).toBe(0)
    expect(error.code).toBe('network_unreachable')
    expect(error.isNetwork).toBe(true)
    expect(error.details.cause).toBeDefined()
  })

  it('propagates an abort signal so a cancelled request is not reported as a failure', async () => {
    const controller = new AbortController()
    fetchMock.mockImplementation((_url: string, init: RequestInit) => {
      expect(init.signal).toBe(controller.signal)
      return Promise.reject(new DOMException('aborted', 'AbortError'))
    })
    await expect(api.get('/wells', { signal: controller.signal })).rejects.toBeInstanceOf(DOMException)
  })
})
