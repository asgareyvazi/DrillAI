/**
 * What a superseded, cancelled or failed read does to the screen.
 *
 * These are the behaviours that are invisible until they are wrong. A run opened and then replaced by
 * another run leaves a request in flight; if nothing cancels it, its failure lands on a screen that
 * has moved on, and the operator is told the backend is unreachable while looking at a healthy page.
 *
 * The transport is mocked at `fetch` — the seam this file is about — and the API client, the
 * endpoints module, the query layer and the components are all the real ones. `ErrorState` is real
 * too, because "did the operator see an error" is the assertion.
 */

import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ErrorState } from '../components/common'
import { I18nProvider } from '../i18n'
import { drillingApi } from './endpoints'
import { setIdentity, shouldRetryRequest } from './client'
import type { RunDetail } from './types'
import runSucceededFixture from '../test/fixtures/run-succeeded.json'

const succeeded = runSucceededFixture as unknown as RunDetail

function detailFor(runId: string): RunDetail {
  return { ...succeeded, run: { ...succeeded.run, id: runId } }
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function errorResponse(status: number, code: string, message: string) {
  return jsonResponse({ error: { code, message, retryable: false, details: {} } }, status)
}

/** The run screen, reduced to what this file asserts about. */
function RunView({ runId }: { runId: string }) {
  const query = useQuery({
    queryKey: ['run', runId],
    queryFn: ({ signal }) => drillingApi.getRun(runId, signal),
  })
  if (query.isLoading) return <p>loading</p>
  if (query.error) return <ErrorState error={query.error} onRetry={() => query.refetch()} />
  return <p data-testid="run-id">{query.data?.run.id}</p>
}

function Harness({ initial }: { initial: string }) {
  const [runId, setRunId] = useState(initial)
  return (
    <>
      <button type="button" onClick={() => setRunId('run_b')}>
        open run b
      </button>
      <RunView runId={runId} />
    </>
  )
}

function renderWithQuery(node: React.ReactNode, client?: QueryClient) {
  const queryClient =
    client ??
    new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>{node}</I18nProvider>
    </QueryClientProvider>,
  )
  return { ...utils, queryClient }
}

const fetchMock = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  setIdentity({ token: null, devRoles: 'engineer' })
  fetchMock.mockReset()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('a read that is superseded', () => {
  it('cancels the old request and shows no failure for it', async () => {
    const signals = new Map<string, AbortSignal>()
    fetchMock.mockImplementation((url: string, init: RequestInit) => {
      const runId = url.includes('run_a') ? 'run_a' : 'run_b'
      signals.set(runId, init.signal as AbortSignal)
      if (runId === 'run_b') return Promise.resolve(jsonResponse(detailFor('run_b')))
      return new Promise((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
      })
    })

    renderWithQuery(<Harness initial="run_a" />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))

    screen.getByRole('button', { name: /open run b/i }).click()

    expect(await screen.findByTestId('run-id')).toHaveTextContent('run_b')
    expect(signals.get('run_a')?.aborted).toBe(true)
    // The whole point: the cancellation is not a failure, and it is certainly not an outage.
    expect(screen.queryByTestId('error-state')).not.toBeInTheDocument()
    expect(screen.queryByText(/unreachable/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/timed out/i)).not.toBeInTheDocument()
  })

  it('cannot let a late response overwrite the run the operator is now looking at', async () => {
    const pending: Array<(value: Response) => void> = []
    fetchMock.mockImplementation((url: string, init: RequestInit) => {
      if (url.includes('run_a')) {
        return new Promise<Response>((resolve, reject) => {
          pending.push(resolve)
          init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
        })
      }
      return Promise.resolve(jsonResponse(detailFor('run_b')))
    })

    const { queryClient } = renderWithQuery(<Harness initial="run_a" />)
    await waitFor(() => expect(pending).toHaveLength(1))

    screen.getByRole('button', { name: /open run b/i }).click()
    expect(await screen.findByTestId('run-id')).toHaveTextContent('run_b')

    // Run A's answer finally arrives. It belongs to a screen that no longer exists.
    pending[0]?.(jsonResponse(detailFor('run_a')))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))

    expect(screen.getByTestId('run-id')).toHaveTextContent('run_b')
    expect(queryClient.getQueryData<RunDetail>(['run', 'run_b'])?.run.id).toBe('run_b')
  })

  it('keeps one run\'s failure out of another run\'s screen', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('run_a')) return Promise.resolve(errorResponse(500, 'platform.internal_error', 'internal error'))
      return Promise.resolve(jsonResponse(detailFor('run_b')))
    })

    const { queryClient } = renderWithQuery(<Harness initial="run_a" />)
    expect(await screen.findByTestId('error-state')).toHaveAttribute('data-error-kind', 'server')

    screen.getByRole('button', { name: /open run b/i }).click()

    expect(await screen.findByTestId('run-id')).toHaveTextContent('run_b')
    expect(screen.queryByTestId('error-state')).not.toBeInTheDocument()
    // Run A's key still carries its own failure — it is not scrubbed, it is simply not shown here.
    expect(queryClient.getQueryState(['run', 'run_a'])?.error).toBeInstanceOf(Error)
    expect(queryClient.getQueryState(['run', 'run_a'])?.status).toBe('error')
    expect(queryClient.getQueryState(['run', 'run_b'])?.status).toBe('success')
  })
})

