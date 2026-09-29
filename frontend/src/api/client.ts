/**
 * Typed API client — the one boundary between the UI and the backend.
 *
 * Everything the UI knows about the backend lives here: the base URL, the development identity
 * header, the error contract, and the classification of a failed request. No business logic is
 * duplicated in components — the frontend renders what the API computed.
 *
 * ## The error model
 *
 * A failed request is classified once, here, into an [`ApiErrorKind`]. Callers branch on the
 * classification (`error.isNotFound`, `error.isTimeout`, …) rather than on status codes scattered
 * across pages, because the same HTTP status does not always mean the same thing and some failures
 * never had an HTTP status at all.
 *
 * The classes are deliberately not collapsed into one:
 *
 * | Failure                            | kind              | HTTP status |
 * | ---------------------------------- | ----------------- | ----------- |
 * | malformed request the server refused | `invalid_request` | 400         |
 * | no usable credentials              | `unauthenticated` | 401         |
 * | credentials without the permission | `forbidden`       | 403         |
 * | resource does not exist            | `not_found`       | 404         |
 * | state conflict / approval needed   | `conflict`        | 409, 428    |
 * | payload failed semantic validation | `validation`      | 422         |
 * | the server failed                  | `server`          | 5xx         |
 * | no HTTP response at all            | `network`         | none (0)    |
 * | the response never arrived in time | `timeout`         | none (0)    |
 * | a response arrived but is not the contract | `malformed` | whatever came back (usually 200) |
 * | the caller cancelled the request   | never thrown — see `ApiError.cancelled` | none |
 *
 * The last row is the important one. A request cancelled by the caller — a screen unmounted, a run
 * superseded by another run — is **not** a failure and is never turned into an `ApiError`. It is
 * re-thrown as the original `AbortError` so the query layer drops it silently. Reporting
 * "the backend is unreachable" because somebody navigated away is the exact lie this model exists to
 * prevent.
 *
 * Identity: the platform authenticates with `Authorization: Bearer <token>`. In development
 * (`DRILLAI_AUTH_ENABLED=false`, refused in production by the backend) the same endpoints accept a
 * development identity via `X-Dev-Roles`; the UI sends it only when a token is not configured, so a
 * real deployment never relies on the header.
 */

export const API_BASE = import.meta.env.VITE_API_BASE ?? '/api/v1'

/**
 * Transport deadline for a request, in milliseconds.
 *
 * A request that hangs is worse than one that fails: the operator sees a spinner and no way to tell
 * "still working" from "never coming back". The deadline is enforced at this boundary — the only
 * place a `fetch` happens — so every screen inherits it, and it is configuration rather than a
 * constant because a deployment with slower engines can raise it. `0` disables it for deployments
 * that need genuinely unbounded calls.
 */
export const DEFAULT_TIMEOUT_MS = Number(import.meta.env.VITE_API_TIMEOUT_MS ?? 60_000)

/** Longest prefix of an unreadable body kept for diagnosis. A body is never rendered wholesale. */
const PREVIEW_LIMIT = 400

export interface ApiClientIdentity {
  token?: string | null
  devRoles?: string | null
}

let identity: ApiClientIdentity = {
  token: (import.meta.env.VITE_API_TOKEN as string | undefined) ?? null,
  devRoles: null,
}

const identityListeners = new Set<() => void>()

/** The identity as a single comparable string, for consumers that need to react to it changing. */
export function identityKey(identity: ApiClientIdentity = getIdentity()): string {
  return `${identity.token ? 'token' : ''}|${identity.devRoles ?? ''}`
}

export function setIdentity(next: ApiClientIdentity): void {
  const before = identityKey(identity)
  identity = next
  if (identityKey(identity) === before) return
  // The identity is external mutable state, and one consumer — the event stream — has to react to it
  // whether or not anything else re-renders. TanStack Query's structural sharing means a refetch that
  // returns the same data can leave a component untouched, so "a render will happen" is not a
  // dependency: this is.
  for (const listener of identityListeners) listener()
}

