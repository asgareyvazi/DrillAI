/**
 * The run monitor's realtime layer: stream in, query cache out.
 *
 * The socket is a transport; TanStack Query holds the run. This hook is the seam between them, and it
 * is deliberately small and boring:
 *
 * * **REST stays the source of truth.** Live events are merged into the run's own query key; the
 *   authoritative answer — status, node runs, approvals, the whole envelope — arrives by
 *   *reconciliation*: a coalesced `GET /runs/{id}` after an event burst, on every reconnect, and once
 *   more when the stream ends. Nothing here invents state the server has not sent.
 * * **The cursor is the sequence, and it never goes backwards.** It is seeded from the events REST has
 *   already returned, advanced by each applied event, and handed to every reconnect. Events at or
 *   below it are dropped, which is what makes a reconnect — or a regression in the server's
 *   ordering — harmless instead of duplicated.
 * * **The socket lives exactly as long as the screen.** It is created when the hook mounts for a run
 *   and disposed on unmount, when the run id changes, and when the identity changes. A stream from a
 *   previous run cannot publish into the current one, because the stream itself refuses frames whose
 *   `run_id` is not its own.
 * * **A run waiting for a human is still streamed.** A parked run can change without this page
 *   doing anything: a decision is taken in the approval inbox, another operator resumes it, a
 *   breakpoint is cleared. Streaming is how the page hears about that; polling under the socket
 *   would be the same stopgap in a new place. A terminal run is the only one with nothing left to
 *   say, and it is not streamed.
 */

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { RunDetail, RunEvent } from '../api/types'
import { identityKey, subscribeIdentity } from '../api/client'
import { highestSeq, mergeRunEvents, RunEventStream } from '../lib/runEvents'
import type { RunStreamDecode, RunStreamStatus } from '../lib/runEvents'

/**
 * Statuses where the server can still produce events.
 *
 * `waiting_approval` and `paused` are included, and that is deliberate: a parked run still *changes*
 * — somebody else decides the approval, another operator resumes it, a breakpoint is cleared — and
 * those events are precisely what a monitor page must not miss. The alternative would be an interval
 * under the socket, which is the stopgap this replaced. A terminal run is the only one with nothing
 * left to say, and it is not streamed.
 */
const STREAMABLE_STATUSES = new Set(['queued', 'running', 'pending', 'waiting_approval', 'paused'])

/** How long to wait after the last event before reconciling once with REST. */
const DEFAULT_RECONCILE_DEBOUNCE_MS = 400

export interface RunStreamDiagnostics {
  /** The last frames the client refused to apply, with the reason — shown, never swallowed. */
  ignored: Array<{ reason: string; at: number }>
  /** Frames received since the stream was opened. */
  received: number
}

export interface RunEventStreamHandle {
  status: RunStreamStatus
  diagnostics: RunStreamDiagnostics
  /** Force a reconciliation now — used after a local mutation, not on a timer. */
  reconcile: () => void
}

export interface UseRunEventStreamOptions {
  /** The run status REST reported; decides whether a stream is worth opening at all. */
  runStatus: string | null | undefined
  /** The events REST has returned for this run; they seed the cursor. */
  restEvents: readonly RunEvent[] | undefined
  enabled?: boolean
  reconcileDebounceMs?: number
  /**
   * Injected by tests, which supply a deterministic socket and clock. The callback wiring and the
   * cursor logic stay in this hook, so a test exercises the same code the application runs rather
   * than a second implementation that only exists in tests.
   */
  createStream?: (options: ConstructorParameters<typeof RunEventStream>[0]) => RunEventStream
}

/**
 * Keep one run's live events flowing into the query cache.
 *
 * Returns the transport's own status, unedited, so the indicator can say "reconnecting" when it is
 * reconnecting and "closed" when the run ended — rather than a boolean that flattens all of that
 * into "live or not".
 */
