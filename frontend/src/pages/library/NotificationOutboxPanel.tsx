import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { drillingApi } from '../../api/endpoints'
import { Badge, Button, Card, EmptyState, ErrorState, Loading, Table } from '../../components/common'
import { useI18n } from '../../i18n'
import { formatDateTime } from '../../lib/format'
import type { NotificationSummary } from '../../api/types'

const MANUAL_REQUEUE_STATES = new Set<NotificationSummary['status']>([
  'dead_letter',
  'failed_terminal',
  'reconciliation_required',
])

function statusTone(status: NotificationSummary['status']): 'neutral' | 'ok' | 'warning' | 'danger' {
  if (status === 'accepted') return 'ok'
  if (status === 'dead_letter' || status === 'failed_terminal') return 'danger'
  if (status === 'reconciliation_required' || status === 'failed_retryable') return 'warning'
  return 'neutral'
}

export default function NotificationOutboxPanel() {
  const { t, locale } = useI18n()
  const queryClient = useQueryClient()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  const [duplicateRiskAcknowledged, setDuplicateRiskAcknowledged] = useState(false)

  const outbox = useQuery({
    queryKey: ['platform-notifications'],
    queryFn: ({ signal }) => drillingApi.notificationOutbox(signal),
    refetchInterval: 15_000,
  })
  const identity = useQuery({
    queryKey: ['platform-identity'],
    queryFn: ({ signal }) => drillingApi.identity(signal),
  })
  const permissions = identity.data?.permissions ?? []
  const hasManagePermission =
    permissions.includes('*') ||
    permissions.includes('integration.manage') ||
    permissions.includes('integration.*')
  const actionLevel = Number(identity.data?.max_action_level?.slice(1) ?? 0)
  const canManuallyRequeue = hasManagePermission && actionLevel >= 4

  const retry = useMutation({
    mutationFn: ({ messageId, auditReason }: { messageId: string; auditReason: string }) =>
      drillingApi.retryNotification(messageId, {
        reason: auditReason,
        acknowledge_duplicate_risk: true,
      }),
    onSuccess: async () => {
      setSelectedId(null)
      setReason('')
      setDuplicateRiskAcknowledged(false)
      await queryClient.invalidateQueries({ queryKey: ['platform-notifications'] })
    },
  })

  const selectForRetry = (messageId: string) => {
    setSelectedId(messageId)
    setReason('')
    setDuplicateRiskAcknowledged(false)
    retry.reset()
  }

  const selected = outbox.data?.items.find((item) => item.id === selectedId)

  return (
    <Card
      title={t('platform.notificationOutbox')}
      subtitle={t('platform.notificationOutboxSubtitle')}
      actions={
        <Button size="sm" onClick={() => void outbox.refetch()} disabled={outbox.isFetching}>
          {t('common.refresh')}
        </Button>
      }
    >
      <div className="mb-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
        <p className="font-semibold">{t('platform.notificationAcceptanceBoundary')}</p>
        <p className="mt-1">{t('platform.notificationReceiptUnavailable')}</p>
      </div>

      {outbox.isPending ? (
        <Loading />
      ) : outbox.error ? (
        <ErrorState error={outbox.error} onRetry={() => void outbox.refetch()} />
      ) : (
        <>
          <Table
            rows={outbox.data?.items ?? []}
            rowKey={(row) => row.id}
            empty={<EmptyState message={t('platform.notificationOutboxEmpty')} />}
            columns={[
              {
                key: 'id',
                header: t('platform.notificationId'),
                render: (row) => <code className="text-xs" dir="ltr">{row.id}</code>,
              },
              {
                key: 'channel',
                header: t('platform.notificationChannel'),
                render: (row) => <span dir="ltr">{row.channel}</span>,
              },
              {
                key: 'status',
                header: t('common.status'),
                render: (row) => (
                  <Badge tone={statusTone(row.status)}>
                    {t(`platform.notificationStatus.${row.status}`)}
                  </Badge>
                ),
              },
              {
                key: 'attempts',
                header: t('platform.notificationAttempts'),
                align: 'end',
                render: (row) => `${row.attempts}/${row.max_attempts}`,
              },
              {
                key: 'next_attempt_at',
                header: t('platform.notificationNextAttempt'),
                render: (row) => <span dir="ltr">{formatDateTime(row.next_attempt_at, locale)}</span>,
              },
              {
                key: 'accepted_at',
                header: t('platform.notificationAcceptedAt'),
                render: (row) => (
                  <span dir="ltr">{formatDateTime(row.accepted_at, locale)}</span>
                ),
              },
              {
                key: 'acceptance_evidence',
                header: t('platform.notificationAcceptance'),
                render: (row) => row.acceptance_evidence
                  ? t(`platform.notificationEvidence.${row.acceptance_evidence}`)
                  : t('common.unknown'),
              },
              {
                key: 'provider_message_id',
                header: t('platform.notificationProviderId'),
                render: (row) => row.provider_message_id
                  ? <code className="text-xs" dir="ltr">{row.provider_message_id}</code>
                  : '—',
              },
              {
                key: 'last_error_code',
                header: t('platform.notificationLastError'),
                render: (row) => row.last_error_code
                  ? <code className="text-xs" dir="ltr">{row.last_error_code}</code>
                  : '—',
              },
              {
                key: 'retry',
                header: t('common.actions'),
                render: (row) => MANUAL_REQUEUE_STATES.has(row.status) && canManuallyRequeue
                  ? (
                    <Button size="sm" onClick={() => selectForRetry(row.id)}>
                      {t('platform.notificationRetry')}
                    </Button>
                  )
                  : '—',
              },
            ]}
          />

          {selected && (
            <section className="mt-4 space-y-3 rounded-md border border-graphite-200 p-3 dark:border-graphite-700" aria-labelledby="notification-retry-heading">
              <h3 id="notification-retry-heading" className="text-sm font-semibold">
                {t('platform.notificationRetryTitle')}: <code dir="ltr">{selected.id}</code>
              </h3>
              <label className="block text-xs font-medium" htmlFor="notification-retry-reason">
                {t('platform.notificationRetryReason')}
                <textarea
                  id="notification-retry-reason"
                  className="mt-1 block min-h-20 w-full rounded-md border border-graphite-300 bg-white p-2 text-sm dark:border-graphite-700 dark:bg-graphite-950"
                  minLength={10}
                  maxLength={500}
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  required
                />
              </label>
              <label className="flex items-start gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={duplicateRiskAcknowledged}
                  onChange={(event) => setDuplicateRiskAcknowledged(event.target.checked)}
                />
                <span>{t('platform.notificationDuplicateRisk')}</span>
              </label>
              {retry.error && <ErrorState error={retry.error} />}
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="danger"
                  disabled={reason.trim().length < 10 || !duplicateRiskAcknowledged || retry.isPending}
                  onClick={() => retry.mutate({ messageId: selected.id, auditReason: reason.trim() })}
                >
                  {retry.isPending ? t('common.loading') : t('platform.notificationConfirmRetry')}
                </Button>
                <Button variant="ghost" onClick={() => setSelectedId(null)}>
                  {t('common.cancel')}
                </Button>
              </div>
              <p role="status" className="text-xs text-graphite-500">
                {t('platform.notificationRetryAuditNote')}
              </p>
            </section>
          )}
        </>
      )}

      {identity.error && (
        <p role="status" className="mt-2 text-xs text-graphite-500">
          {t('platform.notificationRetryPermissionUnknown')}
        </p>
      )}
      {identity.data && !canManuallyRequeue && (
        <p role="status" className="mt-2 text-xs text-graphite-500">
          {t('platform.notificationRetryPermissionRequired')}
        </p>
      )}
    </Card>
  )
}
