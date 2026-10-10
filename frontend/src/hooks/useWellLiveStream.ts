/**
 * React hook for a well's live operational stream (`/wells/{well_id}/live/stream`).
 *
 * Connects the WebSocket transport (`WellLiveStream`) to targeted TanStack Query keys for one
 * well, exposes the canonical operator-visible transport state
 * (`LIVE | RECONNECTING | RESYNCING | POLLING FALLBACK | STALE | DISCONNECTED | ERROR`),
 * coalesces high-rate telemetry refreshes, never drops `alert.*` lifecycle events, and falls back
 * to bounded visibility-aware REST polling when the socket is unavailable.
 */

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { identityKey, subscribeIdentity } from '../api/client'
import type {
  TelemetryLatestReading,
  TelemetryTrend,
  WellLiveSnapshot,
} from '../api/types'
import {
  WellLiveStream,
  type WellGapFrame,
  type WellLiveDecode,
  type WellOutboxEnvelope,
  type WellStreamTransportStatus,
  type WellTransportState,
} from '../lib/wellLiveStream'

const DEFAULT_RECONCILE_DEBOUNCE_MS = 250
const DEFAULT_POLLING_FALLBACK_MS = 5_000
const MAX_RECENT_EVENTS = 50

export interface WellStreamDiagnostics {
  receivedFrames: number
  coalescedTelemetryFrames: number
  omittedTelemetryEvents: number
  lastGap: {
    fromSeq: number
    toSeq: number
    reason: string
    at: string
  } | null
  ignored: Array<{ reason: string; at: number }>
}

export interface UseWellLiveStreamOptions {
  enabled?: boolean
  latestReadings?: readonly TelemetryLatestReading[]
  initialSnapshot?: WellLiveSnapshot | null
  reconcileDebounceMs?: number
  pollingFallbackMs?: number
  createStream?: (options: ConstructorParameters<typeof WellLiveStream>[0]) => WellLiveStream
}

export interface WellLiveStreamHandle {
  transportState: WellTransportState
  status: WellStreamTransportStatus
  snapshot: WellLiveSnapshot | null
  trends: Record<string, TelemetryTrend>
  recentEvents: WellOutboxEnvelope[]
  diagnostics: WellStreamDiagnostics
  isPollingFallback: boolean
  isResyncing: boolean
  resyncNow: () => Promise<void>
}

