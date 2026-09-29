/**
 * The run event stream: the protocol, the URL, and a transport that can be trusted with a cursor.
 *
 * The server keeps the durable log (`run_events`) and this socket tails it. That shape decides
 * everything here:
 *
 * * **The cursor is `seq`, and only `seq`.** The server guarantees one increasing sequence per run,
 *   so the highest sequence the UI has applied is the only honest answer to "where am I?". Array
 *   length, render count and arrival time all look equivalent until the first reconnect, and then
 *   they replay or skip history.
 * * **`stream_closed` is the end, a dropped transport is not.** A closed socket means "this
 *   connection ended"; only `stream_closed` (or a refusal) means "stop reconnecting". Confusing the
 *   two either abandons a live run or hammers a finished one.
 * * **The socket is a transport, never a source of truth.** Frames are delivered to the caller, and
 *   the caller decides what to keep; REST reconciliation is what makes the page converge.
 * * **Nothing here throws at the UI.** A malformed frame, a frame for another run, an unknown frame
 *   type from a newer server — each is reported as an ignored frame with a reason, because a live
 *   monitor that crashes on one bad message is worse than one that shows a diagnostic.
 *
 * Everything the platform-specific parts need (the clock, the timers, the socket constructor) is
 * injectable, which is what makes reconnect and disposal deterministic in unit tests without
 * pretending a fake socket is the backend. The real server is exercised by the browser suite.
 */

import type { RunEvent } from '../api/types'
import { API_BASE, getIdentity } from '../api/client'

// --------------------------------------------------------------------------- protocol

export interface StreamOpenedMessage {
  type: 'stream_opened'
  run_id: string
  status: string
  after_seq: number
}

export interface RunEventMessage {
  type: 'run_event'
  event: RunEvent
}

export interface StreamIdleMessage {
  type: 'stream_idle'
  last_seq: number
}

export interface StreamClosedMessage {
  type: 'stream_closed'
  status: string
}

export interface StreamErrorMessage {
  type: 'stream_error'
  message?: string
}

/** The frames the backend sends on `/runs/{run_id}/events/stream`, field for field. */
export type RunStreamMessage =
  | StreamOpenedMessage
  | RunEventMessage
  | StreamIdleMessage
  | StreamClosedMessage
  | StreamErrorMessage

/**
 * The result of reading one frame: either a message this client understands, or an ignore with the
 * reason it was ignored. Diagnostics are values, not exceptions — a newer server sending a frame type
 * this build does not know must not break a monitor that is otherwise working.
 */
