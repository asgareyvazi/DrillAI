/**
 * Shared presentation primitives.
 *
 * These exist so that every page renders the *same* set of states — loading, empty, unauthorized,
 * not-found, validation error, backend error, and "value missing for a stated reason". Product
 * rule §47 says each page must handle all of them; the cheapest way to make that true is to make
 * the correct rendering the easy one.
 */

import clsx from 'clsx'
import { useEffect, useId, useRef, type ReactNode } from 'react'
import { ApiError, canRetry, isAbortError } from '../../api/client'
import { useI18n } from '../../i18n'
import { formatValue } from '../../lib/format'

// --------------------------------------------------------------------------- layout blocks

export function Card({
  title,
  subtitle,
  actions,
  children,
  className,
  dense,
  ...rest
}: {
  title?: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
  dense?: boolean
} & Omit<React.ComponentPropsWithoutRef<'section'>, 'children' | 'title' | 'className'>) {
  return (
    <section
      className={clsx(
        'rounded-lg border border-graphite-200 bg-white shadow-sm dark:border-graphite-800 dark:bg-graphite-900',
        className,
      )}
      {...rest}
    >
      {(title || actions) && (
        <header className="flex items-start justify-between gap-3 border-b border-graphite-100 px-4 py-3 dark:border-graphite-800">
          <div className="min-w-0">
            {title && <h2 className="truncate text-sm font-semibold tracking-tight">{title}</h2>}
            {subtitle && (
              <p className="mt-0.5 text-xs text-graphite-500 dark:text-graphite-400">{subtitle}</p>
            )}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={dense ? 'p-2' : 'p-4'}>{children}</div>
    </section>
  )
}

export function Button({
  children,
  variant = 'default',
  size = 'md',
  type = 'button',
  disabled,
  onClick,
  title,
  ...rest
}: {
  children: ReactNode
  variant?: 'default' | 'primary' | 'ghost' | 'danger'
  size?: 'sm' | 'md'
  type?: 'button' | 'submit'
  disabled?: boolean
  onClick?: () => void
  title?: string
} & Omit<React.ComponentPropsWithoutRef<'button'>, 'children' | 'title' | 'className' | 'type' | 'disabled' | 'onClick'>) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={clsx(
        'inline-flex items-center justify-center gap-1.5 rounded-md border font-medium transition',
        'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal',
        'disabled:cursor-not-allowed disabled:opacity-50',
        size === 'sm' ? 'px-2 py-1 text-xs' : 'px-3 py-1.5 text-sm',
        variant === 'primary' &&
          'border-signal-deep bg-signal text-white hover:bg-signal-deep dark:border-signal-light',
        variant === 'default' &&
          'border-graphite-300 bg-white text-graphite-800 hover:bg-graphite-50 dark:border-graphite-700 dark:bg-graphite-900 dark:text-graphite-100 dark:hover:bg-graphite-800',
        variant === 'ghost' &&
          'border-transparent bg-transparent text-graphite-600 hover:bg-graphite-100 dark:text-graphite-300 dark:hover:bg-graphite-800',
        variant === 'danger' && 'border-danger bg-danger text-white hover:opacity-90',
      )}
      {...rest}
    >
      {children}
    </button>
  )
}

export function Badge({
  children,
  tone = 'neutral',
  title,
  ...rest
}: {
  children: ReactNode
  tone?: 'neutral' | 'ok' | 'warning' | 'danger' | 'info'
  title?: string
} & Omit<React.ComponentPropsWithoutRef<'span'>, 'children' | 'title' | 'className'>) {
  const tones: Record<string, string> = {
    neutral: 'bg-graphite-100 text-graphite-700 dark:bg-graphite-800 dark:text-graphite-200',
    ok: 'bg-emerald-50 text-ok dark:bg-emerald-950 dark:text-emerald-300',
    warning: 'bg-amber-50 text-warning dark:bg-amber-950 dark:text-amber-300',
    danger: 'bg-red-50 text-danger dark:bg-red-950 dark:text-red-300',
    info: 'bg-sky-50 text-signal-deep dark:bg-sky-950 dark:text-sky-300',
  }
  return (
    <span
      title={title}
      className={clsx(
        'inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium whitespace-nowrap',
        tones[tone],
      )}
      {...rest}
    >
      {children}
    </span>
  )
}

