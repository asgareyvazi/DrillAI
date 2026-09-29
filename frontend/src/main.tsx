import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { setIdentity, shouldRetryRequest } from './api/client'
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
      // Classified once, in the API client, and shared with the retry button — so the automatic
      // behaviour and the manual one can never disagree about whether asking again could help.
      retry: shouldRetryRequest,
    },
  },
})

// The identity header is set from the session store before the first render, and updated whenever
// the user switches identity. A configured token always wins over the development header.
setIdentity({ devRoles: useSession.getState().devRoles })
useSession.subscribe((state) => {
  setIdentity({ devRoles: state.devRoles })
  // Every cached answer was produced for the previous identity — a well list, a capability set, a
  // queue of approvals — so nothing in the cache is answered by the right authority any more.
  // Invalidating refetches what is on screen immediately; the rest is dropped on its next mount.
  void queryClient.invalidateQueries()
})

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
