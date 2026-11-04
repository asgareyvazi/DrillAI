#!/usr/bin/env node
/**
 * Validate actual Playwright execution, not collection-only output.
 *
 * Arguments: <playwright-log-file> <playwright-process-exit-code> <playwright-json-report>. The
 * expected project and total counts come from ci/test-inventory.json, the same manifest used by the
 * pre-run collection-shape check.
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const frontendRoot = path.resolve(here, '..')
const repositoryRoot = path.resolve(frontendRoot, '..')
const manifestPath = path.join(repositoryRoot, 'ci', 'test-inventory.json')
const [logPath, exitCodeText, reportPath] = process.argv.slice(2)
let failureContext = ''

function escapeWorkflowCommandData(value) {
  return value
    .replaceAll('%', '%25')
    .replaceAll('\r', '%0D')
    .replaceAll('\n', '%0A')
    .replaceAll(':', '%3A')
    .replaceAll(',', '%2C')
}

function fail(message) {
  const details = `Playwright execution verification failed: ${message}${failureContext ? `\nPlaywright failure context:\n${failureContext}` : ''}`
  const boundedDetails = details.length > 6000 ? `${details.slice(0, 6000)}…` : details
  process.stderr.write(`${details}\n`)
  // GitHub workflow-command data must escape newlines and punctuation. Keep the actionable Playwright
  // failure context in the check annotation, not only in a runner log archive that may be unavailable.
  process.stderr.write(`::error::${escapeWorkflowCommandData(boundedDetails)}\n`)
  process.exit(1)
}

if (!logPath || !exitCodeText || !/^\d+$/.test(exitCodeText) || !reportPath) {
  fail('usage: verify-e2e-run.mjs <playwright-log-file> <process-exit-code> <playwright-json-report>')
}

let manifest
let rawLog
let report
try {
  manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))
  rawLog = readFileSync(path.resolve(process.cwd(), logPath), 'utf8')
  report = JSON.parse(readFileSync(path.resolve(process.cwd(), reportPath), 'utf8'))
} catch (error) {
  fail(`cannot read manifest, log, or actual Playwright JSON report: ${error instanceof Error ? error.message : String(error)}`)
}
const plain = rawLog.replace(/\u001b\[[0-9;]*[a-zA-Z]/g, '').replace(/\r/g, '\n')
const lines = plain.split(/\n+/).map((line) => line.trim()).filter(Boolean)
const oracleLines = lines.filter((line) => /^(?:Error|Expected|Received):/.test(line)).slice(-12)
const reportErrorLines = []
for (const error of report.errors ?? []) {
  if (error.message) reportErrorLines.push(String(error.message))
}
function collectReportErrors(suites) {
  for (const suite of suites ?? []) {
    for (const spec of suite.specs ?? []) {
      for (const test of spec.tests ?? []) {
        for (const result of test.results ?? []) {
          for (const error of result.errors ?? []) {
            if (error.message) reportErrorLines.push(String(error.message))
            if (error.snippet) reportErrorLines.push(String(error.snippet))
          }
        }
      }
    }
    collectReportErrors(suite.suites)
  }
}
collectReportErrors(report.suites)
failureContext = (oracleLines.length ? oracleLines : reportErrorLines.length ? reportErrorLines : lines.slice(-12)).join('\n')

const processExitCode = Number(exitCodeText)
if (processExitCode !== 0) fail(`Playwright process exited ${processExitCode}`)

const expected = manifest.e2e
if (
  !expected || !expected.projects || !Number.isInteger(expected.total) || !Number.isInteger(expected.files) ||
  !Array.isArray(expected.spec_files) || expected.spec_files.length !== expected.files ||
  new Set(expected.spec_files).size !== expected.spec_files.length
) {
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

const stats = report.stats
if (!stats || stats.expected !== expected.total || stats.unexpected !== 0 || stats.flaky !== 0 || stats.skipped !== 0) {
  fail(
    `Playwright JSON outcome differs from the reviewed inventory: expected=${expected.total}, ` +
      `unexpected=${stats?.unexpected ?? 'missing'}, flaky=${stats?.flaky ?? 'missing'}, skipped=${stats?.skipped ?? 'missing'}`,
  )
}

const executedProjects = new Map(Object.keys(expected.projects).map((project) => [project, 0]))
const executedFiles = new Set()
let testTotal = 0
const resultProblems = []
function collectExecuted(suites) {
  for (const suite of suites ?? []) {
    for (const spec of suite.specs ?? []) {
      const file = String(spec.file ?? '').replaceAll('\\', '/')
      const normalizedFile = file.match(/(?:^|\/)([^/]+\.spec\.ts)$/)?.[1]
      if (normalizedFile) executedFiles.add(normalizedFile)
      else resultProblems.push(`unrecognized Playwright spec file in JSON report: ${file || '(missing file)'}`)
      for (const test of spec.tests ?? []) {
        testTotal += 1
        const project = String(test.projectName ?? '')
        if (!executedProjects.has(project)) {
          resultProblems.push(`unreviewed Playwright project: ${project || '(missing projectName)'}`)
          continue
        }
        executedProjects.set(project, (executedProjects.get(project) ?? 0) + 1)
        const results = test.results ?? []
        if (results.length !== 1 || results[0]?.retry !== 0 || results[0]?.status !== 'passed') {
          resultProblems.push(`${project} ${file}: expected one passing attempt with retry=0, got ${JSON.stringify(results.map((r) => ({ retry: r.retry, status: r.status })))}`)
        }
      }
    }
    collectExecuted(suite.suites)
  }
}
collectExecuted(report.suites)

const mismatches = [...resultProblems]
for (const [project, expectedCount] of Object.entries(expected.projects)) {
  const actual = executedProjects.get(project) ?? 0
  if (actual !== expectedCount) mismatches.push(`${project}: expected ${expectedCount} executed tests, observed ${actual}`)
}
if (executedFiles.size !== expected.files) {
  mismatches.push(`spec files: expected ${expected.files} executed files, observed ${executedFiles.size}`)
}
const expectedFiles = new Set(expected.spec_files)
for (const file of expectedFiles) {
  if (!executedFiles.has(file)) mismatches.push(`reviewed spec file was not executed: ${file}`)
}
for (const file of executedFiles) {
  if (!expectedFiles.has(file)) mismatches.push(`unreviewed spec file was executed: ${file}`)
}
if (testTotal !== expected.total) mismatches.push(`JSON test total: expected ${expected.total}, observed ${testTotal}`)
if (mismatches.length) fail(`${mismatches.join('; ')}. The pre-run inventory check is not execution evidence.`)

const projectSummary = Object.entries(expected.projects).map(([project, count]) => `${project}=${count}`).join(', ')
console.log(`Real Playwright execution verified: ${passed} passed across ${executedFiles.size} spec files; ${projectSummary}; exit=${processExitCode}; retries=0.`)
