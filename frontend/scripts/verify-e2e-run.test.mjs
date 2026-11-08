import { spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const frontendRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const verifierPath = path.join(frontendRoot, 'scripts', 'verify-e2e-run.mjs')

describe('Playwright failure diagnostics', () => {
  it('reports only reviewed project, spec basename, and source line for failed tests', () => {
    const temporaryDirectory = mkdtempSync(path.join(os.tmpdir(), 'drillai-e2e-diagnostic-'))
    const logPath = path.join(temporaryDirectory, 'playwright.log')
    const reportPath = path.join(temporaryDirectory, 'playwright-report.json')
    const absoluteWindowsSpecPath = 'D:\\runner\\work\\DrillAI\\frontend\\e2e\\surveys-workspace.spec.ts'
    const privateTitle = 'a parametrized title that must not be logged'

    try {
      writeFileSync(logPath, 'Error: expect(received).toBe(expected) // Object.is equality\n')
      writeFileSync(
        reportPath,
        JSON.stringify({
          stats: { expected: 126, unexpected: 1, flaky: 0, skipped: 0 },
          suites: [
            {
              title: 'root',
              suites: [
                {
                  title: 'survey workspace',
                  specs: [
                    {
                      title: privateTitle,
                      file: absoluteWindowsSpecPath,
                      line: 235,
                      tests: [
                        {
                          projectName: 'chromium',
                          title: privateTitle,
                          results: [
                            {
                              status: 'failed',
                              retry: 0,
                              errors: [{ message: 'Error: expect(received).toBe(expected)' }],
                            },
                          ],
                        },
                      ],
                    },
                  ],
                },
              ],
            },
          ],
        }),
      )

      const result = spawnSync(process.execPath, [verifierPath, logPath, '1', reportPath], {
        cwd: frontendRoot,
        encoding: 'utf8',
      })
      expect(result.error).toBeUndefined()
      expect(result.status).toBe(1)
      expect(result.stderr).toContain('failed test location: chromium surveys-workspace.spec.ts:235')
      expect(result.stderr).not.toContain('D:/runner')
      expect(result.stderr).not.toContain(privateTitle)
    } finally {
      rmSync(temporaryDirectory, { recursive: true, force: true })
    }
  })
})