export function useWellLiveStream(
  wellId: string,
  options: UseWellLiveStreamOptions = {},
): WellLiveStreamHandle {
  const queryClient = useQueryClient()
  const {
    enabled = true,
    latestReadings,
    initialSnapshot = null,
    reconcileDebounceMs = DEFAULT_RECONCILE_DEBOUNCE_MS,
    pollingFallbackMs = DEFAULT_POLLING_FALLBACK_MS,
  } = options

  const [status, setStatus] = useState<WellStreamTransportStatus>({
    state: 'idle',
    wellId,
    afterSeq: 0,
    cursor: null,
    attempts: 0,
    closeCode: null,
    lastError: null,
  })
  const [snapshot, setSnapshot] = useState<WellLiveSnapshot | null>(initialSnapshot)
  const [recentEvents, setRecentEvents] = useState<WellOutboxEnvelope[]>([])
  const [isResyncing, setIsResyncing] = useState(false)
  const [isPollingFallback, setIsPollingFallback] = useState(false)
  const [diagnostics, setDiagnostics] = useState<WellStreamDiagnostics>({
    receivedFrames: 0,
    coalescedTelemetryFrames: 0,
    omittedTelemetryEvents: 0,
    lastGap: null,
    ignored: [],
  })

  const streamRef = useRef<WellLiveStream | null>(null)
  const afterSeqRef = useRef<number>(0)
  const cursorRef = useRef<string | null>(null)
  const reconcileTimerRef = useRef<number | null>(null)
  const createStreamRef = useRef(options.createStream)

  useEffect(() => {
    createStreamRef.current = options.createStream
  }, [options.createStream])

  useEffect(() => {
    if (!initialSnapshot || initialSnapshot.well_id !== wellId) return
    setSnapshot((prev) => prev ?? initialSnapshot)
    if (initialSnapshot.stream_position > afterSeqRef.current) {
      afterSeqRef.current = initialSnapshot.stream_position
    }
    if (initialSnapshot.cursor && !cursorRef.current) {
      cursorRef.current = initialSnapshot.cursor
    }
  }, [initialSnapshot, wellId])

  const currentIdentity = useSyncExternalStore(subscribeIdentity, identityKey, identityKey)

  const invalidateTelemetryQueries = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['well-telemetry-latest', wellId] })
    void queryClient.invalidateQueries({ queryKey: ['well-live-snapshot', wellId] })
    void queryClient.invalidateQueries({ queryKey: ['timeseries-points', wellId] })
    void queryClient.invalidateQueries({ queryKey: ['alerts', wellId] })
  }, [queryClient, wellId])

  const invalidateAlertQueries = useCallback(
    (alertId?: string | null) => {
      void queryClient.invalidateQueries({ queryKey: ['alerts', wellId] })
      void queryClient.invalidateQueries({ queryKey: ['well-live-snapshot', wellId] })
      if (alertId) {
        void queryClient.invalidateQueries({ queryKey: ['alert-evidence', alertId] })
      }
    },
    [queryClient, wellId],
  )

  const resyncNow = useCallback(async () => {
    if (reconcileTimerRef.current !== null) {
      window.clearTimeout(reconcileTimerRef.current)
      reconcileTimerRef.current = null
    }
    setIsResyncing(true)
    try {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['well-telemetry-latest', wellId] }),
        queryClient.invalidateQueries({ queryKey: ['alerts', wellId] }),
        queryClient.invalidateQueries({ queryKey: ['well-live-snapshot', wellId] }),
        queryClient.invalidateQueries({ queryKey: ['timeseries-points', wellId] }),
      ])
    } finally {
      setIsResyncing(false)
    }
  }, [queryClient, wellId])

  const scheduleTelemetryReconcile = useCallback(() => {
    if (reconcileTimerRef.current !== null) return
    reconcileTimerRef.current = window.setTimeout(() => {
      reconcileTimerRef.current = null
      invalidateTelemetryQueries()
    }, reconcileDebounceMs)
  }, [invalidateTelemetryQueries, reconcileDebounceMs])

  useEffect(
    () => () => {
      if (reconcileTimerRef.current !== null) {
        window.clearTimeout(reconcileTimerRef.current)
        reconcileTimerRef.current = null
      }
    },
    [],
  )

  // Reset stream state when switching wells
  useEffect(() => {
    afterSeqRef.current = 0
    cursorRef.current = null
    setSnapshot(null)
    setRecentEvents([])
    setIsResyncing(false)
    setIsPollingFallback(false)
    setDiagnostics({
      receivedFrames: 0,
      coalescedTelemetryFrames: 0,
      omittedTelemetryEvents: 0,
      lastGap: null,
      ignored: [],
    })
  }, [wellId])

  useEffect(() => {
    if (!enabled || !wellId) {
      streamRef.current?.dispose()
      streamRef.current = null
      setStatus((prev) => ({ ...prev, wellId, state: 'disconnected' }))
      return
    }

    const handleFrame = (decoded: WellLiveDecode) => {
      if (decoded.kind === 'ignored') {
        setDiagnostics((prev) => ({
          ...prev,
          ignored: [...prev.ignored.slice(-19), { reason: decoded.reason, at: Date.now() }],
        }))
        return
      }

      setDiagnostics((prev) => ({
        ...prev,
        receivedFrames: prev.receivedFrames + 1,
      }))

      const { frame } = decoded
      if (frame.type === 'stream_opened') {
        afterSeqRef.current = Math.max(afterSeqRef.current, frame.after_seq)
        if (frame.cursor) cursorRef.current = frame.cursor
      } else if (frame.type === 'snapshot') {
        setSnapshot(frame.snapshot)
        afterSeqRef.current = Math.max(afterSeqRef.current, frame.snapshot.stream_position ?? 0)
        if (frame.snapshot.cursor) cursorRef.current = frame.snapshot.cursor
        queryClient.setQueryData(['well-live-snapshot', wellId], frame.snapshot)
      } else if (frame.type === 'telemetry') {
        afterSeqRef.current = Math.max(afterSeqRef.current, frame.to_seq)
        if (frame.cursor) cursorRef.current = frame.cursor
        setDiagnostics((prev) => ({
          ...prev,
          coalescedTelemetryFrames: prev.coalescedTelemetryFrames + 1,
          omittedTelemetryEvents: prev.omittedTelemetryEvents + frame.omitted,
        }))
        if (frame.resync) {
          void resyncNow()
        } else {
          scheduleTelemetryReconcile()
        }
      } else if (frame.type === 'event') {
        const ev = frame.event
        afterSeqRef.current = Math.max(afterSeqRef.current, ev.sequence)
        if (frame.cursor) cursorRef.current = frame.cursor
        setRecentEvents((prev) => [ev, ...prev].slice(0, MAX_RECENT_EVENTS))
        if (ev.type.startsWith('alert.')) {
          invalidateAlertQueries(ev.subject?.id ?? null)
        } else if (ev.type === 'well_state.changed' || ev.type.startsWith('operation.')) {
          void queryClient.invalidateQueries({ queryKey: ['well-state', wellId] })
          invalidateTelemetryQueries()
        } else {
          scheduleTelemetryReconcile()
        }
      } else if (frame.type === 'gap') {
        const gap: WellGapFrame = frame
        afterSeqRef.current = Math.max(afterSeqRef.current, gap.to_seq)
        if (gap.cursor) cursorRef.current = gap.cursor
        setDiagnostics((prev) => ({
          ...prev,
          lastGap: {
            fromSeq: gap.from_seq,
            toSeq: gap.to_seq,
            reason: gap.reason,
            at: new Date().toISOString(),
          },
        }))
        void resyncNow()
      } else if (frame.type === 'heartbeat') {
        if (frame.position > afterSeqRef.current) {
          afterSeqRef.current = frame.position
          if (frame.cursor) cursorRef.current = frame.cursor
          scheduleTelemetryReconcile()
        }
      } else if (frame.type === 'stream_idle') {
        if (frame.last_seq > afterSeqRef.current) {
          afterSeqRef.current = frame.last_seq
          if (frame.cursor) cursorRef.current = frame.cursor
          scheduleTelemetryReconcile()
        }
      }
    }

    const factory = createStreamRef.current ?? ((opts) => new WellLiveStream(opts))
    const instance = factory({
      wellId,
      getCursorState: () => ({
        afterSeq: afterSeqRef.current,
        cursor: cursorRef.current,
      }),
      onFrame: handleFrame,
      onStatus: (nextStatus) => {
        setStatus(nextStatus)
        if (nextStatus.state === 'live') {
          setIsPollingFallback(false)
        }
      },
    })
    streamRef.current = instance
    instance.connect()

    return () => {
      instance.dispose()
      if (streamRef.current === instance) {
        streamRef.current = null
      }
    }
  }, [
    currentIdentity,
    enabled,
    invalidateAlertQueries,
    invalidateTelemetryQueries,
    queryClient,
    resyncNow,
    scheduleTelemetryReconcile,
    wellId,
  ])

  // Bounded polling fallback when WebSocket is reconnecting or disconnected (never when permanently refused)
  useEffect(() => {
    if (!enabled || !wellId) {
      setIsPollingFallback(false)
      return
    }
    const needsFallback =
      (status.state === 'reconnecting' && status.attempts >= 1) ||
      status.state === 'disconnected'
    if (!needsFallback) {
      setIsPollingFallback(false)
      return
    }

    setIsPollingFallback(true)
    const tick = () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      invalidateTelemetryQueries()
      invalidateAlertQueries()
    }
    const intervalId = window.setInterval(tick, pollingFallbackMs)
    return () => {
      window.clearInterval(intervalId)
    }
  }, [
    enabled,
    invalidateAlertQueries,
    invalidateTelemetryQueries,
    pollingFallbackMs,
    status.attempts,
    status.state,
    wellId,
  ])

  const effectiveReadings = useMemo(
    () => latestReadings ?? snapshot?.latest ?? [],
    [latestReadings, snapshot?.latest],
  )

  const transportState: WellTransportState = useMemo(() => {
    if (status.state === 'error') return 'ERROR'
    if (isResyncing) return 'RESYNCING'
    if (isPollingFallback) return 'POLLING FALLBACK'
    if (status.state === 'reconnecting' || status.state === 'connecting') return 'RECONNECTING'
    if (status.state === 'disconnected' || status.state === 'idle') return 'DISCONNECTED'
    // Socket is live: check whether telemetry data itself is stale so stale data is never shown as LIVE
    const liveCount = effectiveReadings.filter((r) => r.freshness === 'live').length
    const staleCount = effectiveReadings.filter((r) => r.freshness === 'stale').length
    if (effectiveReadings.length > 0 && liveCount === 0 && staleCount > 0) {
      return 'STALE'
    }
    return 'LIVE'
  }, [effectiveReadings, isPollingFallback, isResyncing, status.state])

  return {
    transportState,
    status,
    snapshot,
    trends: snapshot?.trends ?? {},
    recentEvents,
    diagnostics,
    isPollingFallback,
    isResyncing,
    resyncNow,
  }
}
