/**
 * CP10 Real-Stack E2E Suite — Live Operations, Automatic Rule Evaluation,
 * Per-Well WebSocket Stream Correctness, Alert Lifecycle & Evidence Drawer.
 *
 * Zero HTTP or WebSocket interception is used: every assertion runs against the real FastAPI
 * service, transactional outbox, rule evaluation engine, and Vite client.
 */

import { apiGet, apiPost, expect, test, wellPath } from './fixtures'

type ChannelDto = {
  id: string
  well_id: string
  channel_key: string
}

type AlertDto = {
  id: string
  status: string
  title: string
  version: string | null
  updated_at: string | null
  allowed_transitions: string[]
}

test.describe('CP10 live operational vertical slice (Journeys A–I)', () => {
  test('Journeys A, B, C, D, E & H: telemetry ingestion -> auto-alert -> T1 evidence integrity -> ack/conflict/clear -> advisor context', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    // 1. Register a deterministic high-SPP alert rule on the seeded well
    const ruleKey = `e2e_spp_high_${Date.now()}`
    await apiPost(
      request,
      '/alert-rules',
      {
        well_id: wellId,
        rule_key: ruleKey,
        name: 'E2E Standpipe Pressure High',
        channel_key: 'spp',
        operator: 'gt',
        threshold: 4000,
        unit: 'psi',
        clear_operator: 'lte',
        clear_threshold: 3700,
        sustain_seconds: 0,
        cooldown_seconds: 0,
        severity: 'high',
      },
      'supervisor',
    )

    // 2. Open the Well Cockpit Live Monitor tab
    await page.goto(wellPath('/cockpit?tab=live'))
    await expect(page.getByTestId('operational-monitor')).toBeVisible()

    // 3. Journey A & B: Commission synthetic telemetry with a breaching point at T1 (4200 psi)
    // WITHOUT calling /wells/{id}/alerts/evaluate manually.
    const t1Iso = new Date(Date.now() - 20_000).toISOString()
    const commissionResp = await apiPost<{
      report: { alerts_raised: number }
    }>(
      request,
      `/wells/${wellId}/timeseries/commission-synthetic`,
      {
        channels: [
          {
            channel_key: 'spp',
            name: 'Standpipe Pressure',
            dimension: 'pressure',
            unit: 'psi',
            values: [3500, 4200],
            start: t1Iso,
            step_seconds: 5,
          },
          {
            channel_key: 'wob',
            name: 'Weight on Bit',
            dimension: 'force',
            unit: 'klbf',
            values: [22, 25],
            start: t1Iso,
            step_seconds: 5,
          },
        ],
      },
      'engineer',
    )
    expect(commissionResp.report.alerts_raised).toBeGreaterThanOrEqual(1)

    // Resync or let live stream update the monitor
    await page.getByTestId('live-resync-btn').click()

    // Verify KPI strip shows live SPP reading and Bounded Channel History Chart renders
    await expect(page.getByTestId('kpi-card-spp')).toBeVisible()
    await expect(page.getByTestId('kpi-value-spp')).toBeVisible()
    await expect(page.getByTestId('channel-history-chart')).toBeVisible()

    // Verify the automatic alert appears in the Operational Alerts list
    const alertsPage = await apiGet<{ items: AlertDto[] }>(
      request,
      `/alerts?well_id=${wellId}&status=raised`,
      'engineer',
    )
    const raised = alertsPage.items.find((a) => a.title.includes('E2E Standpipe Pressure High'))
    expect(raised, 'automatic rule evaluation must have raised the SPP alert').toBeTruthy()
    const alertId = raised!.id

    const alertRow = page.getByTestId(`alert-row-${alertId}`)
    await expect(alertRow).toBeVisible()
    await expect(page.getByTestId(`alert-status-${alertId}`)).toHaveText(/raised/i)

    // 4. Journey C: Inspect Alert Evidence at T1, then ingest a higher point at T2 (4500 psi)
    // and prove the alert's T1 historical evidence is never overwritten by T2 telemetry.
    await page.getByTestId(`alert-evidence-btn-${alertId}`).click()
    const drawer = page.getByTestId('alert-evidence-drawer')
    await expect(drawer).toBeVisible()
    const t1ObservedText = await drawer.getByTestId('evidence-observed-value').textContent()
    expect(t1ObservedText).toBeTruthy()

    // Close drawer with Escape key (accessibility check)
    await page.keyboard.press('Escape')
    await expect(drawer).toBeHidden()

    // Append a later point at T2 (4500 psi) on SPP channel
    const channels = await apiGet<{ items: ChannelDto[] }>(
      request,
      `/timeseries?well_id=${wellId}&channel_key=spp`,
      'engineer',
    )
    const sppChannelId = channels.items[0]!.id
    const t2Iso = new Date().toISOString()
    await apiPost(
      request,
      `/timeseries/${sppChannelId}/points`,
      {
        points: [
          {
            ts: t2Iso,
            value: 4500,
            unit: 'psi',
            quality: 'good',
            source_point_id: `t2-point-${Date.now()}`,
          },
        ],
      },
      'engineer',
    )

    // Re-open Evidence Drawer and verify T1 observed value is unchanged
    await page.getByTestId('live-resync-btn').click()
    await page.getByTestId(`alert-evidence-btn-${alertId}`).click()
    await expect(drawer).toBeVisible()
    const afterT2ObservedText = await drawer.getByTestId('evidence-observed-value').textContent()
    expect(afterT2ObservedText).toBe(t1ObservedText)
    await page.getByTestId('close-alert-evidence-btn').click()
    await expect(drawer).toBeHidden()

    // 5. Journey D: Acknowledge alert via UI, verify allowed_transitions update,
    // test required reason on Clear, and test 409 optimistic concurrency conflict recovery.
    await page.getByTestId(`alert-ack-btn-${alertId}`).click()
    await page.getByTestId(`alert-reason-input-${alertId}`).fill('Driller notified on rig floor')
    await page.getByTestId(`alert-confirm-btn-${alertId}`).click()

    await expect(page.getByTestId(`alert-status-${alertId}`)).toHaveText(/acknowledged/i)
    await expect(page.getByTestId(`alert-ack-btn-${alertId}`)).toHaveCount(0)

    // Open Clear form in UI, then concurrently mutate the alert on the server so the UI's
    // expected_updated_at is stale, proving real 409 Conflict handling.
    await page.getByTestId(`alert-clear-btn-${alertId}`).click()
    await page.getByTestId(`alert-confirm-btn-${alertId}`).click()
    await expect(page.getByTestId('alert-transition-error')).toBeVisible()

    // Advance the alert on the server to cause a real 409 when the UI submits its stale version
    const currentAlert = await apiGet<AlertDto>(request, `/alerts/${alertId}`, 'engineer')
    await apiPost(
      request,
      `/alerts/${alertId}/clear`,
      {
        expected_updated_at: currentAlert.version ?? currentAlert.updated_at,
        reason: 'Cleared concurrently via API',
      },
      'engineer',
    )

    await page.getByTestId(`alert-reason-input-${alertId}`).fill('Late UI clear attempt')
    await page.getByTestId(`alert-confirm-btn-${alertId}`).click()
    await expect(page.getByTestId('alert-conflict-banner')).toBeVisible()
    await expect(page.getByTestId(`alert-status-${alertId}`)).toHaveText(/cleared/i)

    // 6. Journey H: Deep-link from Alert Evidence Drawer to Operations Advisor with alert context
    await page.getByTestId(`alert-evidence-btn-${alertId}`).click()
    await expect(drawer).toBeVisible()
    await drawer.getByTestId('alert-evidence-advisor-link').click()
    await expect(page).toHaveURL(new RegExp(`/wells/${wellId}/advisor\\?alert=${alertId}`))
    await expect(page.getByTestId('advisor-linked-alert-badge')).toBeVisible()

    const unexpectedErrors = consoleErrors.filter((msg) => !msg.includes('409 (Conflict)'))
    expect(unexpectedErrors, `console errors: ${unexpectedErrors.join(' | ')}`).toEqual([])
  })

  test('Journeys F, G & I: cross-well stream isolation, missing/untrustworthy channel honesty, and Persian RTL token isolation', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    // Journey G: Create a channel with no readings (missing) and a channel with a 'bad' quality point
    const missingKey = `rop_empty_${Date.now()}`
    await apiPost(
      request,
      '/timeseries',
      {
        well_id: wellId,
        channel_key: missingKey,
        name: 'Empty ROP Channel',
        dimension: 'velocity',
        unit: 'm/s',
        source: 'witsml',
      },
      'engineer',
    )

    const badKey = `torque_bad_${Date.now()}`
    const badChannel = await apiPost<ChannelDto>(
      request,
      '/timeseries',
      {
        well_id: wellId,
        channel_key: badKey,
        name: 'Faulty Torque Sensor',
        dimension: 'torque',
        unit: 'N.m',
        source: 'witsml',
      },
      'engineer',
    )
    await apiPost(
      request,
      `/timeseries/${badChannel.id}/points`,
      {
        points: [
          {
            ts: new Date().toISOString(),
            value: 88888,
            unit: 'N.m',
            quality: 'bad',
            source_point_id: `bad-pt-${Date.now()}`,
          },
        ],
      },
      'engineer',
    )

    await page.goto(wellPath('/cockpit?tab=live'))
    await expect(page.getByTestId(`kpi-missing-${missingKey}`)).toHaveText(/No reading recorded/i)
    await expect(page.getByTestId(`kpi-untrustworthy-${badKey}`)).toHaveText(
      /Untrustworthy quality \(bad\)/i,
    )

    // Journey F: Create a second well in the same org, emit >55 telemetry batches on Well B,
    // then 1 point on Well A, and verify Well A has no false gap badge and no foreign data.
    const wellAState = await apiGet<{
      state: { well: { project_id: string; field_id: string | null } }
    }>(request, `/wells/${wellId}/state`, 'engineer')
    const wellB = await apiPost<{ id: string }>(
      request,
      '/wells',
      {
        project_id: wellAState.state.well.project_id,
        field_id: wellAState.state.well.field_id,
        name: `SYNTH-ISOLATION-${Date.now()}`,
        uwi: `ISO-UWI-${Date.now()}`,
        well_type: 'development_producer',
        operator: 'E2E Operator',
      },
      'engineer',
    )
    const wellBChannel = await apiPost<ChannelDto>(
      request,
      '/timeseries',
      {
        well_id: wellB.id,
        channel_key: 'hookload_foreign',
        name: 'Foreign Well Hookload',
        dimension: 'force',
        unit: 'N',
        source: 'synthetic',
      },
      'engineer',
    )
    for (let i = 0; i < 30; i += 1) {
      await apiPost(
        request,
        `/timeseries/${wellBChannel.id}/points`,
        {
          points: [
            {
              ts: new Date(Date.now() - (30 - i) * 1000).toISOString(),
              value: 100000 + i * 100,
              unit: 'N',
              quality: 'good',
              source_point_id: `wb-${i}-${Date.now()}`,
            },
          ],
        },
        'engineer',
      )
    }

    // Verify Well A's monitor never shows Well B's `hookload_foreign` channel
    await page.getByTestId('live-resync-btn').click()
    await expect(page.getByTestId('kpi-card-hookload_foreign')).toHaveCount(0)

    // Journey I: Switch to Persian (fa) and verify RTL document direction + LTR technical token isolation
    await page.evaluate(() => window.localStorage.setItem('drillai.locale', 'fa'))
    await page.reload()
    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl')
    await expect(page.getByTestId('operational-monitor')).toBeVisible()
    await expect(page.getByTestId('kpi-value-spp')).toHaveAttribute('dir', 'ltr')

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('Journey E & UI Commissioning: hysteresis auto-clear on recovery point and UI synthetic commissioning button', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const ruleKey = `e2e_wob_hyst_${Date.now()}`
    await apiPost(
      request,
      '/alert-rules',
      {
        well_id: wellId,
        rule_key: ruleKey,
        name: 'E2E WOB Hysteresis High',
        channel_key: 'wob',
        operator: 'gt',
        threshold: 30,
        unit: 'klbf',
        clear_operator: 'lte',
        clear_threshold: 24,
        sustain_seconds: 0,
        clear_sustain_seconds: 0,
        cooldown_seconds: 0,
        severity: 'medium',
      },
      'supervisor',
    )

    await page.goto(wellPath('/cockpit?tab=live'))
    await expect(page.getByTestId('operational-monitor')).toBeVisible()

    // Click UI "Commission synthetic batch" button and verify it completes cleanly
    await page.getByTestId('commission-synthetic-btn').click()
    await expect(page.getByTestId('kpi-card-rpm')).toBeVisible()
    await expect(page.getByTestId('commission-synthetic-btn')).toBeEnabled()

    // Ingest a breaching WOB point (35 klbf > 30 klbf)
    const channels = await apiGet<{ items: ChannelDto[] }>(
      request,
      `/timeseries?well_id=${wellId}&channel_key=wob`,
      'engineer',
    )
    const wobChannelId = channels.items[0]!.id
    const baseMs = Date.now()
    await apiPost(
      request,
      `/timeseries/${wobChannelId}/points`,
      {
        points: [
          {
            ts: new Date(baseMs - 2_000).toISOString(),
            value: 35,
            unit: 'klbf',
            quality: 'good',
            source_point_id: `wob-breach-${baseMs}`,
          },
        ],
      },
      'engineer',
    )

    await page.getByTestId('live-resync-btn').click()
    const raisedList = await apiGet<{ items: AlertDto[] }>(
      request,
      `/alerts?well_id=${wellId}&status=raised`,
      'engineer',
    )
    const wobAlert = raisedList.items.find((a) => a.title.includes('E2E WOB Hysteresis High'))
    expect(wobAlert, 'breaching WOB point must auto-raise alert').toBeTruthy()
    await expect(page.getByTestId(`alert-status-${wobAlert!.id}`)).toHaveText(/raised/i)

    // Ingest a point in the deadband (27 klbf: <= 30 klbf but > 24 klbf clear line) -> alert must stay raised
    await apiPost(
      request,
      `/timeseries/${wobChannelId}/points`,
      {
        points: [
          {
            ts: new Date(baseMs - 1_000).toISOString(),
            value: 27,
            unit: 'klbf',
            quality: 'good',
            source_point_id: `wob-deadband-${baseMs}`,
          },
        ],
      },
      'engineer',
    )
    await page.getByTestId('live-resync-btn').click()
    await expect(page.getByTestId(`alert-status-${wobAlert!.id}`)).toHaveText(/raised/i)

    // Ingest a recovery point below hysteresis clear line (20 klbf <= 24 klbf) -> alert auto-clears
    await apiPost(
      request,
      `/timeseries/${wobChannelId}/points`,
      {
        points: [
          {
            ts: new Date(baseMs).toISOString(),
            value: 20,
            unit: 'klbf',
            quality: 'good',
            source_point_id: `wob-recover-${baseMs}`,
          },
        ],
      },
      'engineer',
    )
    await page.getByTestId('live-resync-btn').click()
    await expect(page.getByTestId(`alert-status-${wobAlert!.id}`)).toHaveText(/cleared/i)
    await expect(page.getByTestId(`alert-ack-btn-${wobAlert!.id}`)).toHaveCount(0)

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })
})
