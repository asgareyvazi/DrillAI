/**
 * Well live stream transport (`/api/v1/wells/{well_id}/live/stream`).
 *
 * The backend emits a per-well contiguous sequence (`well_sequence`) together with an
 * org+well-scoped cursor token (`cursor`). This module owns:
 * - building the WebSocket URL across dev proxy, preview proxy, and production origins;
 * - decoding and validating all 8 server frame types (`stream_opened`, `snapshot`, `telemetry`,
 *   `event`, `gap`, `heartbeat`, `stream_idle`, `stream_error`);
 * - rejecting frames belonging to another well (`expectedWellId`);
 * - deduplicating events by `(well_sequence, event.id)` with a bounded ring buffer;
 * - reconnecting with bounded exponential backoff while refusing to retry permanent close codes
 *   (`4401`, `4403`, `4404`).
 */

import { API_BASE, getIdentity } from '../api/client'
import type { WellLiveSnapshot } from '../api/types'
import {
  DEFAULT_BACKOFF,
  isRefusalCode,
  type BackoffPolicy,
  type RunEventSocketLike,
  type StreamUrlIdentity,
  type StreamUrlOptions,
  type TimerHandle,
} from './runEvents'

export interface WellOutboxEnvelope {
  id: string
  sequence: number
  well_sequence?: number | null
  org_sequence?: number
  cursor?: string | null
  schema_version: number
  type: string
  occurred_at: string | null
  scope: {
    org_id: string
    well_id: string | null
    wellbore_id: string | null
  }
  subject: {
    kind: string
    id: string | null
  }
  payload: Record<string, unknown>
}

export interface WellStreamOpenedFrame {
  type: 'stream_opened'
  well_id: string
  position: number
  after_seq: number
  cursor: string | null
  cursor_scope?: Record<string, unknown>
  poll_seconds: number
  schema_version: number
}

export interface WellSnapshotFrame {
  type: 'snapshot'
  snapshot: WellLiveSnapshot
}

export interface WellTelemetryCoalescedFrame {
  type: 'telemetry'
  well_id: string
  from_seq: number
  to_seq: number
  count: number
  omitted: number
  resync: boolean
  channels: string[]
  last_ts: string | null
  cursor: string | null
}

export interface WellEventFrame {
  type: 'event'
  event: WellOutboxEnvelope
  cursor: string | null
}

export interface WellGapFrame {
  type: 'gap'
  well_id: string
  from_seq: number
  to_seq: number
  reason: string
  resync: boolean
  cursor: string | null
}

export interface WellHeartbeatFrame {
  type: 'heartbeat'
  well_id: string
  position: number
  cursor: string | null
  ts: string | null
}

export interface WellStreamIdleFrame {
  type: 'stream_idle'
  well_id: string
  last_seq: number
  cursor: string | null
}

export interface WellStreamErrorFrame {
  type: 'stream_error'
  message?: string
}

export type WellLiveFrame =
  | WellStreamOpenedFrame
  | WellSnapshotFrame
  | WellTelemetryCoalescedFrame
  | WellEventFrame
  | WellGapFrame
  | WellHeartbeatFrame
  | WellStreamIdleFrame
  | WellStreamErrorFrame

export type WellLiveDecode =
  | { kind: 'frame'; frame: WellLiveFrame }
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

