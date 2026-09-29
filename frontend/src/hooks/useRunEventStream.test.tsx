/**
 * The run monitor's realtime layer, as the screen uses it.
 *
 * The stream itself is the real `RunEventStream`; only its socket and clock are supplied by the test,
 * so what is exercised here is the code the application runs: the cursor seeding from REST, the merge
 * into the run query, the reconciliation after a burst, the refusal to stream a run that cannot
 * produce events, and the disposal that leaves nothing behind.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { RunEventStream } from '../lib/runEvents'
import type { RunEventSocketLike } from '../lib/runEvents'
import { useRunEventStream } from './useRunEventStream'
import type { RunDetail, RunEvent } from '../api/types'

function event(seq: number, type = 'node_succeeded'): RunEvent {
  return {
    id: `rev_${seq}`,
    run_id: 'run_1',
    seq,
    type,
    message: `${type} ${seq}`,
    level: 'info',
    node_id: null,
    occurred_at: '2026-03-15T06:00:00Z',
    payload: {},
  }
}

class FakeSocket implements RunEventSocketLike {
  onopen: ((ev: Event) => unknown) | null = null
  onmessage: ((ev: MessageEvent) => unknown) | null = null
  onerror: ((ev: Event) => unknown) | null = null
  onclose: ((ev: CloseEvent) => unknown) | null = null
  closed = false

  constructor(readonly url: string) {}

  close(): void {
    this.closed = true
  }

  open(status = 'running', afterSeq = 0): void {
    this.onopen?.(new Event('open'))
    this.send({ type: 'stream_opened', run_id: 'run_1', status, after_seq: afterSeq })
  }

  send(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent)
  }

  push(seq: number): void {
    this.send({ type: 'run_event', event: event(seq) })
  }

  drop(code = 1006): void {
    this.onclose?.({ code } as CloseEvent)
  }
}

/** One rendered hook plus the transport it created, so a test can drive the server's side. */
function setup(options: {
  runStatus?: string
  restEvents?: RunEvent[]
  runId?: string
  reconcileDebounceMs?: number
}) {
  const sockets: FakeSocket[] = []
  const timers: Array<{ id: number; fn: () => void }> = []
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const runId = options.runId ?? 'run_1'

  queryClient.setQueryData<RunDetail>(['run', runId], {
    run: { id: runId, status: options.runStatus ?? 'running' } as unknown as RunDetail['run'],
    workflow: null,
    node_runs: [],
    artifacts: [],
    events: options.restEvents ?? [],
    pending_approval: null,
    resumable: false,
  } as RunDetail)

  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )

  const view = renderHook(
    () =>
      useRunEventStream(runId, {
        runStatus: options.runStatus ?? 'running',
        restEvents: options.restEvents,
        reconcileDebounceMs: options.reconcileDebounceMs ?? 0,
        createStream: (streamOptions) =>
          new RunEventStream({
            ...streamOptions,
            createSocket: (url) => {
              const socket = new FakeSocket(url)
              sockets.push(socket)
              return socket
            },
            setTimer: (fn) => {
              timers.push({ id: timers.length + 1, fn })
              return timers.length
            },
            clearTimer: (handle) => {
              const index = timers.findIndex((timer) => timer.id === Number(handle))
              if (index >= 0) timers.splice(index, 1)
            },
          }),
      }),
    { wrapper },
  )

  return {
    ...view,
    queryClient,
    sockets,
    timers,
    runId,
    events: () => queryClient.getQueryData<RunDetail>(['run', runId])?.events ?? [],
    /** The server's side of the connection, applied inside `act` so React sees every update. */
    server: {
      open: (status = 'running', afterSeq = 0) => {
        const socket = sockets[sockets.length - 1]
        if (!socket) throw new Error('no socket to open')
        act(() => socket.open(status, afterSeq))
      },
      push: (seq: number) => {
        const socket = sockets[sockets.length - 1]
        if (!socket) throw new Error('no socket to push to')
        act(() => socket.push(seq))
      },
      send: (payload: unknown) => {
        const socket = sockets[sockets.length - 1]
        if (!socket) throw new Error('no socket to send to')
        act(() => socket.send(payload))
      },
    },
  }
}

describe('useRunEventStream: REST is the base, the socket is the increment', () => {
  it('seeds the cursor from the events REST already returned', async () => {
    const h = setup({ restEvents: [event(1), event(2), event(3)] })
    await waitFor(() => expect(h.sockets).toHaveLength(1))
    expect(h.sockets[0]?.url).toContain('after_seq=3')
    h.unmount()
  })

  it('merges a live event into the run query, and drops what the cursor already covers', async () => {
    const h = setup({ restEvents: [event(1), event(2)] })
    await waitFor(() => expect(h.sockets).toHaveLength(1))
    h.server.open('running', 2)
    h.server.push(3)
    // 2 is already applied: a reconnect that replays it must not duplicate it.
    h.server.push(2)

    await waitFor(() => expect(h.events().map((row) => row.seq)).toEqual([1, 2, 3]))
    h.unmount()
  })

  it('reports the transport state unedited, including a terminal close', async () => {
    const h = setup({ restEvents: [] })
    await waitFor(() => expect(h.sockets).toHaveLength(1))
    h.server.open()
    await waitFor(() => expect(h.result.current.status.state).toBe('live'))

    h.server.send({ type: 'stream_closed', status: 'succeeded' })
    await waitFor(() => expect(h.result.current.status.state).toBe('closed'))
    expect(h.result.current.status.closedStatus).toBe('succeeded')
    h.unmount()
  })

  it('keeps a malformed frame as a diagnostic instead of losing the stream', async () => {
    const h = setup({ restEvents: [] })
    await waitFor(() => expect(h.sockets).toHaveLength(1))
    h.server.open()
    h.server.send({ type: 'future_event' })
    h.server.send({ type: 'run_event', event: { ...event(9), run_id: 'run_other' } })

    await waitFor(() => expect(h.result.current.diagnostics.ignored).toHaveLength(2))
    expect(h.result.current.status.state).toBe('live')
    expect(h.events()).toHaveLength(0)
    h.unmount()
  })

  it('does not open a stream for a run waiting for a human', async () => {
    const h = setup({ runStatus: 'waiting_approval' })
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(h.sockets).toHaveLength(0)
    expect(h.result.current.status.state).toBe('idle')
    h.unmount()
  })

  it('disposes the socket when the screen goes away, and stops retrying', async () => {
    const h = setup({ restEvents: [] })
    await waitFor(() => expect(h.sockets).toHaveLength(1))
    h.server.open()
    h.unmount()

    expect(h.sockets[0]?.closed).toBe(true)
    expect(h.timers).toHaveLength(0)
  })

  it('reconciles with REST when the run ends, rather than trusting the frame alone', async () => {
    const h = setup({ restEvents: [] })
    const invalidate = vi.spyOn(h.queryClient, 'invalidateQueries')
    await waitFor(() => expect(h.sockets).toHaveLength(1))
    h.server.open()
    h.server.send({ type: 'stream_closed', status: 'failed' })

    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['run', h.runId] })),
    )
    h.unmount()
  })
})
