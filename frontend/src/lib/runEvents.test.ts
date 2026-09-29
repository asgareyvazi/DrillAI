/**
 * The run event stream: protocol, cursor, deduplication, reconnection and disposal.
 *
 * Everything here is deterministic on purpose. The clock, the timers and the socket are injected, so
 * a reconnect test states exactly when the transport dropped and exactly when the retry happened
 * instead of sleeping and hoping. The socket double is a *test* double: the browser suite drives the
 * real server, which is where the wire contract is actually proved.
 *
 * What is proved here is the part a real server cannot prove for us: that the client resumes from the
 * sequence it actually applied, never shows an event twice, never claims to be live while it is not,
 * and leaves nothing running after the screen that owns it is gone.
 */

import { describe, expect, it } from 'vitest'
import {
  DEFAULT_BACKOFF,
  RunEventStream,
  backoffDelays,
  decodeRunStreamFrame,
  highestSeq,
  isRefusalCode,
  mergeRunEvents,
  runEventKey,
  runEventStreamUrl,
} from './runEvents'
import type { RunEventSocketLike, RunStreamDecode, RunStreamStatus } from './runEvents'
import type { RunEvent } from '../api/types'

const at = <T,>(items: T[], index: number): T => {
  const value = items[index]
  if (value === undefined) throw new Error(`no item at ${index}`)
  return value
}

function event(runId: string, seq: number, type = 'node_succeeded'): RunEvent {
  return {
    id: `rev_${runId}_${seq}`,
    run_id: runId,
    seq,
    type,
    message: `${type} at ${seq}`,
    level: 'info',
    node_id: null,
    occurred_at: `2026-03-15T06:00:${String(seq).padStart(2, '0')}Z`,
    payload: {},
  }
}

// --------------------------------------------------------------------------- a deterministic socket

class FakeSocket implements RunEventSocketLike {
  onopen: ((ev: Event) => unknown) | null = null
  onmessage: ((ev: MessageEvent) => unknown) | null = null
  onerror: ((ev: Event) => unknown) | null = null
  onclose: ((ev: CloseEvent) => unknown) | null = null

  closed: { code: number | undefined; reason: string | undefined } | null = null

  constructor(readonly url: string) {}

  close(code?: number, reason?: string): void {
    this.closed = { code, reason }
    this.onclose?.({ code: code ?? 1000 } as CloseEvent)
  }

  /** The server accepting the connection and opening the protocol. */
  open(runId: string, status = 'running', afterSeq = 0): void {
    this.onopen?.(new Event('open'))
    this.send({ type: 'stream_opened', run_id: runId, status, after_seq: afterSeq })
  }

  send(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent)
  }

  sendRaw(data: string): void {
    this.onmessage?.({ data } as MessageEvent)
  }

  drop(code = 1006): void {
    this.onclose?.({ code } as CloseEvent)
  }
}

/** A manual clock: retries happen when the test says they do. */
function harness(options: {
  runId?: string
  cursor?: number
  backoff?: { initialMs: number; maxMs: number; factor: number; jitter: number }
}) {
  const sockets: FakeSocket[] = []
  const timers: Array<{ handle: number; fn: () => void; ms: number }> = []
  const frames: RunStreamDecode[] = []
  const statuses: RunStreamStatus[] = []
  const terminals: string[] = []
  const idles: number[] = []
  let cursor = options.cursor ?? 0
  let handle = 0

  const stream = new RunEventStream({
    runId: options.runId ?? 'run_1',
    getCursor: () => cursor,
    backoff: options.backoff ?? { ...DEFAULT_BACKOFF, jitter: 0 },
    createSocket: (url) => {
      const socket = new FakeSocket(url)
      sockets.push(socket)
      return socket
    },
    onFrame: (decode) => frames.push(decode),
    onStatus: (status) => statuses.push(status),
    onTerminal: (status) => terminals.push(status),
    onIdle: (lastSeq) => idles.push(lastSeq),
    setTimer: (fn, ms) => {
      handle += 1
      timers.push({ handle, fn, ms })
      return handle
    },
    clearTimer: (timerHandle) => {
      const index = timers.findIndex((timer) => timer.handle === Number(timerHandle))
      if (index >= 0) timers.splice(index, 1)
    },
    random: () => 0.5,
  })

  return {
    stream,
    sockets,
    timers,
    frames,
    statuses,
    terminals,
    idles,
    setCursor: (next: number) => {
      cursor = next
    },
    /** Fire every pending retry timer, as the event loop would. */
    flushTimers: () => {
      while (timers.length) {
        const timer = timers.shift() as { fn: () => void; ms: number }
        timer.fn()
      }
    },
    status: () => stream.getStatus().state,
  }
}