export type RunStreamDecode =
  | { kind: 'message'; message: RunStreamMessage }
  | { kind: 'ignored'; reason: string }

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function asString(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

/**
 * Validate a parsed frame against the documented protocol.
 *
 * `expectedRunId` is checked rather than assumed: the events of one run must never be applied to the
 * next one just because a socket outlived the screen that opened it.
 */
export function decodeRunStreamFrame(raw: unknown, expectedRunId: string): RunStreamDecode {
  if (!isRecord(raw)) return { kind: 'ignored', reason: 'frame is not an object' }
  const type = asString(raw.type)
  if (!type) return { kind: 'ignored', reason: 'frame has no type' }

  switch (type) {
    case 'stream_opened': {
      const runId = asString(raw.run_id)
      const status = asString(raw.status)
      const afterSeq = asNumber(raw.after_seq)
      if (runId !== expectedRunId) return { kind: 'ignored', reason: `stream_opened for ${runId ?? 'an unknown run'}` }
      if (status === null || afterSeq === null) return { kind: 'ignored', reason: 'stream_opened is incomplete' }
      return { kind: 'message', message: { type, run_id: runId, status, after_seq: afterSeq } }
    }
    case 'run_event': {
      const event = raw.event
      if (!isRecord(event)) return { kind: 'ignored', reason: 'run_event carries no event' }
      const runId = asString(event.run_id)
      const seq = asNumber(event.seq)
      const eventType = asString(event.type)
      const id = asString(event.id)
      if (runId !== expectedRunId) return { kind: 'ignored', reason: `run_event for ${runId ?? 'an unknown run'}` }
      if (seq === null || seq < 1) return { kind: 'ignored', reason: 'run_event has no usable sequence' }
      if (eventType === null || id === null) return { kind: 'ignored', reason: 'run_event is incomplete' }
      return { kind: 'message', message: { type, event: event as unknown as RunEvent } }
    }
    case 'stream_idle': {
      const lastSeq = asNumber(raw.last_seq)
      if (lastSeq === null) return { kind: 'ignored', reason: 'stream_idle has no last_seq' }
      return { kind: 'message', message: { type, last_seq: lastSeq } }
    }
    case 'stream_closed': {
      const status = asString(raw.status)
      if (status === null) return { kind: 'ignored', reason: 'stream_closed has no status' }
      return { kind: 'message', message: { type, status } }
    }
    case 'stream_error': {
      const message = asString(raw.message) ?? undefined
      return { kind: 'message', message: { type, message } }
    }
    default:
      return { kind: 'ignored', reason: `unknown frame type '${type}'` }
  }
}

// --------------------------------------------------------------------------- event identity and merging

/** The identity of a durable event: one sequence number within one run. */
export function runEventKey(event: Pick<RunEvent, 'run_id' | 'seq'>): string {
  return `${event.run_id}:${event.seq}`
}

/** The highest sequence number in a list, or 0 when there is nothing (the log starts at 1). */
export function highestSeq(events: readonly RunEvent[]): number {
  let highest = 0
  for (const event of events) {
    const seq = Number(event?.seq)
    if (Number.isFinite(seq) && seq > highest) highest = seq
  }
  return highest
}

/**
 * Merge event lists into one history, deduplicated by `run_id + seq` and ordered by `seq`.
 *
 * The same event legitimately arrives twice — through REST, through the socket, and again after a
 * reconnect — and the screen must not show it twice. Ordering is by the server's sequence, never by
 * arrival: an event that arrives late is placed where it happened.
 */
export function mergeRunEvents(existing: readonly RunEvent[], incoming: readonly RunEvent[]): RunEvent[] {
  const byKey = new Map<string, RunEvent>()
  for (const event of existing) byKey.set(runEventKey(event), event)
  // The later source wins field for field: it is a fresher serialization of the same durable row.
  for (const event of incoming) byKey.set(runEventKey(event), event)
  return [...byKey.values()].sort((a, b) => Number(a.seq) - Number(b.seq))
}

// --------------------------------------------------------------------------- URL

const REFUSAL_CODES = new Set([4401, 4403, 4404])

/** Close codes that mean "this connection will never be accepted": retrying cannot help. */
export function isRefusalCode(code: number | null | undefined): boolean {
  return typeof code === 'number' && REFUSAL_CODES.has(code)
}

export interface StreamUrlIdentity {
  token?: string | null
  devRoles?: string | null
}

export interface StreamUrlOptions {
  apiBase?: string
  /** The page's own origin, injected so the URL builder is testable outside a browser. */
  location?: { protocol: string; host: string }
  /**
   * Whether a development role may be put in the query string. Defaults to "not a production build".
   *
   * The development identity normally travels in the `X-Dev-Roles` header, which a WebSocket
   * handshake cannot carry, so the backend accepts it as a query parameter — but only in a
   * deployment with authentication disabled. A production build never sends it at all: the safe
   * default does not depend on the server being configured correctly.
   */
  allowDevRoles?: boolean
}

/**
 * Build the WebSocket URL for a run's event stream.
 *
 * Derived from the configured API base, so the same build works behind the dev proxy, in the
 * end-to-end harness and behind a production reverse proxy: a relative base becomes an absolute
 * `ws(s)://` URL on the page's own origin, and an absolute base keeps its host and switches scheme.
 */
export function runEventStreamUrl(
  runId: string,
  afterSeq: number,
  identity: StreamUrlIdentity = getIdentity(),
  options: StreamUrlOptions = {},
): string {
  const base = options.apiBase ?? API_BASE
  const page = options.location ?? { protocol: window.location.protocol, host: window.location.host }
  // An absolute base decides both the host and the scheme; a relative one stays on the page's origin,
  // which is what makes the dev proxy, the end-to-end harness and a reverse proxy all work unchanged.
  const absolute = /^https?:\/\//.test(base)
  const secure = absolute ? base.startsWith('https://') : page.protocol === 'https:'
  const scheme = secure ? 'wss' : 'ws'
  const parsed = absolute ? new URL(base) : null
  const origin = parsed ? `${scheme}://${parsed.host}` : `${scheme}://${page.host}`
  const basePath = parsed ? parsed.pathname : base.startsWith('/') ? base : `/${base}`

  const path = `${basePath.replace(/\/$/, '')}/runs/${encodeURIComponent(runId)}/events/stream`
  // Built from one absolute string rather than resolved against a base: a `ws:` URL's origin is not
  // guaranteed to be readable in every environment that implements the URL standard.
  const url = new URL(`${origin}${path}`)

  url.searchParams.set('after_seq', String(Math.max(0, Math.trunc(afterSeq))))
  if (identity.token) {
    // A browser cannot set an Authorization header on a WebSocket handshake, so the token goes in the
    // query string; the backend reads it from either.
    url.searchParams.set('token', identity.token)
  } else if (identity.devRoles && (options.allowDevRoles ?? !import.meta.env.PROD)) {
    url.searchParams.set('dev_roles', identity.devRoles)
  }
  return url.toString()
}

// --------------------------------------------------------------------------- connection state

export type RunStreamState = 'idle' | 'connecting' | 'live' | 'reconnecting' | 'disconnected' | 'closed'

export interface RunStreamStatus {
  state: RunStreamState
  runId: string
  /** The highest durable sequence the caller has applied; what a reconnect resumes from. */
  cursor: number
  /** The run status the server reported when the stream opened, if it has opened. */
  runStatus: string | null
  /** The terminal status from `stream_closed`, when the stream ended because the run ended. */
  closedStatus: string | null
  /** Failed connection attempts since the last successful open. */
  attempts: number
  /** The transport close code of the last close, when there was one. */
  closeCode: number | null
  /** Why the stream is not live, in the transport's own words — never a fabricated diagnosis. */
  lastError: string | null
}

export interface BackoffPolicy {
  initialMs: number
  maxMs: number
  factor: number
  /** Fraction of the delay that may be subtracted or added, to avoid synchronised retries. */
  jitter: number
}

export const DEFAULT_BACKOFF: BackoffPolicy = { initialMs: 250, maxMs: 10_000, factor: 2, jitter: 0.2 }

/** The slice of `WebSocket` this client uses; the browser's implementation satisfies it. */
export interface RunEventSocketLike {
  close(code?: number, reason?: string): void
  onopen: ((ev: Event) => unknown) | null
  onmessage: ((ev: MessageEvent) => unknown) | null
  onerror: ((ev: Event) => unknown) | null
  onclose: ((ev: CloseEvent) => unknown) | null
}

/**
 * A timer handle as the host provides it: a number in browsers, an object in Node. Typing it as the
 * union keeps the injected clock honest in both, instead of pinning the class to one runtime.
 */
export type TimerHandle = number | ReturnType<typeof setTimeout>

export interface RunEventStreamOptions {
  runId: string
  /** Where to read the cursor from; the caller owns it, because the caller owns REST reconciliation. */
  getCursor: () => number
  /** Called for every decoded frame, including ignored ones (diagnostics). */
  onFrame?: (decode: RunStreamDecode) => void
  onStatus?: (status: RunStreamStatus) => void
  /** Called when the run reached a terminal state and the stream closed on purpose. */
  onTerminal?: (status: string) => void
  /** The server saw no new events for a while. Not an ending and not a failure. */
  onIdle?: (lastSeq: number) => void
  url?: (params: { runId: string; afterSeq: number }) => string
  createSocket?: (url: string) => RunEventSocketLike
  backoff?: Partial<BackoffPolicy>
  random?: () => number
  setTimer?: (handler: () => void, ms: number) => TimerHandle
  clearTimer?: (handle: TimerHandle) => void
}

/**
 * One live connection to a run's event stream, with bounded reconnection.
 *
 * The caller owns the cursor (it is the one that merges REST and live events), so this class asks for
 * it every time it connects — which is exactly how a reconnect ends up resuming where the UI actually
 * got to rather than where the socket happened to stop.
 */
export class RunEventStream {
  private socket: RunEventSocketLike | null = null
  private timer: TimerHandle | null = null
  private status: RunStreamStatus
  private readonly backoff: BackoffPolicy
  private disposed = false
  /** True while closing on purpose (dispose, terminal close, or a refusal): no reconnect follows. */
  private stopped = false

  constructor(private readonly options: RunEventStreamOptions) {
    this.backoff = { ...DEFAULT_BACKOFF, ...(options.backoff ?? {}) }
    this.status = {
      state: 'idle',
      runId: options.runId,
      cursor: Math.max(0, options.getCursor()),
      runStatus: null,
      closedStatus: null,
      attempts: 0,
      closeCode: null,
      lastError: null,
    }
  }

  getStatus(): RunStreamStatus {
    return { ...this.status }
  }

  /** Open the stream, unless it is already open or opening. */
  connect(): void {
    if (this.disposed || this.stopped) return
    if (this.status.state === 'connecting' || this.status.state === 'live') return
    this.openSocket()
  }

  /** Stop for good: no timers, no socket, no further reconnects. Safe to call more than once. */
  close(): void {
    this.stopped = true
    this.clearTimer()
    this.teardownSocket(1000, 'client closed')
    this.patchStatus({ state: 'closed' })
  }

  /** Release everything. After this the instance is inert and can be dropped. */
  dispose(): void {
    this.disposed = true
    this.close()
  }

  // ------------------------------------------------------------------ internals

  private openSocket(): void {
    const afterSeq = Math.max(0, this.options.getCursor())
    const url = this.options.url
      ? this.options.url({ runId: this.options.runId, afterSeq })
      : runEventStreamUrl(this.options.runId, afterSeq)

    this.patchStatus({
      state: this.status.attempts > 0 ? 'reconnecting' : 'connecting',
      cursor: afterSeq,
      lastError: null,
    })

    let socket: RunEventSocketLike
    try {
      socket = (this.options.createSocket ?? ((target: string) => new WebSocket(target)))(url)
    } catch (error) {
      // A URL the browser refuses (a bad base, an insecure-context refusal) is a real failure and is
      // reported as one, then retried under the same bounded policy as a dropped connection.
      this.patchStatus({
        state: 'disconnected',
        lastError: error instanceof Error ? error.message : String(error),
      })
      this.scheduleReconnect()
      return
    }

    this.socket = socket
    socket.onopen = () => {
      // The transport is up, but the protocol has not confirmed anything yet: `stream_opened` is what
      // makes the stream live. Claiming "live" here would be the dishonest indicator this mission is
      // meant to avoid.
      this.patchStatus({ closeCode: null })
    }
    socket.onmessage = (event: MessageEvent) => this.handleFrame(event.data)
    socket.onerror = () => this.patchStatus({ lastError: 'the connection failed' })
    socket.onclose = (event: CloseEvent) => this.handleClose(event)
  }

  private handleFrame(data: unknown): void {
    if (this.disposed) return
    let parsed: unknown
    try {
      parsed = typeof data === 'string' ? JSON.parse(data) : data
    } catch {
      this.options.onFrame?.({ kind: 'ignored', reason: 'frame is not valid JSON' })
      return
    }

    const decode = decodeRunStreamFrame(parsed, this.options.runId)
    this.options.onFrame?.(decode)
    if (decode.kind === 'ignored') return
    const message = decode.message

    switch (message.type) {
      case 'stream_opened':
        this.patchStatus({
          state: 'live',
          attempts: 0,
          runStatus: message.status,
          // The server echoes the cursor it resumed from; it can never move ours backwards.
          cursor: Math.max(this.status.cursor, message.after_seq),
          lastError: null,
        })
        break
      case 'run_event':
        // The caller applies it; the cursor advance is recorded here so a reconnect resumes after it.
        break
      case 'stream_idle':
        this.options.onIdle?.(message.last_seq)
        break
      case 'stream_closed':
        this.stopped = true
        this.clearTimer()
        this.patchStatus({ state: 'closed', closedStatus: message.status })
        this.options.onTerminal?.(message.status)
        break
      case 'stream_error':
        // The server closes right after; that close is what triggers the reconnect.
        this.patchStatus({ lastError: message.message ?? 'the stream reported an error' })
        break
    }
  }

  private handleClose(event: CloseEvent): void {
    this.socket = null
    if (this.disposed) return
    const code = typeof event?.code === 'number' ? event.code : null
    this.patchStatus({ closeCode: code })

    if (this.stopped) {
      this.patchStatus({ state: 'closed' })
      return
    }
    if (isRefusalCode(code)) {
      // 4401/4403/4404: no token, no `workflow.read`, or no such run. Retrying an identity the server
      // has already refused is a reconnect loop that can never succeed.
      this.stopped = true
      this.patchStatus({
        state: 'closed',
        lastError:
          code === 4404
            ? 'the server has no such run'
            : code === 4403
              ? 'this identity may not read this run'
              : 'the server refused the connection',
      })
      return
    }
    this.patchStatus({ state: 'disconnected', attempts: this.status.attempts + 1 })
    this.scheduleReconnect()
  }

  private scheduleReconnect(): void {
    if (this.disposed || this.stopped || this.timer !== null) return
    const delay = this.nextDelay()
    this.patchStatus({ state: 'reconnecting' })
    const setTimer = this.options.setTimer ?? ((handler: () => void, ms: number) => setTimeout(handler, ms))
    this.timer = setTimer(() => {
      this.timer = null
      if (this.disposed || this.stopped) return
      this.openSocket()
    }, delay)
  }

  private nextDelay(): number {
    const random = this.options.random ?? Math.random
    const { initialMs, maxMs, factor, jitter } = this.backoff
    const attempt = Math.max(0, this.status.attempts - 1)
    const base = Math.min(maxMs, initialMs * factor ** attempt)
    const spread = base * jitter
    return Math.max(0, Math.round(base - spread + random() * spread * 2))
  }

  private teardownSocket(code: number, reason: string): void {
    const socket = this.socket
    this.socket = null
    if (!socket) return
    socket.onopen = null
    socket.onmessage = null
    socket.onerror = null
    socket.onclose = null
    try {
      socket.close(code, reason)
    } catch {
      // A socket that is already gone throws here; that is the state we were trying to reach.
    }
  }

  private clearTimer(): void {
    if (this.timer === null) return
    // Chosen by the handle's own type rather than cast: a browser hands out numbers and Node hands
    // out objects, and `clearTimeout` has an overload for each.
    const clear =
      this.options.clearTimer ??
      ((handle: TimerHandle) => {
        if (typeof handle === 'number') clearTimeout(handle)
        else clearTimeout(handle)
      })
    clear(this.timer)
    this.timer = null
  }

  private patchStatus(patch: Partial<RunStreamStatus>): void {
    const next = { ...this.status, ...patch }
    // Only announce a real change: a status listener is what re-renders the indicator, and repeating
    // the same state (the transport opening, say, before the protocol confirms anything) would make
    // "connecting" look like it happened twice.
    const changed = (Object.keys(next) as Array<keyof RunStreamStatus>).some((key) => next[key] !== this.status[key])
    this.status = next
    if (changed) this.options.onStatus?.(this.getStatus())
  }
}

/** The reconnect timeline, for a UI that wants to say what it is doing. Bounded by design. */
export function backoffDelays(policy: BackoffPolicy = DEFAULT_BACKOFF, attempts = 8): number[] {
  const delays: number[] = []
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    delays.push(Math.min(policy.maxMs, policy.initialMs * policy.factor ** attempt))
  }
  return delays
}
