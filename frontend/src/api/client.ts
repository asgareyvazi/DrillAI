/**
 * Typed API client.
 *
 * Everything the UI knows about the backend lives here: one place for the base URL, the development
 * identity header, the error contract and envelope unwrapping. No business logic is duplicated in
 * components — the frontend renders what the API computed.
 *
 * Identity: the platform authenticates with `Authorization: Bearer <token>`. In development
 * (`DRILLAI_AUTH_ENABLED=false`, refused in production by the backend) the same endpoints accept a
 * development identity via `X-Dev-Roles`; the UI sends it only when a token is not configured, so
 * a real deployment never relies on the header.
 */

export const API_BASE = import.meta.env.VITE_API_BASE ?? '/api/v1'

export interface ApiClientIdentity {
  token?: string | null
  devRoles?: string | null
}

let identity: ApiClientIdentity = {
  token: (import.meta.env.VITE_API_TOKEN as string | undefined) ?? null,
  devRoles: null,
}

export function setIdentity(next: ApiClientIdentity): void {
  identity = next
}

/**
 * The identity the client is currently acting as.
 *
 * Exported because HTTP is no longer the only transport: a WebSocket handshake cannot carry the
 * `X-Dev-Roles` header, so the stream layer has to build the same identity into its URL. Reading it
 * from here keeps one source of truth — a screen can never act as two different principals.
 */
export function getIdentity(): ApiClientIdentity {
  return identity
}

/** A structured backend error. `details` is the API's own error contract, passed through. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: Record<string, unknown>
  readonly requestId: string | null
  readonly retryable: boolean

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {},
    requestId: string | null = null,
    retryable = false,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
    this.requestId = requestId
    this.retryable = retryable
  }

  get isUnauthorized(): boolean {
    return this.status === 401 || this.status === 403
  }

  get isNotFound(): boolean {
    return this.status === 404
  }

  get isValidation(): boolean {
    return this.status === 400 || this.status === 422
  }

  /** 409 from the platform means an action needs human approval before it can run. */
  get isApprovalRequired(): boolean {
    return this.status === 409
  }

  get isNetwork(): boolean {
    return this.status === 0
  }
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: unknown
  signal?: AbortSignal
  query?: Record<string, string | number | boolean | undefined | null | string[]>
}

function buildQuery(query: RequestOptions['query']): string {
  if (!query) return ''
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) {
      for (const item of value) params.append(key, String(item))
    } else {
      params.append(key, String(value))
    }
  }
  const rendered = params.toString()
  return rendered ? `?${rendered}` : ''
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (identity.token) headers.Authorization = `Bearer ${identity.token}`
  else if (identity.devRoles) headers['X-Dev-Roles'] = identity.devRoles

  const init: RequestInit = {
    method: options.method ?? 'GET',
    headers,
    credentials: 'include',
  }
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(options.body)
  }
  if (options.signal) init.signal = options.signal

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}${buildQuery(options.query)}`, init)
  } catch (cause) {
    // A cancelled request is not a failure: the caller aborted it (a screen was left, a newer query
    // superseded it). Passing the abort through lets the query layer ignore it, instead of showing
    // "the backend is unreachable" because a user navigated away.
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
    // A transport failure is not an HTTP error and must not be dressed up as one: the UI shows a
    // distinct "backend unreachable" state, because the fix is different.
    throw new ApiError(0, 'network_unreachable', 'The backend is unreachable.', {
      cause: cause instanceof Error ? cause.message : String(cause),
    })
  }

  const requestId = response.headers.get('x-request-id')
  if (response.status === 204) return undefined as T

  const text = await response.text()
  let payload: unknown = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = { raw: text }
    }
  }

  if (!response.ok) {
    const record = (payload ?? {}) as Record<string, unknown>
    const error = record.error as Record<string, unknown> | undefined
    const detail = record.detail
    let message = response.statusText
    let code = `http_${response.status}`
    let details: Record<string, unknown> = {}
    let retryable = false

    if (error && typeof error === 'object') {
      // The platform error contract: {"error": {code, message, retryable, details}}.
      message = typeof error.message === 'string' ? error.message : message
      code = typeof error.code === 'string' ? error.code : code
      details = (error.details as Record<string, unknown>) ?? {}
      retryable = error.retryable === true
    } else if (typeof detail === 'string') {
      message = detail
    } else if (detail && typeof detail === 'object') {
      const d = detail as Record<string, unknown>
      message = typeof d.message === 'string' ? d.message : message
      code = typeof d.code === 'string' ? d.code : code
      details = (d.details as Record<string, unknown>) ?? {}
    }

    throw new ApiError(response.status, code, message, details, requestId, retryable)
  }

  return payload as T
}

export const api = {
  get: <T>(path: string, options: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...options, method: 'GET' }),
  post: <T>(path: string, body?: unknown, options: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...options, method: 'POST', body }),
  put: <T>(path: string, body?: unknown, options: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...options, method: 'PUT', body }),
}