// --------------------------------------------------------------------------- protocol

describe('decodeRunStreamFrame: the wire protocol, validated rather than assumed', () => {
  it('accepts each documented frame', () => {
    const opened = decodeRunStreamFrame(
      { type: 'stream_opened', run_id: 'run_1', status: 'waiting_approval', after_seq: 7 },
      'run_1',
    )
    expect(opened).toEqual({
      kind: 'message',
      message: { type: 'stream_opened', run_id: 'run_1', status: 'waiting_approval', after_seq: 7 },
    })

    const runEvent = decodeRunStreamFrame({ type: 'run_event', event: event('run_1', 12) }, 'run_1')
    expect(runEvent.kind).toBe('message')
    if (runEvent.kind === 'message' && runEvent.message.type === 'run_event') {
      expect(runEvent.message.event.seq).toBe(12)
    }

    expect(decodeRunStreamFrame({ type: 'stream_idle', last_seq: 12 }, 'run_1')).toEqual({
      kind: 'message',
      message: { type: 'stream_idle', last_seq: 12 },
    })
    expect(decodeRunStreamFrame({ type: 'stream_closed', status: 'succeeded' }, 'run_1')).toEqual({
      kind: 'message',
      message: { type: 'stream_closed', status: 'succeeded' },
    })
    expect(decodeRunStreamFrame({ type: 'stream_error', message: 'boom' }, 'run_1')).toEqual({
      kind: 'message',
      message: { type: 'stream_error', message: 'boom' },
    })
  })

  it('ignores an empty frame instead of throwing', () => {
    expect(decodeRunStreamFrame({}, 'run_1')).toEqual({ kind: 'ignored', reason: 'frame has no type' })
  })

  it('ignores a frame type this build does not know', () => {
    const decoded = decodeRunStreamFrame({ type: 'future_event', foo: 'bar' }, 'run_1')
    expect(decoded).toEqual({ kind: 'ignored', reason: "unknown frame type 'future_event'" })
  })

  it('ignores an event whose sequence is not a number', () => {
    const decoded = decodeRunStreamFrame(
      { type: 'run_event', event: { id: 'rev_1', run_id: 'run_1', type: 'x', seq: 'abc' } },
      'run_1',
    )
    expect(decoded).toEqual({ kind: 'ignored', reason: 'run_event has no usable sequence' })
  })

  it('ignores an event belonging to another run', () => {
    const decoded = decodeRunStreamFrame({ type: 'run_event', event: event('run_other', 3) }, 'run_1')
    expect(decoded).toEqual({ kind: 'ignored', reason: 'run_event for run_other' })
  })

  it('ignores a frame whose shape is wrong at every level', () => {
    expect(decodeRunStreamFrame(null, 'run_1').kind).toBe('ignored')
    expect(decodeRunStreamFrame('a string', 'run_1').kind).toBe('ignored')
    expect(decodeRunStreamFrame([1, 2], 'run_1').kind).toBe('ignored')
    expect(decodeRunStreamFrame({ type: 'run_event' }, 'run_1').kind).toBe('ignored')
    expect(decodeRunStreamFrame({ type: 'stream_idle' }, 'run_1').kind).toBe('ignored')
    expect(decodeRunStreamFrame({ type: 'stream_closed' }, 'run_1').kind).toBe('ignored')
    expect(decodeRunStreamFrame({ type: 'stream_opened', run_id: 'run_1' }, 'run_1').kind).toBe('ignored')
  })
})

// --------------------------------------------------------------------------- URL

