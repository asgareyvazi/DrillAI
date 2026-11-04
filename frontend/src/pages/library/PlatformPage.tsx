/**
 * Platform state.
 *
 * The honest page: what this deployment can do right now, which integrations are configured and
 * which are not, and which model providers exist. A capability that is not configured is shown as
 * not configured with the instruction to enable it — never as a working feature. This is the page
 * that keeps §45 ("no fake completeness") true at the product level.
 */

import { useQuery } from '@tanstack/react-query'
import { drillingApi } from '../../api/endpoints'
import { PageHeader } from '../../components/layout/AppShell'
import { Async, Badge, Card, EmptyState, ErrorState, Json, Table, Value } from '../../components/common'
import { useI18n } from '../../i18n'
import { humanise } from '../../lib/format'
import NotificationOutboxPanel from './NotificationOutboxPanel'

export default function PlatformPage() {
  const { t } = useI18n()
  const capabilities = useQuery({ queryKey: ['capabilities'], queryFn: ({ signal }) => drillingApi.capabilities(signal) })
  const integrations = useQuery({
    queryKey: ['integrations'],
    queryFn: ({ signal }) => drillingApi.integrations(signal),
  })
  const providers = useQuery({ queryKey: ['providers'], queryFn: ({ signal }) => drillingApi.providers(signal) })
  const version = useQuery({ queryKey: ['version'], queryFn: ({ signal }) => drillingApi.version(signal) })
  const health = useQuery({ queryKey: ['health-ready'], queryFn: ({ signal }) => drillingApi.healthReady(signal) })
  const readinessStatus = typeof health.data?.status === 'string' ? health.data.status : null
  const readinessChecks = health.data?.checks && typeof health.data.checks === 'object'
    ? health.data.checks as Record<string, unknown>
    : null
  const ocrCheck = readinessChecks?.ocr && typeof readinessChecks.ocr === 'object'
    ? readinessChecks.ocr as Record<string, unknown>
    : null
  const ocrStatus = typeof ocrCheck?.status === 'string' ? ocrCheck.status : 'disabled'
  const configuredOcrLanguages = Array.isArray(ocrCheck?.configured_languages)
    ? ocrCheck.configured_languages.map(String).join(', ')
    : null

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('platform.title')}
        description="Capabilities, integrations and model providers as this deployment actually resolves them. Nothing here is aspirational."
      />

      <div className="grid gap-3 xl:grid-cols-[2fr_1fr]">
        <Card title={t('platform.capabilities')}>
          <Async query={capabilities}>
            {(data) => (
              <div className="space-y-3">
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  {[
                    ['engines', data.engines.total],
                    ['node types', data.node_types],
                    ['tools', data.tools],
                    ['actions', data.actions],
                    ['context sections', data.context_sections],
                    ['extractors', data.extractors],
                    ['agents', data.agents],
                    ['skills', data.skills],
                  ].map(([label, count]) => (
                    <Value key={String(label)} label={humanise(String(label))} value={count} source="registry" />
                  ))}
                </div>
                <div>
                  <p className="mb-1 text-xs font-medium">Engines by category</p>
                  <div className="flex flex-wrap gap-1">
                    {Object.entries(data.engines.by_category).map(([category, count]) => (
                      <Badge key={category} tone="neutral">
                        {category}: {count}
                      </Badge>
                    ))}
                  </div>
                </div>
                <div>
                  <p className="mb-1 text-xs font-medium">Model task profiles</p>
                  <div className="flex flex-wrap gap-1">
                    {data.task_profiles.map((profile) => (
                      <Badge key={profile} tone="info">
                        {profile}
                      </Badge>
                    ))}
                  </div>
                </div>
                <details>
                  <summary className="cursor-pointer text-xs font-medium">Authorization model</summary>
                  <Json value={data.authorization} max={80} />
                </details>
              </div>
            )}
          </Async>
        </Card>

        <div className="space-y-3">
          <Card title="Deployment" dense>
            <Async query={version}>
              {(data) => <Json value={data} max={40} />}
            </Async>
            <div className="mt-2">
              {health.data ? (
                <Badge tone={readinessStatus === 'ok' ? 'ok' : 'warning'}>
                  {readinessStatus === 'ok' ? t('platform.deploymentReady') : t('platform.deploymentDegraded')}
                </Badge>
              ) : health.error ? (
                <Badge tone="danger">{t('platform.deploymentUnreachable')}</Badge>
              ) : (
                <Badge tone="neutral">{t('common.loading')}</Badge>
              )}
            </div>
            {health.error && <ErrorState error={health.error} />}
            {ocrCheck && (
              <div className="mt-3 space-y-2 rounded-md border border-graphite-200 p-3 text-xs dark:border-graphite-700">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{t('platform.ocrRuntime')}</span>
                  <Badge tone={ocrStatus === 'ready' ? 'ok' : ocrStatus === 'blocked' ? 'warning' : 'neutral'}>
                    {ocrStatus === 'ready'
                      ? t('platform.ocrRuntimeReady')
                      : ocrStatus === 'blocked'
                        ? t('platform.ocrRuntimeBlocked')
                        : t('platform.ocrRuntimeDisabled')}
                  </Badge>
                </div>
                {configuredOcrLanguages && (
                  <p className="text-graphite-600 dark:text-graphite-300">
                    {t('platform.ocrConfiguredLanguages')}: <bdi dir="ltr">{configuredOcrLanguages}</bdi>
                  </p>
                )}
                <p className="text-graphite-600 dark:text-graphite-300">{t('platform.ocrQualityNotEvaluated')}</p>
                {typeof ocrCheck.reason === 'string' && (
                  <p className="text-warning-700 dark:text-warning-300" role="status">{ocrCheck.reason}</p>
                )}
              </div>
            )}
          </Card>

          <Card title={t('platform.providers')} dense>
            <Async query={providers}>
              {(data) => (
                <div className="space-y-2">
                  <p className="text-xs text-graphite-500">
                    configured provider: {data.configured_provider ?? 'none'}
                  </p>
                  <Table
                    rows={data.items}
                    rowKey={(row) => row.name}
                    empty={<EmptyState message="No provider is registered." />}
                    columns={[
                      { key: 'name', header: 'Provider', render: (row) => <span className="font-mono text-xs">{row.name}</span> },
                      { key: 'privacy', header: 'Privacy', render: (row) => <Badge tone={row.privacy_class === 'local' ? 'ok' : 'warning'}>{row.privacy_class}</Badge> },
                      { key: 'models', header: 'Models', render: (row) => row.models.length, align: 'end' },
                    ]}
                  />
                  <p className="text-[11px] text-graphite-500">
                    Model routing is policy-driven; the router reports the decision it would actually make.
                  </p>
                </div>
              )}
            </Async>
          </Card>
        </div>
      </div>

      <Card title={t('platform.integrations')} subtitle="each integration reports its own configuration state">
        <Async query={integrations}>
          {(data) => (
            <div className="space-y-3">
              <Table
                rows={Object.entries(data).map(([key, value]) => ({ key, value }))}
                rowKey={(row) => row.key}
                columns={[
                  { key: 'integration', header: 'Integration', render: (row) => humanise(row.key) },
                  {
                    key: 'state',
                    header: t('common.status'),
                    render: (row) => {
                      const text = typeof row.value === 'string' ? row.value : JSON.stringify(row.value)
                      const configured = !/none|not_configured|disabled|false|unavailable/i.test(text)
                      return <Badge tone={configured ? 'ok' : 'warning'}>{text}</Badge>
                    },
                  },
                ]}
              />
              <p className="text-[11px] text-graphite-600 dark:text-graphite-300">{t('platform.notConfiguredHint')}</p>
              <details>
                <summary className="cursor-pointer text-xs font-medium">Raw integration payload</summary>
                <Json value={data} max={120} />
              </details>
            </div>
          )}
        </Async>
      </Card>

      <NotificationOutboxPanel />
    </div>
  )
}
