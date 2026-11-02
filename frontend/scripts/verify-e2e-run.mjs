#!/usr/bin/env node
/**
 * Validate actual Playwright execution, not collection-only output.
 *
 * Arguments: <playwright-log-file> <playwright-process-exit-code>. Expected project and total counts
 * come from ci/test-inventory.json, the same manifest used by the pre-run shape check.
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const frontendRoot = path.resolve(here, '..')
const repositoryRoot = path.resolve(frontendRoot, '..')
const manifestPath = path.join(repositoryRoot, 'ci', 'test-inventory.json')
const [logPath, exitCodeText] = process.argv.slice(2)
let failureContext = ''

function fail(message) {
  process.stderr.write(
    `::error::Playwright execution verification failed: ${message}${failureContext ? `\nPlaywright failure context:\n${failureContext}` : ''}\n`,
  )
  process.exit(1)
}

if (!logPath || !exitCodeText || !/^\d+$/.test(exitCodeText)) {
  fail('usage: verify-e2e-run.mjs <playwright-log-file> <process-exit-code>')
}

let manifest
let rawLog
try {
  manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))
  rawLog = readFileSync(path.resolve(process.cwd(), logPath), 'utf8')
} catch (error) {
  fail(`cannot read manifest or Playwright log: ${error instanceof Error ? error.message : String(error)}`)
}
const plain = rawLog.replace(/\u001b\[[0-9;]*[a-zA-Z]/g, '').replace(/\r/g, '\n')
const lines = plain.split(/\n+/).map((line) => line.trim()).filter(Boolean)
const oracleLines = lines.filter((line) => /^(?:Error|Expected|Received):/.test(line)).slice(-12)
failureContext = (oracleLines.length ? oracleLines : lines.slice(-12)).join('\n')

const processExitCode = Number(exitCodeText)
if (processExitCode !== 0) fail(`Playwright process exited ${processExitCode}`)

const expected = manifest.e2e
if (!expected || !expected.projects || !Number.isInteger(expected.total) || !Number.isInteger(expected.files)) {
  fail(`incomplete E2E inventory manifest at ${manifestPath}`)
}
if (Object.values(expected.projects).reduce((sum, count) => sum + count, 0) !== expected.total) {
  fail('manifest project totals do not equal the reviewed total')
}

const summaryCandidates = lines.filter((line) => /^\d+\s+(?:passed|failed|flaky|skipped)\b/.test(line))
const summary = summaryCandidates.at(-1)
if (!summary) fail('no final Playwright result summary was printed')
const summaryMatch = summary.match(/^(\d+)\s+passed(?:\s+\([^)]*\))?$/)
if (!summaryMatch) fail(`expected only a clean passed summary, got: ${summary}`)
const passed = Number(summaryMatch[1])
if (passed !== expected.total) fail(`expected ${expected.total} passed browser tests, got ${passed}`)

const executedProjects = new Map(Object.keys(expected.projects).map((project) => [project, 0]))
const executedFiles = new Set()
for (const line of lines) {
  const start = line.match(/^[✓✔]\s+\d+\s+\[([^\]]+)\]\s+›\s+(.+)$/)
  if (!start || !executedProjects.has(start[1])) continue
  executedProjects.set(start[1], (executedProjects.get(start[1]) ?? 0) + 1)
  const file = start[2].match(/(?:^|\s)(e2e\/[^\s:]+\.spec\.ts):\d+/)
  if (file?.[1]) executedFiles.add(file[1])
}

const mismatches = []
for (const [project, expectedCount] of Object.entries(expected.projects)) {
  const actual = executedProjects.get(project) ?? 0
  if (actual !== expectedCount) mismatches.push(`${project}: expected ${expectedCount} executed tests, observed ${actual}`)
}
if (executedFiles.size !== expected.files) {
  mismatches.push(`spec files: expected ${expected.files} executed files, observed ${executedFiles.size}`)
}
const actualProjectTotal = [...executedProjects.values()].reduce((sum, count) => sum + count, 0)
if (actualProjectTotal !== expected.total) {
  mismatches.push(`project execution sum: expected ${expected.total}, observed ${actualProjectTotal}`)
}
if (mismatches.length) fail(`${mismatches.join('; ')}. The pre-run inventory check is not execution evidence.`)

const projectSummary = Object.entries(expected.projects).map(([project, count]) => `${project}=${count}`).join(', ')
console.log(`Real Playwright execution verified: ${passed} passed across ${executedFiles.size} spec files; ${projectSummary}; exit=${processExitCode}.`)
