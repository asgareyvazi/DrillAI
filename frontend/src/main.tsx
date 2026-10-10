import { QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { queryClient } from './api/queryClient'
import { App } from './App'
import { I18nProvider } from './i18n'
import { installIdentityBridge } from './stores/identityBridge'
import './styles.css'

// Identity is wired before the first render: the header a read carries, the event stream, and the
// cache all have to agree on who is asking. See `stores/identityBridge.ts` for why a switch replaces
// the cache instead of invalidating it.
installIdentityBridge()

createRoot(document.getElementById('root') as HTMLElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </I18nProvider>
    </QueryClientProvider>
  </StrictMode>,
)
