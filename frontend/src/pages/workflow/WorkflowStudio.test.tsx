/**
 * The studio's capability gate, when the identity read fails.
 *
 * The editor enables and disables its actions from the permission list the server sends. If that read
 * fails, the list is `undefined`, every permission check answers *no*, and the studio used to explain
 * the disabled buttons by telling the engineer their identity does not hold `workflow.publish` — a
 * statement about somebody's account that the server never made. These tests pin the two states apart.
 *
 * Only the identity read is driven here; the canvas itself is exercised by the end-to-end journeys.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { drillingApi } from '../../api/endpoints'
import { I18nProvider } from '../../i18n'
import WorkflowStudio from './WorkflowStudio'

vi.mock('../../api/endpoints', () => ({
  drillingApi: {
    identity: vi.fn(),
    listWorkflows: vi.fn(),
    nodeTypes: vi.fn(),
  },
  platformApi: {},
  workflowApi: {},
  runApi: {},
  docsApi: {},
  engineeringApi: {},
  registryApi: {},
  ragApi: {},
}))

// The canvas is a separate concern with its own library; this file only needs the header's actions.
vi.mock('@xyflow/react', async () => {
  const actual = await vi.importActual<typeof import('@xyflow/react')>('@xyflow/react')
  return {
    ...actual,
    ReactFlow: () => <div data-testid="canvas" />,
    Background: () => null,
    Controls: () => null,
  }
})

const api = vi.mocked(drillingApi)

const workflow = {
  id: 'wfl_1',
  key: 'daily-drilling-intelligence',
  name: 'Daily Drilling Intelligence',
  status: 'published',
  current_version: 1,
  published_version_id: 'wfv_1',
}

function renderStudio() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <MemoryRouter initialEntries={[`/workflows?workflow=${workflow.id}`]}>
          <WorkflowStudio />
        </MemoryRouter>
      </I18nProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  api.listWorkflows.mockResolvedValue({ items: [workflow], total: 1, limit: 100, offset: 0 } as never)
  api.nodeTypes.mockResolvedValue({ items: [], families: {}, total: 0 } as never)
})

describe('the studio identity gate', () => {
  it('says the permissions could not be read, not that the identity lacks one', async () => {
    api.identity.mockRejectedValue(new ApiError(500, 'platform.internal_error', 'boom'))
    renderStudio()

    const notice = await screen.findByTestId('identity-unavailable')
    expect(notice).toHaveTextContent(/permissions of this identity could not be read/i)
    // The false statement this replaces.
    expect(notice).not.toHaveTextContent(/does not hold 'workflow.publish'/)
    expect(within(notice).getByRole('button', { name: /retry/i })).toBeInTheDocument()
  })

  it('re-reads the identity when the operator retries, and clears the notice', async () => {
    api.identity.mockRejectedValueOnce(new ApiError(0, 'network.unreachable', 'down'))
    renderStudio()

    const notice = await screen.findByTestId('identity-unavailable')
    api.identity.mockResolvedValue({
      subject: 'dev',
      role_keys: ['engineer'],
      permissions: ['workflow.*'],
    } as never)
    await userEvent.click(within(notice).getByRole('button', { name: /retry/i }))

    await waitFor(() => expect(screen.queryByTestId('identity-unavailable')).toBeNull())
    expect(api.identity).toHaveBeenCalledTimes(2)
  })

  it('still names a genuine permission gap as a permission gap', async () => {
    api.identity.mockResolvedValue({
      subject: 'dev',
      role_keys: ['viewer'],
      permissions: ['workflow.read'],
    } as never)
    renderStudio()

    // No failure anywhere: this identity really may not publish.
    await waitFor(() => expect(screen.queryByTestId('identity-unavailable')).toBeNull())
    await waitFor(() => {
      const publish = screen.getByRole('button', { name: /^publish$/i })
      expect(publish).toBeDisabled()
      expect(publish).toHaveAttribute('title', expect.stringContaining("does not hold 'workflow.publish'"))
    })
  })
})
