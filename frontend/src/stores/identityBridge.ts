/**
 * The one place where changing identity is made effective.
 *
 * Switching identity is not a display change: three separate pieces of the running application hold
 * a copy of who is asking, and all three have to move together.
 *
 * 1. **The outgoing requests.** `setIdentity` updates what the API client puts in `X-Dev-Roles` (or
 *    in the bearer token) on the *next* request. Anything already in flight keeps the identity it was
 *    sent with, which is correct — it was a legal question when it was asked.
 * 2. **The event stream.** The socket is a second transport with its own handshake, and it carries
 *    the identity in its URL. The stream layer subscribes to the same identity listeners this file
 *    touches, closes the old socket, and opens a new one carrying the new identity, so events
 *    addressed to the previous principal cannot arrive on a screen that is no longer that principal.
 * 3. **The cache.** Every cached answer was produced *for* the previous identity — a well list, a
 *    capability set, a queue of approvals — and the switch has to do two things at once, not one:
 *
 *    * *nothing read as the previous principal stays readable*, and
 *    * *the screens on display read again* under the new principal.
 *
 *    `invalidateQueries()` does only the second: the old answer stays on screen for the whole round
 *    trip, which is the defect. `clear()` does only the first: the answers go, and a query that is
 *    already mounted has nothing to invalidate, so it is never asked again and the screen is left
 *    empty. `resetQueries()` is the operation that means both — each query returns to its initial
 *    state (its data is dropped, and an in-flight fetch started under the previous identity is
 *    cancelled rather than allowed to land afterwards) and the *active* queries are then fetched
 *    again. Those fetches are issued after `setIdentity`, so they carry the new identity.
 *
 * Selective invalidation would need a set of query keys that are identity-independent. There is none:
 * every endpoint declares a permission the server evaluates per request, so *any* read may
 * legitimately answer differently — or refuse — for a different principal. The safe partition is all
 * of them.
 *
 * The subscription is deliberately about `devRoles` alone. It used to run on every store change, so
 * choosing oilfield units instead of SI dropped the entire cache: a display preference is not an
 * identity, and losing a screen's worth of engineering data for one would be a defect in its own right.
 *
 * `client` is a parameter so the behaviour can be tested against a real query client; the entry point
 * passes the application's single instance.
 */

import type { QueryClient } from '@tanstack/react-query'
import { setIdentity } from '../api/client'
import { queryClient } from '../api/queryClient'
import { useSession } from './session'

export interface IdentityBridge {
  /** The current development-role selection, before any switch. */
  readonly initial: string
  /** Stop observing the session store. */
  dispose: () => void
}

/**
 * Makes the session store's identity selection effective, now and on every later change.
 *
 * Returns the initial selection and a disposer (the disposer is used by tests; the application keeps
 * the bridge for the lifetime of the page).
 */
export function installIdentityBridge(client: QueryClient = queryClient): IdentityBridge {
  // Before the first render: a read issued in the first paint must already carry the session's
  // identity, not the client's built-in default.
  const initial = useSession.getState().devRoles
  setIdentity({ devRoles: initial })

  let current = initial
  const dispose = useSession.subscribe((state) => {
    if (state.devRoles === current) return
    current = state.devRoles
    setIdentity({ devRoles: state.devRoles })
    /*
     * `resetQueries()` resets every cached query — active and idle alike — and refetches the active
     * ones. The reset destroys each query first, which cancels an in-flight fetch silently: a response
     * that was computed for the previous identity can therefore never be written into the cache after
     * the switch, which is the one window a mere reset of the displayed data would leave open.
     */
    void client.resetQueries()
  })

  return { initial, dispose }
}