/** Subscribe to identity changes; returns the unsubscribe function. */
export function subscribeIdentity(listener: () => void): () => void {
  identityListeners.add(listener)
  return () => identityListeners.delete(listener)
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

// --------------------------------------------------------------------------- classification

export type ApiErrorKind =
  | 'invalid_request'
  | 'unauthenticated'
  | 'forbidden'
  | 'not_found'
  | 'conflict'
  | 'validation'
  | 'server'
  | 'network'
  | 'timeout'
  | 'malformed'
  | 'cancelled'
  | 'unknown'

function classify(status: number, code: string): ApiErrorKind {
  switch (status) {
    case 0:
      return 'network'
    case 400:
      return 'invalid_request'
    case 401:
      return 'unauthenticated'
    case 403:
      return 'forbidden'
    case 404:
      return 'not_found'
    case 409:
    case 428:
      return 'conflict'
    case 422:
      return 'validation'
    default:
      if (status >= 500) return 'server'
      if (code === 'protocol.malformed_response') return 'malformed'
      return 'unknown'
  }
}

export interface ApiErrorExtras {
  /** Overrides the classification derived from the status — used by the transport failures. */
  kind?: ApiErrorKind
  /** The path that was requested, so an error carries what it was about. */
  path?: string | null
  contentType?: string | null
  cause?: unknown
  /** True when `message` came from the server's own error contract rather than from this client. */
  messageFromServer?: boolean
}

/** A structured backend error. `details` is the API's own error contract, passed through. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: Record<string, unknown>
  readonly requestId: string | null
  readonly retryable: boolean
  readonly kind: ApiErrorKind
  readonly path: string | null
  readonly contentType: string | null
  /**
   * Whether `message` is the server's own explanation.
   *
   * A screen shows the headline for the failure class and this sentence underneath it — but only
   * when it is the server talking. Repeating "The backend is unreachable." under a headline that
   * already says so would be padding, not information.
   */
  readonly messageFromServer: boolean

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {},
    requestId: string | null = null,
    retryable = false,
    extras: ApiErrorExtras = {},
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
    this.requestId = requestId
    this.retryable = retryable
    this.kind = extras.kind ?? classify(status, code)
    this.path = extras.path ?? null
    this.contentType = extras.contentType ?? null
    this.messageFromServer = extras.messageFromServer === true
  }

  get isInvalidRequest(): boolean {
    return this.kind === 'invalid_request'
  }

  /** No usable credentials. Distinct from `isForbidden` on purpose: the fix is different. */
  get isUnauthenticated(): boolean {
    return this.kind === 'unauthenticated'
  }

  /** Credentials were understood but lack the permission. */
  get isForbidden(): boolean {
    return this.kind === 'forbidden'
  }

  get isNotFound(): boolean {
    return this.kind === 'not_found'
  }

  /** 409/428: the server's state does not allow this, or a human gate is still missing. */
  get isConflict(): boolean {
    return this.kind === 'conflict'
  }

  /** 409 with the platform's approval contract: the action needs a human decision first. */
  get isApprovalRequired(): boolean {
    return this.kind === 'conflict' && this.code === 'security.approval_required'
  }

  /** 422: the payload was understood and rejected on its merits. */
  get isValidation(): boolean {
    return this.kind === 'validation'
  }

  get isServer(): boolean {
    return this.kind === 'server'
  }

  /** No HTTP response: the request never reached the server. */
  get isNetwork(): boolean {
    return this.kind === 'network'
  }

  /** The server was reached but did not answer in time. Never reported as "unreachable". */
  get isTimeout(): boolean {
    return this.kind === 'timeout'
  }

  /** A response arrived and is not the contract: invalid JSON, or a payload of the wrong shape. */
  get isMalformed(): boolean {
    return this.kind === 'malformed'
  }

  /**
   * Whether retrying could plausibly help.
   *
   * The backend's own flag wins when it is present, because it knows whether the failure was
   * transient. Otherwise transport failures and 5xx are retryable; a refusal that is about the
   * request (validation, permissions, a missing resource, a conflict) is not — asking again cannot
   * change the answer.
   */
  get isRetryable(): boolean {
    if (this.kind === 'network' || this.kind === 'timeout' || this.kind === 'server') return true
    if (this.retryable) return true
    return false
  }

  /**
   * A cancellation the caller *chose* to surface.
   *
   * The transport never produces this — an abort cancels a request and is dropped silently. It
   * exists for the rare caller that wants to say "cancelled" out loud (a long upload the user
   * stopped) and needs it to be a different thing from a failure.
   */
  static cancelled(path: string | null = null): ApiError {
    return new ApiError(0, 'request.cancelled', 'The request was cancelled.', {}, null, false, {
      kind: 'cancelled',
      path,
    })
  }

  /** A stable, non-secret rendering for logs and test failures. */
  get summary(): string {
    const where = this.path ? ` ${this.path}` : ''
    const id = this.requestId ? ` (request ${this.requestId})` : ''
    return `${this.kind}${where}: ${this.message}${id}`
  }
}

