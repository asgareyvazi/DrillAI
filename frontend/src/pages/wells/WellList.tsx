/**
 * Well selection: the entry point of the acceptance chain (create/select Well → Cockpit).
 *
 * Shows only what the API reports. A well with no spud date, no planned depth or no twin state is
 * displayed as exactly that — the list never fills a gap with an assumption.
 *
 * ## Search and filtering are the server's
 *
 * The search box and the project filter are sent to `GET /wells` as `q` and `project_id`, and the table
 * renders exactly what came back. Filtering the rows already in the browser would search only the page
 * that happened to be loaded: the result would be a list that looks complete and is not. The subtitle
 * says how many of the scope's wells are being shown, because "12 wells" and "12 of 340" mean very
 * different things to somebody looking for a well.
 *
 * ## This is the master-data entry point
 *
 * A well can be created from here, and each row opens the Master Data workspace for that well. Editing
 * does not happen in this table: a rename belongs where the ledger, the lineage and the context counts
 * are visible at the same time.
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import type { Field, Well } from '../../api/types'
import { PageHeader } from '../../components/layout/AppShell'
import { Async, Badge, Button, Card, Drawer, EmptyState, ErrorState, Table } from '../../components/common'
import { useI18n } from '../../i18n'
import { formatDate, formatNumber, formatStatus, humanise } from '../../lib/format'
import { useSession } from '../../stores/session'
import { WellCreateForm } from '../master-data/WellCreateForm'
import { FormField, Select, TextInput } from '../master-data/form'

/**
 * A debounce, because a search that fires on every keystroke is one request per keystroke.
 *
 * Written out rather than pulled from a library: a timer and a piece of state are the whole feature.
 * The effect clears its timer on every change, so only the last keystroke of a burst reaches the
 * server, and it is cleaned up on unmount so a pending timer cannot set state on a gone component.
 */
function useDebounced<T>(value: T, ms = 300): T {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms)
    return () => clearTimeout(timer)
  }, [value, ms])
  return settled
}

