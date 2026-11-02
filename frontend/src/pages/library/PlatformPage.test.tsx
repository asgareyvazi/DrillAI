import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
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
    expect(screen.getByText('Tesseract is missing configured language data: fas')).toBeInTheDocument()
  })

  it('localizes runtime state in Persian and keeps language identifiers LTR within RTL content', async () => {
    localStorage.setItem('drillai.locale', 'fa')
    renderPlatformPage()

    expect(await screen.findByText('دارای افت آمادگی')).toBeInTheDocument()
    expect(screen.getByText('زمان‌اجرای OCR')).toBeInTheDocument()
    expect(screen.getByText('زمان‌اجرا در دسترس نیست')).toBeInTheDocument()
    expect(screen.getByText('eng, fas')).toHaveAttribute('dir', 'ltr')
    expect(document.documentElement.dir).toBe('rtl')
  })
})
