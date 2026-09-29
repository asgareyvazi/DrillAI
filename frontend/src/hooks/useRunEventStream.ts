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
 * * **A run waiting for a human is not streamed at all.** Nothing changes until a person decides, and
 *   the frame rate would be zero: the server would idle, the client would reconnect on its own or the
 *   operator's decision would refresh the page. When the decision is made on this page the mutation
 *   invalidates the query, so the operator sees it immediately either way.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { RunDetail, RunEvent } from '../api/types'
import { getIdentity } from '../api/client'
import { highestSeq, mergeRunEvents, RunEventStream } from '../lib/runEvents'
import type { RunStreamDecode, RunStreamStatus } from '../lib/runEvents'

/** Statuses where the server can still produce events. `waiting_approval` and `paused` are not. */
const STREAMABLE_STATUSES = new Set(['queued', 'running', 'pending'])

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
  // feeding a screen that now acts as another one.
  const identity = getIdentity()
  const identityKey = `${identity.token ?? ''}|${identity.devRoles ?? ''}`

  // The cursor starts where REST says the log is. A later REST response (a reconciliation that ran
  // ahead of the stream) can only ever move it forward, never back.
  const restCursor = useMemo(() => highestSeq(restEvents ?? []), [restEvents])
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

  // Only a run REST has already described as live-ish is streamed. Waiting for that answer costs one
  // request's latency and saves a handshake that would be thrown away for every run that is parked at
  // an approval or already finished — which, on a monitor page opened from history, is most of them.
  const streamable = enabled && runStatus !== undefined && runStatus !== null && STREAMABLE_STATUSES.has(runStatus)

  useEffect(() => {
    if (!streamable) {
      // A run that cannot produce more events has nothing to stream; leaving a socket open would only
      // make the indicator claim a liveness that does not exist.
      streamRef.current?.dispose()
      streamRef.current = null
      setStatus((previous) => ({ ...previous, state: 'idle', cursor: cursorRef.current }))
      return
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
  }, [streamable, runId, identityKey, queryClient, reconcile, scheduleReconcile])

  return { status, diagnostics, reconcile }
}
