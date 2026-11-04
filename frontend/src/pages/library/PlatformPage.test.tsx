import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '../../i18n'
import PlatformPage from './PlatformPage'

const api = vi.hoisted(() => ({
  capabilities: vi.fn(),
  integrations: vi.fn(),
  providers: vi.fn(),
  version: vi.fn(),
  healthReady: vi.fn(),
  identity: vi.fn(),
  notificationOutbox: vi.fn(),
  retryNotification: vi.fn(),
}))

vi.mock('../../api/endpoints', () => ({ drillingApi: api }))

function renderPlatformPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <I18nProvider>
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      </I18nProvider>
    )
  }
  return render(<PlatformPage />, { wrapper: Wrapper })
}

beforeEach(() => {
  api.capabilities.mockReset().mockResolvedValue({
    engines: { total: 14, by_category: { drilling: 14 } },
    node_types: 1,
    tools: 1,
    actions: 1,
    context_sections: 1,
    extractors: 1,
    agents: 1,
    skills: 1,
    task_profiles: ['analysis'],
    authorization: { auth_enabled: true },
  })
  api.integrations.mockReset().mockResolvedValue({ items: [] })
  api.providers.mockReset().mockResolvedValue({ items: [], configured_provider: 'stub' })
  api.version.mockReset().mockResolvedValue({ api_version: '1.0.0', environment: 'test' })
  api.identity.mockReset().mockResolvedValue({ permissions: [], max_action_level: 'L0' })
  api.notificationOutbox.mockReset().mockResolvedValue({ items: [], limit: 50 })
  api.retryNotification.mockReset().mockResolvedValue({ requeued: true, item: {} })
  api.healthReady.mockReset().mockResolvedValue({
    status: 'degraded',
    checks: {
      ocr: {
        enabled: true,
        status: 'blocked',
        runtime_status: 'blocked',
        runtime_ready: false,
        configured_languages: ['eng', 'fas'],
        reason: 'Tesseract is missing configured language data: fas',
        quality_evaluated: false,
      },
    },
  })
})

describe('PlatformPage OCR runtime status', () => {
  it('distinguishes a degraded runtime from ready and does not label availability as quality', async () => {
    localStorage.removeItem('drillai.locale')
    renderPlatformPage()

    expect(await screen.findByText('Degraded')).toBeInTheDocument()
    expect(screen.getByText('OCR runtime')).toBeInTheDocument()
    expect(screen.getByText('Runtime unavailable')).toBeInTheDocument()
    expect(screen.getByText((_text, element) =>
      element?.tagName === 'P' && element.textContent?.includes('Configured languages: eng, fas') === true,
    )).toBeInTheDocument()
    expect(screen.getByText('Runtime availability does not measure OCR accuracy or engineering correctness.')).toBeInTheDocument()
    expect(screen.getByText('Run the local synthetic canary in this backend environment to check that Tesseract returns text. It is not an accuracy benchmark.')).toBeInTheDocument()
    expect(screen.getByText('python -m drillai.ingestion.ocr_runtime --self-test')).toHaveAttribute('dir', 'ltr')
    expect(screen.getByText('Tesseract is missing configured language data: fas')).toBeInTheDocument()
  })

  it('requires duplicate-risk acknowledgement and records manual retry intent without claiming delivery', async () => {
    localStorage.removeItem('drillai.locale')
    const row = {
      id: 'obm_ambiguous_1',
      channel: 'webhook',
      status: 'reconciliation_required',
      attempts: 2,
      max_attempts: 3,
      next_attempt_at: null,
      accepted_at: null,
      acceptance_evidence: null,
      provider_message_id: null,
      delivery_confirmed: false,
      external_provider_verified: false,
      recipient_present: true,
      workflow_run_id: null,
      well_id: null,
      last_error_code: 'stale_sending_ambiguous',
      last_attempt_at: '2026-11-03T12:00:00Z',
      created_at: '2026-11-03T11:55:00Z',
    }
    api.notificationOutbox.mockResolvedValue({ items: [row], limit: 50 })
    api.identity.mockResolvedValue({ permissions: ['integration.manage'], max_action_level: 'L4' })
    api.retryNotification.mockResolvedValue({ requeued: true, item: { ...row, status: 'queued' } })
    renderPlatformPage()

    expect(await screen.findByText('Manual reconciliation required')).toBeInTheDocument()
    expect(screen.getByText('Transport acceptance is not recipient delivery.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Review retry' }))
    const reason = screen.getByLabelText('Audit reason (10–500 characters)')
    fireEvent.change(reason, { target: { value: 'Operator reviewed ambiguity' } })
    const acknowledgement = screen.getByLabelText(
      'I acknowledge that the earlier attempt may have reached the transport and requeueing could produce a duplicate.',
    )
    fireEvent.click(acknowledgement)
    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge and requeue' }))

    await waitFor(() => expect(api.retryNotification).toHaveBeenCalledWith(
      'obm_ambiguous_1',
      { reason: 'Operator reviewed ambiguity', acknowledge_duplicate_risk: true },
    ))
  })

  it('localizes runtime state in Persian and keeps language identifiers LTR within RTL content', async () => {
    localStorage.setItem('drillai.locale', 'fa')
    renderPlatformPage()

    expect(await screen.findByText('دارای افت آمادگی')).toBeInTheDocument()
    expect(screen.getByText('زمان‌اجرای OCR')).toBeInTheDocument()
    expect(screen.getByText('زمان‌اجرا در دسترس نیست')).toBeInTheDocument()
    expect(screen.getByText('eng, fas')).toHaveAttribute('dir', 'ltr')
    expect(screen.getByText('python -m drillai.ingestion.ocr_runtime --self-test')).toHaveAttribute('dir', 'ltr')
    expect(screen.getByText('برای بررسی اینکه Tesseract متنی برمی‌گرداند، آزمون مصنوعی محلی را در همین محیط backend اجرا کنید. این آزمون معیار سنجش دقت نیست.')).toBeInTheDocument()
    expect(document.documentElement.dir).toBe('rtl')
  })
})