export default function WellList() {
  const { t, locale } = useI18n()
  const { unitSystem } = useSession()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [creating, setCreating] = useState(false)
  const [search, setSearch] = useState('')
  const [projectId, setProjectId] = useState('')
  const query = useDebounced(search)

  const wells = useQuery({
    queryKey: ['wells', { project_id: projectId, q: query }],
    queryFn: ({ signal }) =>
      drillingApi.listWells(
        { project_id: projectId || undefined, q: query || undefined, limit: 200 },
        signal,
      ),
  })
  const projects = useQuery({ queryKey: ['projects'], queryFn: ({ signal }) => drillingApi.listProjects(signal) })
  const fields = useQuery({
    queryKey: ['fields', 'all'],
    queryFn: ({ signal }) => drillingApi.listFields({ limit: 500 }, signal),
  })
  const rigs = useQuery({ queryKey: ['rigs'], queryFn: ({ signal }) => drillingApi.listRigs(signal) })

  // "Filtered" is what the *request* carried, not what is in the box: while a keystroke is still
  // being debounced the list on screen is the previous query's answer, and the counts must describe
  // the answer actually shown.
  const filtered = query !== '' || projectId !== ''
  const fieldName = (well: Well): string => {
    if (well.field_id === null) return '—'
    const field = (fields.data?.items ?? []).find((row: Field) => row.id === well.field_id)
    // A field id that cannot be resolved to a name is shown as the id, in mono, rather than as a dash:
    // the well *is* in a field, and claiming otherwise would be wrong.
    return field ? field.name : well.field_id
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('nav.wells')}
        description={t('wells.description')}
        actions={
          <Button variant="primary" onClick={() => setCreating(true)} data-testid="wells-new">
            {t('wells.newWell')}
          </Button>
        }
      />

      <Card title={t('master.scope')} dense>
        <div className="grid gap-3 p-2 sm:grid-cols-3">
          <FormField label={t('wells.searchLabel')} hint={t('wells.searchHint')}>
            <TextInput
              value={search}
              onChange={setSearch}
              placeholder={t('wells.searchPlaceholder')}
              name="well-search"
            />
          </FormField>
          <FormField label={t('wells.projectFilter')}>
            <Select
              value={projectId}
              onChange={setProjectId}
              options={(projects.data?.items ?? []).map((project) => ({ value: project.id, label: project.name }))}
              placeholder={t('wells.allProjects')}
            />
          </FormField>
          <div className="flex items-end">
            {filtered && (
              <Button
                variant="ghost"
                onClick={() => {
                  setSearch('')
                  setProjectId('')
                }}
                data-testid="wells-clear"
              >
                {t('wells.clearSearch')}
              </Button>
            )}
          </div>
        </div>
      </Card>

      {/*
        Each half of the subtitle is only said when the request behind it answered. It used to read
        `${wells.data?.total ?? 0} wells …`, so a failed well read was printed as the number zero — a
        fabricated count, on the same screen that also reported the failure below it.
      */}
      <Card
        title={t('wells.registered')}
        subtitle={
          wells.data
            ? filtered
              ? t('wells.resultsOf', { shown: wells.data.items.length, total: wells.data.total })
              : projects.data
                ? `${wells.data.total} wells across ${projects.data.total} projects`
                : `${wells.data.total} wells`
            : undefined
        }
      >
        <Async
          query={wells}
          empty={(data) =>
            data.items.length === 0 ? (
              <EmptyState
                message={filtered ? t('wells.noMatches') : t('empty.noWells')}
                hint={filtered ? t('wells.noMatchesHint') : undefined}
              />
            ) : false
          }
        >
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
                      {/* An identifier, not prose: LTR and monospaced so RTL cannot reorder it. */}
                      <span className="block font-mono text-[10px] text-graphite-500" dir="ltr">
                        {row.uwi ?? row.id}
                      </span>
                    </span>
                  ),
                },
                { key: 'type', header: t('master.wellType'), render: (row) => <Badge tone="neutral">{humanise(row.well_type)}</Badge> },
                { key: 'status', header: t('master.status'), render: (row) => formatStatus(row.status) },
                { key: 'field', header: t('master.field'), render: (row) => fieldName(row) },
                { key: 'operator', header: t('master.operator'), render: (row) => row.operator ?? '—' },
                {
                  key: 'planned',
                  header: t('master.plannedTd'),
                  align: 'end',
                  // A well with no recorded plan shows "—": an unplanned well is not a well with zero
                  // planned depth, and printing `0 m` would be a number nobody measured.
                  render: (row) =>
                    row.total_depth_planned_si === null
                      ? '—'
                      : `${formatNumber(row.total_depth_planned_si, locale, 0)} ${unitSystem === 'oilfield' ? 'ft' : 'm'}`,
                },
                { key: 'spud', header: t('master.spudDate'), render: (row) => formatDate(row.spud_date, locale) },
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
                    <span className="flex items-center justify-end gap-1">
                      <Link to={`/master-data?well=${row.id}`}>
                        <Button size="sm" variant="ghost" data-testid={`well-master-${row.id}`}>
                          {t('wells.masterData')}
                        </Button>
                      </Link>
                      <Link to={`/wells/${row.id}/cockpit`}>
                        <Button size="sm" variant="ghost">
                          {t('nav.cockpit')}
                        </Button>
                      </Link>
                    </span>
                  ),
                },
              ]}
            />
          )}
        </Async>
      </Card>

      {/*
        The project list is enrichment: the wells above are complete without it, so its failure must
        not take the page down. It must not vanish either — an absent card reads as "there are no
        projects", which is a statement this page has no evidence for.
      */}
      {projects.error && (
        <Card title={t('common.projects')} dense>
          <div className="space-y-1" data-testid="projects-unavailable">
            <p className="text-sm">{t('wells.projectsUnavailable')}</p>
            <p className="text-xs text-graphite-500">{t('wells.projectsHint')}</p>
            <ErrorState error={projects.error} onRetry={() => void projects.refetch()} />
          </div>
        </Card>
      )}

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

      <Drawer open={creating} title={t('master.createWellTitle')} onClose={() => setCreating(false)} wide>
        <WellCreateForm
          projects={projects.data?.items ?? []}
          fields={fields.data?.items ?? []}
          rigs={rigs.data?.items ?? []}
          defaultProjectId={projectId}
          onCreated={(well) => {
            setCreating(false)
            void queryClient.invalidateQueries({ queryKey: ['wells'] })
            navigate(`/master-data?well=${well.id}`)
          }}
        />
      </Drawer>
    </div>
  )
}
