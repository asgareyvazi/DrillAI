/**
 * The application's one query client.
 *
 * It lives here rather than inline in the entry point so its policy can be asserted: the retry rule is
 * shared with the retry button (so the automatic behaviour and the manual one can never disagree about
 * whether asking again could help), and the network mode decides whether a read is even attempted
 * during an outage.
 */

import { QueryClient } from '@tanstack/react-query'
import { shouldRetryRequest } from './client'

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Engineering data is expensive to recompute server-side and cheap to hold: keep it fresh for
        // a minute.
        staleTime: 30_000,
        refetchOnWindowFocus: false,
        // Classified once, in the API client.
        retry: shouldRetryRequest,
        // Attempt the request even when the browser reports no network, instead of holding it until
        // the connection comes back. The default ("online") *pauses* the read, which leaves the screen
        // on its loading state for as long as the outage lasts: an operator sees a spinner where the
        // product should say the backend is unreachable, and nothing can be retried because nothing
        // was ever sent. Sending it means the transport classifies the failure like any other, and the
        // error state can say what happened and offer a retry.
        networkMode: 'always',
      },
    },
  })
}

export const queryClient = createQueryClient()
