import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { drillingApi } from '../../api/endpoints'
import type { ApiTokenMetadata, ManagedAuthUser, PlatformIdentity } from '../../api/types'
import { I18nProvider } from '../../i18n'
import SecuritySettingsPage from './SecuritySettingsPage'

vi.mock('../../api/endpoints', () => ({
  drillingApi: {
    identity: vi.fn(),
    changePassword: vi.fn(),
    listAuthUsers: vi.fn(),
    createAuthUser: vi.fn(),
    updateAuthUser: vi.fn(),
    listApiTokens: vi.fn(),
    createApiToken: vi.fn(),
    revokeApiToken: vi.fn(),
  },
  platformApi: {},
  workflowApi: {},
  runApi: {},
  docsApi: {},
  engineeringApi: {},
  registryApi: {},
  ragApi: {},
}))

const api = vi.mocked(drillingApi)

const identity = {
  principal_id: 'usr_admin',
  principal_kind: 'user',
  display_name: 'Org Admin',
  user_email: 'admin@example.test',
  org_id: 'org_alpha',
  role_keys: ['org_admin'],
  roles: [],
  available_roles: [
    { key: 'viewer', name: 'Viewer', description: 'Read only', permissions: ['well.read'], max_action_level: 'L0' },
    { key: 'engineer', name: 'Drilling engineer', description: 'Engineering', permissions: ['well.*'], max_action_level: 'L2' },
    { key: 'admin', name: 'Platform administrator', description: 'Platform', permissions: ['admin.**'], max_action_level: 'L4' },
    { key: 'well_manager', name: 'Well manager', description: 'Wildcard automation', permissions: ['**'], max_action_level: 'L5' },
  ],
  permissions: ['user.*', 'vault.*', 'connector.*', 'audit.read', '*.read', 'project.write'],
  max_action_level: 'L4',
  auth_enabled: true,
  identity_source: 'session_cookie',
  development_presets: [],
  locale: 'en',
  note: '',
} as unknown as PlatformIdentity

const users: ManagedAuthUser[] = [
  {
    id: 'usr_admin',
    email: 'admin@example.test',
    display_name: 'Org Admin',
    job_title: 'Administrator',
    is_active: true,
    role_keys: ['org_admin'],
    last_login_at: null,
    created_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 'usr_engineer',
    email: 'engineer@example.test',
    display_name: 'Drilling Engineer',
    job_title: 'Engineer',
    is_active: true,
    role_keys: ['engineer'],
    last_login_at: '2026-10-01T08:30:00Z',
    created_at: '2026-01-02T00:00:00Z',
  },
]

const token: ApiTokenMetadata = {
  id: 'tok_1',
  org_id: 'org_alpha',
  user_id: 'usr_admin',
  name: 'well-reader',
  token_prefix: 'dk_prefix',
  scopes: ['well.read'],
  created_at: '2026-10-01T00:00:00Z',
  expires_at: '2026-12-30T00:00:00Z',
  last_used_at: null,
  revoked_at: null,
  is_active: true,
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <MemoryRouter initialEntries={['/security']}>
          <SecuritySettingsPage />
        </MemoryRouter>
      </I18nProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  window.sessionStorage.clear()
  api.identity.mockResolvedValue(identity)
  api.listAuthUsers.mockResolvedValue({ items: users, total: users.length })
  api.listApiTokens.mockResolvedValue({ items: [token], total: 1 })
  api.changePassword.mockResolvedValue({ status: 'password_changed', revoked_other_sessions: 2 })
  api.createAuthUser.mockResolvedValue({ ...users[1]!, id: 'usr_new', email: 'new@example.test' })
  api.updateAuthUser.mockImplementation(async (userId, body) => ({ ...users[1]!, id: userId, ...body } as ManagedAuthUser))
  api.createApiToken.mockResolvedValue({ ...token, id: 'tok_new', token: 'dk_secret_once' })
  api.revokeApiToken.mockResolvedValue({ ...token, revoked_at: '2026-10-20T00:00:00Z', is_active: false })
})

describe('identity and access settings', () => {
  it('creates tenant users without offering platform or wildcard-automation roles', async () => {
    const user = userEvent.setup()
    renderPage()

    await screen.findByTestId('auth-user-table')
    expect(screen.getByLabelText(/Viewer viewer/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Drilling engineer engineer/)).toBeInTheDocument()
    expect(screen.queryByLabelText(/Platform administrator admin/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/Well manager well_manager/)).not.toBeInTheDocument()

    await user.type(screen.getByLabelText('Work email'), 'new@example.test')
    await user.type(screen.getByLabelText('Display name'), 'New Engineer')
    await user.type(screen.getByLabelText(/Initial password/), 'Temporary-CP26-Password!2026')
    await user.click(screen.getByLabelText(/Drilling engineer engineer/))
    await user.click(screen.getByTestId('create-user-submit'))

    await waitFor(() => expect(api.createAuthUser).toHaveBeenCalledWith({
      email: 'new@example.test',
      display_name: 'New Engineer',
      job_title: null,
      password: 'Temporary-CP26-Password!2026',
      role_keys: ['viewer', 'engineer'],
    }))
  })

  it('shows a token secret once in memory, and only metadata after the form is dismissed', async () => {
    const user = userEvent.setup()
    renderPage()

    await screen.findByTestId('api-token-table')
    await user.type(screen.getByLabelText('Token name'), 'cli-reader')
    await user.clear(screen.getByTestId('token-scopes-input'))
    await user.type(screen.getByTestId('token-scopes-input'), 'well.read')
    await user.click(screen.getByTestId('create-token-submit'))

    const secretPanel = await screen.findByTestId('one-time-token')
    expect(within(secretPanel).getByText('dk_secret_once')).toBeInTheDocument()
    await waitFor(() => expect(api.createApiToken).toHaveBeenCalledWith({
      name: 'cli-reader',
      scopes: ['well.read'],
      expires_in_days: 90,
    }))

    await user.click(within(secretPanel).getByRole('button', { name: 'I saved the token' }))
    expect(screen.queryByText('dk_secret_once')).not.toBeInTheDocument()
    const storedValues = [window.localStorage, window.sessionStorage].flatMap((storage) =>
      Array.from({ length: storage.length }, (_, index) => storage.getItem(storage.key(index) ?? '') ?? ''),
    )
    expect(storedValues.join('\n')).not.toContain('dk_secret_once')
  })

  it('does not offer the current user an account-deactivation or role-edit action', async () => {
    renderPage()
    const ownRow = await screen.findByTestId('auth-user-usr_admin')
    expect(within(ownRow).getByRole('button', { name: 'Deactivate' })).toBeDisabled()
    expect(within(ownRow).getByRole('button', { name: 'Edit' })).toBeDisabled()
  })
})
