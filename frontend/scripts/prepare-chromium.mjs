/**
 * Make a Chromium binary available to Playwright, and print where it is.
 *
 * Two situations are supported, in order of preference:
 *
 * 1. **`DRILLAI_CHROMIUM_PATH`** — an operator (or CI image) already installed a browser. Nothing to do.
 * 2. **The container-oriented build in `@sparticuz/chromium`** — used by the sandbox this project was
 *    developed in, which has no access to the Playwright download CDN. That package ships the binary
 *    (brotli-compressed) *and*, in `bin/al2023.tar.br`, the shared libraries a stripped container image
 *    lacks — NSS and NSPR in particular. Without those the binary exits 127 with "libnspr4.so: not
 *    found", which reads like a broken download rather than a missing system library.
 *
 * The script writes the extracted libraries to a temporary prefix and prints the two environment
 * variables Playwright needs, so the harness can be launched reproducibly:
 *
 *   DRILLAI_CHROMIUM_PATH="$(node scripts/prepare-chromium.mjs --print-path)" \
 *   DRILLAI_CHROMIUM_LIBS="$(node scripts/prepare-chromium.mjs --print-libs)" \
 *   npx playwright test
 *
 * `--print-env` emits both as a shell snippet for `eval "$(...)"`.
 */

import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import zlib from 'node:zlib'

const here = path.dirname(fileURLToPath(import.meta.url))
const frontendRoot = path.resolve(here, '..')

const LIB_ROOT = process.env.DRILLAI_CHROMIUM_LIBS ?? path.join(tmpdir(), 'drillai-chromium-libs')

async function resolveExecutable() {
  if (process.env.DRILLAI_CHROMIUM_PATH) {
    if (!existsSync(process.env.DRILLAI_CHROMIUM_PATH)) {
      throw new Error(`DRILLAI_CHROMIUM_PATH points at a missing file: ${process.env.DRILLAI_CHROMIUM_PATH}`)
    }
    return process.env.DRILLAI_CHROMIUM_PATH
  }

  // On Windows or macOS, @sparticuz/chromium (an Amazon Linux 2023 x86_64 ELF binary) cannot run natively.
  // Resolve Playwright's native Chromium binary and install it automatically if not yet downloaded.
  if (process.platform !== 'linux') {
    const { chromium: pwChromium } = await import('@playwright/test')
    let executablePath = ''
    try {
      executablePath = pwChromium.executablePath()
    } catch {
      executablePath = ''
    }
    if (!executablePath || !existsSync(executablePath)) {
      const playwrightCli = path.join(frontendRoot, 'node_modules', '@playwright', 'test', 'cli.js')
      execFileSync(process.execPath, [playwrightCli, 'install', 'chromium'], {
        cwd: frontendRoot,
        stdio: 'inherit',
      })
      executablePath = pwChromium.executablePath()
    }
    return executablePath
  }

  const packageDir = path.join(frontendRoot, 'node_modules', '@sparticuz', 'chromium')
  const bundle = path.join(packageDir, 'bin')
  if (!existsSync(bundle)) {
    throw new Error(
      'no browser available: set DRILLAI_CHROMIUM_PATH, or install @sparticuz/chromium and run ' +
        '"npx playwright install chromium"',
    )
  }

  // 1. shared libraries the container build expects the host to provide
  const archive = path.join(bundle, 'al2023.tar.br')
  if (existsSync(archive) && !existsSync(path.join(LIB_ROOT, 'lib', 'libnss3.so'))) {
    rmSync(LIB_ROOT, { recursive: true, force: true })
    mkdirSync(LIB_ROOT, { recursive: true })
    const tarPath = path.join(tmpdir(), 'drillai-al2023.tar')
    writeFileSync(tarPath, zlib.brotliDecompressSync(readFileSync(archive)))
    execFileSync('tar', ['-xf', tarPath, '-C', LIB_ROOT], { stdio: 'inherit' })
    rmSync(tarPath, { force: true })
  }

  // 2. the binary itself (the package inflates its own brotli payload on first use)
  const sparticuz = (await import('@sparticuz/chromium')).default
  return sparticuz.executablePath()
}

const executable = await resolveExecutable()
const libs = process.platform === 'linux' && existsSync(path.join(LIB_ROOT, 'lib')) ? path.join(LIB_ROOT, 'lib') : ''

const mode = process.argv[2] ?? ''
if (mode === '--print-path') console.log(executable)
else if (mode === '--print-libs') console.log(libs)
else if (mode === '--print-env') {
  console.log(`export DRILLAI_CHROMIUM_PATH=${JSON.stringify(executable)}`)
  console.log(`export DRILLAI_CHROMIUM_LIBS=${JSON.stringify(libs)}`)
} else {
  console.log(`chromium: ${executable}`)
  console.log(`libraries: ${libs || '(system default)'}`)
  try {
    if (process.platform === 'win32') {
      // On Windows, chrome.exe is compiled with the Windows GUI subsystem and ignores `--version`
      // on stdout (opening a background window that does not exit). Verify the binary exists on disk
      // and confirm the Playwright CLI version instead.
      if (!existsSync(executable)) {
        throw new Error(`Chromium executable does not exist at ${executable}`)
      }
      const playwrightCli = path.join(frontendRoot, 'node_modules', '@playwright', 'test', 'cli.js')
      const pwVersion = execFileSync(process.execPath, [playwrightCli, '--version'], {
        cwd: frontendRoot,
        encoding: 'utf8',
        timeout: 15000,
      }).trim()
      console.log(`verified: ${executable} (${pwVersion})`)
    } else {
      const version = execFileSync(executable, ['--version'], {
        env: { ...process.env, ...(libs ? { LD_LIBRARY_PATH: libs } : {}) },
        encoding: 'utf8',
        timeout: 15000,
      }).trim()
      console.log(`verified: ${version}`)
    }
  } catch (error) {
    console.error(`could not run the browser: ${error.message}`)
    process.exitCode = 1
  }
}
