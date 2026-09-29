import { QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { setIdentity } from './api/client'
import { queryClient } from './api/queryClient'
import { App } from './App'
import { I18nProvider } from './i18n'
import { useSession } from './stores/session'
import './styles.css'

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
