/**
 * Who the interface is acting as, and what the server says that identity may do.
 *
 * The read is keyed on the development-role selection, which is the only thing about the client that
 * can change the answer: two different selections are two different principals, and one key for both
 * of them would hand the second principal the first one's permissions. The key is shared with every
 * other consumer of the same question (the shell, the workflow studio, the action gates), so opening
 * a page does not ask the server who the user is a second time.
 *
 * This hook reports; it never decides. `permissions` and `max_action_level` come from
 * `/platform/identity`, which is the server's own summary of what it will enforce per request.
 */

import { useQuery } from '@tanstack/react-query'
import { drillingApi } from '../api/endpoints'
import { useSession } from '../stores/session'

export function useIdentity() {
  const devRoles = useSession((state) => state.devRoles)
  return useQuery({
    queryKey: ['identity', devRoles],
    queryFn: ({ signal }) => drillingApi.identity(signal),
  })
}