describe('runEventStreamUrl: derived from the configured API base', () => {
  const page = { protocol: 'http:', host: 'localhost:5199' }

  it('builds a ws URL on the page origin from a relative base', () => {
    const url = runEventStreamUrl('run_1', 0, { token: null, devRoles: null }, { apiBase: '/api/v1', location: page })
    expect(url).toBe('ws://localhost:5199/api/v1/runs/run_1/events/stream?after_seq=0')
  })

  it('uses wss on an https page', () => {
    const url = runEventStreamUrl(
      'run_1',
      3,
      { token: null, devRoles: null },
      { apiBase: '/api/v1', location: { protocol: 'https:', host: 'drill.example.com' } },
    )
    expect(url).toBe('wss://drill.example.com/api/v1/runs/run_1/events/stream?after_seq=3')
  })

  it('keeps the host of an absolute base and switches the scheme', () => {
    const url = runEventStreamUrl(
      'run_1',
      10,
      { token: null, devRoles: null },
      { apiBase: 'http://127.0.0.1:8099/api/v1', location: page },
    )
    expect(url).toBe('ws://127.0.0.1:8099/api/v1/runs/run_1/events/stream?after_seq=10')

    const secure = runEventStreamUrl(
      'run_1',
      10,
      { token: null, devRoles: null },
      { apiBase: 'https://api.example.com/api/v1', location: page },
    )
    expect(secure).toBe('wss://api.example.com/api/v1/runs/run_1/events/stream?after_seq=10')
  })

  it('encodes the run id rather than interpolating it raw', () => {
    const url = runEventStreamUrl(
      'run/1 2',
      0,
      { token: null, devRoles: null },
      { apiBase: '/api/v1', location: page },
    )
    expect(url).toContain('/runs/run%2F1%202/events/stream')
  })

  it('carries the token when one is configured, and the development role when one is not', () => {
    const withToken = runEventStreamUrl(
      'run_1',
      0,
      { token: 'tok_123', devRoles: 'engineer' },
      { apiBase: '/api/v1', location: page },
    )
    expect(withToken).toContain('token=tok_123')
    expect(withToken).not.toContain('dev_roles')

    const withDevRoles = runEventStreamUrl(
      'run_1',
      0,
      { token: null, devRoles: 'well_manager' },
      { apiBase: '/api/v1', location: page, allowDevRoles: true },
    )
    expect(withDevRoles).toContain('dev_roles=well_manager')
  })

  it('never sends a development role when the build forbids it', () => {
    // A production bundle must not impersonate, whatever the session store happens to hold.
    const url = runEventStreamUrl(
      'run_1',
      0,
      { token: null, devRoles: 'well_manager' },
      { apiBase: '/api/v1', location: page, allowDevRoles: false },
    )
    expect(url).not.toContain('dev_roles')
    expect(url).toBe('ws://localhost:5199/api/v1/runs/run_1/events/stream?after_seq=0')
  })

  it('never sends a negative or fractional cursor', () => {
    const url = runEventStreamUrl(
      'run_1',
      -5.4,
      { token: null, devRoles: null },
      { apiBase: '/api/v1', location: page },
    )
    expect(url).toContain('after_seq=0')
  })
})

// --------------------------------------------------------------------------- merging

describe('mergeRunEvents: one durable event, however many times it arrives', () => {
  it('keeps one copy when REST and the socket carry the same event', () => {
    const rest = [event('run_1', 1), event('run_1', 2)]
    const merged = mergeRunEvents(rest, [event('run_1', 2), event('run_1', 3)])
    expect(merged.map((row) => row.seq)).toEqual([1, 2, 3])
  })

  it('keeps one copy when the socket arrives first and REST after', () => {
    const merged = mergeRunEvents([event('run_1', 5)], [event('run_1', 5)])
    expect(merged).toHaveLength(1)
  })

  it('keeps one copy when a reconnect replays what was already applied', () => {
    const applied = [event('run_1', 1), event('run_1', 2)]
    const replayed = [event('run_1', 1), event('run_1', 2), event('run_1', 3)]
    expect(mergeRunEvents(applied, replayed).map((row) => row.seq)).toEqual([1, 2, 3])
    expect(mergeRunEvents(replayed, applied).map((row) => row.seq)).toEqual([1, 2, 3])
  })

  it('orders by the server sequence, not by arrival', () => {
    const merged = mergeRunEvents([event('run_1', 12)], [event('run_1', 11)])
    expect(merged.map((row) => row.seq)).toEqual([11, 12])
  })

  it('does not merge two runs that happen to share a sequence number', () => {
    const merged = mergeRunEvents([event('run_1', 4)], [event('run_2', 4)])
    expect(merged).toHaveLength(2)
    expect(new Set(merged.map(runEventKey)).size).toBe(2)
  })

  it('reports the highest sequence, or 0 for an empty log', () => {
    expect(highestSeq([])).toBe(0)
    expect(highestSeq([event('run_1', 3), event('run_1', 9), event('run_1', 4)])).toBe(9)
  })
})