// --------------------------------------------------------------------------- states

export function Loading({ label }: { label?: string }) {
  const { t } = useI18n()
  return (
    <div role="status" aria-live="polite" className="flex items-center gap-2 p-4 text-sm text-graphite-500">
      <span
        aria-hidden
        className="inline-block size-3 animate-spin rounded-full border-2 border-graphite-300 border-t-signal"
      />
      {label ?? t('common.loading')}
    </div>
  )
}

export function EmptyState({ message, hint }: { message?: string; hint?: string }) {
  const { t } = useI18n()
  return (
    <div className="rounded-md border border-dashed border-graphite-300 p-6 text-center dark:border-graphite-700">
      <p className="text-sm text-graphite-600 dark:text-graphite-300">{message ?? t('common.empty')}</p>
      {hint && <p className="mt-1 text-xs text-graphite-500">{hint}</p>}
    </div>
  )
}

/** The headline for each failure class. One mapping, so no page invents its own wording. */
const ERROR_MESSAGE_KEYS = {
  invalid_request: 'errors.invalidRequest',
  unauthenticated: 'errors.notAuthenticated',
  forbidden: 'errors.forbidden',
  not_found: 'errors.notFound',
  conflict: 'errors.conflict',
  validation: 'errors.validation',
  server: 'errors.server',
  network: 'errors.network',
  timeout: 'errors.timeout',
  malformed: 'errors.malformed',
  cancelled: 'errors.cancelled',
  unknown: 'errors.generic',
} as const

/** What the operator can do about it, when there is something to say. */
const ERROR_HINT_KEYS: Partial<Record<keyof typeof ERROR_MESSAGE_KEYS, string>> = {
  invalid_request: 'errors.hintInvalidRequest',
  unauthenticated: 'errors.hintNotAuthenticated',
  forbidden: 'errors.hintForbidden',
  not_found: 'errors.hintNotFound',
  conflict: 'errors.hintConflict',
  server: 'errors.hintServer',
  timeout: 'errors.hintTimeout',
  malformed: 'errors.hintMalformed',
}

/** Longest detail payload rendered, so an error page can never become a wall of JSON. */
const DETAIL_LIMIT = 800

/**
 * The one place a failure is rendered.
 *
 * Which failure it is decides both the wording and whether a retry is offered: a retry button on a
 * problem that cannot be fixed by asking again ("this identity may not read this run") is a lie told
 * in the shape of a control. A cancelled request renders nothing at all — it is not a failure.
 */
