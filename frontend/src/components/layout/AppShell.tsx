/**
 * Application shell: navigation, identity/unit controls, locale switching.
 *
 * The sidebar is data-driven from a single route table so a page cannot exist in the router without
 * appearing in the navigation (and vice versa) — the two drifting apart is how frontends end up
 * with unreachable screens.
 */

import { useQuery } from '@tanstack/react-query'
import clsx from 'clsx'
import type { ReactNode } from 'react'
import { NavLink, Outlet, useParams } from 'react-router-dom'
import { ApiError, isAbortError } from '../../api/client'
import { drillingApi } from '../../api/endpoints'
import { LOCALES, LOCALE_LABELS, useI18n, type Locale } from '../../i18n'
import { useSession, selectedRoleKeys } from '../../stores/session'
import { Badge } from '../common'
import { formatActionLevel } from '../../lib/format'

export interface NavItem {
  to: string
  labelKey: string
  /** Well-scoped destinations are only shown inside a well context. */
  scope: 'global' | 'well'
}

export const NAV_ITEMS: NavItem[] = [
  { to: '/wells', labelKey: 'nav.wells', scope: 'global' },
  { to: '/master-data', labelKey: 'nav.masterData', scope: 'global' },
  { to: '/wells/:wellId/cockpit', labelKey: 'nav.cockpit', scope: 'well' },
  { to: '/wells/:wellId/documents', labelKey: 'nav.documents', scope: 'well' },
  { to: '/wells/:wellId/operations', labelKey: 'nav.operations', scope: 'well' },
  { to: '/wells/:wellId/engineering', labelKey: 'nav.engineering', scope: 'well' },
  { to: '/wells/:wellId/optimisation', labelKey: 'nav.optimisation', scope: 'well' },
  { to: '/wells/:wellId/advisor', labelKey: 'nav.advisor', scope: 'well' },
  { to: '/wells/:wellId/reports', labelKey: 'nav.reports', scope: 'well' },
  { to: '/workflows', labelKey: 'nav.workflowStudio', scope: 'global' },
  { to: '/runs', labelKey: 'nav.runs', scope: 'global' },
  { to: '/library', labelKey: 'nav.library', scope: 'global' },
  { to: '/platform', labelKey: 'nav.platform', scope: 'global' },
]

function LocaleSwitch() {
  const { locale, setLocale, t } = useI18n()
  return (
    <label className="flex items-center gap-1.5 text-xs">
      <span className="text-graphite-500">{t('common.language')}</span>
      <select
        value={locale}
        onChange={(event) => setLocale(event.target.value as Locale)}
        data-testid="locale-switch"
        className="rounded border border-graphite-300 bg-white px-1.5 py-1 dark:border-graphite-700 dark:bg-graphite-900"
      >
        {LOCALES.map((code) => (
          <option key={code} value={code}>
            {LOCALE_LABELS[code]}
          </option>
        ))}
      </select>
    </label>
  )
}

/**
 * The identity the session is acting as, and the ceiling it may act up to.
 *
 * The list of roles to choose from is the server's (`development_presets` on `/platform/identity`),
 * never a copy kept here: a frontend list drifts, and this one had — the backend advertises eight
 * catalogued roles and the hard-coded list offered six, so an auditor, a data manager or an
 * integrity engineer could not be exercised from the interface at all.
 *
 * The names come from the server's own catalogue (`available_roles`). They are the platform's names
 * for its roles, so they are shown as the server wrote them rather than translated here; only the
 * chrome around them is localised, including the marker that says these are development identities.
 */
function IdentityControls() {
  const { t } = useI18n()
  const { devRoles, setDevRoles, unitSystem, setUnitSystem } = useSession()
  const tokenConfigured = Boolean(import.meta.env.VITE_API_TOKEN)
  // The same query key the studio reads: one identity, one cached answer, shared.
  const identity = useQuery({ queryKey: ['identity', devRoles], queryFn: ({ signal }) => drillingApi.identity(signal) })

  const names = new Map((identity.data?.available_roles ?? []).map((role) => [role.key, role.name]))
  const presets = identity.data?.development_presets ?? []
  const selected = selectedRoleKeys(devRoles)
  const label = (key: string) => {
    const name = names.get(key)
    return name ? `${name} · ${key}` : key
  }
  // The selection is always offered, even when the server's list is missing or does not contain it:
  // a `<select>` whose value has no option renders the wrong thing, and the current identity is a
  // fact about this session whatever the last read returned.
  const options = presets.includes(devRoles) ? presets : [devRoles, ...presets]
  const ceiling = identity.data?.max_action_level
  const roleNames = (identity.data?.roles ?? [])
    .map((role) => role.name)
    .join(', ')

  return (
    <div className="flex flex-wrap items-center gap-3">
      <label className="flex items-center gap-1.5 text-xs">
        <span className="text-graphite-500">{t('common.detail')}</span>
        <select
          value={unitSystem}
          onChange={(event) => setUnitSystem(event.target.value as 'si' | 'oilfield')}
          className="rounded border border-graphite-300 bg-white px-1.5 py-1 dark:border-graphite-700 dark:bg-graphite-900"
        >
          <option value="si">SI</option>
          <option value="oilfield">Oilfield</option>
        </select>
      </label>
      {tokenConfigured ? (
        <Badge tone="ok">{t('roles.bearerConfigured')}</Badge>
      ) : (
        <label className="flex items-center gap-1.5 text-xs" title={t('roles.note')}>
          <span className="text-graphite-500">{t('roles.title')}</span>
          <select
            value={devRoles}
            onChange={(event) => setDevRoles(event.target.value)}
            data-testid="shell-role-switch"
            className="rounded border border-graphite-300 bg-white px-1.5 py-1 dark:border-graphite-700 dark:bg-graphite-900"
          >
            {options.map((key) => (
              <option key={key} value={key}>
                {key === devRoles && selected.length > 1
                  ? `${selected.map((one) => names.get(one) ?? one).join(' + ')} · ${t('roles.development')}`
                  : `${label(key)} · ${t('roles.development')}`}
              </option>
            ))}
          </select>
        </label>
      )}
      {/*
        What the server says this identity is, and how far it may act. Both are the server's values:
        a role list the interface invented, or a ceiling it guessed, would be a second truth about
        authorization sitting next to the one that is enforced.
      */}
      {identity.data && (
        <span
          className="text-[11px] text-graphite-500"
          data-testid="shell-identity"
          data-ceiling={ceiling}
        >
          {roleNames || t('roles.none')} · {formatActionLevel(ceiling)}
        </span>
      )}
      {identity.error && !tokenConfigured && (
        <span className="text-[11px] text-warning" data-testid="roles-not-read">
          {t('roles.notRead')}
        </span>
      )}
      <LocaleSwitch />
    </div>
  )
}