export function decodeWellLiveFrame(raw: unknown, expectedWellId: string): WellLiveDecode {
  if (!isRecord(raw)) return { kind: 'ignored', reason: 'frame is not an object' }
  const type = asString(raw.type)
  if (!type) return { kind: 'ignored', reason: 'frame has no type' }

  switch (type) {
    case 'stream_opened': {
      const wellId = asString(raw.well_id)
      const position = asNumber(raw.position)
      const afterSeq = asNumber(raw.after_seq)
      if (wellId !== expectedWellId) {
        return { kind: 'ignored', reason: `stream_opened for ${wellId ?? 'unknown well'}` }
      }
      if (position === null || afterSeq === null) {
        return { kind: 'ignored', reason: 'stream_opened is incomplete' }
      }
      return {
        kind: 'frame',
        frame: {
          type,
          well_id: wellId,
          position,
          after_seq: afterSeq,
          cursor: asString(raw.cursor),
          cursor_scope: isRecord(raw.cursor_scope) ? raw.cursor_scope : undefined,
          poll_seconds: asNumber(raw.poll_seconds) ?? 0.5,
          schema_version: asNumber(raw.schema_version) ?? 1,
        },
      }
    }
    case 'snapshot': {
      if (!isRecord(raw.snapshot)) {
        return { kind: 'ignored', reason: 'snapshot frame carries no snapshot object' }
      }
      const wellId = asString(raw.snapshot.well_id)
      if (wellId !== expectedWellId) {
        return { kind: 'ignored', reason: `snapshot for ${wellId ?? 'unknown well'}` }
      }
      return {
        kind: 'frame',
        frame: {
          type,
          snapshot: raw.snapshot as unknown as WellLiveSnapshot,
        },
      }
    }
    case 'telemetry': {
      const wellId = asString(raw.well_id)
      const fromSeq = asNumber(raw.from_seq)
      const toSeq = asNumber(raw.to_seq)
      if (wellId !== expectedWellId) {
        return { kind: 'ignored', reason: `telemetry frame for ${wellId ?? 'unknown well'}` }
      }
      if (fromSeq === null || toSeq === null) {
        return { kind: 'ignored', reason: 'telemetry frame missing sequence bounds' }
      }
      const channels = Array.isArray(raw.channels)
        ? raw.channels.filter((item): item is string => typeof item === 'string')
        : []
      return {
        kind: 'frame',
        frame: {
          type,
          well_id: wellId,
          from_seq: fromSeq,
          to_seq: toSeq,
          count: asNumber(raw.count) ?? 1,
          omitted: asNumber(raw.omitted) ?? 0,
          resync: raw.resync === true,
          channels,
          last_ts: asString(raw.last_ts),
          cursor: asString(raw.cursor),
        },
      }
    }
    case 'event': {
      if (!isRecord(raw.event)) {
        return { kind: 'ignored', reason: 'event frame carries no event envelope' }
      }
      const ev = raw.event
      const id = asString(ev.id)
      const seq = asNumber(ev.well_sequence) ?? asNumber(ev.sequence)
      const eventType = asString(ev.type)
      const scope = isRecord(ev.scope) ? ev.scope : null
      const scopeWellId = scope ? asString(scope.well_id) : null
      if (scopeWellId && scopeWellId !== expectedWellId) {
        return { kind: 'ignored', reason: `event for ${scopeWellId}` }
      }
      if (!id || seq === null || seq < 1 || !eventType) {
        return { kind: 'ignored', reason: 'event envelope is incomplete' }
      }
      return {
        kind: 'frame',
        frame: {
          type,
          event: {
            ...(ev as unknown as WellOutboxEnvelope),
            sequence: seq,
          },
          cursor: asString(raw.cursor) ?? asString(ev.cursor),
        },
      }
    }
    case 'gap': {
      const wellId = asString(raw.well_id)
      const fromSeq = asNumber(raw.from_seq)
      const toSeq = asNumber(raw.to_seq)
      if (wellId !== expectedWellId) {
        return { kind: 'ignored', reason: `gap for ${wellId ?? 'unknown well'}` }
      }
      if (fromSeq === null || toSeq === null) {
        return { kind: 'ignored', reason: 'gap frame is incomplete' }
      }
      return {
        kind: 'frame',
        frame: {
          type,
          well_id: wellId,
          from_seq: fromSeq,
          to_seq: toSeq,
          reason: asString(raw.reason) ?? 'backlog_exceeded',
          resync: raw.resync !== false,
          cursor: asString(raw.cursor),
        },
      }
    }
    case 'heartbeat': {
      const wellId = asString(raw.well_id)
      const position = asNumber(raw.position)
      if (wellId !== expectedWellId) {
        return { kind: 'ignored', reason: `heartbeat for ${wellId ?? 'unknown well'}` }
      }
      if (position === null) {
        return { kind: 'ignored', reason: 'heartbeat has no position' }
      }
      return {
        kind: 'frame',
        frame: {
          type,
          well_id: wellId,
          position,
          cursor: asString(raw.cursor),
          ts: asString(raw.ts),
        },
      }
    }
    case 'stream_idle': {
      const wellId = asString(raw.well_id)
      const lastSeq = asNumber(raw.last_seq)
      if (wellId && wellId !== expectedWellId) {
        return { kind: 'ignored', reason: `stream_idle for ${wellId}` }
      }
      if (lastSeq === null) {
        return { kind: 'ignored', reason: 'stream_idle has no last_seq' }
      }
      return {
        kind: 'frame',
        frame: {
          type,
          well_id: wellId ?? expectedWellId,
          last_seq: lastSeq,
          cursor: asString(raw.cursor),
        },
      }
    }
    case 'stream_error': {
      return {
        kind: 'frame',
        frame: {
          type,
          message: asString(raw.message) ?? undefined,
        },
      }
    }
    default:
      return { kind: 'ignored', reason: `unknown frame type '${type}'` }
  }
}

