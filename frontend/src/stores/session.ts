/**
 * Client session state: which identity the UI is acting as, and the display-unit preference.
 *
 * This is *not* an authorization mechanism. The API enforces permissions; the roles held here only
 * let a developer exercise the development identity header (`X-Dev-Roles`, which the backend
 * refuses in production). Nothing in the UI is granted or hidden on the basis of this store alone —
 * a hidden button is a convenience, and every endpoint is still checked server-side.
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { UnitSystem } from '../lib/format'

const ROLE_PRESETS = [
  { key: 'engineer,admin', label: 'Drilling engineer (dev)' },
  { key: 'engineer', label: 'Drilling engineer' },
  { key: 'viewer', label: 'Viewer (read-only)' },
  { key: 'admin', label: 'Administrator' },
] as const

export interface SessionState {
  /** Development identity preset sent as `X-Dev-Roles`. */
  devRoles: string
  unitSystem: UnitSystem
  knownRolePresets: typeof ROLE_PRESETS
  setDevRoles: (roles: string) => void
  setUnitSystem: (system: UnitSystem) => void
}

export const useSession = create<SessionState>()(
  persist(
    (set) => ({
      devRoles: 'engineer,admin',
      unitSystem: 'si',
      knownRolePresets: ROLE_PRESETS,
      setDevRoles: (devRoles) => set({ devRoles }),
      setUnitSystem: (unitSystem) => set({ unitSystem }),
    }),
    { name: 'drillai.session' },
  ),
)

export { ROLE_PRESETS }
