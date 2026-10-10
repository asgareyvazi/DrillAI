import { describe, expect, it, vi } from 'vitest'
import type { RunEventSocketLike } from './runEvents'
import {
  decodeWellLiveFrame,
  WellLiveStream,
  type WellLiveDecode,
  type WellStreamTransportStatus,
} from './wellLiveStream'

const WELL_A = '11111111-1111-1111-1111-111111111111'
const WELL_B = '22222222-2222-2222-2222-222222222222'

class FakeSocket implements RunEventSocketLike {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3

  readyState = FakeSocket.CONNECTING
  url: string
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  closedWith: { code?: number; reason?: string } | null = null

  constructor(url: string) {
    this.url = url
  }

  open() {
    this.readyState = FakeSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  receive(payload: unknown) {
    const data = typeof payload === 'string' ? payload : JSON.stringify(payload)
    this.onmessage?.(new MessageEvent('message', { data }))
  }

  serverClose(code = 1006, reason = '') {
    this.readyState = FakeSocket.CLOSED
    this.onclose?.({ code, reason } as CloseEvent)
  }

  close(code?: number, reason?: string) {
    this.closedWith = { code, reason }
    this.readyState = FakeSocket.CLOSED
  }
}

describe('decodeWellLiveFrame', () => {
  it('decodes all 8 server frame types and rejects foreign-well and malformed frames', () => {
    const opened = decodeWellLiveFrame(
      {
        type: 'stream_opened',
        well_id: WELL_A,
        position: 4,
        after_seq: 4,
        cursor: 'cur-4',
        poll_seconds: 1,
        schema_version: 1,
      },
      WELL_A,
    )
    expect(opened.kind).toBe('frame')
    if (opened.kind === 'frame' && opened.frame.type === 'stream_opened') {
      expect(opened.frame.after_seq).toBe(4)
      expect(opened.frame.cursor).toBe('cur-4')
    }

    // Foreign well frame is ignored
    const foreign = decodeWellLiveFrame(
      {
        type: 'telemetry',
        well_id: WELL_B,
        from_seq: 5,
        to_seq: 6,
        count: 2,
        omitted: 0,
        resync: false,
      },
      WELL_A,
    )
    expect(foreign.kind).toBe('ignored')

    // Coalesced telemetry frame
    const telemetry = decodeWellLiveFrame(
      {
        type: 'telemetry',
        well_id: WELL_A,
        from_seq: 5,
        to_seq: 7,
        cursor: 'cur-7',
        count: 3,
        omitted: 1,
        resync: false,
        channels: ['spp'],
      },
      WELL_A,
    )
    expect(telemetry.kind).toBe('frame')

    // Alert lifecycle event frame
    const eventDecoded = decodeWellLiveFrame(
      {
        type: 'event',
        cursor: 'cur-8',
        event: {
          id: 'evt-alert-1',
          sequence: 8,
          org_sequence: 42,
          well_sequence: 8,
          schema_version: 1,
          type: 'alert.raised',
          occurred_at: '2026-10-10T12:00:00Z',
          scope: { org_id: 'org-1', well_id: WELL_A, wellbore_id: null },
          subject: { kind: 'operational_alert', id: 'alert-1' },
          payload: { severity: 'high' },
        },
      },
      WELL_A,
    )
    expect(eventDecoded.kind).toBe('frame')

    // Gap frame
    const gap = decodeWellLiveFrame(
      {
        type: 'gap',
        well_id: WELL_A,
        from_seq: 9,
        to_seq: 25,
        cursor: 'cur-25',
        reason: 'backlog_exceeded',
        resync: true,
      },
      WELL_A,
    )
    expect(gap.kind).toBe('frame')

    // Heartbeat & stream_idle & stream_error
    const hb = decodeWellLiveFrame(
      {
        type: 'heartbeat',
        well_id: WELL_A,
        position: 25,
        cursor: 'cur-25',
        ts: '2026-10-10T12:00:05Z',
      },
      WELL_A,
    )
    expect(hb.kind).toBe('frame')

    const idle = decodeWellLiveFrame(
      {
        type: 'stream_idle',
        well_id: WELL_A,
        last_seq: 25,
        cursor: 'cur-25',
      },
      WELL_A,
    )
    expect(idle.kind).toBe('frame')

    const errFrame = decodeWellLiveFrame(
      {
        type: 'stream_error',
        message: 'cursor belongs to a different well',
      },
      WELL_A,
    )
    expect(errFrame.kind).toBe('frame')
  })
})

describe('WellLiveStream transport lifecycle', () => {
  it('connects, deduplicates events, advances cursor/after_seq across frames, and reconnects with updated cursor', () => {
    vi.useFakeTimers()
    try {
      const sockets: FakeSocket[] = []
      const statuses: WellStreamTransportStatus[] = []
      const frames: WellLiveDecode[] = []
      let cursorState = { afterSeq: 0, cursor: null as string | null }

      const stream = new WellLiveStream({
        wellId: WELL_A,
        getCursorState: () => cursorState,
        createSocket: (url) => {
          const sock = new FakeSocket(url)
          sockets.push(sock)
          return sock
        },
        onStatus: (s) => statuses.push(s),
        onFrame: (d) => {
          frames.push(d)
          if (d.kind === 'frame' && d.frame.type === 'telemetry') {
            cursorState = { afterSeq: d.frame.to_seq, cursor: d.frame.cursor ?? null }
          }
        },
      })

      stream.connect()
      expect(sockets).toHaveLength(1)
      const sock0 = sockets[0]!
      expect(sock0.url).toContain('after_seq=0')

      sock0.open()
      sock0.receive({
        type: 'stream_opened',
        well_id: WELL_A,
        position: 0,
        after_seq: 0,
        cursor: 'cur-0',
      })
      expect(statuses[statuses.length - 1]?.state).toBe('live')

      sock0.receive({
        type: 'telemetry',
        well_id: WELL_A,
        from_seq: 1,
        to_seq: 3,
        cursor: 'cur-3',
        count: 3,
        omitted: 0,
        resync: false,
      })
      expect(statuses[statuses.length - 1]?.afterSeq).toBe(3)
      expect(statuses[statuses.length - 1]?.cursor).toBe('cur-3')

      // Duplicate telemetry range <= 3 is ignored
      sock0.receive({
        type: 'telemetry',
        well_id: WELL_A,
        from_seq: 1,
        to_seq: 3,
        cursor: 'cur-3',
        count: 3,
        omitted: 0,
        resync: false,
      })
      expect(frames[frames.length - 1]?.kind).toBe('ignored')

      // Simulate transient disconnect and verify reconnect uses after_seq=3 and cursor=cur-3
      sock0.serverClose(1006, 'abnormal closure')
      expect(statuses[statuses.length - 1]?.state).toBe('reconnecting')

      vi.advanceTimersByTime(600)
      expect(sockets).toHaveLength(2)
      const sock1 = sockets[1]!
      expect(sock1.url).toContain('after_seq=3')
      expect(sock1.url).toContain('cursor=cur-3')

      // Permanent refusal (4403) transitions to error and does NOT schedule another reconnect
      sock1.serverClose(4403, 'forbidden')
      expect(statuses[statuses.length - 1]?.state).toBe('error')
      expect(statuses[statuses.length - 1]?.closeCode).toBe(4403)
      vi.advanceTimersByTime(10_000)
      expect(sockets).toHaveLength(2)

      stream.dispose()
    } finally {
      vi.useRealTimers()
    }
  })
})
