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

/**
 * The identities the switcher offers.
 *
 * These are development identities, sent as `X-Dev-Roles`, and the backend refuses the header
 * outright when authentication is enabled. The list mirrors the platform's own presets (see the
 * identity endpoint's `development_presets`) so a journey can be walked as the role that is actually
 * allowed to take each step: an engineer may draft and run, a supervisor may publish and decide
 * approvals, a viewer may only read.
 */
const ROLE_PRESETS = [
  { key: 'engineer,admin', label: 'Drilling engineer (dev)' },
  { key: 'engineer', label: 'Drilling engineer' },
  { key: 'drilling_supervisor', label: 'Drilling supervisor (publish & approve)' },
  { key: 'well_manager', label: 'Well manager' },
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
