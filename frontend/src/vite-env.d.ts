/// <reference types="vite/client" />

/**
 * Environment variables the browser bundle reads.
 *
 * `VITE_API_TOKEN` is for deployments where the UI is served behind an authenticated gateway that
 * injects a token; it must never be set to a long-lived credential in a public build.
 */
interface ImportMetaEnv {
  readonly VITE_API_BASE?: string
  readonly VITE_API_TOKEN?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