export interface WellStreamCursorState {
  afterSeq: number
  cursor?: string | null
}

export function wellLiveStreamUrl(
  wellId: string,
  cursorState: WellStreamCursorState,
  identity: StreamUrlIdentity = getIdentity(),
  options: StreamUrlOptions = {},
): string {
  const base = options.apiBase ?? API_BASE
  const page = options.location ?? {
    protocol: window.location.protocol,
    host: window.location.host,
  }
  const absolute = /^https?:\/\//.test(base)
  const secure = absolute ? base.startsWith('https://') : page.protocol === 'https:'
  const scheme = secure ? 'wss' : 'ws'
  const parsed = absolute ? new URL(base) : null
  const origin = parsed ? `${scheme}://${parsed.host}` : `${scheme}://${page.host}`
  const basePath = parsed ? parsed.pathname : base.startsWith('/') ? base : `/${base}`

  const path = `${basePath.replace(/\/$/, '')}/wells/${encodeURIComponent(wellId)}/live/stream`
  const url = new URL(`${origin}${path}`)

  url.searchParams.set('after_seq', String(Math.max(0, Math.trunc(cursorState.afterSeq))))
  if (cursorState.cursor) {
    url.searchParams.set('cursor', cursorState.cursor)
  }
  if (identity.token) {
    url.searchParams.set('token', identity.token)
  } else if (identity.devRoles && (options.allowDevRoles ?? !import.meta.env.PROD)) {
    url.searchParams.set('dev_roles', identity.devRoles)
  }
  return url.toString()
}

export type WellTransportState =
  | 'LIVE'
  | 'RECONNECTING'
  | 'RESYNCING'
  | 'POLLING FALLBACK'
  | 'STALE'
  | 'DISCONNECTED'
  | 'ERROR'

export interface WellStreamTransportStatus {
  state: 'idle' | 'connecting' | 'live' | 'reconnecting' | 'disconnected' | 'error'
  wellId: string
  afterSeq: number
  cursor: string | null
  attempts: number
  closeCode: number | null
  lastError: string | null
}

export interface WellLiveStreamOptions {
  wellId: string
  getCursorState: () => WellStreamCursorState
  onFrame?: (decode: WellLiveDecode) => void
  onStatus?: (status: WellStreamTransportStatus) => void
  url?: (params: { wellId: string; afterSeq: number; cursor: string | null }) => string
  createSocket?: (url: string) => RunEventSocketLike
  backoff?: Partial<BackoffPolicy>
  random?: () => number
  setTimer?: (handler: () => void, ms: number) => TimerHandle
  clearTimer?: (handle: TimerHandle) => void
}

const MAX_SEEN_IDS = 512

export class WellLiveStream {
  private socket: RunEventSocketLike | null = null
  private timer: TimerHandle | null = null
  private status: WellStreamTransportStatus
  private readonly backoff: BackoffPolicy
  private readonly seenEventIds = new Set<string>()
  private readonly seenOrder: string[] = []
  private disposed = false
  private stopped = false

