import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import type { ApiTokenMetadata, ManagedAuthUser, PlatformIdentity } from '../../api/types'
import { Badge, Button, Card, EmptyState, ErrorState, Loading } from '../../components/common'
import { PageHeader } from '../../components/layout/AppShell'
import { useI18n } from '../../i18n'
import { useSession } from '../../stores/session'

function permissionMatches(pattern: string, permission: string): boolean {
  if (pattern === '*' || pattern === '**' || pattern === permission) return true
  if (pattern.endsWith('.**')) return permission.startsWith(`${pattern.slice(0, -3)}.`)
  if (pattern.endsWith('.*')) {
    const prefix = pattern.slice(0, -2)
    return permission.startsWith(`${prefix}.`) && permission.slice(prefix.length + 1).indexOf('.') === -1
  }
  if (!pattern.includes('*')) return false
  const escaped = pattern.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replaceAll('*', '.*')
  return new RegExp(`^${escaped}$`).test(permission)
}

function hasPermission(identity: PlatformIdentity, permission: string): boolean {
  return identity.permissions.some((pattern) => permissionMatches(pattern, permission))
}

function dateLabel(value: string | null, never: string): string {
  return value ? new Date(value).toLocaleString() : never
}

function updateSelection(current: string[], key: string, checked: boolean): string[] {
  return checked ? [...new Set([...current, key])] : current.filter((item) => item !== key)
}

function tokenStatus(token: ApiTokenMetadata, active: string, inactive: string) {
  return token.is_active
    ? <Badge tone="ok">{active}</Badge>
    : <Badge tone="warning">{inactive}</Badge>
}

export default function SecuritySettingsPage() {
  const { t } = useI18n()
  const { devRoles } = useSession()
  const queryClient = useQueryClient()
  const identityQuery = useQuery({
    queryKey: ['identity', devRoles],
    queryFn: ({ signal }) => drillingApi.identity(signal),
  })

  if (identityQuery.isLoading) {
    return <div className="space-y-4"><PageHeader title={t('security.title')} description={t('security.description')} /><Loading /></div>
  }
  if (identityQuery.error) {
    return (
      <div className="space-y-4">
        <PageHeader title={t('security.title')} description={t('security.description')} />
        <ErrorState error={identityQuery.error} />
        <Link className="text-sm font-medium text-signal-deep underline" to="/login">{t('security.signIn')}</Link>
      </div>
    )
  }

  const identity = identityQuery.data
  if (!identity?.auth_enabled) {
    return (
      <div className="space-y-4">
        <PageHeader title={t('security.title')} description={t('security.description')} />
        <Card><p className="text-sm text-graphite-600 dark:text-graphite-300">{t('security.authDisabled')}</p></Card>
      </div>
    )
  }

  return (
    <SecurityWorkspace
      identity={identity}
      onIdentityChanged={() => void queryClient.invalidateQueries({ queryKey: ['identity'] })}
    />
  )
}

