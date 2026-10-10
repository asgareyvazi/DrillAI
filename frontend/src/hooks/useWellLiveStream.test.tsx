import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import type { TelemetryLatestReading } from '../api/types'
import {
  WellLiveStream,
  type WellLiveDecode,
  type WellLiveStreamOptions,
  type WellStreamTransportStatus,
} from '../lib/wellLiveStream'
import { useWellLiveStream } from './useWellLiveStream'

const WELL_ID = '11111111-2222-3333-4444-555555555555'

function makeReading(freshness: 'live' | 'stale' | 'missing'): TelemetryLatestReading {
  return {
    channel_id: 'ch-spp',
    channel_key: 'spp',
    label: 'Standpipe Pressure',
    dimension: 'pressure',
    unit: 'Pa',
    value: 25_000_000,
    quality: 'good',
    quality_flags: [],
    is_trustworthy: true,
    observed_at: '2026-10-10T10:00:00Z',
    received_at: '2026-10-10T10:00:00Z',
    age_seconds: freshness === 'stale' ? 600 : 2,
    freshness,
    source: 'witsml',
    source_ref: null,
    is_late: false,
  }
}

describe('useWellLiveStream', () => {
  it('never reports LIVE when all telemetry readings are stale, switches to POLLING FALLBACK on reconnect, and invalidates targeted queries on alert events', () => {
    vi.useFakeTimers()
    try {
      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false, gcTime: 0 } },
      })
      const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')

      let capturedOptions: WellLiveStreamOptions | null = null
      const createStream = (opts: WellLiveStreamOptions) => {
        capturedOptions = opts
        return {
          connect: () => {
            opts.onStatus?.({
              state: 'live',
              wellId: opts.wellId,
              afterSeq: 1,
              cursor: 'cur-1',
              attempts: 0,
              closeCode: null,
              lastError: null,
            })
          },
          close: vi.fn(),
          dispose: vi.fn(),
          getStatus: () =>
            ({
              state: 'live',
              wellId: opts.wellId,
              afterSeq: 1,
              cursor: 'cur-1',
              attempts: 0,
              closeCode: null,
              lastError: null,
            }) satisfies WellStreamTransportStatus,
        } as unknown as WellLiveStream
      }

      const wrapper = ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      )

      const { result, rerender } = renderHook(
        ({ readings }: { readings: TelemetryLatestReading[] }) =>
          useWellLiveStream(WELL_ID, {
            latestReadings: readings,
            createStream,
            reconcileDebounceMs: 100,
            pollingFallbackMs: 1000,
          }),
        {
          initialProps: { readings: [makeReading('live')] },
          wrapper,
        },
      )

      expect(result.current.transportState).toBe('LIVE')

      // When all readings become stale, transportState MUST be 'STALE', never 'LIVE'
      rerender({ readings: [makeReading('stale')] })
      expect(result.current.transportState).toBe('STALE')

      // Emit an alert.raised event frame -> immediately invalidates ['alerts', WELL_ID]
      act(() => {
        const frame: WellLiveDecode = {
          kind: 'frame',
          frame: {
            type: 'event',
            cursor: 'cur-2',
            event: {
              id: 'ev-alert-1',
              sequence: 2,
              well_sequence: 2,
              org_sequence: 10,
              schema_version: 1,
              type: 'alert.raised',
              occurred_at: '2026-10-10T10:00:05Z',
              scope: { org_id: 'org-1', well_id: WELL_ID, wellbore_id: null },
              subject: { kind: 'operational_alert', id: 'alert-99' },
              payload: { severity: 'high' },
            },
          },
        }
        capturedOptions?.onFrame?.(frame)
      })

      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['alerts', WELL_ID] })
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['alert-evidence', 'alert-99'] })

      // Transition to reconnecting (attempts: 1) -> activates POLLING FALLBACK
      act(() => {
        capturedOptions?.onStatus?.({
          state: 'reconnecting',
          wellId: WELL_ID,
          afterSeq: 2,
          cursor: 'cur-2',
          attempts: 1,
          closeCode: 1006,
          lastError: 'closed',
        })
      })
      expect(result.current.isPollingFallback).toBe(true)
      expect(result.current.transportState).toBe('POLLING FALLBACK')
    } finally {
      vi.useRealTimers()
    }
  })
})