  constructor(private readonly options: WellLiveStreamOptions) {
    this.backoff = { ...DEFAULT_BACKOFF, ...(options.backoff ?? {}) }
    const initial = options.getCursorState()
    this.status = {
      state: 'idle',
      wellId: options.wellId,
      afterSeq: Math.max(0, initial.afterSeq),
      cursor: initial.cursor ?? null,
      attempts: 0,
      closeCode: null,
      lastError: null,
    }
  }

  getStatus(): WellStreamTransportStatus {
    return { ...this.status }
  }

  connect(): void {
    if (this.disposed || this.stopped) return
    if (this.status.state === 'connecting' || this.status.state === 'live') return
    this.openSocket()
  }

  close(): void {
    this.stopped = true
    this.clearTimer()
    this.teardownSocket(1000, 'client closed')
    if (this.status.state !== 'error') {
      this.patchStatus({ state: 'disconnected' })
    }
  }

  dispose(): void {
    this.disposed = true
    this.close()
  }

  private rememberEventId(id: string): boolean {
    if (this.seenEventIds.has(id)) return false
    this.seenEventIds.add(id)
    this.seenOrder.push(id)
    if (this.seenOrder.length > MAX_SEEN_IDS) {
      const oldest = this.seenOrder.shift()
      if (oldest) this.seenEventIds.delete(oldest)
    }
    return true
  }

  private openSocket(): void {
    this.clearTimer()
    const cursorState = this.options.getCursorState()
    const afterSeq = Math.max(this.status.afterSeq, Math.max(0, cursorState.afterSeq))
    const cursor = cursorState.cursor ?? this.status.cursor
    this.patchStatus({
      state: this.status.attempts > 0 ? 'reconnecting' : 'connecting',
      afterSeq,
      cursor,
    })

    const targetUrl = this.options.url
      ? this.options.url({ wellId: this.options.wellId, afterSeq, cursor })
      : wellLiveStreamUrl(this.options.wellId, { afterSeq, cursor })

    const makeSocket =
      this.options.createSocket ?? ((url: string) => new WebSocket(url) as RunEventSocketLike)
    let socket: RunEventSocketLike
    try {
      socket = makeSocket(targetUrl)
    } catch (err) {
      this.handleDisconnect(null, err instanceof Error ? err.message : String(err))
      return
    }
    this.socket = socket

    socket.onopen = () => {
      if (this.disposed || this.socket !== socket) return
      this.patchStatus({
        state: 'live',
        attempts: 0,
        closeCode: null,
        lastError: null,
      })
    }

    socket.onmessage = (messageEvent: MessageEvent) => {
      if (this.disposed || this.socket !== socket) return
      let parsed: unknown
      try {
        parsed = JSON.parse(String(messageEvent.data))
      } catch {
        this.options.onFrame?.({ kind: 'ignored', reason: 'frame is not valid JSON' })
        return
      }
      const decoded = decodeWellLiveFrame(parsed, this.options.wellId)
      if (decoded.kind === 'ignored') {
        this.options.onFrame?.(decoded)
        return
      }

      const { frame } = decoded
      if (frame.type === 'stream_opened') {
        const nextSeq = Math.max(this.status.afterSeq, frame.after_seq)
        this.patchStatus({
          state: 'live',
          afterSeq: nextSeq,
          cursor: frame.cursor ?? this.status.cursor,
          attempts: 0,
          lastError: null,
        })
      } else if (frame.type === 'snapshot') {
        const nextSeq = Math.max(this.status.afterSeq, frame.snapshot.stream_position ?? 0)
        this.patchStatus({
          afterSeq: nextSeq,
          cursor: frame.snapshot.cursor ?? this.status.cursor,
        })
      } else if (frame.type === 'telemetry') {
        if (frame.to_seq <= this.status.afterSeq) {
          this.options.onFrame?.({
            kind: 'ignored',
            reason: `duplicate telemetry range up to ${frame.to_seq}`,
          })
          return
        }
        this.patchStatus({
          afterSeq: Math.max(this.status.afterSeq, frame.to_seq),
          cursor: frame.cursor ?? this.status.cursor,
        })
      } else if (frame.type === 'event') {
        const seq = frame.event.sequence
        if (seq <= this.status.afterSeq || !this.rememberEventId(frame.event.id)) {
          this.options.onFrame?.({
            kind: 'ignored',
            reason: `duplicate event ${frame.event.id}@${seq}`,
          })
          return
        }
        this.patchStatus({
          afterSeq: Math.max(this.status.afterSeq, seq),
          cursor: frame.cursor ?? this.status.cursor,
        })
      } else if (frame.type === 'gap') {
        this.patchStatus({
          afterSeq: Math.max(this.status.afterSeq, frame.to_seq),
          cursor: frame.cursor ?? this.status.cursor,
        })
      } else if (frame.type === 'heartbeat') {
        if (frame.position > this.status.afterSeq) {
          this.patchStatus({
            afterSeq: frame.position,
            cursor: frame.cursor ?? this.status.cursor,
          })
        }
      } else if (frame.type === 'stream_idle') {
        if (frame.last_seq > this.status.afterSeq) {
          this.patchStatus({
            afterSeq: frame.last_seq,
            cursor: frame.cursor ?? this.status.cursor,
          })
        }
      } else if (frame.type === 'stream_error') {
        this.patchStatus({
          lastError: frame.message ?? 'server reported a stream error',
        })
      }

      this.options.onFrame?.(decoded)
    }

    socket.onerror = () => {
      if (this.disposed || this.socket !== socket) return
      if (!this.status.lastError) {
        this.patchStatus({ lastError: 'websocket transport error' })
      }
    }

    socket.onclose = (closeEvent: CloseEvent) => {
      if (this.disposed || this.socket !== socket) return
      this.socket = null
      const code = typeof closeEvent?.code === 'number' ? closeEvent.code : null
      const reason =
        typeof closeEvent?.reason === 'string' && closeEvent.reason.trim()
          ? closeEvent.reason.trim()
          : this.status.lastError
      this.handleDisconnect(code, reason)
    }
  }