export function ErrorState({
  error,
  onRetry,
  showCancelled = false,
}: {
  error: unknown
  onRetry?: () => void
  /** Render an explicitly-cancelled request. Off by default: cancellation is not an error. */
  showCancelled?: boolean
}) {
  const { t } = useI18n()
  const apiError = error instanceof ApiError ? error : null
  const kind = apiError?.kind

  // A cancelled request is not a failure of anything, so there is nothing to render. The raw abort is
  // checked as well as the classified kind, because a cancellation can reach a query's error state
  // from a path this client did not create (a controller passed in by a caller, a fetch rejected by
  // the browser) — and an abort rendered as "Something went wrong" is exactly the false alarm the
  // classification exists to prevent.
  if (isAbortError(error)) return null
  if (kind === 'cancelled') return showCancelled ? <EmptyState message={t('errors.cancelled')} /> : null

  /*
   * A conflict and a missing approval share the HTTP shape (409) and nothing else. "The server
   * refused this because of its current state — reload to see the current state" is the right offer
   * for a conflict: the screen is out of date and reading it again is the fix. For an action above
   * the identity's ceiling the screen is not out of date at all — the server is waiting for a
   * recorded approval, and reloading will change nothing. Offering it would send an operator to do
   * something that cannot help, which is exactly what the recovery labels exist to prevent.
   */
  const approvalRequired = apiError?.isApprovalRequired === true

  const message = apiError
    ? t(
        (approvalRequired ? 'errors.approvalRequired' : ERROR_MESSAGE_KEYS[apiError.kind]) as 'errors.generic',
      )
    : error instanceof Error
      ? error.message
      : t('errors.generic')
  const hintKey = apiError
    ? approvalRequired
      ? 'errors.hintApprovalRequired'
      : ERROR_HINT_KEYS[apiError.kind]
    : undefined
  // The server's own sentence, when it wrote one. "missing permission 'workflow.read'" is worth more
  // than any generic phrasing this page could invent, and it is what makes a 403 actionable.
  const explanation = apiError?.messageFromServer ? apiError.message : null
  const detail = apiError && Object.keys(apiError.details).length > 0 ? apiError.details : null
  const detailText = detail ? JSON.stringify(detail, null, 2) : null
  const rendered = detailText && detailText.length > DETAIL_LIMIT ? `${detailText.slice(0, DETAIL_LIMIT)}…` : detailText
  // Two different offers. Retrying is for failures that might not repeat (a deadline, an outage, a
  // server fault). A conflict is not one of those — the same request will be refused again — but the
  // screen *is* out of date, so the offer is to read the current state, and only when the caller has
  // something to re-read.
  const retryable = onRetry !== undefined && canRetry(error)
  const reconcilable = onRetry !== undefined && !retryable && kind === 'conflict' && !approvalRequired

  return (
    <div
      role="alert"
      data-testid="error-state"
      data-error-kind={apiError?.kind ?? 'unknown'}
      data-http-status={apiError?.status ?? undefined}
      className="rounded-md border border-danger/40 bg-red-50/60 p-4 dark:bg-red-950/30"
    >
      <p className="text-sm font-medium text-danger">{message}</p>
      {explanation && <p className="mt-1 text-xs text-graphite-700 dark:text-graphite-200">{explanation}</p>}
      {hintKey && <p className="mt-1 text-xs text-graphite-500">{t(hintKey as 'errors.generic')}</p>}
      {apiError && (
        <p className="mt-1 font-mono text-[11px] text-graphite-500" data-testid="error-request-id">
          {apiError.code}
          {apiError.requestId ? ` · request ${apiError.requestId}` : ''}
        </p>
      )}
      {rendered && (
        <pre className="mt-2 max-h-40 overflow-auto rounded bg-white/70 p-2 text-[11px] dark:bg-graphite-950/50">
          {rendered}
        </pre>
      )}
      {retryable && (
        <div className="mt-3">
          <Button size="sm" onClick={onRetry}>
            {t('common.retry')}
          </Button>
        </div>
      )}
      {reconcilable && (
        <div className="mt-3">
          <Button size="sm" data-testid="error-reconcile" onClick={onRetry}>
            {t('errors.reloadState')}
          </Button>
        </div>
      )}
    </div>
  )
}

/** Renders whichever of loading/error/empty/data applies, so pages cannot forget a state. */
export function Async<T>({
  query,
  empty,
  children,
}: {
  query: { isLoading: boolean; error: unknown; data: T | undefined; refetch?: () => void }
  empty?: (data: T) => ReactNode
  children: (data: T) => ReactNode
}) {
  if (query.isLoading) return <Loading />
  if (query.error) return <ErrorState error={query.error} onRetry={query.refetch} />
  if (query.data === undefined) return <Loading />
  const isEmpty = empty?.(query.data)
  if (isEmpty !== undefined && isEmpty !== null && isEmpty !== false) return <>{isEmpty}</>
  return <>{children(query.data)}</>
}

// --------------------------------------------------------------------------- data display

/**
 * A labelled engineering value.
 *
 * The value is rendered through `formatValue`, which distinguishes "zero" from "no value". A
 * missing measurement shows the reason the backend gave, never a zero.
 */
