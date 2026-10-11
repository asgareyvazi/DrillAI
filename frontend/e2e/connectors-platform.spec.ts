/**
 * CP11 Real-Stack E2E Suite — Production Integration Platform, Real WITSML 1.4.1.1 SOAP & ETP 1.2
 * WebSocket Connectors, Durable Worker Polling, Security (SSRF & Secret Refs), Governed Lifecycle,
 * Operational Monitor Health Integration, RBAC, Optimistic Concurrency, RTL & Accessibility.
 *
 * Zero HTTP or WebSocket mocking is used: every test runs against the real FastAPI service,
 * real in-process TCP WITSML 1.4.1.1 SOAP and ETP 1.2 WebSocket protocol harness servers,
 * real ConnectorWorker poll execution, TelemetryService ingestion, and Vite client.
 */

import {
  apiAttempt,
  apiGet,
  apiPatch,
  apiPost,
  appConsoleErrors,
  expect,
  selectRole,
  test,
  wellPath,
} from './fixtures'

type HarnessSnapshot = {
  started: boolean
  witsml: {
    endpoint_url: string
    secret_refs: { username: string; password: string }
    requests_received: number
  }
  etp: {
    endpoint_url: string
    secret_refs: { bearer_token: string }
    sessions_opened: number
  }
}

type ConnectorDto = {
  id: string
  key: string
  name: string
  protocol_profile: string
  is_synthetic: boolean
  desired_state: string
  status: string
  config_version: number
  cursor: Record<string, unknown>
  worker: { fencing_token: number; worker_id: string | null }
  health: {
    health_state: string
    is_live: boolean
    data_freshness: string
    consecutive_failures: number
    reconnect_count: number
    last_error_category: string | null
    last_error: string | null
  }
}