/** True for the `AbortError` a cancelled `fetch` rejects with, however the runtime spells it. */
export function isAbortError(cause: unknown): boolean {
  if (typeof DOMException !== 'undefined' && cause instanceof DOMException) return cause.name === 'AbortError'
  return cause instanceof Error && cause.name === 'AbortError'
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * Should TanStack Query retry this failure?
 *
 * A bounded, classified policy — the same one the UI uses to decide whether to offer a retry, so the
 * button and the automatic behaviour cannot disagree. Two automatic attempts, never a storm.
 */
export function shouldRetryRequest(failureCount: number, error: unknown): boolean {
  if (failureCount >= 2) return false
  if (error instanceof ApiError) return error.isRetryable
  return false
}

/** Whether a screen should offer the operator a manual retry for this failure. */
export function canRetry(error: unknown): boolean {
  if (error instanceof ApiError) return error.isRetryable
  // An unrecognised error is not automatically a transport problem, but offering a retry is harmless
  // and the alternative — a dead end — is worse.
  return error instanceof Error
}

// --------------------------------------------------------------------------- request

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: unknown
  /** A `FormData` body (uploads), passed through untouched so the browser sets the boundary. */
  form?: FormData
  /**
   * The caller's cancellation signal — React Query's per-query signal, in practice. A caller abort
   * is never an error: the original abort propagates so the query layer drops the result silently.
   */
  signal?: AbortSignal
  query?: Record<string, string | number | boolean | undefined | null | string[]>
  /**
   * Overrides the transport deadline for this call. `null` disables it (a genuinely unbounded call).
   */
  timeoutMs?: number | null
  /**
   * Validates the payload of a *successful* response. Returns an issue description, or `null` when
   * the payload is the shape the caller needs. A failure here means the server answered but not with
   * the contract this address promised, which is a protocol problem — not an outage.
   */
  validate?: (payload: unknown) => string | null
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

function preview(text: string): string {
  return text.length > PREVIEW_LIMIT ? `${text.slice(0, PREVIEW_LIMIT)}…` : text
}

/** The safest message for a status when the body carries no error contract of its own. */
function fallbackMessage(status: number, statusText: string): string {
  if (status === 0) return 'The backend is unreachable.'
  return statusText || `HTTP ${status}`
}

interface EnvelopeFields {
  code: string
  message: string
  details: Record<string, unknown>
  retryable: boolean
  traceId: string | null
  /** Whether `message` was read from the payload rather than defaulted from the status line. */
  servedMessage: boolean
}

/**
 * Read the platform's error envelope, falling back to what a proxy or a FastAPI default would send.
 *
 * `{"error": {code, message, retryable, details, trace_id}}` is the contract. A bare
 * `{"detail": ...}` is what a route that failed before the handlers did would produce, and a body
 * that is not JSON at all is what an intermediary produces. All three are read; none is invented.
 */
function readEnvelope(payload: unknown, status: number, statusText: string): EnvelopeFields {
  const fields: EnvelopeFields = {
    code: `http_${status}`,
    message: fallbackMessage(status, statusText),
    details: {},
    retryable: false,
    traceId: null,
    servedMessage: false,
  }
  if (!isRecord(payload)) return fields

  const error = payload.error
  if (isRecord(error)) {
    if (typeof error.code === 'string') fields.code = error.code
    if (typeof error.message === 'string') {
      fields.message = error.message
      fields.servedMessage = true
    }
    if (isRecord(error.details)) fields.details = error.details
    fields.retryable = error.retryable === true
    if (typeof error.trace_id === 'string') fields.traceId = error.trace_id
    return fields
  }

  const detail = payload.detail
  if (typeof detail === 'string') {
    fields.message = detail
    fields.servedMessage = true
  } else if (isRecord(detail)) {
    if (typeof detail.code === 'string') fields.code = detail.code
    if (typeof detail.message === 'string') {
      fields.message = detail.message
      fields.servedMessage = true
    }
    if (isRecord(detail.details)) fields.details = detail.details
  }
  return fields
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
  if (options.form !== undefined) {
    // No Content-Type: the browser must set it, because it carries the multipart boundary.
    init.body = options.form
  } else if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(options.body)
  }

  // One controller per request, owned here: it carries the transport deadline and is linked to the
  // caller's signal so either can cancel the fetch. The two are told apart afterwards by which one
  // fired — a deadline is a failure, a caller abort is not.
  const deadlineMs = options.timeoutMs === undefined ? DEFAULT_TIMEOUT_MS : options.timeoutMs
  const controller = new AbortController()
  let timedOut = false
  let timer: ReturnType<typeof setTimeout> | null = null
  const abortFromCaller = () => controller.abort(options.signal?.reason)
  if (options.signal) {
    if (options.signal.aborted) abortFromCaller()
    else options.signal.addEventListener('abort', abortFromCaller, { once: true })
  }
  if (deadlineMs !== null && deadlineMs > 0) {
    timer = setTimeout(() => {
      timedOut = true
      controller.abort(new DOMException(`request exceeded ${deadlineMs}ms`, 'TimeoutError'))
    }, deadlineMs)
  }
  init.signal = controller.signal

  const release = () => {
    if (timer !== null) clearTimeout(timer)
    options.signal?.removeEventListener('abort', abortFromCaller)
  }

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}${buildQuery(options.query)}`, init)
  } catch (cause) {
    release()
    // The deadline fired: a transport failure with a name, not a vague network error.
    if (timedOut) {
      throw new ApiError(0, 'network.timeout', `The request timed out after ${deadlineMs} ms.`, {
        timeout_ms: deadlineMs,
      }, null, true, { kind: 'timeout', path, cause })
    }
    // The caller cancelled: pass the abort through untouched. This is what keeps a run switch from
    // being reported as an outage — the query layer recognises the abort and drops it.
    if (options.signal?.aborted || isAbortError(cause)) throw cause
    // A genuine transport failure: no HTTP response was received, and no status is invented.
    throw new ApiError(
      0,
      'network.unreachable',
      'The backend is unreachable.',
      { cause: cause instanceof Error ? cause.message : String(cause) },
      null,
      true,
      { kind: 'network', path, cause },
    )
  }
  release()

  const requestId = response.headers.get('x-request-id')
  const contentType = response.headers.get('content-type')
  if (response.status === 204) return undefined as T

  const text = await response.text()
  let payload: unknown = null
  let parseFailed = false
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      parseFailed = true
    }
  }

  if (!response.ok) {
    if (parseFailed) {
      // The server answered with something that is not JSON. The status is still meaningful, so it is
      // reported *as that status* — with the unreadable body kept, bounded, for diagnosis.
      throw new ApiError(
        response.status,
        `http_${response.status}`,
        fallbackMessage(response.status, response.statusText),
        { preview: preview(text) },
        requestId,
        response.status >= 500,
        { path, contentType },
      )
    }
    const envelope = readEnvelope(payload, response.status, response.statusText)
    throw new ApiError(
      response.status,
      envelope.code,
      envelope.message,
      envelope.details,
      envelope.traceId ?? requestId,
      envelope.retryable,
      { path, contentType, messageFromServer: envelope.servedMessage },
    )
  }

  if (parseFailed) {
    // HTTP 200 with a body that is not JSON. This reached the server and came back — it is *not* an
    // outage, and it must never be reported as one. Returning `{raw: text}` here (as this used to)
    // pushed the failure into the first component that touched the payload, which then crashed with
    // "cannot read property of undefined" and no clue where the problem was.
    throw new ApiError(
      200,
      'protocol.malformed_response',
      'The server returned a response this page could not read.',
      { content_type: contentType, preview: preview(text) },
      requestId,
      false,
      { kind: 'malformed', path, contentType },
    )
  }

  if (options.validate) {
    const issue = options.validate(payload)
    if (issue) {
      throw new ApiError(
        200,
        'protocol.malformed_response',
        'The server returned a response this page could not read.',
        { issue, preview: preview(JSON.stringify(payload) ?? '') },
        requestId,
        false,
        { kind: 'malformed', path, contentType },
      )
    }
  }

  return payload as T
}

/** Payload validators for the shapes the UI depends on. Used by the endpoints that need them. */
export const expect = {
  /** The payload must be an object with this property present. */
  object:
    (key: string) =>
    (payload: unknown): string | null =>
      isRecord(payload) && payload[key] !== undefined && payload[key] !== null
        ? null
        : `expected an object with a "${key}" property`,
  /** The payload must be a paged envelope: an object carrying an `items` array. */
  paged:
    () =>
    (payload: unknown): string | null =>
      isRecord(payload) && Array.isArray(payload.items) ? null : 'expected a paged response with an "items" array',
  /** The payload must be an array. */
  array:
    () =>
    (payload: unknown): string | null =>
      Array.isArray(payload) ? null : 'expected an array',
}

export const api = {
  get: <T>(path: string, options: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...options, method: 'GET' }),
  post: <T>(path: string, body?: unknown, options: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...options, method: 'POST', body }),
  put: <T>(path: string, body?: unknown, options: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...options, method: 'PUT', body }),
  /** Multipart upload through the same boundary, so it inherits the timeout and the error contract. */
  upload: <T>(path: string, form: FormData, options: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...options, method: 'POST', form }),
}