export function Value({
  label,
  value,
  unit,
  source,
  quality,
  unitSystem,
  reason,
  evidence,
  onEvidence,
}: {
  label: ReactNode
  value: unknown
  unit?: string | null
  source?: string | null
  quality?: string | null
  unitSystem?: 'si' | 'oilfield'
  /** Why there is no value, when the backend says so. */
  reason?: string | null
  evidence?: ReactNode
  onEvidence?: () => void
}) {
  const { locale, t } = useI18n()
  const rendered = formatValue(value, { unit, unitSystem, locale })
  return (
    <div className="rounded-md border border-graphite-100 bg-graphite-50/40 p-2.5 dark:border-graphite-800 dark:bg-graphite-900/60">
      <dt className="text-[11px] font-medium tracking-wide text-graphite-500 uppercase dark:text-graphite-400">
        {label}
      </dt>
      <dd className="mt-1 flex items-baseline gap-1.5">
        <span
          className={clsx(
            'font-mono text-lg leading-none',
            rendered.missing ? 'text-graphite-400' : 'text-graphite-900 dark:text-graphite-50',
          )}
          title={rendered.title}
        >
          {rendered.text}
        </span>
        {rendered.unit && <span className="text-xs text-graphite-500">{rendered.unit}</span>}
      </dd>
      {rendered.missing && reason && (
        <p className="mt-1 text-[11px] text-graphite-500">
          {t('common.detail')}: {reason}
        </p>
      )}
      {(source || quality || onEvidence) && (
        <div className="mt-1 flex items-center gap-1.5">
          {source && (
            <span className="truncate font-mono text-[10px] text-graphite-400" title={source}>
              {source}
            </span>
          )}
          {quality && <Badge tone="neutral">{quality}</Badge>}
          {evidence}
          {onEvidence && (
            <button
              type="button"
              onClick={onEvidence}
              className="text-[11px] text-signal-deep underline-offset-2 hover:underline dark:text-signal-light"
            >
              {t('common.whyQuestion')}
            </button>
          )}
        </div>
      )}
    </div>
  )
}

export function ProgressBar({
  percent,
  label,
  basis,
}: {
  percent: number | null
  label?: ReactNode
  basis?: string | null
}) {
  const { locale } = useI18n()
  /*
   * The bar is named by the label that is drawn above it, through `aria-labelledby` rather than a
   * copied `aria-label`: the label is a node, not a string, and a second copy of it would be a second
   * thing to keep in step. A progress bar a screen reader reports as "progress bar 70%" has not said
   * what is 70% complete.
   */
  const labelId = `${useId().replace(/[^a-zA-Z0-9_-]/g, '')}-progress-label`
  if (percent === null) {
    return (
      <div className="text-xs text-graphite-500">
        {label} — <span className="italic">not computable: no planned depth is on file</span>
      </div>
    )
  }
  const clamped = Math.max(0, Math.min(100, percent))
  return (
    <div>
      <div className="flex items-baseline justify-between text-xs">
        <span id={labelId} className="text-graphite-600 dark:text-graphite-300">
          {label}
        </span>
        <span className="font-mono">
          {new Intl.NumberFormat(locale === 'fa' ? 'fa-IR' : 'en-US', { maximumFractionDigits: 1 }).format(clamped)}%
        </span>
      </div>
      <div
        role="progressbar"
        aria-labelledby={labelId}
        aria-valuenow={Math.round(clamped)}
        aria-valuemin={0}
        aria-valuemax={100}
        className="mt-1 h-2 overflow-hidden rounded-full bg-graphite-200 dark:bg-graphite-800"
      >
        <div className="h-full rounded-full bg-signal" style={{ width: `${clamped}%` }} />
      </div>
      {basis && <p className="mt-1 text-[11px] text-graphite-500">basis: {basis}</p>}
    </div>
  )
}

/** Simple horizontal bar chart used for Pareto and category shares. No chart library needed. */
export function BarList({
  rows,
  onSelect,
}: {
  rows: Array<{ key: string; label: string; value: number; display?: string; tone?: string }>
  onSelect?: (key: string) => void
}) {
  const max = rows.reduce((acc, row) => Math.max(acc, Math.abs(row.value)), 0) || 1
  return (
    <ul className="space-y-1.5">
      {rows.map((row) => (
        <li key={row.key}>
          <button
            type="button"
            onClick={onSelect ? () => onSelect(row.key) : undefined}
            className={clsx(
              'w-full rounded px-1.5 py-1 text-start',
              onSelect && 'hover:bg-graphite-50 dark:hover:bg-graphite-800',
            )}
          >
            <div className="flex items-baseline justify-between gap-2 text-xs">
              <span className="truncate">{row.label}</span>
              <span className="font-mono text-graphite-600 dark:text-graphite-300">
                {row.display ?? row.value}
              </span>
            </div>
            <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-graphite-100 dark:bg-graphite-800">
              <div
                className={clsx('h-full rounded-full', row.tone ?? 'bg-signal')}
                style={{ width: `${(Math.abs(row.value) / max) * 100}%` }}
              />
            </div>
          </button>
        </li>
      ))}
    </ul>
  )
}