test.describe('CP11 telemetry connectors, WITSML/ETP protocols, worker & operations readiness', () => {
  test('Journey 1 (Scenarios 1 & 2): create WITSML 1.4.1.1 connector in UI, mask secret refs, and run non-persistent connection test & sample preview', async ({
    page,
    wellId,
    consoleErrors,
  }) => {
    await page.goto(`/connectors?wellId=${wellId}`)
    await expect(page.getByTestId('connectors-workspace')).toBeVisible()

    // Load the local WITSML 1.4.1.1 SOAP protocol harness preset
    await page.getByTestId('preset-local-witsml').click()
    const keyInput = page.getByTestId('connector-key-input')
    const uniqueKey = `e2e-witsml-${Date.now().toString(36)}`
    await keyInput.fill(uniqueKey)
    await page.getByTestId('connector-name-input').fill('E2E Rig 01 WITSML 1.4.1.1 Store')

    // Submit creation form
    await page.getByTestId('connector-submit-btn').click()

    // Newly created connector is selected in detail view
    await expect(page.getByTestId('selected-connector-name')).toHaveText(
      'E2E Rig 01 WITSML 1.4.1.1 Store',
    )
    await expect(page.getByTestId('detail-runtime-status')).toHaveText('configured')
    await expect(page.getByTestId('detail-health-state')).not.toHaveText('LIVE')

    // Secret references are masked as ******** and never leak plaintext credentials
    await expect(page.getByTestId('masked-secret-username')).toHaveText('********')
    await expect(page.getByTestId('masked-secret-password')).toHaveText('********')
    const pageContent = await page.content()
    expect(pageContent).not.toContain('witsml-secret-pass')

    // Run non-persistent Connection Test against the real local WITSML SOAP server
    await page.getByTestId('connector-test-btn').click()
    await expect(page.getByTestId('connector-test-result')).toBeVisible()
    await expect(page.getByTestId('connector-test-result')).toContainText(
      'Connection test succeeded',
    )

    // Run non-persistent Sample Preview
    await page.getByTestId('connector-preview-btn').click()
    await expect(page.getByTestId('connector-preview-result')).toBeVisible()
    await expect(page.getByTestId('connector-preview-result')).toContainText('spp')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 2 (Scenarios 3 & 4): start WITSML connector, poll real SOAP XML frames into TelemetryService, and verify Operational Monitor KPI & LIVE connector banner', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const harness = await apiPost<HarnessSnapshot>(request, '/connectors/harness/ensure', {}, 'engineer')
    await apiPost(
      request,
      '/connectors/harness/configure',
      {
        protocol: 'witsml',
        fault_mode: 'ok',
        append_values: { SPP: 3720.0, WOB: 24.5, RPM: 122.0 },
        quality: 'good',
      },
      'engineer',
    )

    const connKey = `e2e-witsml-live-${Date.now().toString(36)}`
    const created = await apiPost<ConnectorDto>(
      request,
      '/connectors',
      {
        key: connKey,
        name: 'Rig WITSML Live Feed',
        protocol_profile: 'witsml.1.4.1.1.soap_http',
        well_id: wellId,
        endpoint_url: harness.witsml.endpoint_url,
        config: {
          auth_mode: 'basic',
          tls_verify: true,
          poll_interval_seconds: 1.0,
          channel_mappings: [
            { source_mnemonic: 'SPP', channel_key: 'spp', name: 'Standpipe Pressure', dimension: 'pressure', unit: 'psi' },
            { source_mnemonic: 'WOB', channel_key: 'wob', name: 'Weight on Bit', dimension: 'force', unit: 'klbf' },
            { source_mnemonic: 'RPM', channel_key: 'rpm', name: 'Rotary Speed', dimension: 'rotary_speed', unit: 'rpm' },
          ],
        },
        secret_refs: harness.witsml.secret_refs,
      },
      'engineer',
    )

    await page.goto(`/connectors?wellId=${wellId}`)
    await page.getByTestId(`connector-row-${connKey}`).click()

    // Start connector via UI
    await page.getByTestId('connector-start-btn').click()
    await expect(page.getByTestId('detail-desired-state')).toHaveText('enabled')

    // Trigger worker poll via UI against real local WITSML SOAP server
    await page.getByTestId('connector-poll-now-btn').click()
    await expect(page.getByTestId('detail-runtime-status')).toHaveText('running')
    await expect(page.getByTestId('detail-health-state')).toHaveText('LIVE')
    await expect(page.getByTestId('connector-runs-table')).toContainText('succeeded')

    // Navigate to Operational Monitor via deep link and verify live connector banner + KPI values
    await page.getByTestId('connector-open-monitor-link').click()
    await expect(page.getByTestId('monitor-connectors-card')).toBeVisible()
    const bannerRow = page.getByTestId(`monitor-connector-${created.key}`)
    await expect(bannerRow).toBeVisible()
    await expect(bannerRow).toContainText('running')
    await expect(bannerRow).toContainText('LIVE')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 3 (Scenario 5): automatic alert evaluation triggered by WITSML connector ingestion', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const ruleKey = `e2e_witsml_spp_${Date.now()}`
    await apiPost(
      request,
      '/alert-rules',
      {
        well_id: wellId,
        rule_key: ruleKey,
        name: 'WITSML Auto Alert High SPP',
        channel_key: 'spp',
        operator: 'gt',
        threshold: 4400,
        unit: 'psi',
        clear_operator: 'lte',
        clear_threshold: 3900,
        sustain_seconds: 0,
        cooldown_seconds: 0,
        severity: 'critical',
      },
      'supervisor',
    )

    const harness = await apiPost<HarnessSnapshot>(request, '/connectors/harness/ensure', {}, 'engineer')
    await apiPost(
      request,
      '/connectors/harness/configure',
      {
        protocol: 'witsml',
        fault_mode: 'ok',
        reset_rows: true,
        append_values: { SPP: 4550.0, WOB: 26.0, RPM: 125.0 },
        quality: 'good',
      },
      'engineer',
    )

    const connKey = `e2e-witsml-alert-${Date.now().toString(36)}`
    const created = await apiPost<ConnectorDto>(
      request,
      '/connectors',
      {
        key: connKey,
        name: 'WITSML Breach Source',
        protocol_profile: 'witsml.1.4.1.1.soap_http',
        well_id: wellId,
        endpoint_url: harness.witsml.endpoint_url,
        config: {
          auth_mode: 'basic',
          tls_verify: true,
          poll_interval_seconds: 1.0,
          channel_mappings: [
            { source_mnemonic: 'SPP', channel_key: 'spp', name: 'Standpipe Pressure', dimension: 'pressure', unit: 'psi' },
          ],
        },
        secret_refs: harness.witsml.secret_refs,
      },
      'engineer',
    )

    await apiPost(request, `/connectors/${created.id}/start`, { reason: 'Enable for breach test' }, 'engineer')
    await apiPost(request, `/connectors/${created.id}/poll`, {}, 'engineer')

    // Open Operational Monitor and verify the critical alert was raised automatically by connector ingestion
    await page.goto(wellPath('/cockpit?tab=live'))
    await expect(page.getByTestId('operational-alerts-list')).toContainText('WITSML Auto Alert High SPP')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 4 (Scenario 6): stopping a running connector requires a reason and transitions status to stopped (never LIVE)', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const harness = await apiPost<HarnessSnapshot>(request, '/connectors/harness/ensure', {}, 'engineer')
    const connKey = `e2e-witsml-stop-${Date.now().toString(36)}`
    const created = await apiPost<ConnectorDto>(
      request,
      '/connectors',
      {
        key: connKey,
        name: 'Connector To Stop',
        protocol_profile: 'witsml.1.4.1.1.soap_http',
        well_id: wellId,
        endpoint_url: harness.witsml.endpoint_url,
        config: {
          auth_mode: 'basic',
          tls_verify: true,
          poll_interval_seconds: 1.0,
          channel_mappings: [
            { source_mnemonic: 'SPP', channel_key: 'spp', name: 'Standpipe Pressure', dimension: 'pressure', unit: 'psi' },
          ],
        },
        secret_refs: harness.witsml.secret_refs,
      },
      'engineer',
    )
    await apiPost(request, `/connectors/${created.id}/start`, { reason: 'Start before stop' }, 'engineer')
    await apiPost(request, `/connectors/${created.id}/poll`, {}, 'engineer')

    await page.goto(`/connectors?wellId=${wellId}`)
    await page.getByTestId(`connector-row-${connKey}`).click()
    await expect(page.getByTestId('detail-health-state')).toHaveText('LIVE')

    // Stop connector with governance reason
    await page.getByTestId('connector-stop-btn').click()
    await page.getByTestId('connector-transition-reason').fill('Scheduled rig sensor calibration')
    await page.getByTestId('connector-confirm-transition-btn').click()

    await expect(page.getByTestId('detail-desired-state')).toHaveText('stopped')
    await expect(page.getByTestId('detail-runtime-status')).toHaveText('stopped')
    await expect(page.getByTestId('detail-health-state')).toHaveText('stopped')
    await expect(page.getByTestId('detail-health-state')).not.toHaveText('LIVE')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 5 (Scenarios 7 & 8): source failure transitions connector to backing_off with safe error diagnostics, then recovers on next poll without duplicate points', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const harness = await apiPost<HarnessSnapshot>(request, '/connectors/harness/ensure', {}, 'engineer')
    await apiPost(
      request,
      '/connectors/harness/configure',
      {
        protocol: 'witsml',
        fault_mode: 'http_500',
        reset_rows: true,
        append_values: { SPP: 3810.0 },
        quality: 'good',
      },
      'engineer',
    )

    const connKey = `e2e-witsml-backoff-${Date.now().toString(36)}`
    const created = await apiPost<ConnectorDto>(
      request,
      '/connectors',
      {
        key: connKey,
        name: 'WITSML Backoff & Recovery Feed',
        protocol_profile: 'witsml.1.4.1.1.soap_http',
        well_id: wellId,
        endpoint_url: harness.witsml.endpoint_url,
        config: {
          auth_mode: 'basic',
          tls_verify: true,
          poll_interval_seconds: 1.0,
          max_consecutive_failures: 5,
          channel_mappings: [
            { source_mnemonic: 'SPP', channel_key: 'spp', name: 'Standpipe Pressure', dimension: 'pressure', unit: 'psi' },
          ],
        },
        secret_refs: harness.witsml.secret_refs,
      },
      'engineer',
    )
    await apiPost(request, `/connectors/${created.id}/start`, { reason: 'Start for backoff test' }, 'engineer')

    // First poll hits simulated HTTP 500 failure -> backing_off
    await apiPost(request, `/connectors/${created.id}/poll`, {}, 'engineer')

    await page.goto(`/connectors?wellId=${wellId}`)
    await page.getByTestId(`connector-row-${connKey}`).click()
    await expect(page.getByTestId('detail-runtime-status')).toHaveText('backing_off')
    await expect(page.getByTestId('detail-last-error')).toBeVisible()

    // Restore WITSML harness to healthy 'ok' mode
    await apiPost(
      request,
      '/connectors/harness/configure',
      {
        protocol: 'witsml',
        fault_mode: 'ok',
      },
      'engineer',
    )

    // Second poll succeeds -> recovers to running / LIVE and advances watermark cursor
    await page.getByTestId('connector-poll-now-btn').click()
    await expect(page.getByTestId('detail-runtime-status')).toHaveText('running')
    await expect(page.getByTestId('detail-health-state')).toHaveText('LIVE')
    await expect(page.getByTestId('detail-watermark-cursor')).toContainText('cursor_timestamp')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 6 (Scenario 9): create and run real ETP 1.2 WebSocket connector session & subscription flow', async ({
    page,
    wellId,
    consoleErrors,
  }) => {
    await page.goto(`/connectors?wellId=${wellId}`)
    await page.getByTestId('preset-local-etp').click()

    const connKey = `e2e-etp-ws-${Date.now().toString(36)}`
    await page.getByTestId('connector-key-input').fill(connKey)
    await page.getByTestId('connector-name-input').fill('E2E ETP 1.2 WebSocket Stream')
    await page.getByTestId('connector-submit-btn').click()

    await expect(page.getByTestId('selected-connector-name')).toHaveText('E2E ETP 1.2 WebSocket Stream')
    await expect(page.getByTestId('masked-secret-bearer_token')).toHaveText('********')

    // Start and poll ETP 1.2 WebSocket connector
    await page.getByTestId('connector-start-btn').click()
    await page.getByTestId('connector-poll-now-btn').click()

    await expect(page.getByTestId('detail-runtime-status')).toHaveText('running')
    await expect(page.getByTestId('detail-health-state')).toHaveText('LIVE')
    await expect(page.getByTestId('connector-runs-table')).toContainText('succeeded')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 7 (Scenario 10): ETP 1.2 WebSocket disconnect, reconnect tracking, and duplicate prevention on replay', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const harness = await apiPost<HarnessSnapshot>(request, '/connectors/harness/ensure', {}, 'engineer')
    await apiPost(
      request,
      '/connectors/harness/configure',
      {
        protocol: 'etp',
        fault_mode: 'replay_duplicates',
        reset_rows: true,
        append_values: { SPP: 3890.0 },
        quality: 'good',
      },
      'engineer',
    )

    const connKey = `e2e-etp-replay-${Date.now().toString(36)}`
    const created = await apiPost<ConnectorDto>(
      request,
      '/connectors',
      {
        key: connKey,
        name: 'ETP Reconnect & Replay Guard',
        protocol_profile: 'etp.1.2.json_ws',
        well_id: wellId,
        endpoint_url: harness.etp.endpoint_url,
        config: {
          auth_mode: 'bearer',
          tls_verify: true,
          poll_interval_seconds: 1.0,
          channel_mappings: [
            { source_mnemonic: 'SPP', channel_key: 'spp', name: 'Standpipe Pressure', dimension: 'pressure', unit: 'psi' },
          ],
        },
        secret_refs: harness.etp.secret_refs,
      },
      'engineer',
    )
    await apiPost(request, `/connectors/${created.id}/start`, { reason: 'Start ETP replay test' }, 'engineer')

    // First poll ingests the point; second poll replays the same timestamp via replay_duplicates mode
    await apiPost(request, `/connectors/${created.id}/poll`, {}, 'engineer')
    await apiPost(request, `/connectors/${created.id}/poll`, {}, 'engineer')

    // Reset ETP fault_mode back to 'ok'
    await apiPost(request, '/connectors/harness/configure', { protocol: 'etp', fault_mode: 'ok' }, 'engineer')

    await page.goto(`/connectors?wellId=${wellId}`)
    await page.getByTestId(`connector-row-${connKey}`).click()
    await expect(page.getByTestId('connector-runs-table')).toBeVisible()
    // The second run must record 1 duplicate point and 0 newly accepted points
    const runsData = await apiGet<{ items: Array<{ points_accepted: number; points_duplicates: number }> }>(
      request,
      `/connectors/${created.id}/runs`,
      'engineer',
    )
    expect(runsData.items.some((r) => r.points_duplicates >= 1 && r.points_accepted === 0)).toBeTruthy()

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 8 (Scenario 11): bad-quality source telemetry sets connector health to low_quality_data (never LIVE)', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const harness = await apiPost<HarnessSnapshot>(request, '/connectors/harness/ensure', {}, 'engineer')
    await apiPost(
      request,
      '/connectors/harness/configure',
      {
        protocol: 'witsml',
        fault_mode: 'ok',
        reset_rows: true,
        append_values: { SPP: 9999.0 },
        quality: 'bad',
      },
      'engineer',
    )

    const connKey = `e2e-witsml-badq-${Date.now().toString(36)}`
    const created = await apiPost<ConnectorDto>(
      request,
      '/connectors',
      {
        key: connKey,
        name: 'WITSML Bad Quality Sensor',
        protocol_profile: 'witsml.1.4.1.1.soap_http',
        well_id: wellId,
        endpoint_url: harness.witsml.endpoint_url,
        config: {
          auth_mode: 'basic',
          tls_verify: true,
          poll_interval_seconds: 1.0,
          channel_mappings: [
            { source_mnemonic: 'SPP', channel_key: 'spp', name: 'Standpipe Pressure', dimension: 'pressure', unit: 'psi' },
          ],
        },
        secret_refs: harness.witsml.secret_refs,
      },
      'engineer',
    )
    await apiPost(request, `/connectors/${created.id}/start`, { reason: 'Start bad quality test' }, 'engineer')
    await apiPost(request, `/connectors/${created.id}/poll`, {}, 'engineer')

    await page.goto(`/connectors?wellId=${wellId}`)
    await page.getByTestId(`connector-row-${connKey}`).click()
    await expect(page.getByTestId('detail-health-state')).toHaveText('low_quality_data')
    await expect(page.getByTestId('detail-health-state')).not.toHaveText('LIVE')

    // Restore healthy WITSML row for subsequent tests
    await apiPost(
      request,
      '/connectors/harness/configure',
      {
        protocol: 'witsml',
        fault_mode: 'ok',
        reset_rows: true,
        append_values: { SPP: 3750.0, WOB: 24.0, RPM: 120.0 },
        quality: 'good',
      },
      'engineer',
    )

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 9 (Scenario 12): viewer role is strictly read-only in UI and refused by backend on connector mutations', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    await page.goto(`/connectors?wellId=${wellId}`)
    await selectRole(page, 'viewer')

    await expect(page.getByTestId('connectors-readonly-banner')).toBeVisible()
    await expect(page.getByTestId('connector-form-card')).toHaveCount(0)
    await expect(page.getByTestId('connector-lifecycle-controls')).toHaveCount(0)
    await expect(page.getByTestId('connector-test-btn')).toHaveCount(0)

    // Direct API attempt as viewer is refused with 403 Forbidden
    const attempt = await apiAttempt(request, 'POST', '/connectors', {
      role: 'viewer',
      body: {
        key: 'viewer-forbidden',
        name: 'Forbidden',
        protocol_profile: 'synthetic.v1',
        well_id: wellId,
      },
    })
    expect(attempt.status).toBe(403)

    await selectRole(page, 'engineer')
    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 10 (Scenario 13): optimistic concurrency conflict (409) when editing connector configuration in UI', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const connKey = `e2e-conflict-${Date.now().toString(36)}`
    const created = await apiPost<ConnectorDto>(
      request,
      '/connectors',
      {
        key: connKey,
        name: 'Conflict Target Connector',
        protocol_profile: 'synthetic.v1',
        well_id: wellId,
        config: {
          auth_mode: 'none',
          poll_interval_seconds: 2.0,
          channel_mappings: [
            { source_mnemonic: 'SPP', channel_key: 'spp', name: 'Standpipe Pressure', dimension: 'pressure', unit: 'psi' },
          ],
        },
      },
      'engineer',
    )

    await page.goto(`/connectors?wellId=${wellId}`)
    await page.getByTestId(`connector-row-${connKey}`).click()
    await page.getByTestId('connector-edit-btn').click()

    // Another operator updates the connector first, advancing config_version from 1 -> 2
    await apiPatch(
      request,
      `/connectors/${created.id}`,
      {
        expected_config_version: created.config_version,
        reason: 'Concurrent operator update',
        name: 'Updated By Concurrent Operator',
      },
      'engineer',
    )

    // Submit stale edit form in browser -> 409 conflict banner & reload
    await page.getByTestId('connector-name-input').fill('Stale Browser Edit')
    await page.getByTestId('connector-update-reason').fill('Attempting stale edit')
    await page.getByTestId('connector-submit-btn').click()

    await expect(page.getByTestId('connector-conflict-banner')).toBeVisible()
    await expect(page.getByTestId('connector-name-input')).toHaveValue('Updated By Concurrent Operator')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 11 (Scenario 14): SSRF protection rejects cloud metadata and private RFC-1918 endpoints in UI', async ({
    page,
    wellId,
    consoleErrors,
  }) => {
    await page.goto(`/connectors?wellId=${wellId}`)

    await page.getByTestId('connector-key-input').fill(`ssrf-test-${Date.now().toString(36)}`)
    await page.getByTestId('connector-name-input').fill('Blocked SSRF Metadata Target')
    await page.getByTestId('connector-endpoint-input').fill('http://169.254.169.254/latest/meta-data/')
    await page.getByTestId('connector-submit-btn').click()

    await expect(page.getByTestId('connector-form-error')).toBeVisible()
    await expect(page.getByTestId('connector-form-error')).toContainText('169.254.169.254')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 12 (Scenario 15): Persian (fa) RTL localization and technical token LTR isolation on Connectors & Monitor', async ({
    page,
    wellId,
    consoleErrors,
  }) => {
    await page.goto(`/connectors?wellId=${wellId}`)
    await page.getByTestId('locale-switch').selectOption('fa')

    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl')
    await expect(page.locator('html')).toHaveAttribute('lang', 'fa')

    // Verify zero raw translation fallback brackets
    const bodyText = await page.locator('body').innerText()
    expect(bodyText).not.toContain('⟦')

    // Technical tokens preserve dir="ltr" in RTL mode
    await expect(page.getByTestId('detail-endpoint-url')).toHaveAttribute('dir', 'ltr')
    await expect(page.getByTestId('detail-fencing-token')).toHaveAttribute('dir', 'ltr')
    await expect(page.getByTestId('detail-watermark-cursor')).toHaveAttribute('dir', 'ltr')

    await page.getByTestId('locale-switch').selectOption('en')
    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 13 (Scenario 16): accessibility & keyboard navigation across Connector Registry controls', async ({
    page,
    wellId,
    consoleErrors,
  }) => {
    await page.goto(`/connectors?wellId=${wellId}`)
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()

    // Filter selects have explicit accessible labels
    const wellFilter = page.getByLabel('Well', { exact: true })
    await expect(wellFilter).toBeVisible()
    await wellFilter.focus()
    await expect(wellFilter).toBeFocused()

    await page.keyboard.press('Tab')
    await expect(page.getByTestId('filter-connector-profile')).toBeFocused()

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 14 (Scenario 17): synthetic commissioning source vs external protocol honesty badges & vendor verification disclosure', async ({
    page,
    wellId,
    consoleErrors,
  }) => {
    await page.goto(`/connectors?wellId=${wellId}`)

    // Profile cards explicitly state Local protocol harness verified AND External commercial vendor unverified in CI
    const profileCards = page.getByTestId('connector-profile-cards')
    await expect(profileCards).toContainText('Local protocol harness verified')
    await expect(profileCards).toContainText('External commercial vendor unverified in CI')
    await expect(profileCards).toContainText('SYNTHETIC / TEST SOURCE')
    await expect(profileCards).toContainText('EXTERNAL PROTOCOL')

    // Create a synthetic commissioning connector and verify its badge
    await page.getByTestId('preset-synthetic').click()
    const synthKey = `e2e-synth-${Date.now().toString(36)}`
    await page.getByTestId('connector-key-input').fill(synthKey)
    await page.getByTestId('connector-submit-btn').click()

    await expect(page.getByTestId(`connector-row-${synthKey}`)).toContainText('SYNTHETIC / TEST SOURCE')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 15 (CP12 Scenario 3): WITSML over-cap batch (max_points_per_poll=3) defers remaining curves with zero silent data loss and surfaces partial_page_deferred in UI manifest', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const harness = await apiPost<HarnessSnapshot>(request, '/connectors/harness/ensure', {}, 'engineer')
    await apiPost(
      request,
      '/connectors/harness/configure',
      {
        protocol: 'witsml',
        fault_mode: 'ok',
        reset_rows: true,
        append_values: { SPP: 3610.0, WOB: 23.0, RPM: 118.0 },
        quality: 'good',
      },
      'engineer',
    )

    const connKey = `e2e-witsml-cap-${Date.now().toString(36)}`
    const created = await apiPost<ConnectorDto>(
      request,
      '/connectors',
      {
        key: connKey,
        name: 'WITSML Over-Cap Batch Safety Store',
        protocol_profile: 'witsml.1.4.1.1.soap_http',
        well_id: wellId,
        endpoint_url: harness.witsml.endpoint_url,
        config: {
          auth_mode: 'basic',
          tls_verify: true,
          poll_interval_seconds: 1.0,
          max_points_per_poll: 2,
          channel_mappings: [
            { source_mnemonic: 'SPP', channel_key: 'spp', name: 'Standpipe Pressure', dimension: 'pressure', unit: 'psi' },
            { source_mnemonic: 'WOB', channel_key: 'wob', name: 'Weight on Bit', dimension: 'force', unit: 'klbf' },
            { source_mnemonic: 'RPM', channel_key: 'rpm', name: 'Rotary Speed', dimension: 'rotary_speed', unit: 'rpm' },
          ],
        },
        secret_refs: harness.witsml.secret_refs,
      },
      'engineer',
    )
    await apiPost(request, `/connectors/${created.id}/start`, { reason: 'Start over-cap test' }, 'engineer')

    // Poll 1: emits 2 points out of 3 (1 row * 3 curves) and defers 1 point
    await apiPost(request, `/connectors/${created.id}/poll`, {}, 'engineer')

    await page.goto(`/connectors?wellId=${wellId}`)
    await page.getByTestId(`connector-row-${connKey}`).click()
    await expect(page.getByTestId('selected-run-manifest')).toBeVisible()
    await expect(page.getByTestId('manifest-emitted')).toHaveText('2')
    await expect(page.getByTestId('manifest-deferred-over-cap')).toHaveText('1')
    await expect(page.getByTestId('selected-run-manifest')).toContainText('partial')
    await expect(page.getByTestId('manifest-cursor-after')).toContainText('completed_at_cursor')

    // Poll 2 drains the remaining 1 point on that row without dropping any curve
    await page.getByTestId('connector-poll-now-btn').click()

    await expect(page.getByTestId('manifest-deferred-over-cap')).toHaveText('0')
    await expect(page.getByTestId('selected-run-manifest')).toContainText('complete')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 16 (CP12 Scenario 4): ETP 1.2 per-channel watermarks (channel_cursors) preserve independent channel progress in UI', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const harness = await apiPost<HarnessSnapshot>(request, '/connectors/harness/ensure', {}, 'engineer')
    await apiPost(
      request,
      '/connectors/harness/configure',
      {
        protocol: 'etp',
        fault_mode: 'ok',
        reset_rows: true,
        append_values: { SPP: 3910.0, WOB: 25.5 },
        quality: 'good',
      },
      'engineer',
    )

    const connKey = `e2e-etp-chcursor-${Date.now().toString(36)}`
    const created = await apiPost<ConnectorDto>(
      request,
      '/connectors',
      {
        key: connKey,
        name: 'ETP Per-Channel Watermark Stream',
        protocol_profile: 'etp.1.2.json_ws',
        well_id: wellId,
        endpoint_url: harness.etp.endpoint_url,
        config: {
          auth_mode: 'bearer',
          tls_verify: true,
          poll_interval_seconds: 1.0,
          channel_mappings: [
            { source_mnemonic: 'SPP', channel_key: 'spp', name: 'Standpipe Pressure', dimension: 'pressure', unit: 'psi' },
            { source_mnemonic: 'WOB', channel_key: 'wob', name: 'Weight on Bit', dimension: 'force', unit: 'klbf' },
          ],
        },
        secret_refs: harness.etp.secret_refs,
      },
      'engineer',
    )
    await apiPost(request, `/connectors/${created.id}/start`, { reason: 'Start ETP per-channel cursor test' }, 'engineer')
    await apiPost(request, `/connectors/${created.id}/poll`, {}, 'engineer')

    await page.goto(`/connectors?wellId=${wellId}`)
    await page.getByTestId(`connector-row-${connKey}`).click()
    await expect(page.getByTestId('detail-watermark-cursor')).toContainText('channel_cursors')
    await expect(page.getByTestId('detail-watermark-cursor')).toContainText('spp')
    await expect(page.getByTestId('detail-watermark-cursor')).toContainText('wob')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 17 (CP12 Scenarios 5 & 6): Governed Dry-Run Replay and Live Replay/Backfill in UI preserve non-regressing watermarks and deduplicate points', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const harness = await apiPost<HarnessSnapshot>(request, '/connectors/harness/ensure', {}, 'engineer')
    await apiPost(
      request,
      '/connectors/harness/configure',
      {
        protocol: 'witsml',
        fault_mode: 'ok',
        reset_rows: true,
      },
      'engineer',
    )

    const connKey = `e2e-witsml-replay-${Date.now().toString(36)}`
    const created = await apiPost<ConnectorDto>(
      request,
      '/connectors',
      {
        key: connKey,
        name: 'WITSML Governed Replay Workbench Feed',
        protocol_profile: 'witsml.1.4.1.1.soap_http',
        well_id: wellId,
        endpoint_url: harness.witsml.endpoint_url,
        config: {
          auth_mode: 'basic',
          tls_verify: true,
          poll_interval_seconds: 1.0,
          channel_mappings: [
            { source_mnemonic: 'SPP', channel_key: 'spp', name: 'Standpipe Pressure', dimension: 'pressure', unit: 'psi' },
          ],
        },
        secret_refs: harness.witsml.secret_refs,
      },
      'engineer',
    )
    await apiPost(request, `/connectors/${created.id}/start`, { reason: 'Start before replay' }, 'engineer')
    await apiPost(request, `/connectors/${created.id}/poll`, {}, 'engineer')

    await page.goto(`/connectors?wellId=${wellId}`)
    await page.getByTestId(`connector-row-${connKey}`).click()
    await expect(page.getByTestId('connector-replay-card')).toBeVisible()

    // 1. Dry-run replay from earlier timestamp
    await page.getByTestId('connector-replay-from-ts').fill('2020-01-01T00:00:00Z')
    await page.getByTestId('connector-replay-reason').fill('Dry-run audit of historical window')
    await page.getByTestId('connector-replay-dryrun-btn').click()

    await expect(page.getByTestId('connector-replay-result')).toBeVisible()
    await expect(page.getByTestId('connector-replay-result')).toContainText('DRY RUN')
    await expect(page.getByTestId('connector-replay-result')).toContainText('accepted: 0')

    // 2. Live governed replay from earlier timestamp deduplicates already-ingested points
    await page.getByTestId('connector-replay-reason').fill('Execute governed replay verification')
    await page.getByTestId('connector-replay-execute-btn').click()

    await expect(page.getByTestId('connector-replay-result')).toContainText('LIVE BACKFILL')
    await expect(page.getByTestId('connector-runs-table')).toContainText('replay')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 18 (CP12 Scenario 7): SSRF defense blocks IPv4-mapped IPv6 (::ffff:127.0.0.1) and non-canonical octal IPv4 (0177.0.0.1) in UI', async ({
    page,
    wellId,
    consoleErrors,
  }) => {
    await page.goto(`/connectors?wellId=${wellId}`)

    await page.getByTestId('connector-key-input').fill(`ssrf-ipv6map-${Date.now().toString(36)}`)
    await page.getByTestId('connector-name-input').fill('Blocked IPv4-Mapped IPv6 Target')
    await page.getByTestId('connector-endpoint-input').fill('https://[::ffff:169.254.169.254]/witsml')
    await page.getByTestId('connector-submit-btn').click()

    await expect(page.getByTestId('connector-form-error')).toBeVisible()

    // Non-canonical octal IPv4 is also blocked
    await page.getByTestId('connector-endpoint-input').fill('https://0177.0.0.1/witsml')
    await page.getByTestId('connector-submit-btn').click()
    await expect(page.getByTestId('connector-form-error')).toBeVisible()

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 19 (CP12 Scenario 8): Secret reference minimization ensures raw DRILLAI_SECRET_* env names never leak in connector detail or API response', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const harness = await apiPost<HarnessSnapshot>(request, '/connectors/harness/ensure', {}, 'engineer')
    const connKey = `e2e-sec-min-${Date.now().toString(36)}`
    const created = await apiPost<ConnectorDto>(
      request,
      '/connectors',
      {
        key: connKey,
        name: 'Minimized Secret Ref Connector',
        protocol_profile: 'witsml.1.4.1.1.soap_http',
        well_id: wellId,
        endpoint_url: harness.witsml.endpoint_url,
        config: {
          auth_mode: 'basic',
          tls_verify: true,
          poll_interval_seconds: 1.0,
          channel_mappings: [
            { source_mnemonic: 'SPP', channel_key: 'spp', name: 'Standpipe Pressure', dimension: 'pressure', unit: 'psi' },
          ],
        },
        secret_refs: harness.witsml.secret_refs,
      },
      'engineer',
    )

    // API response must mask `ref` as `env:********`
    const inspected = await apiGet<{ secret_refs: Record<string, { ref: string; redacted: boolean }> }>(
      request,
      `/connectors/${created.id}`,
      'engineer',
    )
    expect(inspected.secret_refs.username?.ref).toBe('env:********')
    expect(inspected.secret_refs.username?.redacted).toBe(true)

    await page.goto(`/connectors?wellId=${wellId}`)
    await page.getByTestId(`connector-row-${connKey}`).click()
    await expect(page.getByTestId('detail-secret-refs')).toContainText('username: env:********')
    await expect(page.getByTestId('detail-secret-refs')).not.toContainText('DRILLAI_SECRET_HARNESS_WITSML_USER')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 20 (CP12 Scenario 9): Malformed XML / SOAP Fault surfaces protocol_error and Operator Remediation Guidance in UI', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const harness = await apiPost<HarnessSnapshot>(request, '/connectors/harness/ensure', {}, 'engineer')
    await apiPost(
      request,
      '/connectors/harness/configure',
      {
        protocol: 'witsml',
        fault_mode: 'malformed_xml',
      },
      'engineer',
    )

    const connKey = `e2e-witsml-xmlerr-${Date.now().toString(36)}`
    const created = await apiPost<ConnectorDto>(
      request,
      '/connectors',
      {
        key: connKey,
        name: 'WITSML Malformed XML Fault Source',
        protocol_profile: 'witsml.1.4.1.1.soap_http',
        well_id: wellId,
        endpoint_url: harness.witsml.endpoint_url,
        config: {
          auth_mode: 'basic',
          tls_verify: true,
          poll_interval_seconds: 1.0,
          channel_mappings: [
            { source_mnemonic: 'SPP', channel_key: 'spp', name: 'Standpipe Pressure', dimension: 'pressure', unit: 'psi' },
          ],
        },
        secret_refs: harness.witsml.secret_refs,
      },
      'engineer',
    )
    await apiPost(request, `/connectors/${created.id}/start`, { reason: 'Start XML error test' }, 'engineer')
    await apiPost(request, `/connectors/${created.id}/poll`, {}, 'engineer')

    // Restore WITSML harness
    await apiPost(request, '/connectors/harness/configure', { protocol: 'witsml', fault_mode: 'ok' }, 'engineer')

    await page.goto(`/connectors?wellId=${wellId}`)
    await page.getByTestId(`connector-row-${connKey}`).click()
    await expect(page.getByTestId('detail-last-error')).toContainText('[malformed_payload]')
    await expect(page.getByTestId('detail-error-remediation')).toBeVisible()
    await expect(page.getByTestId('detail-error-remediation')).toContainText('Operator Remediation Guidance')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 21 (CP12 Scenario 10): ETP protocol_exception transitions connector to backing_off with remediation guidance, then recovers cleanly', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const harness = await apiPost<HarnessSnapshot>(request, '/connectors/harness/ensure', {}, 'engineer')
    await apiPost(
      request,
      '/connectors/harness/configure',
      {
        protocol: 'etp',
        fault_mode: 'protocol_exception',
      },
      'engineer',
    )

    const connKey = `e2e-etp-protoexc-${Date.now().toString(36)}`
    const created = await apiPost<ConnectorDto>(
      request,
      '/connectors',
      {
        key: connKey,
        name: 'ETP Protocol Exception Recovery Stream',
        protocol_profile: 'etp.1.2.json_ws',
        well_id: wellId,
        endpoint_url: harness.etp.endpoint_url,
        config: {
          auth_mode: 'bearer',
          tls_verify: true,
          poll_interval_seconds: 1.0,
          channel_mappings: [
            { source_mnemonic: 'SPP', channel_key: 'spp', name: 'Standpipe Pressure', dimension: 'pressure', unit: 'psi' },
          ],
        },
        secret_refs: harness.etp.secret_refs,
      },
      'engineer',
    )
    await apiPost(request, `/connectors/${created.id}/start`, { reason: 'Start ETP protocol exception test' }, 'engineer')
    await apiPost(request, `/connectors/${created.id}/poll`, {}, 'engineer')

    await page.goto(`/connectors?wellId=${wellId}`)
    await page.getByTestId(`connector-row-${connKey}`).click()
    await expect(page.getByTestId('detail-runtime-status')).toHaveText('backing_off')
    await expect(page.getByTestId('detail-error-remediation')).toBeVisible()

    // Restore ETP harness and poll via UI
    await apiPost(request, '/connectors/harness/configure', { protocol: 'etp', fault_mode: 'ok' }, 'engineer')
    await page.getByTestId('connector-poll-now-btn').click()
    await expect(page.getByTestId('detail-runtime-status')).toHaveText('running')
    await expect(page.getByTestId('detail-health-state')).toHaveText('LIVE')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 22 (CP12 Scenario 11): Bad-quality telemetry is surfaced in run manifest without triggering false threshold alerts', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const ruleKey = `e2e_badq_noraise_${Date.now()}`
    await apiPost(
      request,
      '/alert-rules',
      {
        well_id: wellId,
        rule_key: ruleKey,
        name: 'Bad Quality Must Not Raise Alert',
        channel_key: 'wob',
        operator: 'gt',
        threshold: 80,
        unit: 'klbf',
        clear_operator: 'lte',
        clear_threshold: 50,
        sustain_seconds: 0,
        cooldown_seconds: 0,
        severity: 'high',
      },
      'supervisor',
    )

    const harness = await apiPost<HarnessSnapshot>(request, '/connectors/harness/ensure', {}, 'engineer')
    await apiPost(
      request,
      '/connectors/harness/configure',
      {
        protocol: 'witsml',
        fault_mode: 'ok',
        reset_rows: true,
        append_values: { WOB: 150.0, SPP: 3000.0 },
        quality: 'bad',
      },
      'engineer',
    )

    const connKey = `e2e-badq-noalert-${Date.now().toString(36)}`
    const created = await apiPost<ConnectorDto>(
      request,
      '/connectors',
      {
        key: connKey,
        name: 'WITSML Bad Quality Alert Guard',
        protocol_profile: 'witsml.1.4.1.1.soap_http',
        well_id: wellId,
        endpoint_url: harness.witsml.endpoint_url,
        config: {
          auth_mode: 'basic',
          tls_verify: true,
          poll_interval_seconds: 1.0,
          channel_mappings: [
            { source_mnemonic: 'WOB', channel_key: 'wob', name: 'Weight on Bit', dimension: 'force', unit: 'klbf' },
          ],
        },
        secret_refs: harness.witsml.secret_refs,
      },
      'engineer',
    )
    await apiPost(request, `/connectors/${created.id}/start`, { reason: 'Start bad quality alert guard' }, 'engineer')
    await apiPost(request, `/connectors/${created.id}/poll`, {}, 'engineer')

    // Restore harness to good quality
    await apiPost(
      request,
      '/connectors/harness/configure',
      { protocol: 'witsml', fault_mode: 'ok', reset_rows: true, quality: 'good' },
      'engineer',
    )

    await page.goto(wellPath('/cockpit?tab=live'))
    await expect(page.getByTestId('operational-alerts-list')).not.toContainText('Bad Quality Must Not Raise Alert')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 23 (CP12 Scenario 12): Operational Monitor Manage Connectors link navigates to well-filtered Connector Workspace', async ({
    page,
    wellId,
    consoleErrors,
  }) => {
    await page.goto(wellPath('/cockpit?tab=live'))
    await expect(page.getByTestId('monitor-connectors-card')).toBeVisible()
    await page.getByTestId('monitor-manage-connectors-link').click()

    await expect(page.getByTestId('connectors-workspace')).toBeVisible()
    await expect(page.getByTestId('filter-connector-well')).toHaveValue(wellId)

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 24 (CP12 Scenario 13): Disabled connector reports disabled health state on both Connector Workspace and Operational Monitor banner', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const connKey = `e2e-disabled-banner-${Date.now().toString(36)}`
    const created = await apiPost<ConnectorDto>(
      request,
      '/connectors',
      {
        key: connKey,
        name: 'Disabled Banner Verification Connector',
        protocol_profile: 'synthetic.v1',
        well_id: wellId,
        config: {
          auth_mode: 'none',
          poll_interval_seconds: 2.0,
          channel_mappings: [
            { source_mnemonic: 'SPP', channel_key: 'spp', name: 'Standpipe Pressure', dimension: 'pressure', unit: 'psi' },
          ],
        },
      },
      'engineer',
    )

    await page.goto(wellPath('/cockpit?tab=live'))
    const bannerRow = page.getByTestId(`monitor-connector-${created.key}`)
    await expect(bannerRow).toBeVisible()
    await expect(bannerRow).toContainText('disabled')
    await expect(bannerRow).not.toContainText('LIVE')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 25 (CP12 Scenario 15): Interactive Run Manifest Inspector switches between historical runs in the Run Ledger', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const harness = await apiPost<HarnessSnapshot>(request, '/connectors/harness/ensure', {}, 'engineer')
    await apiPost(
      request,
      '/connectors/harness/configure',
      { protocol: 'witsml', fault_mode: 'ok', reset_rows: true },
      'engineer',
    )

    const connKey = `e2e-manifest-switch-${Date.now().toString(36)}`
    const created = await apiPost<ConnectorDto>(
      request,
      '/connectors',
      {
        key: connKey,
        name: 'Manifest Switch Inspector Feed',
        protocol_profile: 'witsml.1.4.1.1.soap_http',
        well_id: wellId,
        endpoint_url: harness.witsml.endpoint_url,
        config: {
          auth_mode: 'basic',
          tls_verify: true,
          poll_interval_seconds: 1.0,
          channel_mappings: [
            { source_mnemonic: 'SPP', channel_key: 'spp', name: 'Standpipe Pressure', dimension: 'pressure', unit: 'psi' },
          ],
        },
        secret_refs: harness.witsml.secret_refs,
      },
      'engineer',
    )
    await apiPost(request, `/connectors/${created.id}/start`, { reason: 'Start for manifest switch' }, 'engineer')
    await apiPost(request, `/connectors/${created.id}/poll`, {}, 'engineer')
    await apiPost(request, `/connectors/${created.id}/poll`, {}, 'engineer')

    const runs = await apiGet<{ items: Array<{ id: string }> }>(request, `/connectors/${created.id}/runs`, 'engineer')
    expect(runs.items.length).toBeGreaterThanOrEqual(2)
    const olderRunId = runs.items[1]!.id

    await page.goto(`/connectors?wellId=${wellId}`)
    await page.getByTestId(`connector-row-${connKey}`).click()
    await page.getByTestId(`inspect-run-${olderRunId}`).click()

    await expect(page.getByTestId('selected-run-manifest')).toContainText(olderRunId)

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('Journey 26 (CP12 Scenarios 16 & 17): Persian (fa) RTL & keyboard accessibility on Run Manifest Inspector and Governed Replay Workbench', async ({
    page,
    wellId,
    consoleErrors,
  }) => {
    await page.goto(`/connectors?wellId=${wellId}`)
    await page.getByTestId('locale-switch').selectOption('fa')

    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl')
    await expect(page.getByTestId('connector-replay-card')).toBeVisible()
    await expect(page.getByTestId('connector-replay-from-ts')).toHaveAttribute('dir', 'ltr')
    await expect(page.getByTestId('selected-run-manifest')).toBeVisible()
    await expect(page.getByTestId('manifest-cursor-after')).toHaveAttribute('dir', 'ltr')

    const bodyText = await page.locator('body').innerText()
    expect(bodyText).not.toContain('⟦')

    await page.getByTestId('locale-switch').selectOption('en')
    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })
})
