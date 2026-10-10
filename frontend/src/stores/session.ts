/**
 * Client session state: which identity the UI is acting as, and the display-unit preference.
 *
 * This is *not* an authorization mechanism. The API enforces permissions; the roles held here only
 * let a developer exercise the development identity header (`X-Dev-Roles`, which the backend
 * refuses in production). Nothing in the UI is granted or hidden on the basis of this store alone —
 * a hidden button is a convenience, and every endpoint is still checked server-side.
 *
 * The store deliberately holds **no catalogue** of roles. It used to carry its own list of presets,
 * and that list had already drifted from the server's: the backend advertises eight development
 * presets (`development_presets` on `/platform/identity`) and the hard-coded list offered six, so
 * three catalogued roles — `integrity_engineer`, `data_manager`, `auditor` — could not be exercised
 * from the interface at all. The switcher now renders the server's list. What remains here is the
 * *selection*, which is session state, and the development default, which is a starting point rather
 * than a claim about what exists.
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { UnitSystem } from '../lib/format'

/**
 * The development identity a fresh session acts as: the engineering role plus the platform
 * administrator, because a single developer session has to be able to create the container objects
 * it works in. Both are catalogued roles the backend recognises; the combination is not a claim by
 * the interface, and the server refuses the header entirely once authentication is enabled.
 */
export const DEFAULT_DEV_ROLES = 'engineer,admin'

export interface SessionState {
  /** Development identity sent as `X-Dev-Roles`; a comma-separated list of catalogued role keys. */
  devRoles: string
  unitSystem: UnitSystem
  setDevRoles: (roles: string) => void
  setUnitSystem: (system: UnitSystem) => void
}

export const useSession = create<SessionState>()(
  persist(
    (set) => ({
      devRoles: DEFAULT_DEV_ROLES,
      unitSystem: 'si',
      setDevRoles: (devRoles) => set({ devRoles }),
      setUnitSystem: (unitSystem) => set({ unitSystem }),
    }),
    { name: 'drillai.session' },
  ),
)

/** The role keys in the current selection, in the order the caller wrote them. */
export function selectedRoleKeys(devRoles: string): string[] {
  return devRoles
    .split(',')
    .map((key) => key.trim())
    .filter(Boolean)
}
