/**
 * Fail closed if Playwright's collected E2E inventory changes without an explicit CI review.
 *
 * This is a collection-only check: `--list` does not start the API/web servers or run tests. The
 * actual browser suite runs immediately afterwards and is validated separately by
 * `verify-e2e-run.mjs`. Expected counts are owned by ci/test-inventory.json.
 */
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const frontendRoot = path.resolve(here, '..')
const repositoryRoot = path.resolve(frontendRoot, '..')
const runner = path.join(here, 'run-e2e.mjs')
const manifestPath = path.join(repositoryRoot, 'ci', 'test-inventory.json')
const { e2e: expected } = JSON.parse(readFileSync(manifestPath, 'utf8'))

if (!expected || typeof expected.total !== 'number' || typeof expected.files !== 'number' || !expected.projects) {
  throw new Error(`E2E inventory manifest is incomplete: ${manifestPath}`)
}
const expectedTotal = Object.values(expected.projects).reduce((sum, count) => sum + count, 0)
if (expectedTotal !== expected.total) {
  throw new Error(`E2E inventory manifest project totals ${expectedTotal}, expected ${expected.total}`)
}

const result = spawnSync(process.execPath, [runner, '--list', '--reporter=list'], {
  cwd: frontendRoot,
  encoding: 'utf8',
  env: process.env,
  maxBuffer: 16 * 1024 * 1024,
})

if (result.error) throw result.error
const output = `${result.stdout ?? ''}\n${result.stderr ?? ''}`
if (result.status !== 0) {
  process.stderr.write(output)
  throw new Error(`Playwright inventory listing exited ${result.status ?? 'without a status'}`)
}

const plain = output.replace(/\u001b\[[0-9;]*[a-zA-Z]/g, '')
const counts = new Map(Object.keys(expected.projects).map((project) => [project, 0]))
for (const line of plain.split(/\r?\n/)) {
  const match = line.match(/^\s*\[([^\]]+)\]\s+›/)
  if (match?.[1] && counts.has(match[1])) counts.set(match[1], (counts.get(match[1]) ?? 0) + 1)
}

const totalMatch = plain.match(/^Total:\s*(\d+)\s+tests\s+in\s+(\d+)\s+files\s*$/m)
const total = totalMatch?.[1] ? Number(totalMatch[1]) : Number.NaN
const files = totalMatch?.[2] ? Number(totalMatch[2]) : Number.NaN
const mismatches = Object.entries(expected.projects).flatMap(([project, expectedCount]) => {
  const actualCount = counts.get(project)
  return actualCount === expectedCount ? [] : [`${project}: expected ${expectedCount}, collected ${actualCount ?? 'none'}`]
})
if (total !== expected.total) {
  mismatches.push(`total: expected ${expected.total}, collected ${Number.isNaN(total) ? 'no total line' : total}`)
}
if (files !== expected.files) {
  mismatches.push(`files: expected ${expected.files}, collected ${Number.isNaN(files) ? 'no total line' : files}`)
}
const actualSum = [...counts.values()].reduce((sum, count) => sum + count, 0)
if (actualSum !== expected.total) mismatches.push(`project sum: expected ${expected.total}, collected ${actualSum}`)

if (mismatches.length > 0) {
  process.stderr.write(`${plain}\n`)
  throw new Error(`E2E inventory differs from the reviewed manifest:\n- ${mismatches.join('\n- ')}`)
}

const projectSummary = Object.entries(expected.projects).map(([project, count]) => `${project}=${count}`).join(', ')
console.log(`E2E inventory verified: ${expected.total} tests in ${expected.files} files; ${projectSummary}.`)
