/**
 * Run the Playwright suite with a browser that is guaranteed to exist.
 *
 * `npx playwright test` on its own assumes the machine has a browser Playwright knows about. This
 * wrapper resolves one first (see `prepare-chromium.mjs`), exports the two variables the config reads,
 * and then runs Playwright with whatever extra arguments were passed through:
 *
 *   npm run test:e2e
 *   npm run test:e2e -- e2e/workflow.spec.ts --reporter=list
 */

import { spawn } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const frontendRoot = path.resolve(here, '..')

const resolve = async (flag) => {
  const { execFileSync } = await import('node:child_process')
  return execFileSync(process.execPath, [path.join(here, 'prepare-chromium.mjs'), flag], {
    cwd: frontendRoot,
    encoding: 'utf8',
  }).trim()
}

const executable = await resolve('--print-path')
const libs = await resolve('--print-libs')

const playwright = path.join(frontendRoot, 'node_modules', '.bin', 'playwright')
const child = spawn(playwright, ['test', ...process.argv.slice(2)], {
  cwd: frontendRoot,
  stdio: 'inherit',
  env: {
    ...process.env,
    DRILLAI_CHROMIUM_PATH: executable,
    ...(libs ? { DRILLAI_CHROMIUM_LIBS: libs } : {}),
  },
})

child.on('exit', (code, signal) => {
  if (signal) process.kill(process.pid, signal)
  else process.exit(code ?? 1)
})
