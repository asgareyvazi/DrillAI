/**
 * Fail closed if Playwright's collected E2E inventory changes without an explicit CI review.
 *
 * This is a collection-only check: `--list` does not start the API/web servers or run tests. The
 * actual suite still runs immediately afterwards and must report 121 passes in each OS job.
 */
import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const frontendRoot = path.resolve(here, '..')
const runner = path.join(here, 'run-e2e.mjs')
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
const counts = new Map([
  ['chromium', 0],
  ['chromium-faults', 0],
  ['chromium-auth', 0],
])
for (const line of plain.split(/\r?\n/)) {
  const match = line.match(/^\s*\[(chromium(?:-faults|-auth)?)\]\s+›/)
  if (match?.[1] && counts.has(match[1])) counts.set(match[1], (counts.get(match[1]) ?? 0) + 1)
}

const totalMatch = plain.match(/^Total:\s*(\d+)\s+tests\s+in\s+(\d+)\s+files\s*$/m)
const total = totalMatch?.[1] ? Number(totalMatch[1]) : Number.NaN
const files = totalMatch?.[2] ? Number(totalMatch[2]) : Number.NaN
const expected = new Map([
  ['chromium', 114],
  ['chromium-faults', 4],
  ['chromium-auth', 3],
])
const actualSum = [...counts.values()].reduce((sum, count) => sum + count, 0)
const mismatches = [...expected].flatMap(([project, expectedCount]) => {
  const actualCount = counts.get(project)
  return actualCount === expectedCount ? [] : [`${project}: expected ${expectedCount}, collected ${actualCount ?? 'none'}`]
})
if (total !== 121) mismatches.push(`total: expected 121, collected ${Number.isNaN(total) ? 'no total line' : total}`)
if (files !== 17) mismatches.push(`files: expected 17, collected ${Number.isNaN(files) ? 'no total line' : files}`)
if (actualSum !== 121) mismatches.push(`project sum: expected 121, collected ${actualSum}`)

if (mismatches.length > 0) {
  process.stderr.write(`${plain}\n`)
  throw new Error(`E2E inventory does not match the reviewed 121-test plan:\n- ${mismatches.join('\n- ')}`)
}

console.log('E2E inventory verified: 121 tests in 17 files; chromium=114, chromium-faults=4, chromium-auth=3.')