export function Table<T>({
  rows,
  columns,
  rowKey,
  empty,
  onRowClick,
  isRowActive,
}: {
  rows: T[]
  columns: Array<{ key: string; header: ReactNode; render: (row: T) => ReactNode; align?: 'start' | 'end' }>
  rowKey: (row: T, index: number) => string
  empty?: ReactNode
  onRowClick?: (row: T) => void
  /** Marks the row the page is showing in detail. Announced as `aria-current`, not colour alone. */
  isRowActive?: (row: T) => boolean
}) {
  if (rows.length === 0) return <>{empty ?? <EmptyState />}</>
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-graphite-200 text-start dark:border-graphite-700">
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={clsx(
                  'px-2 py-1.5 text-[11px] font-semibold tracking-wide text-graphite-500 uppercase',
                  column.align === 'end' ? 'text-end' : 'text-start',
                )}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr
              key={rowKey(row, index)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              /*
               * A row a user can click is a control, and a control that can only be reached with a
               * mouse is not reachable. The keyboard contract is the one every button has: the row
               * takes focus, `Enter` activates it, and `Space` activates it without scrolling the
               * page out from under the reader. The activation is the *same* handler the click uses,
               * so the two paths cannot diverge.
               *
               * `aria-current` states which record is open for assistive technology the same way it
               * states it visually — colour alone would carry it for sighted users only.
               */
              tabIndex={onRowClick ? 0 : undefined}
              onKeyDown={
                onRowClick
                  ? (event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault()
                        onRowClick(row)
                      }
                    }
                  : undefined
              }
              aria-current={isRowActive?.(row) ? 'true' : undefined}
              className={clsx(
                'border-b border-graphite-100 last:border-0 dark:border-graphite-800',
                onRowClick && 'cursor-pointer hover:bg-graphite-50 dark:hover:bg-graphite-800/60',
                isRowActive?.(row) && 'bg-graphite-50 dark:bg-graphite-800/60',
              )}
            >
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={clsx('px-2 py-1.5 align-top', column.align === 'end' && 'text-end font-mono')}
                >
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function Tabs({
  tabs,
  active,
  onChange,
  children,
}: {
  tabs: Array<{ key: string; label: ReactNode; badge?: ReactNode }>
  active: string
  onChange: (key: string) => void
  /**
   * The content of the tabs, passed as children so the group owns the panel.
   *
   * A tab that says `aria-controls="…-panel-catalogue"` is only telling the truth if an element with
   * that id exists; rendering the panel here is what guarantees the two halves of the relationship
   * are always in step, whichever tab happens to be selected. Pages keep their own conditional
   * rendering inside (`{tab === 'x' && …}`), so nothing about how a tab decides what to show changes.
   */
  children?: ReactNode
}) {
  const { direction } = useI18n()
  /*
   * `useId()` returns a value containing colons (`:r1:`), which is a legal HTML id and an illegal CSS
   * identifier — `document.querySelector('#:r1:-panel-x')` throws. An id that cannot be looked up is
   * an id a test, a style rule or a `document.getElementById` in a support session cannot use, so the
   * punctuation is removed once here.
   */
  const id = `tabs-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`
  const refs = useRef<Array<HTMLButtonElement | null>>([])
  const activeIndex = Math.max(
    0,
    tabs.findIndex((tab) => tab.key === active),
  )

  /**
   * Keyboard navigation, as the tab pattern defines it.
   *
   * Which arrow means "next" is a question about the *reading direction*, not about the keyboard: in
   * a right-to-left page the tab after this one is drawn to the left, so `ArrowLeft` moves forward.
   * Answering with the physical key would make the control move the wrong way for every Persian user.
   * `ArrowRight` therefore means "previous" when the document is RTL — the browser reports the
   * direction the page is actually rendered in, and this reads it.
   */
  const step = (from: number, delta: number) => {
    if (tabs.length === 0) return
    const next = (from + delta + tabs.length) % tabs.length
    const target = tabs[next]
    if (!target) return
    onChange(target.key)
    refs.current[next]?.focus()
  }

  const onKeyDown = (event: React.KeyboardEvent, index: number) => {
    const forward = direction === 'rtl' ? 'ArrowLeft' : 'ArrowRight'
    const backward = direction === 'rtl' ? 'ArrowRight' : 'ArrowLeft'
    if (event.key === forward) {
      event.preventDefault()
      step(index, 1)
    } else if (event.key === backward) {
      event.preventDefault()
      step(index, -1)
    } else if (event.key === 'Home') {
      event.preventDefault()
      step(0, 0)
    } else if (event.key === 'End') {
      event.preventDefault()
      step(tabs.length - 1, 0)
    }
  }

  return (
    <>
      <div
        role="tablist"
        className="flex flex-wrap gap-1 border-b border-graphite-200 pb-1 dark:border-graphite-800"
      >
        {tabs.map((tab, index) => (
          <button
            key={tab.key}
            ref={(node) => {
              refs.current[index] = node
            }}
            id={`${id}-tab-${tab.key}`}
            role="tab"
            aria-selected={active === tab.key}
            /*
             * Only the selected tab is in the tab order, which is what the pattern asks for: the group
             * is entered once with `Tab`, and moved through with the arrow keys. Every tab also names
             * the panel it controls, so a screen reader can announce the relationship and jump to it.
             */
            aria-controls={`${id}-panel-${tab.key}`}
            tabIndex={index === activeIndex ? 0 : -1}
            onKeyDown={(event) => onKeyDown(event, index)}
            onClick={() => onChange(tab.key)}
            className={clsx(
              'inline-flex items-center gap-1.5 rounded-t px-3 py-1.5 text-sm transition',
              active === tab.key
                ? 'border-b-2 border-signal font-semibold text-signal-deep dark:text-signal-light'
                : 'text-graphite-600 hover:bg-graphite-50 dark:text-graphite-300 dark:hover:bg-graphite-800',
            )}
        >
              {tab.label}
              {tab.badge}
            </button>
          ))}
      </div>
      {children !== undefined && (
        /*
         * The panel the selected tab controls: it names the tab that labels it, and it is focusable
         * for the case a panel holds no control of its own — a panel a keyboard user cannot enter is
         * content a keyboard user cannot reach.
         */
        <div
          role="tabpanel"
          id={`${id}-panel-${active}`}
          aria-labelledby={`${id}-tab-${active}`}
          tabIndex={0}
          className="focus-visible:outline focus-visible:outline-2 focus-visible:outline-signal"
        >
          {children}
        </div>
      )}
    </>
  )
}

/**
 * A panel that takes over the screen until it is dismissed.
 *
 * `role="dialog"` and `aria-modal="true"` are a *claim*: they tell assistive technology that the rest
 * of the page is inert. A claim like that has to be true, and it is only true if the keyboard follows
 * it — so the drawer does the three things that make it so, and does them in this order:
 *
 * 1. **Move focus in.** On open, focus goes to the first thing worth focusing inside the panel: the
 *    first control it contains, or the panel itself when it has none. A dialog that opens while focus
 *    stays on the page behind it is a dialog a keyboard user has to hunt for.
 * 2. **Keep focus in.** `Tab` and `Shift+Tab` cycle within the panel rather than walking out into the
 *    page that the dialog has just declared inert. The focusable set is read from the document at the
 *    moment of the key press (not from a list captured at open time), so the panel's own contents —
 *    a retry button that appears, a filter that disappears — stay reachable.
 * 3. **Hand focus back.** On close, focus returns to whatever opened it, which is where the reader
 *    was: dropping focus onto the document body sends a keyboard user back to the top of the page.
 *
 * `Escape` closes. The backdrop is a real, labelled button so that a pointer can dismiss the panel by
 * clicking outside it; it is deliberately outside the focus trap, because a keyboard user already has
 * the close button and `Escape`, and tabbing onto a full-screen "Close" would be a trap of its own.
 */
export function Drawer({
  open,
  title,
  onClose,
  children,
  wide,
}: {
  open: boolean
  title: ReactNode
  onClose: () => void
  children: ReactNode
  wide?: boolean
}) {
  const { t } = useI18n()
  const labelId = useId()
  const panelRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    // Where focus was before the panel opened, so it can be handed back on close.
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const panel = panelRef.current

    /*
     * The panel's focusable controls, as they are *now*.
     *
     * Visibility is asked of the element itself (`checkVisibility`) rather than inferred from
     * `offsetParent`: `offsetParent` is a layout property, and a dialog's behaviour must not depend on
     * whether the environment has laid the page out — a trap that finds nothing because the test
     * renderer has no layout would silently move focus to the panel instead of to its first control.
     */
    const isVisible = (element: HTMLElement): boolean => {
      if (typeof element.checkVisibility === 'function') return element.checkVisibility()
      // A laid-out element occupies space; a `display: none` one does not. When the environment has no
      // layout at all (a component test), the question cannot be answered, and the element is taken at
      // face value — silently dropping it would move focus to the panel instead of its first control.
      if (element.getClientRects().length > 0) return true
      return element.hidden !== true && element.style.display !== 'none'
    }
    const focusable = (): HTMLElement[] => {
      if (!panel) return []
      const selector =
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      return Array.from(panel.querySelectorAll<HTMLElement>(selector)).filter(isVisible)
    }

    // 1. Focus moves in: the first control, or the panel itself when there is none.
    const first = focusable()[0]
    ;(first ?? panel)?.focus()

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const controls = focusable()
      if (controls.length === 0) {
        // Nothing to cycle through: keep the panel itself focused rather than letting Tab escape.
        event.preventDefault()
        panel?.focus()
        return
      }
      const firstControl = controls[0] as HTMLElement
      const lastControl = controls[controls.length - 1] as HTMLElement
      const active = document.activeElement
      if (event.shiftKey && (active === firstControl || active === panel)) {
        event.preventDefault()
        lastControl.focus()
      } else if (!event.shiftKey && active === lastControl) {
        event.preventDefault()
        firstControl.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      // 3. Focus goes back to the control that opened the panel.
      if (opener && document.contains(opener)) opener.focus()
    }
  }, [open, onClose])

  if (!open) return null
  return (
    <div className="fixed inset-0 z-40 flex" role="dialog" aria-modal="true" aria-labelledby={labelId}>
      <button
        type="button"
        aria-label={t('common.close')}
        onClick={onClose}
        className="flex-1 bg-graphite-950/40"
      />
      <aside
        ref={panelRef}
        tabIndex={-1}
        className={clsx(
          'flex h-full flex-col overflow-hidden border-s border-graphite-200 bg-white shadow-xl dark:border-graphite-800 dark:bg-graphite-950',
          wide ? 'w-full max-w-2xl' : 'w-full max-w-md',
        )}
      >
        <header className="flex items-center justify-between border-b border-graphite-200 px-4 py-3 dark:border-graphite-800">
          <h2 id={labelId} className="text-sm font-semibold">
            {title}
          </h2>
          <Button variant="ghost" size="sm" onClick={onClose}>
            {t('common.close')}
          </Button>
        </header>
        <div className="flex-1 overflow-auto p-4">{children}</div>
      </aside>
    </div>
  )
}

