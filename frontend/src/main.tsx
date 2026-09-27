import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { ApiError, setIdentity } from './api/client'
import { App } from './App'
import { I18nProvider } from './i18n'
import { useSession } from './stores/session'
import './styles.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Engineering data is expensive to recompute server-side and cheap to hold: keep it fresh for
      // a minute and do not retry a request the server explicitly refused.
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: (failureCount, error) => {
        if (error instanceof ApiError && (error.isUnauthorized || error.isNotFound || error.isValidation)) {
          return false
        }
        return failureCount < 2
      },
    },
  },
})

// The identity header is set from the session store before the first render, and updated whenever
// the user switches identity. A configured token always wins over the development header.
setIdentity({ devRoles: useSession.getState().devRoles })
useSession.subscribe((state) => setIdentity({ devRoles: state.devRoles }))

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
