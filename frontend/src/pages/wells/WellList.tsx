/**
 * Well selection: the entry point of the acceptance chain (create/select Well → Cockpit).
 *
 * Shows only what the API reports. A well with no spud date, no planned depth or no twin state is
 * displayed as exactly that — the list never fills a gap with an assumption.
 */

import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import type { Well } from '../../api/types'
import { PageHeader } from '../../components/layout/AppShell'
import { Async, Badge, Button, Card, EmptyState, Table } from '../../components/common'
import { useI18n } from '../../i18n'
import { formatDate, formatNumber, formatStatus } from '../../lib/format'
import { useSession } from '../../stores/session'

export default function WellList() {
  const { t, locale } = useI18n()
  const { unitSystem } = useSession()
  const navigate = useNavigate()
  const wells = useQuery({ queryKey: ['wells'], queryFn: () => drillingApi.listWells({ limit: 200 }) })
  const projects = useQuery({ queryKey: ['projects'], queryFn: () => drillingApi.listProjects() })

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('nav.wells')}
        description="Select a well to open its cockpit. Every downstream workspace is scoped to one well."
      />

      <Card
        title="Registered wells"
        subtitle={
          projects.data
            ? `${wells.data?.total ?? 0} wells across ${projects.data.total} projects`
            : undefined
        }
      >
        <Async query={wells} empty={(data) => (data.items.length === 0 ? <EmptyState message={t('empty.noWells')} hint="Wells are created through POST /api/v1/wells; scripts/seed_demo.py creates a labelled demo scenario." /> : false)}>
          {(data) => (
            <Table
              rows={data.items}
              rowKey={(row: Well) => row.id}
              onRowClick={(row) => navigate(`/wells/${row.id}/cockpit`)}
              columns={[
                {
                  key: 'name',
                  header: 'Well',
                  render: (row) => (
                    <span>
                      <Link
                        to={`/wells/${row.id}/cockpit`}
                        className="font-medium text-signal-deep underline-offset-2 hover:underline dark:text-signal-light"
                      >
                        {row.name}
                      </Link>
                      <span className="block font-mono text-[10px] text-graphite-500">{row.id}</span>
                    </span>
                  ),
                },
                { key: 'type', header: 'Type', render: (row) => <Badge tone="neutral">{row.well_type}</Badge> },
                { key: 'status', header: 'Status', render: (row) => formatStatus(row.status) },
                { key: 'operator', header: 'Operator', render: (row) => row.operator ?? '—' },
                {
                  key: 'planned',
                  header: 'Planned TD',
                  align: 'end',
                  render: (row) =>
                    row.total_depth_planned_si === null
                      ? '—'
                      : `${formatNumber(row.total_depth_planned_si, locale, 0)} ${unitSystem === 'oilfield' ? 'ft' : 'm'}`,
                },
                { key: 'spud', header: 'Spud', render: (row) => formatDate(row.spud_date, locale) },
                {
                  key: 'twin',
                  header: 'Twin',
                  render: (row) => (
                    <Badge tone={row.twin_state === 'none' ? 'neutral' : 'ok'}>{row.twin_state}</Badge>
                  ),
                },
                {
                  key: 'open',
                  header: '',
                  render: (row) => (
                    <Link to={`/wells/${row.id}/cockpit`}>
                      <Button size="sm" variant="ghost">
                        {t('nav.cockpit')}
                      </Button>
                    </Link>
                  ),
                },
              ]}
            />
          )}
        </Async>
      </Card>

      {projects.data && projects.data.total > 0 && (
        <Card title="Projects" dense>
          <Table
            rows={projects.data.items}
            rowKey={(row) => row.id}
            columns={[
              { key: 'name', header: 'Project', render: (row) => row.name },
              { key: 'code', header: 'Code', render: (row) => row.code ?? '—' },
              { key: 'phase', header: 'Phase', render: (row) => row.phase },
              { key: 'wells', header: 'Wells', render: (row) => row.well_count ?? '—', align: 'end' },
            ]}
          />
        </Card>
      )}
    </div>
  )
}