function HealthBadge() {
  const { t } = useI18n()
  const health = useQuery({
    queryKey: ['health-ready'],
    queryFn: ({ signal }) => drillingApi.healthReady(signal),
    retry: false,
    refetchInterval: 60_000,
  })
  if (health.isLoading || isAbortError(health.error)) return <Badge tone="neutral">{t('app.healthChecking')}</Badge>
  if (health.error) {
    // The badge says which failure it is. Calling every one of them "unreachable" sends an operator to
    // check connectivity for a server fault or a deadline, which is the wrong place to look.
    const kind = health.error instanceof ApiError ? health.error.kind : null
    const label =
      kind === 'network'
        ? t('app.healthUnreachable')
        : kind === 'timeout'
          ? t('app.healthNotAnswering')
          : t('app.healthError')
    return (
      <Badge tone="danger" data-testid="shell-health">
        {label}
      </Badge>
    )
  }
  const status = String((health.data as Record<string, unknown>)?.status ?? 'ready')
  return (
    <Badge tone={status === 'ready' ? 'ok' : 'warning'} data-testid="shell-health">
      {status}
    </Badge>
  )
}

export function AppShell() {
  const { t } = useI18n()
  const params = useParams<{ wellId?: string }>()
  const wellId = params.wellId
  const well = useQuery({
    queryKey: ['well', wellId],
    queryFn: ({ signal }) => drillingApi.getWell(wellId as string, signal),
    enabled: Boolean(wellId),
    retry: false,
  })

  const visible = NAV_ITEMS.filter((item) => item.scope === 'global' || Boolean(wellId))

  return (
    <div className="flex min-h-screen bg-graphite-50 text-graphite-900 dark:bg-graphite-950 dark:text-graphite-100">
      <aside className="hidden w-60 shrink-0 border-e border-graphite-200 bg-white lg:block dark:border-graphite-800 dark:bg-graphite-900">
        <div className="border-b border-graphite-200 px-4 py-3 dark:border-graphite-800">
          <p className="text-sm font-semibold tracking-tight">{t('app.name')}</p>
          <p className="text-[11px] text-graphite-500">{t('app.tagline')}</p>
        </div>
        <nav className="p-2">
          <ul className="space-y-0.5">
            {visible.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to.replace(':wellId', wellId ?? '')}
                  className={({ isActive }) =>
                    clsx(
                      'block rounded px-2.5 py-1.5 text-sm',
                      isActive
                        ? 'bg-signal/10 font-medium text-signal-deep dark:text-signal-light'
                        : 'text-graphite-600 hover:bg-graphite-100 dark:text-graphite-300 dark:hover:bg-graphite-800',
                    )
                  }
                >
                  {t(item.labelKey)}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-graphite-200 bg-white px-4 py-2.5 dark:border-graphite-800 dark:bg-graphite-900">
          <div className="min-w-0">
            {/*
              Three states, not two. "No well is selected" and "the selected well could not be read"
              are different facts, and this header used to render both as the application name — so a
              cockpit whose well read failed looked like a page with no well at all, while the URL and
              every panel below were unmistakably scoped to one. The id is known from the route, so it
              is shown; nothing about the well is invented.
            */}
            <p className="truncate text-sm font-semibold" data-testid="shell-well-name">
              {well.data ? well.data.name : wellId ? null : t('app.name')}
              {wellId && !well.data && (
                <span className="font-mono text-xs font-normal">{wellId}</span>
              )}
            </p>
            <p className="truncate text-[11px] text-graphite-500" data-testid="shell-well-detail">
              {well.data
                ? `${well.data.well_type} · ${well.data.status}${well.data.operator ? ` · ${well.data.operator}` : ''}`
                : wellId
                  ? well.error
                    ? t('app.wellNotRead')
                    : t('app.tagline')
                  : t('app.tagline')}
            </p>
          </div>
          <div className="flex items-center gap-3">
            <HealthBadge />
            <IdentityControls />
          </div>
        </header>

        <main className="min-w-0 flex-1 p-4">
          <Outlet />
        </main>

        <footer className="border-t border-graphite-200 px-4 py-2 text-[11px] text-graphite-500 dark:border-graphite-800">
          {t('roles.note')}
        </footer>
      </div>
    </div>
  )
}

export function PageHeader({
  title,
  description,
  actions,
  breadcrumb,
}: {
  title: string
  description?: string
  actions?: ReactNode
  breadcrumb?: ReactNode
}) {
  return (
    <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        {breadcrumb && <div className="mb-1 text-[11px] text-graphite-500">{breadcrumb}</div>}
        <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-0.5 max-w-3xl text-xs text-graphite-500">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}
