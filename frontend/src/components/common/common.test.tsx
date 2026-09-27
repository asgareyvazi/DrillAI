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

  it('renders the error state with a retry that calls refetch', async () => {
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

    await userEvent.click(screen.getByRole('button', { name: /retry/i }))
    expect(refetch).toHaveBeenCalledTimes(1)
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
    const { unmount } = renderWithI18n(<ErrorState error={new ApiError(0, 'network_unreachable', 'x')} />)
    expect(screen.getByRole('alert')).toHaveTextContent(/backend is unreachable/i)
    unmount()

    renderWithI18n(<ErrorState error={new ApiError(404, 'platform.not_found', 'x')} />)
    expect(screen.getByRole('alert')).toHaveTextContent(/not found/i)
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
