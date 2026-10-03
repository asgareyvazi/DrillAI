/**
 * Master Data — the workspace where the asset spine is read and edited.
 *
 * It is *not* the cockpit, and the separation is deliberate: the cockpit answers "where is this well
 * now", while this answers "what is this well, what is inside it, and who changed what". Mixing the two
 * is how a rename ends up next to a depth readout.
 *
 * ## The shape of the page
 *
 * Scope (project → field → query) → the wells in that scope → one well, shown as four panels:
 * identity, wellbores & lineage, sections, and the governance ledger. The scope and the selected well
 * live in the URL, so a reload, a bookmark or a shared link opens the same page rather than resetting
 * to the first well in the list.
 *
 * ## One read per subject
 *
 * The selected well is read once, through `GET /wells/{id}/structure`, which returns the well (with the
 * transitions it may take), its wellbores, each wellbore's sections and what hangs off them. Asking for
 * each wellbore's sections separately would be an N+1 the server should not invite, and it would also
 * let the panels disagree with each other while their reads were in flight.
 *
 * ## Search is the server's
 *
 * The search box sends `q` to `GET /wells`; the list renders what came back. Nothing here filters rows
 * client-side, because a client-side filter searches the page you happened to load and silently hides
 * the wells it never received — which reads exactly like "there are no such wells".
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import type { Well } from '../../api/types'
import { PageHeader } from '../../components/layout/AppShell'
import { Async, Badge, Button, Card, Drawer, EmptyState, Table, Tabs } from '../../components/common'
import { useI18n } from '../../i18n'
import { formatDateTime, formatStatus, humanise } from '../../lib/format'
import { AuditPanel } from './AuditPanel'
import { FieldPanel } from './FieldPanel'
import { SectionPanel } from './SectionPanel'
import { WellCreateForm } from './WellCreateForm'
import { WellIdentityPanel } from './WellIdentityPanel'
import { WellborePanel } from './WellborePanel'
import { FormField, Select, TextInput } from './form'

function useDebounced<T>(value: T, ms = 300): T {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms)
    return () => clearTimeout(timer)
  }, [value, ms])
  return settled
}

export default function MasterDataWorkspace() {
  const { t, locale } = useI18n()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const projectId = searchParams.get('project') ?? ''
  const fieldId = searchParams.get('field') ?? ''
  const wellId = searchParams.get('well') ?? ''
  const tab = searchParams.get('tab') ?? 'identity'

  const [search, setSearch] = useState('')
  const query = useDebounced(search)
  const [creating, setCreating] = useState(false)
  const [selectedWellboreId, setSelectedWellboreId] = useState<string | null>(null)

  const setParams = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(searchParams)
    for (const [key, value] of Object.entries(changes)) {
      if (value === null || value === '') next.delete(key)
      else next.set(key, value)
    }
    setSearchParams(next, { replace: true })
  }

  /*
   * The scope is chosen in two steps and each step clears what it invalidates: a different project has
   * different fields, and a different field has different wells. Leaving a stale id in the URL would
   * make the page ask for a well in a scope that no longer contains it, and the answer would be a 404
   * the reader could do nothing with.
   */
  const projects = useQuery({ queryKey: ['projects'], queryFn: ({ signal }) => drillingApi.listProjects(signal) })
  const rigs = useQuery({ queryKey: ['rigs'], queryFn: ({ signal }) => drillingApi.listRigs(signal) })
  const fields = useQuery({
    queryKey: ['fields', projectId],
    queryFn: ({ signal }) => drillingApi.listFields({ project_id: projectId || undefined, limit: 200 }, signal),
  })
  const wells = useQuery({
    queryKey: ['wells', { project_id: projectId, field_id: fieldId, q: query }],
    queryFn: ({ signal }) =>
      drillingApi.listWells(
        {
          project_id: projectId || undefined,
          field_id: fieldId || undefined,
          q: query || undefined,
          limit: 200,
        },
        signal,
      ),
  })
  const structure = useQuery({
    queryKey: ['well-structure', wellId],
    queryFn: ({ signal }) => drillingApi.wellStructure(wellId, signal),
    enabled: wellId !== '',
  })

  // The selected wellbore follows the data: when the well changes, or the hole it pointed at is gone,
  // the first wellbore is selected rather than a stale id being sent to the server. Memoised so the
  // effects below depend on the *array identity* the query produced, not on a new `[]` per render.
  const wellbores = useMemo(() => structure.data?.wellbores ?? [], [structure.data])
  /**
   * The default is the *active* hole — the one being drilled — and only then the first in the list.
   *
   * The distinction is not cosmetic: after a sidetrack is activated, the well's current work is in the
   * new hole, and opening the panel on the original bore would show a section that has not moved while
   * the hole that is actually turning to the right is one click away. When no hole is active (a planned
   * well) the sequence order is the honest default.
   */
  const defaultWellboreId = structure.data?.well.active_wellbore_id ?? wellbores[0]?.id ?? null
  useEffect(() => {
    if (selectedWellboreId === null || !wellbores.some((row) => row.id === selectedWellboreId)) {
      setSelectedWellboreId(defaultWellboreId)
    }
  }, [wellbores, selectedWellboreId, defaultWellboreId])

  const activeWellbore = useMemo(
    () => wellbores.find((row) => row.id === selectedWellboreId) ?? null,
    [wellbores, selectedWellboreId],
  )

  const refreshWell = () => {
    void queryClient.invalidateQueries({ queryKey: ['well-structure', wellId] })
    void queryClient.invalidateQueries({ queryKey: ['wells'] })
    void queryClient.invalidateQueries({ queryKey: ['well-audit-log', wellId] })
  }

  const selectProject = (nextProject: string) =>
    setParams({ project: nextProject, field: null, well: null })
  const selectWell = (well: Well) => setParams({ well: well.id, tab: 'identity' })

  // A project-level entry point exists for the case where the reader arrives with no scope at all:
  // the first project the server returned, never a hard-coded id.
  const activeProject = projectId !== '' ? projectId : (projects.data?.items[0]?.id ?? '')

  return (
    <div className="space-y-4">
      <PageHeader title={t('master.title')} description={t('master.description')} />

      <Card title={t('master.scope')} dense>
        <div className="grid gap-3 p-2 sm:grid-cols-4">
          <FormField label={t('master.project')}>
            <Select
              value={projectId}
              onChange={selectProject}
              options={(projects.data?.items ?? []).map((project) => ({ value: project.id, label: project.name }))}
              placeholder={t('wells.allProjects')}
            />
          </FormField>
          <FormField label={t('master.field')}>
            <Select
              value={fieldId}
              onChange={(value) => setParams({ field: value, well: null })}
              options={(fields.data?.items ?? []).map((field) => ({ value: field.id, label: field.name }))}
              placeholder={t('master.allFields')}
              disabled={projectId === ''}
            />
          </FormField>
          <FormField label={t('wells.searchLabel')} hint={t('wells.searchHint')}>
            <TextInput value={search} onChange={setSearch} placeholder={t('wells.searchPlaceholder')} />
          </FormField>
          <div className="flex items-end">
            <Button onClick={() => setCreating(true)} data-testid="master-new-well">
              {t('wells.newWell')}
            </Button>
          </div>
        </div>
      </Card>

      <div className="grid gap-4 lg:grid-cols-[minmax(280px,360px)_1fr]">
        <div className="space-y-4">
          <Card
            title={t('master.wells')}
            subtitle={wells.data ? t('wells.resultsOf', { shown: wells.data.items.length, total: wells.data.total }) : undefined}
          >
            <Async
              query={wells}
              empty={(data) =>
                data.items.length === 0 ? (
                  <EmptyState
                    message={query !== '' ? t('wells.noMatches') : t('empty.noWells')}
                    hint={query !== '' ? t('wells.noMatchesHint') : undefined}
                  />
                ) : false
              }
            >
              {(data) => (
                <Table
                  rows={data.items}
                  rowKey={(row) => row.id}
                  isRowActive={(row) => row.id === wellId}
                  onRowClick={selectWell}
                  columns={[
                    {
                      key: 'name',
                      header: t('master.well'),
                      render: (row) => (
                        <span>
                          {row.name}
                          <span className="block font-mono text-[10px] text-graphite-500" dir="ltr">
                            {row.uwi ?? row.id}
                          </span>
                        </span>
                      ),
                    },
                    { key: 'type', header: t('master.wellType'), render: (row) => humanise(row.well_type) },
                    { key: 'status', header: t('master.status'), render: (row) => formatStatus(row.status) },
                    {
                      key: 'updated',
                      header: t('master.changed'),
                      render: (row) => formatDateTime(row.updated_at ?? null, locale),
                    },
                  ]}
                />
              )}
            </Async>
          </Card>

          {activeProject !== '' && (
            <Async query={fields} empty={() => false}>
              {(data) => (
                <FieldPanel
                  projectId={activeProject}
                  fields={data.items}
                  onChanged={() => {
                    void queryClient.invalidateQueries({ queryKey: ['fields'] })
                    void queryClient.invalidateQueries({ queryKey: ['well-structure', wellId] })
                  }}
                  onSelectField={(id) => setParams({ field: id, well: null })}
                />
              )}
            </Async>
          )}
        </div>

        <div className="space-y-4">
          {wellId === '' ? (
            <Card title={t('master.title')}>
              <EmptyState message={t('master.selectWell')} hint={t('master.selectScope')} />
            </Card>
          ) : (
            <Async query={structure}>
              {(data) => (
                <>
                  <Card
                    title={data.well.name}
                    subtitle={`${data.well.uwi ?? t('master.notRecorded')} · ${humanise(data.well.well_type)}`}
                    actions={
                      <span className="flex items-center gap-2">
                        <Badge tone="neutral">{formatStatus(data.well.status)}</Badge>
                        {data.counts.documents !== undefined && (
                          <Badge tone="info">{`${data.counts.documents} documents`}</Badge>
                        )}
                      </span>
                    }
                  >
                    <Tabs
                      tabs={[
                        { key: 'identity', label: t('master.identity') },
                        {
                          key: 'structure',
                          label: t('master.structure'),
                          badge: <Badge tone="neutral">{data.wellbores.length}</Badge>,
                        },
                        {
                          key: 'sections',
                          label: t('master.sections'),
                          badge: (
                            <Badge tone="neutral">
                              {data.wellbores.reduce((total, hole) => total + hole.sections.length, 0)}
                            </Badge>
                          ),
                        },
                        { key: 'ledger', label: t('master.ledger') },
                      ]}
                      active={tab}
                      onChange={(key) => setParams({ tab: key })}
                    />
                  </Card>

                  {tab === 'identity' && (
                    <WellIdentityPanel
                      well={data.well}
                      rigs={rigs.data?.items ?? []}
                      fields={fields.data?.items ?? []}
                      onChanged={refreshWell}
                    />
                  )}
                  {tab === 'structure' && (
                    <WellborePanel wellId={wellId} wellbores={data.wellbores} onChanged={refreshWell} />
                  )}
                  {tab === 'sections' && (
                    <SectionPanel
                      wellbore={activeWellbore}
                      sections={activeWellbore?.sections ?? []}
                      onChanged={refreshWell}
                    />
                  )}
                  {tab === 'ledger' && <AuditPanel wellId={wellId} />}
                </>
              )}
            </Async>
          )}
        </div>
      </div>

      <Drawer open={creating} title={t('master.createWellTitle')} onClose={() => setCreating(false)} wide>
        <WellCreateForm
          projects={projects.data?.items ?? []}
          fields={fields.data?.items ?? []}
          rigs={rigs.data?.items ?? []}
          defaultProjectId={activeProject}
          onCreated={(well) => {
            setCreating(false)
            setParams({ well: well.id, tab: 'identity' })
          }}
        />
      </Drawer>
    </div>
  )
}
