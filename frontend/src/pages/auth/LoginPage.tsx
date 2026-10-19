/**
 * Interactive per-user login screen (`/login`).
 *
 * Authenticates against `POST /api/v1/auth/login`, which sets an `HttpOnly`, `SameSite` session
 * cookie (`drillai_session`) and returns a per-session CSRF synchronizer token (`csrf_token`) kept
 * only in memory by the API client. No bearer token is ever baked into the production bundle or
 * written to `localStorage`.
 */

import { useState, type FormEvent } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { drillingApi } from '../../api/endpoints'
import { setSessionAuth } from '../../api/client'
import { Button, Card, ErrorState } from '../../components/common'
import { useI18n } from '../../i18n'

export default function LoginPage() {
  const { t } = useI18n()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const queryClient = useQueryClient()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [orgSlug, setOrgSlug] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const nextPath = searchParams.get('next') || '/wells'

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const session = await drillingApi.login({
        email: email.trim(),
        password,
        ...(orgSlug.trim() ? { org_slug: orgSlug.trim() } : {}),
      })
      setSessionAuth({
        sessionId: session.session_id,
        csrfToken: session.csrf_token,
      })
      await queryClient.resetQueries()
      navigate(nextPath.startsWith('/') ? nextPath : '/wells', { replace: true })
    } catch (caught) {
      setError(caught)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="mx-auto max-w-md py-10">
      <Card title={t('auth.title')} subtitle={t('auth.subtitle')}>
        <form onSubmit={handleSubmit} data-testid="login-form" className="space-y-4">
          {Boolean(error) && <ErrorState error={error} />}

          <div>
            <label htmlFor="login-email" className="block text-xs font-medium text-graphite-700 dark:text-graphite-300">
              {t('auth.emailLabel')}
            </label>
            <input
              id="login-email"
              type="email"
              required
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder={t('auth.emailPlaceholder')}
              data-testid="login-email-input"
              className="mt-1 w-full rounded border border-graphite-300 bg-white px-3 py-2 text-sm dark:border-graphite-700 dark:bg-graphite-900"
            />
          </div>

          <div>
            <label
              htmlFor="login-password"
              className="block text-xs font-medium text-graphite-700 dark:text-graphite-300"
            >
              {t('auth.passwordLabel')}
            </label>
            <input
              id="login-password"
              type="password"
              required
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={t('auth.passwordPlaceholder')}
              data-testid="login-password-input"
              className="mt-1 w-full rounded border border-graphite-300 bg-white px-3 py-2 text-sm dark:border-graphite-700 dark:bg-graphite-900"
            />
          </div>

          <div>
            <label htmlFor="login-org" className="block text-xs font-medium text-graphite-700 dark:text-graphite-300">
              {t('auth.orgSlugLabel')}
            </label>
            <input
              id="login-org"
              type="text"
              value={orgSlug}
              onChange={(e) => setOrgSlug(e.target.value)}
              placeholder={t('auth.orgSlugPlaceholder')}
              data-testid="login-org-input"
              className="mt-1 w-full rounded border border-graphite-300 bg-white px-3 py-2 text-sm dark:border-graphite-700 dark:bg-graphite-900"
            />
          </div>

          <div className="pt-1">
            <Button type="submit" disabled={submitting} data-testid="login-submit-button">
              {submitting ? t('auth.signingIn') : t('auth.submitBtn')}
            </Button>
          </div>

          <p className="text-[11px] text-graphite-500">{t('auth.securityNotice')}</p>
        </form>
      </Card>
    </div>
  )
}