// --------------------------------------------------------------------------- transport lifecycle

describe('RunEventStream: honest state, resumable cursor, bounded retries', () => {
  it('starts connecting, and only becomes live when the protocol says so', () => {
    const h = harness({ runId: 'run_1', cursor: 0 })
    h.stream.connect()
    expect(h.status()).toBe('connecting')
    expect(h.sockets).toHaveLength(1)
    expect(at(h.sockets, 0).url).toContain('after_seq=0')

    // The transport opening is not the stream being live.
    at(h.sockets, 0).onopen?.(new Event('open'))
    expect(h.status()).toBe('connecting')

    at(h.sockets, 0).open('run_1', 'running', 0)
    expect(h.status()).toBe('live')
    h.stream.dispose()
  })

  it('resumes from the cursor the caller gives it, not from where the socket stopped', () => {
    const h = harness({ runId: 'run_1', cursor: 0 })
    h.stream.connect()
    at(h.sockets, 0).open('run_1')

    // Events 1..10 arrive and the caller applies them all.
    h.setCursor(10)
    at(h.sockets, 0).drop(1006)
    expect(h.status()).toBe('reconnecting')
    expect(at(h.timers, 0).ms).toBe(DEFAULT_BACKOFF.initialMs)

    h.flushTimers()
    expect(h.sockets).toHaveLength(2)
    expect(at(h.sockets, 1).url).toContain('after_seq=10')
    h.stream.dispose()
  })

  it('never moves the cursor backwards, whatever the server echoes', () => {
    const h = harness({ runId: 'run_1', cursor: 25 })
    h.stream.connect()
    at(h.sockets, 0).open('run_1', 'running', 25)
    expect(h.stream.getStatus().cursor).toBe(25)

    // A stale echo cannot rewind the client into replaying history.
    at(h.sockets, 0).send({ type: 'stream_opened', run_id: 'run_1', status: 'running', after_seq: 3 })
    expect(h.stream.getStatus().cursor).toBe(25)
    h.stream.dispose()
  })

  it('retries with bounded backoff and stops growing at the ceiling', () => {
    const h = harness({ runId: 'run_1', cursor: 0 })
    h.stream.connect()
    at(h.sockets, 0).drop(1006)
    const delays: number[] = []
    for (let attempt = 0; attempt < 8; attempt += 1) {
      const timer = h.timers.shift()
      if (!timer) break
      delays.push(timer.ms)
      timer.fn()
      at(h.sockets, h.sockets.length - 1).drop(1006)
    }
    expect(delays).toEqual([250, 500, 1000, 2000, 4000, 8000, 10_000, 10_000])
    h.stream.dispose()
  })

  it('resets the backoff after a connection is established again', () => {
    const h = harness({ runId: 'run_1', cursor: 0 })
    h.stream.connect()
    at(h.sockets, 0).drop(1006)
    h.flushTimers()
    at(h.sockets, 1).drop(1006)
    expect(at(h.timers, 0).ms).toBe(500)

    h.flushTimers()
    at(h.sockets, 2).open('run_1')
    expect(h.stream.getStatus().attempts).toBe(0)
    at(h.sockets, 2).drop(1006)
    expect(at(h.timers, 0).ms).toBe(250)
    h.stream.dispose()
  })

  it('stops reconnecting after the run reaches a terminal state', () => {
    const h = harness({ runId: 'run_1', cursor: 0 })
    h.stream.connect()
    at(h.sockets, 0).open('run_1')
    at(h.sockets, 0).send({ type: 'run_event', event: event('run_1', 11) })
    at(h.sockets, 0).send({ type: 'stream_closed', status: 'succeeded' })
    // The server closes the socket right after the frame.
    at(h.sockets, 0).drop(1000)

    expect(h.status()).toBe('closed')
    expect(h.stream.getStatus().closedStatus).toBe('succeeded')
    expect(h.terminals).toEqual(['succeeded'])
    h.flushTimers()
    expect(h.sockets).toHaveLength(1)
    h.stream.dispose()
  })

  it('does not reconnect after a refusal — the identity will not be accepted by waiting', () => {
    const h = harness({ runId: 'run_1', cursor: 0 })
    h.stream.connect()
    at(h.sockets, 0).drop(4403)
    expect(h.status()).toBe('closed')
    h.flushTimers()
    expect(h.sockets).toHaveLength(1)
    h.stream.dispose()
  })

  it('distinguishes "up to date" from "finished" when the stream goes idle', () => {
    const h = harness({ runId: 'run_1', cursor: 0 })
    h.stream.connect()
    at(h.sockets, 0).open('run_1')
    at(h.sockets, 0).send({ type: 'stream_idle', last_seq: 9 })
    expect(h.idles).toEqual([9])
    expect(h.status()).toBe('live')
    expect(h.stream.getStatus().closedStatus).toBeNull()
    h.stream.dispose()
  })

  it('survives malformed frames without losing the connection', () => {
    const h = harness({ runId: 'run_1', cursor: 0 })
    h.stream.connect()
    at(h.sockets, 0).open('run_1')
    at(h.sockets, 0).sendRaw('{not json')
    at(h.sockets, 0).send({})
    at(h.sockets, 0).send({ type: 'future_event' })
    at(h.sockets, 0).send({ type: 'run_event', event: { ...event('run_other', 2) } })
    expect(h.status()).toBe('live')
    expect(h.frames.filter((frame) => frame.kind === 'ignored')).toHaveLength(4)
    expect(h.frames.filter((frame) => frame.kind === 'message')).toHaveLength(1) // stream_opened
    h.stream.dispose()
  })

  it('reports a server-side stream error without pretending the stream is fine', () => {
    const h = harness({ runId: 'run_1', cursor: 0 })
    h.stream.connect()
    at(h.sockets, 0).open('run_1')
    at(h.sockets, 0).send({ type: 'stream_error', message: 'the log could not be read' })
    expect(h.stream.getStatus().lastError).toBe('the log could not be read')
    at(h.sockets, 0).drop(1011)
    expect(h.status()).toBe('reconnecting')
    h.stream.dispose()
  })

  it('closes the socket and stops everything on dispose', () => {
    const h = harness({ runId: 'run_1', cursor: 0 })
    h.stream.connect()
    at(h.sockets, 0).open('run_1')
    h.stream.dispose()

    expect(at(h.sockets, 0).closed?.code).toBe(1000)
    expect(h.status()).toBe('closed')
    expect(h.timers).toHaveLength(0)

    // Even a socket that reports its close after disposal must not restart anything.
    h.stream.connect()
    expect(h.sockets).toHaveLength(1)
  })

  it('stops a pending retry when the owner closes the stream', () => {
    const h = harness({ runId: 'run_1', cursor: 0 })
    h.stream.connect()
    at(h.sockets, 0).drop(1006)
    expect(h.timers).toHaveLength(1)
    h.stream.close()
    expect(h.timers).toHaveLength(0)
    h.flushTimers()
    expect(h.sockets).toHaveLength(1)
  })

  it('does not open a second socket when connect is called twice', () => {
    const h = harness({ runId: 'run_1', cursor: 0 })
    h.stream.connect()
    h.stream.connect()
    expect(h.sockets).toHaveLength(1)
    h.stream.dispose()
  })

  it('reports each state transition once, in order, so the indicator can be honest', () => {
    const h = harness({ runId: 'run_1', cursor: 0 })
    h.stream.connect()
    at(h.sockets, 0).open('run_1')
    at(h.sockets, 0).drop(1006)
    h.flushTimers()
    at(h.sockets, 1).open('run_1')
    h.stream.dispose()

    // Consecutive repeats are collapsed, not filtered out: the listener also receives field-level
    // updates (a new cursor, a close code) that leave the state unchanged, and the indicator must not
    // redraw itself for those. What matters here is that no transition is skipped or invented.
    const states = h.statuses
      .map((status) => status.state)
      .filter((state, index, all) => index === 0 || state !== all[index - 1])
    expect(states).toEqual(['connecting', 'live', 'disconnected', 'reconnecting', 'live', 'closed'])
  })

  it('classifies the refusal codes the server actually sends', () => {
    expect(isRefusalCode(4401)).toBe(true)
    expect(isRefusalCode(4403)).toBe(true)
    expect(isRefusalCode(4404)).toBe(true)
    expect(isRefusalCode(1006)).toBe(false)
    expect(isRefusalCode(null)).toBe(false)
  })

  it('publishes a bounded reconnect timeline', () => {
    const delays = backoffDelays()
    expect(delays[0]).toBe(250)
    expect(delays[delays.length - 1]).toBe(10_000)
    expect(Math.max(...delays)).toBeLessThanOrEqual(10_000)
  })
})
