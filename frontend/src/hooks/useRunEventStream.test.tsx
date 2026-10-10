/**
 * The run monitor's realtime layer, as the screen uses it.
 *
 * The stream itself is the real `RunEventStream`; only its socket and clock are supplied by the test,
 * so what is exercised here is the code the application runs: the cursor seeding from REST, the merge
 * into the run query, the reconciliation after a burst, the refusal to stream a run that cannot
 * produce events, and the disposal that leaves nothing behind.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { RunEventStream } from '../lib/runEvents'
import type { RunEventSocketLike } from '../lib/runEvents'
import { useRunEventStream } from './useRunEventStream'
import type { RunDetail, RunEvent } from '../api/types'

function event(seq: number, type = 'node_succeeded', runId = 'run_1'): RunEvent {
  return {
    id: `rev_${seq}`,
    run_id: runId,
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
    // Addressed to the run this socket is connected to, the way the server addresses them: the decoder
    // refuses a `run_event` for another run, on purpose.
    const runId = /\/runs\/([^/]+)\/events\/stream/.exec(this.url)?.[1] ?? 'run_1'
    this.send({ type: 'run_event', event: event(seq, 'node_succeeded', runId) })
  }

  drop(code = 1006): void {
    this.onclose?.({ code } as CloseEvent)
  }
}

/**
 * One rendered screen plus the transport it created, so a test can drive the server's side.
 *
 * The hook is rendered through a small component rather than `renderHook` because a test also needs to
 * re-render it with a *changed* run status — the reconciliation case, where REST reports the run has
 * ended while the stream is still open.
 */