export function Field({
  label,
  hint,
  children,
}: {
  label: ReactNode
  hint?: ReactNode
  children: ReactNode
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-graphite-600 dark:text-graphite-300">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[11px] text-graphite-500">{hint}</span>}
    </label>
  )
}

export function Json({ value, max = 400 }: { value: unknown; max?: number }) {
  const text = JSON.stringify(value, null, 2) ?? 'null'
  const shown = text.length > max * 10 ? `${text.slice(0, max * 10)}\n…` : text
  return (
    /*
     * A JSON body is punctuation and structure: `{`, `"key":`, `[`, `]`. In a right-to-left document
     * those characters are neutral and get placed by the paragraph's direction, which turns nested
     * braces inside out. The payload is not prose, so it states its own direction.
     */
    <pre
      dir="ltr"
      className="max-h-72 overflow-auto rounded bg-graphite-50 p-2 text-[11px] leading-snug dark:bg-graphite-950"
    >
      {shown}
    </pre>
  )
}

/** NPT / priority tone helper shared by the NPT panels so colour means the same thing everywhere. */
export function toneForSeverity(severity: string | null | undefined): 'ok' | 'warning' | 'danger' | 'neutral' {
  switch ((severity ?? '').toLowerCase()) {
    case 'high':
    case 'critical':
    case 'severe':
      return 'danger'
    case 'medium':
    case 'moderate':
      return 'warning'
    case 'low':
      return 'ok'
    default:
      return 'neutral'
  }
}
