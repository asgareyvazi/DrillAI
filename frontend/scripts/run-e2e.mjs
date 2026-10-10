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
import net from 'node:net'
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

/**
 * Refuse to start when the API or the web port is already taken.
 *
 * The suite must run against servers it started itself — the API is seeded immediately before it
 * starts, and the web server must serve this revision. A server left over from an earlier run would
 * make the suite report on something else, so the wrapper stops with an instruction instead of
 * letting Playwright fail later with a port error that reads like a flake.
 */
const PORTS = [
  ['API', Number(process.env.DRILLAI_E2E_API_PORT ?? 8099)],
  ['web', Number(process.env.DRILLAI_E2E_WEB_PORT ?? 5199)],
]

function inUse(port) {
  return new Promise((resolvePromise) => {
    const socket = net.connect({ host: '127.0.0.1', port })
    socket.setTimeout(500)
    socket.on('connect', () => {
      socket.destroy()
      resolvePromise(true)
    })
    socket.on('error', () => resolvePromise(false))
    socket.on('timeout', () => {
      socket.destroy()
      resolvePromise(false)
    })
  })
}

for (const [label, port] of PORTS) {
  if (await inUse(port)) {
    console.error(
      `port ${port} (${label}) is already in use.\n` +
        `The end-to-end suite starts its own ${label} server and seeds its own database, so a server\n` +
        `left over from an earlier run would make the results meaningless.\n` +
        `Stop the process holding the port (for example: kill $(lsof -t -i:${port})) and run again.`,
    )
    process.exit(1)
  }
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