  private handleDisconnect(code: number | null, reason: string | null): void {
    if (this.disposed || this.stopped) return
    if (isRefusalCode(code)) {
      this.stopped = true
      this.patchStatus({
        state: 'error',
        closeCode: code,
        lastError: reason ?? `connection refused (${code})`,
      })
      return
    }
    const attempts = this.status.attempts + 1
    this.patchStatus({
      state: 'reconnecting',
      attempts,
      closeCode: code,
      lastError: reason ?? (code !== null ? `socket closed (${code})` : 'socket closed'),
    })
    const delay = this.computeDelay(attempts)
    const setTimer = this.options.setTimer ?? ((fn, ms) => window.setTimeout(fn, ms))
    this.timer = setTimer(() => {
      this.timer = null
      if (!this.disposed && !this.stopped) {
        this.openSocket()
      }
    }, delay)
  }

  private computeDelay(attempt: number): number {
    const raw = Math.min(
      this.backoff.maxMs,
      this.backoff.initialMs * Math.pow(this.backoff.factor, Math.max(0, attempt - 1)),
    )
    const rand = (this.options.random ?? Math.random)()
    const jitterSpan = raw * this.backoff.jitter
    const offset = (rand * 2 - 1) * jitterSpan
    return Math.max(50, Math.round(raw + offset))
  }

  private clearTimer(): void {
    if (this.timer === null) return
    const clear =
      this.options.clearTimer ?? ((handle: TimerHandle) => window.clearTimeout(handle as number))
    clear(this.timer)
    this.timer = null
  }

  private teardownSocket(code?: number, reason?: string): void {
    const sock = this.socket
    this.socket = null
    if (!sock) return
    sock.onopen = null
    sock.onmessage = null
    sock.onerror = null
    sock.onclose = null
    try {
      sock.close(code, reason)
    } catch {
      // Ignore close errors on already-closed sockets
    }
  }

  private patchStatus(patch: Partial<WellStreamTransportStatus>): void {
    this.status = { ...this.status, ...patch }
    this.options.onStatus?.(this.getStatus())
  }
}
