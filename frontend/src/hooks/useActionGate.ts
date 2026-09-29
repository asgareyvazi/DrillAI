/**
 * Whether an action is worth offering, and if not, which of the server's reasons applies.
 *
 * The server answers this question twice: once when the screen is drawn (`/platform/identity` for the
 * ceiling and the permission list, `/registry/actions` for the level each action is registered at) and
 * once when the action is attempted (`authorize()`). This hook exists so the first answer and the second
 * cannot disagree — the level is read from the same catalogue the server enforces, never written into a
 * screen — and so the *reason* shown to an operator is the reason the server would give.
 *
 * `authorize()` checks the ceiling before the permission:
 *
 * ```python
 * if principal.max_action_level.rank < level.rank:      # ceiling
 *     raise PermissionDenied(... {"required_level": ..., "ceiling": ...})
 * if not principal.has_permission(permission):          # permission
 *     raise PermissionDenied(... {"permission": ..., "role_keys": ...})
 * ```
 *
 * The order matters for the sentence a person reads. A viewer asking to run an engine is refused for the
 * ceiling, and telling them they lack `engine.run` would send them to ask for a permission that would
 * still not let them act. So the same order is applied here.
 *
 * The four states below are the four facts that exist:
 *
 * - `checking` — the answer is still being read; nothing is claimed yet.
 * - `unknown` — the answer could not be read. The offer is withheld and the control says exactly that,
 *   without claiming a refusal: a failed read is a network problem, and turning it into "you lack this
 *   permission" would be a false statement about somebody's account.
 * - `level-below` — the identity holds (or may hold) the permission, but its ceiling is lower than the
 *   level the action is registered at.
 * - `no-permission` — the ceiling is sufficient and the permission is not held.
 * - `ready` — both gates pass, so the offer is honest. It is still not authority: the server decides on
 *   the request.
 *
 * A fifth state the server can produce — an action above the ceiling being refused *for want of a
 * recorded approval* (`security.approval_required`) — is not knowable here, because it depends on
 * approvals that exist in the database rather than on the identity. It is not guessed at; it arrives as
 * the server's own 409, and `ErrorState` renders it as the decision it is.
 */

import { useQuery } from '@tanstack/react-query'
import { drillingApi } from '../api/endpoints'
import { levelRank } from '../lib/format'
import { holdsPermission } from '../lib/permissions'
import { useI18n } from '../i18n'
import { useIdentity } from './useIdentity'

export type ActionGateState = 'checking' | 'unknown' | 'no-permission' | 'level-below' | 'ready'

export interface ActionGate {
  state: ActionGateState
  /** The level the server registers the action at, when the catalogue could be read. */
  level: string | null
  /** The identity's ceiling, when the identity could be read. */
  ceiling: string | null
  /**
   * Whether the offer should be withheld. True for every state but `ready`, including a failed read —
   * withholding an offer asserts nothing, and the `reason` says which of the four situations it is.
   */
  suggestDisabled: boolean
  /** A translated sentence for a `title` or an inline hint; empty when the action is ready. */
  reason: string
}

export function useActionGate(actionKey: string): ActionGate {
  const { t } = useI18n()
  const identity = useIdentity()
  const actions = useQuery({
    queryKey: ['platform-actions'],
    queryFn: ({ signal }) => drillingApi.actions(signal),
  })

  const action = actions.data?.items.find((item) => item.key === actionKey) ?? null
  const ceiling = identity.data?.max_action_level ?? null
  const level = action?.level ?? null

  let state: ActionGateState
  if (identity.isPending || actions.isPending) {
    state = 'checking'
  } else if (!identity.data || !action) {
    // Either the identity could not be read, or this action is not in the server's catalogue — in both
    // cases the client does not know what the server would decide, and says exactly that.
    state = 'unknown'
  } else if (levelRank(ceiling) < levelRank(level)) {
    state = 'level-below'
  } else if (!holdsPermission(identity.data.permissions, action.permission)) {
    state = 'no-permission'
  } else {
    state = 'ready'
  }

  const roles = identity.data?.role_keys.join(', ') || t('roles.none')
  const reason =
    state === 'checking'
      ? t('permissions.checking')
      : state === 'level-below'
        ? t('permissions.levelBelow', { ceiling: ceiling ?? '', required: level ?? '' })
        : state === 'no-permission'
          ? t('permissions.noPermission', { permission: action?.permission ?? actionKey, roles })
          : state === 'unknown'
            ? t('permissions.notRead')
            : ''

  return {
    state,
    level,
    ceiling,
    suggestDisabled: state !== 'ready',
    reason,
  }
}