function SecurityWorkspace({
  identity,
  onIdentityChanged,
}: {
  identity: PlatformIdentity
  onIdentityChanged: () => void
}) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const canReadUsers = hasPermission(identity, 'user.read')
  const canManageUsers = hasPermission(identity, 'user.manage')
  const isPlatformAdmin = identity.role_keys.includes('admin') && hasPermission(identity, 'system.egress_policy.manage')
  const roles = identity.available_roles.filter(
    (role) => isPlatformAdmin || !['admin', 'well_manager'].includes(role.key),
  )
  const usersQuery = useQuery({
    queryKey: ['auth-users', identity.org_id],
    queryFn: ({ signal }) => drillingApi.listAuthUsers(signal),
    enabled: canReadUsers,
  })
  const tokensQuery = useQuery({
    queryKey: ['auth-api-tokens', identity.org_id, identity.principal_id],
    queryFn: ({ signal }) => drillingApi.listApiTokens(signal),
  })

  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [passwordError, setPasswordError] = useState<unknown>(null)
  const [passwordSuccess, setPasswordSuccess] = useState(false)
  const [changingPassword, setChangingPassword] = useState(false)

  const [newEmail, setNewEmail] = useState('')
  const [newDisplayName, setNewDisplayName] = useState('')
  const [newJobTitle, setNewJobTitle] = useState('')
  const [temporaryPassword, setTemporaryPassword] = useState('')
  const [newRoleKeys, setNewRoleKeys] = useState<string[]>(['viewer'])
  const [userError, setUserError] = useState<unknown>(null)
  const [creatingUser, setCreatingUser] = useState(false)
  const [editingUser, setEditingUser] = useState<ManagedAuthUser | null>(null)
  const [editName, setEditName] = useState('')
  const [editJobTitle, setEditJobTitle] = useState('')
  const [editRoleKeys, setEditRoleKeys] = useState<string[]>([])

  const [tokenName, setTokenName] = useState('')
  const [tokenScopes, setTokenScopes] = useState('well.read')
  const [tokenExpiry, setTokenExpiry] = useState('90')
  const [tokenUserId, setTokenUserId] = useState('')
  const [tokenError, setTokenError] = useState<unknown>(null)
  const [oneTimeToken, setOneTimeToken] = useState<string | null>(null)
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const [creatingToken, setCreatingToken] = useState(false)
  const [pendingRevoke, setPendingRevoke] = useState<string | null>(null)

  const roleNames = new Map(identity.available_roles.map((role) => [role.key, role.name]))
  const users = usersQuery.data?.items ?? []
  const targetUsers = users.filter((user) => user.is_active)
  const isSelf = (user: ManagedAuthUser) => user.id === identity.principal_id
  const roleEditBlocked = (user: ManagedAuthUser) =>
    !isPlatformAdmin && user.role_keys.some((key) => ['admin', 'well_manager'].includes(key))

  async function submitPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setPasswordError(null)
    setPasswordSuccess(false)
    setChangingPassword(true)
    try {
      await drillingApi.changePassword({ current_password: currentPassword, new_password: newPassword })
      setCurrentPassword('')
      setNewPassword('')
      setPasswordSuccess(true)
    } catch (error) {
      setPasswordError(error)
    } finally {
      setChangingPassword(false)
    }
  }

  async function submitCreateUser(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setUserError(null)
    if (newRoleKeys.length === 0) return
    setCreatingUser(true)
    try {
      await drillingApi.createAuthUser({
        email: newEmail.trim(),
        display_name: newDisplayName.trim(),
        job_title: newJobTitle.trim() || null,
        password: temporaryPassword,
        role_keys: newRoleKeys,
      })
      setNewEmail('')
      setNewDisplayName('')
      setNewJobTitle('')
      setTemporaryPassword('')
      setNewRoleKeys(['viewer'])
      await queryClient.invalidateQueries({ queryKey: ['auth-users'] })
    } catch (error) {
      setUserError(error)
    } finally {
      setCreatingUser(false)
    }
  }

  function beginEdit(user: ManagedAuthUser) {
    setEditingUser(user)
    setEditName(user.display_name)
    setEditJobTitle(user.job_title ?? '')
    setEditRoleKeys([...user.role_keys])
    setUserError(null)
  }

  async function saveUser(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!editingUser || editRoleKeys.length === 0) return
    setUserError(null)
    try {
      await drillingApi.updateAuthUser(editingUser.id, {
        display_name: editName.trim(),
        job_title: editJobTitle.trim() || null,
        role_keys: editRoleKeys,
      })
      setEditingUser(null)
      await queryClient.invalidateQueries({ queryKey: ['auth-users'] })
      onIdentityChanged()
    } catch (error) {
      setUserError(error)
    }
  }

  async function toggleUserActive(user: ManagedAuthUser) {
    setUserError(null)
    try {
      await drillingApi.updateAuthUser(user.id, { is_active: !user.is_active })
      await queryClient.invalidateQueries({ queryKey: ['auth-users'] })
    } catch (error) {
      setUserError(error)
    }
  }

  async function submitToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setTokenError(null)
    setOneTimeToken(null)
    setCopyState('idle')
    const scopes = [...new Set(tokenScopes.split(/[\s,]+/).map((scope) => scope.trim()).filter(Boolean))]
    setCreatingToken(true)
    try {
      const created = await drillingApi.createApiToken({
        name: tokenName.trim(),
        scopes,
        expires_in_days: tokenExpiry === 'never' ? null : Number(tokenExpiry),
        ...(tokenUserId ? { user_id: tokenUserId } : {}),
      })
      if (!created.token) throw new Error('The server did not return the one-time token secret.')
      setOneTimeToken(created.token)
      setTokenName('')
      await queryClient.invalidateQueries({ queryKey: ['auth-api-tokens'] })
    } catch (error) {
      setTokenError(error)
    } finally {
      setCreatingToken(false)
    }
  }

  async function copyToken() {
    if (!oneTimeToken) return
    try {
      await navigator.clipboard.writeText(oneTimeToken)
      setCopyState('copied')
    } catch {
      setCopyState('failed')
    }
  }

  async function revokeToken(tokenId: string) {
    setTokenError(null)
    try {
      await drillingApi.revokeApiToken(tokenId)
      setPendingRevoke(null)
      await queryClient.invalidateQueries({ queryKey: ['auth-api-tokens'] })
    } catch (error) {
      setTokenError(error)
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader title={t('security.title')} description={t('security.description')} />

      <Card title={t('security.changePassword')}>
        <form className="grid gap-3 md:grid-cols-3" onSubmit={(event) => void submitPassword(event)}>
          <label className="space-y-1 text-xs">
            <span>{t('security.currentPassword')}</span>
            <input
              type="password"
              autoComplete="current-password"
              required
              value={currentPassword}
              onChange={(event) => setCurrentPassword(event.target.value)}
              className="w-full rounded border border-graphite-300 bg-white px-3 py-2 text-sm dark:border-graphite-700 dark:bg-graphite-900"
            />
          </label>
          <label className="space-y-1 text-xs">
            <span>{t('security.newPassword')}</span>
            <input
              type="password"
              autoComplete="new-password"
              required
              minLength={12}
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              className="w-full rounded border border-graphite-300 bg-white px-3 py-2 text-sm dark:border-graphite-700 dark:bg-graphite-900"
            />
          </label>
          <div className="flex items-end">
            <Button type="submit" variant="primary" disabled={changingPassword} data-testid="change-password-submit">
              {changingPassword ? t('common.loading') : t('security.updatePassword')}
            </Button>
          </div>
        </form>
        {Boolean(passwordError) && <div className="mt-3"><ErrorState error={passwordError} /></div>}
        {passwordSuccess && <p className="mt-3 text-xs text-ok" role="status">{t('security.passwordUpdated')}</p>}
      </Card>

      {canReadUsers && (
        <Card title={t('security.users')} subtitle={t('security.userManagementHint')}>
          {canManageUsers && (
            <form className="mb-4 grid gap-3 border-b border-graphite-100 pb-4 dark:border-graphite-800 md:grid-cols-2" onSubmit={(event) => void submitCreateUser(event)}>
              <label className="space-y-1 text-xs">
                <span>{t('security.email')}</span>
                <input type="email" required value={newEmail} onChange={(event) => setNewEmail(event.target.value)} className="w-full rounded border border-graphite-300 bg-white px-3 py-2 text-sm dark:border-graphite-700 dark:bg-graphite-900" />
              </label>
              <label className="space-y-1 text-xs">
                <span>{t('security.displayName')}</span>
                <input required value={newDisplayName} onChange={(event) => setNewDisplayName(event.target.value)} className="w-full rounded border border-graphite-300 bg-white px-3 py-2 text-sm dark:border-graphite-700 dark:bg-graphite-900" />
              </label>
              <label className="space-y-1 text-xs">
                <span>{t('security.jobTitle')}</span>
                <input value={newJobTitle} onChange={(event) => setNewJobTitle(event.target.value)} className="w-full rounded border border-graphite-300 bg-white px-3 py-2 text-sm dark:border-graphite-700 dark:bg-graphite-900" />
              </label>
              <label className="space-y-1 text-xs">
                <span>{t('security.temporaryPassword')}</span>
                <input type="password" autoComplete="new-password" required minLength={12} value={temporaryPassword} onChange={(event) => setTemporaryPassword(event.target.value)} className="w-full rounded border border-graphite-300 bg-white px-3 py-2 text-sm dark:border-graphite-700 dark:bg-graphite-900" />
                <span className="block text-[11px] text-graphite-500">{t('security.passwordHint')}</span>
              </label>
              <fieldset className="md:col-span-2">
                <legend className="mb-2 text-xs font-medium">{t('security.roles')}</legend>
                <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                  {roles.map((role) => (
                    <label key={role.key} className="flex items-center gap-2 rounded border border-graphite-200 px-2 py-1.5 text-xs dark:border-graphite-700">
                      <input
                        type="checkbox"
                        checked={newRoleKeys.includes(role.key)}
                        onChange={(event) => setNewRoleKeys((current) => updateSelection(current, role.key, event.target.checked))}
                      />
                      <span>{role.name} <code className="text-[10px] text-graphite-500">{role.key}</code></span>
                    </label>
                  ))}
                </div>
                <p className="mt-2 text-[11px] text-graphite-500">{t('security.roleHint')}</p>
              </fieldset>
              <div className="md:col-span-2">
                <Button type="submit" variant="primary" disabled={creatingUser || newRoleKeys.length === 0} data-testid="create-user-submit">
                  {creatingUser ? t('common.loading') : t('security.createUser')}
                </Button>
              </div>
            </form>
          )}

          {Boolean(userError) && <div className="mb-3"><ErrorState error={userError} /></div>}
          {usersQuery.isLoading ? <Loading /> : usersQuery.error ? <ErrorState error={usersQuery.error} onRetry={() => void usersQuery.refetch()} /> : users.length === 0 ? <EmptyState message={t('security.noUsers')} /> : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-start text-xs" aria-label={t('security.users')} data-testid="auth-user-table">
                <thead className="text-graphite-500">
                  <tr className="border-b border-graphite-200 dark:border-graphite-700">
                    <th className="px-2 py-2 text-start">{t('security.email')}</th>
                    <th className="px-2 py-2 text-start">{t('security.roles')}</th>
                    <th className="px-2 py-2 text-start">{t('common.status')}</th>
                    <th className="px-2 py-2 text-start">{t('security.lastLogin')}</th>
                    {canManageUsers && <th className="px-2 py-2 text-end">{t('common.actions')}</th>}
                  </tr>
                </thead>
                <tbody>
                  {users.map((user) => (
                    <tr key={user.id} className="border-b border-graphite-100 align-top dark:border-graphite-800" data-testid={`auth-user-${user.id}`}>
                      <td className="px-2 py-2">
                        <div className="font-medium">{user.display_name}</div>
                        <div className="text-graphite-500">{user.email}</div>
                      </td>
                      <td className="px-2 py-2">{user.role_keys.map((key) => roleNames.get(key) ?? key).join(', ')}</td>
                      <td className="px-2 py-2"><Badge tone={user.is_active ? 'ok' : 'warning'}>{user.is_active ? t('security.active') : t('security.inactive')}</Badge></td>
                      <td className="px-2 py-2">{dateLabel(user.last_login_at, t('security.never'))}</td>
                      {canManageUsers && (
                        <td className="px-2 py-2 text-end">
                          <div className="flex justify-end gap-2">
                            <Button
                              size="sm"
                              onClick={() => beginEdit(user)}
                              disabled={isSelf(user) || roleEditBlocked(user)}
                              title={isSelf(user) ? t('security.selfDeactivateBlocked') : roleEditBlocked(user) ? t('security.roleHint') : undefined}
                            >
                              {t('security.edit')}
                            </Button>
                            <Button
                              size="sm"
                              variant={user.is_active ? 'danger' : 'default'}
                              onClick={() => void toggleUserActive(user)}
                              disabled={isSelf(user)}
                              title={isSelf(user) ? t('security.selfDeactivateBlocked') : undefined}
                            >
                              {user.is_active ? t('security.deactivate') : t('security.activate')}
                            </Button>
                          </div>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {editingUser && (
            <form className="mt-4 space-y-3 rounded-lg border border-signal/30 bg-signal/5 p-3" onSubmit={(event) => void saveUser(event)} data-testid="edit-user-form">
              <h3 className="text-sm font-semibold">{t('security.edit')}: {editingUser.email}</h3>
              <div className="grid gap-3 md:grid-cols-2">
                <label className="space-y-1 text-xs"><span>{t('security.displayName')}</span><input required value={editName} onChange={(event) => setEditName(event.target.value)} className="w-full rounded border border-graphite-300 bg-white px-3 py-2 text-sm dark:border-graphite-700 dark:bg-graphite-900" /></label>
                <label className="space-y-1 text-xs"><span>{t('security.jobTitle')}</span><input value={editJobTitle} onChange={(event) => setEditJobTitle(event.target.value)} className="w-full rounded border border-graphite-300 bg-white px-3 py-2 text-sm dark:border-graphite-700 dark:bg-graphite-900" /></label>
              </div>
              <fieldset>
                <legend className="mb-2 text-xs font-medium">{t('security.roles')}</legend>
                <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                  {roles.filter((role) => editingUser.role_keys.includes(role.key) || !['admin', 'well_manager'].includes(role.key)).map((role) => (
                    <label key={role.key} className="flex items-center gap-2 rounded border border-graphite-200 px-2 py-1.5 text-xs dark:border-graphite-700">
                      <input
                        type="checkbox"
                        disabled={['admin', 'well_manager'].includes(role.key) && !isPlatformAdmin}
                        checked={editRoleKeys.includes(role.key)}
                        onChange={(event) => setEditRoleKeys((current) => updateSelection(current, role.key, event.target.checked))}
                      />
                      <span>{role.name} <code className="text-[10px] text-graphite-500">{role.key}</code></span>
                    </label>
                  ))}
                </div>
              </fieldset>
              <div className="flex gap-2">
                <Button type="submit" variant="primary" disabled={editRoleKeys.length === 0}>{t('security.saveUser')}</Button>
                <Button onClick={() => setEditingUser(null)}>{t('common.cancel')}</Button>
              </div>
            </form>
          )}
        </Card>
      )}

      <Card title={t('security.apiTokens')} subtitle={t('security.tokenManagementHint')}>
        <form className="mb-4 grid gap-3 border-b border-graphite-100 pb-4 dark:border-graphite-800 md:grid-cols-2" onSubmit={(event) => void submitToken(event)}>
          <label className="space-y-1 text-xs">
            <span>{t('security.tokenName')}</span>
            <input required maxLength={200} value={tokenName} onChange={(event) => setTokenName(event.target.value)} className="w-full rounded border border-graphite-300 bg-white px-3 py-2 text-sm dark:border-graphite-700 dark:bg-graphite-900" />
          </label>
          <label className="space-y-1 text-xs">
            <span>{t('security.expiresIn')}</span>
            <select value={tokenExpiry} onChange={(event) => setTokenExpiry(event.target.value)} className="w-full rounded border border-graphite-300 bg-white px-3 py-2 text-sm dark:border-graphite-700 dark:bg-graphite-900">
              <option value="30">{t('security.expiry30')}</option>
              <option value="90">{t('security.expiry90')}</option>
              <option value="365">{t('security.expiry365')}</option>
              <option value="never">{t('security.noExpiry')}</option>
            </select>
          </label>
          <label className="space-y-1 text-xs md:col-span-2">
            <span>{t('security.tokenScopes')}</span>
            <textarea required rows={3} value={tokenScopes} onChange={(event) => setTokenScopes(event.target.value)} className="w-full rounded border border-graphite-300 bg-white px-3 py-2 text-sm font-mono dark:border-graphite-700 dark:bg-graphite-900" data-testid="token-scopes-input" />
            <span className="block text-[11px] text-graphite-500">{t('security.scopesHint')}</span>
          </label>
          {canManageUsers && targetUsers.length > 0 && (
            <label className="space-y-1 text-xs">
              <span>{t('security.issueFor')}</span>
              <select value={tokenUserId} onChange={(event) => setTokenUserId(event.target.value)} className="w-full rounded border border-graphite-300 bg-white px-3 py-2 text-sm dark:border-graphite-700 dark:bg-graphite-900">
                <option value="">{t('security.myAccount')}</option>
                {targetUsers.filter((user) => user.id !== identity.principal_id).map((user) => (
                  <option key={user.id} value={user.id}>{user.display_name} · {user.email}</option>
                ))}
              </select>
            </label>
          )}
          <div className="flex items-end">
            <Button type="submit" variant="primary" disabled={creatingToken} data-testid="create-token-submit">
              {creatingToken ? t('common.loading') : t('security.createToken')}
            </Button>
          </div>
        </form>
        {Boolean(tokenError) && <div className="mb-3"><ErrorState error={tokenError} /></div>}
        {oneTimeToken && (
          <div className="mb-4 space-y-2 rounded-lg border border-warning/40 bg-amber-50 p-3 dark:bg-amber-950/30" role="status" data-testid="one-time-token">
            <p className="text-xs font-medium text-warning">{t('security.oneTimeWarning')}</p>
            <code className="block break-all rounded bg-white p-2 text-xs dark:bg-graphite-900">{oneTimeToken}</code>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={() => void copyToken()}>{copyState === 'copied' ? t('security.copied') : t('security.copyToken')}</Button>
              <Button size="sm" onClick={() => { setOneTimeToken(null); setCopyState('idle') }}>{t('security.savedToken')}</Button>
              {copyState === 'failed' && <span className="text-[11px] text-warning">{t('security.copyFailed')}</span>}
            </div>
          </div>
        )}
        {tokensQuery.isLoading ? <Loading /> : tokensQuery.error ? <ErrorState error={tokensQuery.error} onRetry={() => void tokensQuery.refetch()} /> : (tokensQuery.data?.items.length ?? 0) === 0 ? <EmptyState message={t('security.noTokens')} /> : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-start text-xs" aria-label={t('security.apiTokens')} data-testid="api-token-table">
              <thead className="text-graphite-500">
                <tr className="border-b border-graphite-200 dark:border-graphite-700">
                  <th className="px-2 py-2 text-start">{t('security.tokenName')}</th>
                  <th className="px-2 py-2 text-start">{t('security.issueFor')}</th>
                  <th className="px-2 py-2 text-start">{t('security.tokenScopes')}</th>
                  <th className="px-2 py-2 text-start">{t('common.status')}</th>
                  <th className="px-2 py-2 text-start">{t('security.expiresIn')}</th>
                  <th className="px-2 py-2 text-end">{t('common.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {tokensQuery.data?.items.map((token) => {
                  const owner = users.find((user) => user.id === token.user_id)
                  return (
                    <tr key={token.id} className="border-b border-graphite-100 align-top dark:border-graphite-800" data-testid={`api-token-${token.id}`}>
                      <td className="px-2 py-2"><div className="font-medium">{token.name}</div><code className="text-[10px] text-graphite-500">{token.token_prefix}</code></td>
                      <td className="px-2 py-2">{owner?.display_name ?? token.user_id}</td>
                      <td className="px-2 py-2 font-mono">{token.scopes.join(', ') || t('common.none')}</td>
                      <td className="px-2 py-2">{tokenStatus(token, t('security.active'), token.revoked_at ? t('security.revoked') : t('security.inactive'))}</td>
                      <td className="px-2 py-2">{dateLabel(token.expires_at, t('security.never'))}</td>
                      <td className="px-2 py-2 text-end">
                        {token.is_active && (pendingRevoke === token.id ? (
                          <span className="inline-flex gap-1">
                            <Button size="sm" variant="danger" onClick={() => void revokeToken(token.id)}>{t('security.confirmRevoke')}</Button>
                            <Button size="sm" onClick={() => setPendingRevoke(null)}>{t('common.cancel')}</Button>
                          </span>
                        ) : (
                          <Button size="sm" variant="danger" onClick={() => setPendingRevoke(token.id)}>{t('security.revoke')}</Button>
                        ))}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