export function useRunEventStream(
  runId: string,
  options: UseRunEventStreamOptions,
): RunEventStreamHandle {
  const queryClient = useQueryClient()
  const { runStatus, restEvents, enabled = true, reconcileDebounceMs = DEFAULT_RECONCILE_DEBOUNCE_MS } = options

  const [status, setStatus] = useState<RunStreamStatus>({
    state: 'idle',
    runId,
    cursor: 0,
    runStatus: null,
    closedStatus: null,
    attempts: 0,
    closeCode: null,
    lastError: null,
  })
  const [diagnostics, setDiagnostics] = useState<RunStreamDiagnostics>({ ignored: [], received: 0 })

  const streamRef = useRef<RunEventStream | null>(null)
  const cursorRef = useRef(0)
  const reconcileTimer = useRef<number | null>(null)
  // Held in a ref, not read from the options object: a caller that passes an inline factory would
  // otherwise change this value on every render, and the effect below would tear the stream down and
  // rebuild it as fast as React renders. The stream's identity is the run and the identity, nothing
  // else.
  const createStreamRef = useRef(options.createStream)
  useEffect(() => {
    createStreamRef.current = options.createStream
  }, [options.createStream])

  // The identity is part of the stream's identity: a socket opened as one principal must not keep
  // feeding a screen that now acts as another one. It is read through a subscription rather than off
  // a render, because a refetch that returns the same data does not re-render — and the socket would
  // then still be the previous identity's.
  const currentIdentity = useSyncExternalStore(subscribeIdentity, identityKey, identityKey)

  // The cursor starts where REST says the log is. A later REST response (a reconciliation that ran
  // ahead of the stream) can only ever move it forward, never back.
  const restCursor = useMemo(() => highestSeq(restEvents ?? []), [restEvents])
  const cursorRunRef = useRef(runId)
  useEffect(() => {
    cursorRef.current = Math.max(cursorRef.current, restCursor)
  }, [restCursor])

  const reconcile = useCallback(() => {
    if (reconcileTimer.current !== null) {
      window.clearTimeout(reconcileTimer.current)
      reconcileTimer.current = null
    }
    void queryClient.invalidateQueries({ queryKey: ['run', runId] })
    void queryClient.invalidateQueries({ queryKey: ['run-approvals', runId] })
    // The list shows the same run's status; it is one row of it, not a different screen's data.
    void queryClient.invalidateQueries({ queryKey: ['runs'] })
  }, [queryClient, runId])

  const scheduleReconcile = useCallback(() => {
    if (reconcileTimer.current !== null) return
    reconcileTimer.current = window.setTimeout(() => {
      reconcileTimer.current = null
      reconcile()
    }, reconcileDebounceMs)
  }, [reconcile, reconcileDebounceMs])

  useEffect(
    () => () => {
      if (reconcileTimer.current !== null) {
        window.clearTimeout(reconcileTimer.current)
        reconcileTimer.current = null
      }
    },
    [],
  )

  // A stream opens once REST has described the run's status, so a finished run — the common case when
  // a monitor page is opened from history — never opens one.
  const streamable = enabled && runStatus !== undefined && runStatus !== null && STREAMABLE_STATUSES.has(runStatus)

  useEffect(() => {
    if (!streamable) {
      // The run is finished (or has not been described yet): nothing more can arrive, so a socket
      // would only make the indicator claim a liveness that does not exist.
      streamRef.current?.dispose()
      streamRef.current = null
      // A stream that ended because the run ended keeps saying so. Resetting it to "not streamed"
      // would erase the one moment the operator wants to see: the run finished, and this page was
      // watching it when it did. (A monitor opened on an already-finished run starts idle.)
      setStatus((previous) =>
        previous.state === 'closed' ? previous : { ...previous, state: 'idle', cursor: cursorRef.current },
      )
      return
    }

    /*
     * A cursor is a position in one log. The monitor stays mounted across a `?run=` change, so without
     * this the *previous* run's position would be reused: the new socket would ask for events after a
     * sequence the new run never had, and `applyEvent` below would drop every event at or below it.
     * The screen would look connected while the new run's log stayed empty.
     *
     * The cursor therefore belongs to the run it was read from: a change of run starts again from that
     * run's own REST cursor, and REST is still the authority for where it starts.
     */
    if (cursorRunRef.current !== runId) {
      cursorRunRef.current = runId
      cursorRef.current = restCursor
    }

    const applyEvent = (event: RunEvent) => {
      const seq = Number(event.seq)
      if (!Number.isFinite(seq) || seq <= cursorRef.current) return
      cursorRef.current = seq
      queryClient.setQueryData<RunDetail>(['run', runId], (previous) => {
        if (!previous) return previous
        return { ...previous, events: mergeRunEvents(previous.events ?? [], [event]) }
      })
      scheduleReconcile()
    }

    const streamOptions: ConstructorParameters<typeof RunEventStream>[0] = {
      runId,
      getCursor: () => cursorRef.current,
      onFrame: (decode: RunStreamDecode) => {
        setDiagnostics((previous) => {
          if (decode.kind === 'ignored') {
            // Bounded: a diagnostic list that grows without limit is a memory leak with a label.
            const ignored = [...previous.ignored, { reason: decode.reason, at: Date.now() }].slice(-5)
            return { ...previous, ignored }
          }
          if (decode.message.type === 'run_event') {
            applyEvent(decode.message.event)
            return { ...previous, received: previous.received + 1 }
          }
          return previous
        })
      },
      onStatus: setStatus,
      onTerminal: () => {
        // The run ended: take the authoritative state, not the transport's summary of it.
        reconcile()
      },
      onIdle: () => {
        // "No new events" is a good moment to check REST, and never a reason to claim the run ended.
        scheduleReconcile()
      },
    }

    const factory = createStreamRef.current
    const stream = factory ? factory(streamOptions) : new RunEventStream(streamOptions)

    streamRef.current = stream
    // The status listener fires on every change; seed the indicator with the state it is entering.
    setStatus(stream.getStatus())
    stream.connect()

    return () => {
      stream.dispose()
      if (streamRef.current === stream) streamRef.current = null
    }
  }, [streamable, runId, currentIdentity, restCursor, queryClient, reconcile, scheduleReconcile])

  return { status, diagnostics, reconcile }
}
