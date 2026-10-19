/**
 * Build-time and runtime guard preventing privileged server credentials or shared API tokens
 * from being baked into client-side Vite bundles (`dist/assets/*.js`).
 *
 * Vite statically replaces `import.meta.env.VITE_*` references at build time. Any secret placed in
 * a `VITE_*` variable is readable by anyone who downloads the public JavaScript bundle.
 */

export interface FrontendEnvCheckContext {
  command: 'build' | 'serve'
  mode: string
}

const FORBIDDEN_PROD_VITE_KEYS = new Set(['VITE_API_TOKEN'])

const FORBIDDEN_VITE_SECRET_PATTERNS: readonly RegExp[] = [
  /^VITE_.*SECRET/i,
  /^VITE_.*MASTER_KEY/i,
  /^VITE_.*PRIVATE_KEY/i,
  /^VITE_.*DATABASE_URL/i,
  /^VITE_.*LLM_API_KEY/i,
  /^VITE_.*SMTP_PASSWORD/i,
  /^VITE_.*TELEGRAM_BOT_TOKEN/i,
  /^VITE_.*PASSWORD/i,
]

export function validateFrontendEnvSecurity(
  env: Record<string, string | undefined>,
  context: FrontendEnvCheckContext,
): string[] {
  const issues: string[] = []
  const isProdBuild = context.command === 'build' && context.mode === 'production'

  for (const [key, rawVal] of Object.entries(env)) {
    if (!key.startsWith('VITE_')) continue
    const val = (rawVal ?? '').trim()
    if (!val) continue

    if (FORBIDDEN_PROD_VITE_KEYS.has(key) && isProdBuild) {
      issues.push(
        `${key} is set during a production frontend build. Embedding shared bearer tokens in public JavaScript bundles is forbidden; use interactive per-user HttpOnly cookie sessions instead.`,
      )
      continue
    }

    if (FORBIDDEN_VITE_SECRET_PATTERNS.some((pattern) => pattern.test(key))) {
      issues.push(
        `${key} exposes a server-side secret pattern via the public VITE_ prefix and would be leaked into client bundles.`,
      )
    }
  }

  return issues
}

export function assertSafeFrontendEnv(
  env: Record<string, string | undefined>,
  context: FrontendEnvCheckContext,
): void {
  const issues = validateFrontendEnvSecurity(env, context)
  if (issues.length > 0) {
    throw new Error(`Frontend environment security check failed:\n- ${issues.join('\n- ')}`)
  }
}