describe('the failure state a screen shows', () => {
  it('clears when a retry succeeds', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    renderWithQuery(<RunView runId="run_a" />)
    expect(await screen.findByTestId('error-state')).toHaveAttribute('data-error-kind', 'network')

    fetchMock.mockResolvedValueOnce(jsonResponse(detailFor('run_a')))
    screen.getByRole('button', { name: /retry/i }).click()

    expect(await screen.findByTestId('run-id')).toHaveTextContent('run_a')
    expect(screen.queryByTestId('error-state')).not.toBeInTheDocument()
  })

  it('shows a deep link to a run that does not exist as "not found", not as an outage', async () => {
    fetchMock.mockResolvedValue(errorResponse(404, 'platform.not_found', "run 'run_missing' not found"))
    renderWithQuery(<RunView runId="run_missing" />)

    const state = await screen.findByTestId('error-state')
    expect(state).toHaveAttribute('data-error-kind', 'not_found')
    expect(state).toHaveAttribute('data-http-status', '404')
    expect(state).toHaveTextContent(/not found/i)
    expect(state).not.toHaveTextContent(/unreachable/i)
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument()
  })

  it('shows an identity that may not read this run as a permission problem', async () => {
    fetchMock.mockResolvedValue(
      errorResponse(403, 'security.permission_denied', "missing permission 'workflow.read'"),
    )
    renderWithQuery(<RunView runId="run_a" />)

    const state = await screen.findByTestId('error-state')
    expect(state).toHaveAttribute('data-error-kind', 'forbidden')
    expect(state).toHaveTextContent(/workflow.read/)
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument()
  })

  it('reports a body it cannot read as a protocol failure with the request id kept', async () => {
    fetchMock.mockResolvedValue(
      new Response('<html>not json</html>', {
        status: 200,
        headers: { 'Content-Type': 'application/json', 'x-request-id': 'req_bad_body' },
      }),
    )
    renderWithQuery(<RunView runId="run_a" />)

    const state = await screen.findByTestId('error-state')
    expect(state).toHaveAttribute('data-error-kind', 'malformed')
    expect(state).toHaveTextContent('req_bad_body')
    expect(state).not.toHaveTextContent(/unreachable/i)
  })
})

describe('the retry policy the query layer actually applies', () => {
  it('retries a transport failure twice and gives up', async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: shouldRetryRequest, retryDelay: 0 } },
    })
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    renderWithQuery(<RunView runId="run_a" />, client)

    expect(await screen.findByTestId('error-state')).toHaveAttribute('data-error-kind', 'network')
    expect(fetchMock).toHaveBeenCalledTimes(3) // the first attempt and two retries, then it stops
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('does not retry a not-found at all', async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: shouldRetryRequest, retryDelay: 0 } },
    })
    fetchMock.mockResolvedValue(errorResponse(404, 'platform.not_found', 'no such run'))
    renderWithQuery(<RunView runId="run_a" />, client)

    await screen.findByTestId('error-state')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