function Harness(props: {
  runId: string
  status: string
  restEvents?: RunEvent[]
  reconcileDebounceMs: number
  sockets: FakeSocket[]
  timers: Array<{ id: number; fn: () => void }>
  onHandle: (handle: ReturnType<typeof useRunEventStream>) => void
}) {
  const handle = useRunEventStream(props.runId, {
    runStatus: props.status,
    restEvents: props.restEvents,
    reconcileDebounceMs: props.reconcileDebounceMs,
    createStream: (streamOptions) =>
      new RunEventStream({
        ...streamOptions,
        createSocket: (url) => {
          const socket = new FakeSocket(url)
          props.sockets.push(socket)
          return socket
        },
        setTimer: (fn) => {
          props.timers.push({ id: props.timers.length + 1, fn })
          return props.timers.length
        },
        clearTimer: (timerHandle) => {
          const index = props.timers.findIndex((timer) => timer.id === Number(timerHandle))
          if (index >= 0) props.timers.splice(index, 1)
        },
      }),
  })
  props.onHandle(handle)
  return null
}

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
  const renderProps = {
    runId,
    status: options.runStatus ?? 'running',
    restEvents: options.restEvents,
    reconcileDebounceMs: options.reconcileDebounceMs ?? 0,
    sockets,
    timers,
    onHandle: () => {},
  }

  queryClient.setQueryData<RunDetail>(['run', runId], {
    run: { id: runId, status: renderProps.status } as unknown as RunDetail['run'],
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

  let handle: ReturnType<typeof useRunEventStream> | null = null
  // The run the screen is showing *now*: the log a test reads is the one on display.
  let currentRunId = runId
  const view = render(
    <Harness {...renderProps} onHandle={(next) => (handle = next)} />,
    { wrapper },
  )

  return {
    view,
    queryClient,
    sockets,
    timers,
    runId,
    result: () => handle as NonNullable<typeof handle>,
    /**
     * The same screen after the run in the URL changed — the query-parameter case, not a remount.
     *
     * The new run has its own detail row (the log begins at 1), because that is what REST would have
     * returned for it.
     */
    switchRun: (nextRunId: string, events: RunEvent[]) => {
      currentRunId = nextRunId
      queryClient.setQueryData<RunDetail>(['run', nextRunId], {
        run: { id: nextRunId, status: 'running' } as unknown as RunDetail['run'],
        workflow: null,
        node_runs: [],
        artifacts: [],
        events: events.map((row) => ({ ...row, run_id: nextRunId })),
        pending_approval: null,
        resumable: false,
      } as RunDetail)
      view.rerender(
        <Harness
          {...renderProps}
          runId={nextRunId}
          restEvents={events}
          onHandle={(next) => (handle = next)}
        />,
      )
    },
    /** The same screen after REST reported a different run status — a reconciliation, not a remount. */
    rerenderStatus: (status: string) => view.rerender(<Harness {...renderProps} status={status} onHandle={(next) => (handle = next)} />),
    events: () => queryClient.getQueryData<RunDetail>(['run', currentRunId])?.events ?? [],
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
    unmount: () => view.unmount(),
  }
}

describe('useRunEventStream: REST is the base, the socket is the increment', () => {
  it('seeds the cursor from the events REST already returned', async () => {
    const h = setup({ restEvents: [event(1), event(2), event(3)] })
    await waitFor(() => expect(h.sockets).toHaveLength(1))
    expect(h.sockets[0]?.url).toContain('after_seq=3')
    h.unmount()
  })

  it('starts a different run at that run’s own cursor, not at the previous run’s', async () => {
    /*
     * `?run=A` followed by `?run=B` keeps the monitor mounted: the parameter changes, the hook's
     * instance does not. A cursor is a position in *one* log, and the previous log's position is
     * meaningless in the new one — worse than meaningless, because the hook drops every event whose
     * `seq` is at or below the cursor. A stale cursor therefore discards the whole live log of the
     * newly selected run, silently, while the screen looks connected.
     */
    const h = setup({ runId: 'run_a', restEvents: [event(1), event(2), event(3), event(4)] })
    await waitFor(() => expect(h.sockets).toHaveLength(1))
    expect(h.sockets[0]?.url).toContain('after_seq=4')

    // The operator selects another run from the list, whose log starts again at 1.
    h.switchRun('run_b', [event(1)])
    await waitFor(() => expect(h.sockets).toHaveLength(2))
    // The new socket asks for the new run's log, from the new run's cursor.
    expect(h.sockets[1]?.url).toContain('after_seq=1')
    h.server.open('running', 1)

    // And its events are applied: the second run's log advances on screen.
    h.server.push(2)
    await waitFor(() => expect(h.events().map((row) => row.seq)).toEqual([1, 2]))
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
    await waitFor(() => expect(h.result().status.state).toBe('live'))

    h.server.send({ type: 'stream_closed', status: 'succeeded' })
    await waitFor(() => expect(h.result().status.state).toBe('closed'))
    expect(h.result().status.closedStatus).toBe('succeeded')
    h.unmount()
  })

  it('keeps saying the stream closed when the run that ended is the one being watched', async () => {
    const h = setup({ runStatus: 'running', restEvents: [event(1)] })
    await waitFor(() => expect(h.sockets).toHaveLength(1))
    h.server.open()
    h.server.send({ type: 'stream_closed', status: 'succeeded' })
    await waitFor(() => expect(h.result().status.state).toBe('closed'))

    // The reconciliation that follows reports a terminal run, which stops the stream — but the
    // operator must still see that it was watched to its end, not "not streamed".
    act(() => h.rerenderStatus('succeeded'))
    await waitFor(() => expect(h.result().status.closedStatus).toBe('succeeded'))
    expect(h.result().status.state).toBe('closed')
    h.unmount()
  })

  it('keeps a malformed frame as a diagnostic instead of losing the stream', async () => {
    const h = setup({ restEvents: [] })
    await waitFor(() => expect(h.sockets).toHaveLength(1))
    h.server.open()
    h.server.send({ type: 'future_event' })
    h.server.send({ type: 'run_event', event: { ...event(9), run_id: 'run_other' } })

    await waitFor(() => expect(h.result().diagnostics.ignored).toHaveLength(2))
    expect(h.result().status.state).toBe('live')
    expect(h.events()).toHaveLength(0)
    h.unmount()
  })

  it('streams a run parked at an approval, because a decision taken elsewhere is an event', async () => {
    const h = setup({ runStatus: 'waiting_approval', restEvents: [event(1)] })
    await waitFor(() => expect(h.sockets).toHaveLength(1))
    h.server.open('waiting_approval', 1)
    // Somebody decides in the approval inbox: the run resumes and finishes while this page watches.
    h.server.send({ type: 'run_event', event: event(2, 'approval_decided') })
    h.server.send({ type: 'stream_closed', status: 'succeeded' })

    await waitFor(() => expect(h.result().status.closedStatus).toBe('succeeded'))
    expect(h.events().map((row) => row.seq)).toEqual([1, 2])
    h.unmount()
  })

  it('does not open a stream for a run that has already finished', async () => {
    const h = setup({ runStatus: 'succeeded' })
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(h.sockets).toHaveLength(0)
    expect(h.result().status.state).toBe('idle')
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
