/// <reference types="vite/client" />

/**
 * Public environment variables the browser bundle reads.
 *
 * Security invariant: privileged server secrets and shared bearer tokens (`VITE_API_TOKEN`) must
 * NEVER be exposed under `VITE_*` in production builds (`drillai-env-security-guard` rejects them
 * at build time). Browser authentication uses per-user HttpOnly session cookies + CSRF tokens.
 */
interface ImportMetaEnv {
  readonly VITE_API_BASE?: string
  readonly VITE_API_TIMEOUT_MS?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
