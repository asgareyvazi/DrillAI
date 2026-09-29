/**
 * The shared data-state primitives.
 *
 * Every screen renders through `<Async>`, so its contract is a product contract: a loading region
 * that announces itself, an error region that names the failure and offers a retry, an empty region
 * that is visibly empty, and content only when content exists. If these four states drift, every page
 * drifts with them.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { Async, Badge, EmptyState, ErrorState, Loading, Table, Value } from './index'
import { I18nProvider } from '../../i18n'

function renderWithI18n(node: React.ReactNode) {
  return render(<I18nProvider>{node}</I18nProvider>)
}

describe('<Async>', () => {
  it('shows a labelled, announced loading state', () => {
    renderWithI18n(<Async query={{ isLoading: true, error: null, data: undefined }}>{() => <p>data</p>}</Async>)
    const status = screen.getByRole('status')
    expect(status).toHaveAttribute('aria-live', 'polite')
    expect(status).toHaveTextContent(/loading/i)
    expect(screen.queryByText('data')).not.toBeInTheDocument()
  })

  it('renders a transient failure with a retry that calls refetch', async () => {
    const refetch = vi.fn()
    renderWithI18n(
      <Async
        query={{
          isLoading: false,
          error: new ApiError(0, 'network.unreachable', 'the backend is unreachable'),
          data: undefined,
          refetch,
        }}
      >
        {() => <p>data</p>}
      </Async>,
    )

    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent(/backend is unreachable/i)
    expect(alert).toHaveAttribute('data-error-kind', 'network')

    await userEvent.click(screen.getByRole('button', { name: /retry/i }))
    expect(refetch).toHaveBeenCalledTimes(1)
  })

  it('renders a permission failure without offering a retry that cannot help', () => {
    const refetch = vi.fn()
    renderWithI18n(
      <Async
        query={{
          isLoading: false,
          error: new ApiError(403, 'security.permission_denied', 'missing permission', {}, 'req_9'),
          data: undefined,
          refetch,
        }}
      >
        {() => <p>data</p>}
      </Async>,
    )

    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent(/do not have permission/i)
    expect(alert).toHaveTextContent('security.permission_denied')
    expect(alert).toHaveTextContent('req_9')
    expect(alert).toHaveAttribute('data-error-kind', 'forbidden')
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument()
  })

  it('prefers the empty state over content when the caller says it is empty', () => {
    renderWithI18n(
      <Async
        query={{ isLoading: false, error: null, data: { items: [] as string[] } }}
        empty={(data) => (data.items.length === 0 ? <EmptyState message="No wells are registered yet." /> : undefined)}
      >
        {() => <p>data</p>}
      </Async>,
    )
    expect(screen.getByText('No wells are registered yet.')).toBeInTheDocument()
    expect(screen.queryByText('data')).not.toBeInTheDocument()
  })

  it('renders content once data exists and no empty state applies', () => {
    renderWithI18n(
      <Async query={{ isLoading: false, error: null, data: { items: ['a'] } }} empty={() => undefined}>
        {(data) => <p>data: {data.items.length}</p>}
      </Async>,
    )
    expect(screen.getByText('data: 1')).toBeInTheDocument()
  })

  it('keeps showing the loading state while data has not arrived yet', () => {
    renderWithI18n(<Async query={{ isLoading: false, error: null, data: undefined }}>{() => <p>data</p>}</Async>)
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(screen.queryByText('data')).not.toBeInTheDocument()
  })
})

describe('<ErrorState>', () => {
  it('names a network failure differently from a not-found', () => {
    const { unmount } = renderWithI18n(<ErrorState error={new ApiError(0, 'network.unreachable', 'x')} />)
    expect(screen.getByRole('alert')).toHaveTextContent(/backend is unreachable/i)
    unmount()

    renderWithI18n(<ErrorState error={new ApiError(404, 'platform.not_found', 'x')} />)
    expect(screen.getByRole('alert')).toHaveTextContent(/not found/i)
  })

  it('gives every failure class its own wording and its own kind', () => {
    const cases: Array<[ApiError, string, RegExp]> = [
      [new ApiError(400, 'platform.unsupported_operation', 'nope'), 'invalid_request', /could not accept this request/i],
      [new ApiError(401, 'security.authentication_required', 'no'), 'unauthenticated', /not signed in/i],
      [new ApiError(403, 'security.permission_denied', 'no'), 'forbidden', /do not have permission/i],
      [new ApiError(404, 'platform.not_found', 'no'), 'not_found', /not found/i],
      [new ApiError(409, 'platform.conflict', 'the approval has not been decided yet'), 'conflict', /refused this because of its current state/i],
      [new ApiError(422, 'platform.validation_failed', 'unknown report kind'), 'validation', /rejected by the server/i],
      [new ApiError(500, 'platform.internal_error', 'internal error'), 'server', /server failed to complete/i],
      [new ApiError(0, 'network.unreachable', 'down'), 'network', /backend is unreachable/i],
      [new ApiError(0, 'network.timeout', 'slow', {}, null, true, { kind: 'timeout' }), 'timeout', /timed out/i],
      [
        new ApiError(200, 'protocol.malformed_response', 'bad', {}, null, false, { kind: 'malformed' }),
        'malformed',
        /response this page could not read/i,
      ],
    ]

    for (const [error, kind, wording] of cases) {
      const { unmount } = renderWithI18n(<ErrorState error={error} />)
      const alert = screen.getByRole('alert')
      expect(alert, kind).toHaveAttribute('data-error-kind', kind)
      expect(alert, kind).toHaveTextContent(wording)
      unmount()
    }
  })

  it('never calls a protocol failure or a timeout "unreachable"', () => {
    for (const error of [
      new ApiError(0, 'network.timeout', 'slow', {}, null, true, { kind: 'timeout' }),
      new ApiError(200, 'protocol.malformed_response', 'bad', {}, null, false, { kind: 'malformed' }),
    ]) {
      const { unmount } = renderWithI18n(<ErrorState error={error} />)
      expect(screen.getByRole('alert')).not.toHaveTextContent(/unreachable/i)
      unmount()
    }
  })

  it('shows the request id the server returned, so a failure can be traced', () => {
    renderWithI18n(
      <ErrorState
        error={new ApiError(500, 'platform.internal_error', 'internal error', {}, 'req_20260929', false, {
          kind: 'server',
        })}
      />,
    )
    expect(screen.getByTestId('error-request-id')).toHaveTextContent('request req_20260929')
  })

  it('offers a retry for the failures where asking again can help, and not otherwise', () => {
    const retryable = [
      new ApiError(0, 'network.unreachable', 'down'),
      new ApiError(0, 'network.timeout', 'slow', {}, null, true, { kind: 'timeout' }),
      new ApiError(503, 'http_503', 'unavailable'),
    ]
    for (const error of retryable) {
      const onRetry = vi.fn()
      const { unmount } = renderWithI18n(<ErrorState error={error} onRetry={onRetry} />)
      expect(screen.getByRole('button', { name: /retry/i }), error.summary).toBeInTheDocument()
      unmount()
    }

    const final = [
      new ApiError(400, 'platform.unsupported_operation', 'nope'),
      new ApiError(401, 'security.authentication_required', 'no'),
      new ApiError(403, 'security.permission_denied', 'no'),
      new ApiError(404, 'platform.not_found', 'no'),
      new ApiError(409, 'platform.conflict', 'no'),
      new ApiError(422, 'platform.validation_failed', 'no'),
      new ApiError(200, 'protocol.malformed_response', 'bad', {}, null, false, { kind: 'malformed' }),
    ]
    for (const error of final) {
      const onRetry = vi.fn()
      const { unmount } = renderWithI18n(<ErrorState error={error} onRetry={onRetry} />)
      expect(screen.queryByRole('button', { name: /retry/i }), error.summary).not.toBeInTheDocument()
      unmount()
    }
  })

  it('renders nothing at all for a cancelled request unless the caller asks for it', () => {
    const { container, unmount } = renderWithI18n(<ErrorState error={ApiError.cancelled('/runs/run_a')} />)
    expect(container).toBeEmptyDOMElement()
    unmount()

    renderWithI18n(<ErrorState error={ApiError.cancelled('/runs/run_a')} showCancelled />)
    expect(screen.getByText(/cancelled/i)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('bounds a large detail payload instead of printing the whole response', () => {
    renderWithI18n(
      <ErrorState
        error={
          new ApiError(422, 'platform.validation_failed', 'too much', {
            rows: Array.from({ length: 200 }, (_, index) => `row-${index}`),
          })
        }
      />,
    )
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('row-0')
    expect(alert).toHaveTextContent('…')
    expect(alert.textContent!.length).toBeLessThan(2_400)
  })

  it('renders the backend detail payload for a validation failure', () => {
    renderWithI18n(
      <ErrorState
        error={
          new ApiError(422, 'platform.validation_failed', 'unknown report kind', {
            known: ['daily_drilling', 'npt'],
          })
        }
      />,
    )
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('platform.validation_failed')
    expect(alert).toHaveTextContent('daily_drilling')
  })

  it('never hides an unexpected error behind a success-looking panel', () => {
    renderWithI18n(<ErrorState error={new Error('boom')} />)
    expect(screen.getByRole('alert')).toHaveTextContent('boom')
  })

  it('omits the retry button when the caller cannot retry', () => {
    renderWithI18n(<ErrorState error={new ApiError(403, 'security.permission_denied', 'nope')} />)
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument()
  })
})

describe('<Value>', () => {
  it('shows the reason when a value is missing, never a zero', () => {
    renderWithI18n(<Value label="Current depth" value={null} unit="m" reason="no measured depth is recorded" />)
    expect(screen.getByText('Current depth')).toBeInTheDocument()
    expect(screen.queryByText('0')).not.toBeInTheDocument()
    expect(screen.getByText(/no measured depth is recorded/i)).toBeInTheDocument()
  })

  it('renders a recorded zero as a value', () => {
    renderWithI18n(<Value label="Variance" value={0} unit="m" />)
    expect(screen.getByText('0')).toBeInTheDocument()
  })
})

describe('<Table> and <Badge>', () => {
  it('uses the caller-supplied row key, including the row index', () => {
    renderWithI18n(
      <Table
        columns={[{ key: 'name', header: 'Name', render: (row: { name: string }) => row.name }]}
        rows={[{ name: 'NF-12' }, { name: 'NF-12' }]}
        rowKey={(_row, index) => `row-${index}`}
      />,
    )
    expect(screen.getAllByRole('row')).toHaveLength(3) // header + two rows
    expect(screen.getAllByText('NF-12')).toHaveLength(2)
  })

  it('renders the caller-supplied empty state instead of an empty table body', () => {
    renderWithI18n(
      <Table
        columns={[{ key: 'name', header: 'Name', render: () => null }]}
        rows={[]}
        rowKey={() => 'x'}
        empty={<EmptyState message="No engine runs recorded." />}
      />,
    )
    expect(screen.getByText('No engine runs recorded.')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('announces the loading state through the shared primitive', () => {
    renderWithI18n(<Loading label="Loading wells…" />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading wells…')
  })

  it('renders severity tone without changing the text', () => {
    renderWithI18n(<Badge tone="danger">NPT 6 h</Badge>)
    expect(screen.getByText('NPT 6 h')).toBeInTheDocument()
  })
})
