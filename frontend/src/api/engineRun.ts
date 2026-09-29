/**
 * The engine-run body, adapted to the shape the workspace renders.
 *
 * `POST /registry/engines/{key}/run` answers with `EngineExecution.to_payload()` plus an `engine`
 * block — the canonical payload the backend documents as "also the shape a workflow node exposes to
 * references":
 *
 * ```json
 * { "engine_key": …, "engine_version": …, "result": {…}, "violations": […], "warnings": […],
 *   "is_feasible": true, "assumptions": […], "limitations": […], "engine_run_id": …,
 *   "inputs_hash": …, "outputs_hash": …, "engine": { "key": …, "version": …, "validation_status": … } }
 * ```
 *
 * The *recorded* run — `GET /wells/{id}/engine-runs` — names the same values `outputs` and
 * `constraint_violations`. The workspace renders the recorded shape, and it used to read the live run
 * with it: `result.outputs` was `undefined`, so `undefined.length` threw while the panel was drawing
 * a perfectly successful run, and the page replaced a real result with a crash. Two names for one
 * concept is a contract detail, not a reason to lose the answer, so the translation happens here, once,
 * at the boundary — where the response is also checked, so a future rename becomes a reported
 * malformed response instead of a blank panel.
 */

import { ApiError } from './client'
import type { EngineRunEnvelope } from './types'

/** The body of `/registry/engines/{key}/run`, as the server writes it. */
export interface EngineRunResponse {
  engine_key: string
  engine_version: string
  result: Record<string, unknown>
  is_feasible: boolean
  warnings: string[]
  violations: Array<Record<string, unknown>>
  assumptions: string[]
  limitations: string[]
  engine_run_id: string | null
  inputs_hash: string
  outputs_hash: string
  engine: { key: string; version: string; validation_status: string }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function strings(value: unknown): string[] | null {
  return Array.isArray(value) && value.every((item) => typeof item === 'string') ? (value as string[]) : null
}

function records(value: unknown): Array<Record<string, unknown>> | null {
  return Array.isArray(value) && value.every(isRecord) ? (value as Array<Record<string, unknown>>) : null
}

/**
 * Check the body and name it the way the workspace reads it.
 *
 * Throws an `ApiError` of the *malformed response* kind — the same class the client raises when a
 * response does not match its contract — so an interface that cannot read an answer says so instead of
 * rendering `undefined` as a result.
 */
export function engineRunEnvelope(payload: unknown): EngineRunEnvelope {
  const malformed = (issue: string): never => {
    throw new ApiError(
      200,
      'protocol.malformed_response',
      'The server returned a response this page could not read.',
      { issue },
      null,
      false,
      { kind: 'malformed' },
    )
  }

  if (!isRecord(payload)) malformed('expected an engine-run object')
  const body = payload as Record<string, unknown>
  if (!isRecord(body.engine)) malformed('the engine-run body carries no "engine" block')
  const engine = body.engine as Record<string, unknown>
  if (typeof engine.key !== 'string' || typeof engine.version !== 'string') {
    malformed('the "engine" block carries no key/version')
  }
  if (!isRecord(body.result)) malformed('the engine-run body carries no "result" object')
  const violations = records(body.violations)
  if (violations === null) malformed('"violations" is not a list of objects')
  const warnings = strings(body.warnings)
  if (warnings === null) malformed('"warnings" is not a list of strings')
  const assumptions = strings(body.assumptions)
  const limitations = strings(body.limitations)
  if (assumptions === null || limitations === null) {
    malformed('"assumptions" and "limitations" must be lists of strings')
  }
  if (typeof body.inputs_hash !== 'string' || typeof body.outputs_hash !== 'string') {
    malformed('the engine-run body carries no input/output hashes')
  }

  return {
    engine_run_id: typeof body.engine_run_id === 'string' ? body.engine_run_id : '',
    engine: {
      key: engine.key as string,
      version: engine.version as string,
      validation_status: typeof engine.validation_status === 'string' ? engine.validation_status : 'unknown',
    },
    // The live run calls it `result`; the recorded run calls it `outputs`. One concept, one name here.
    outputs: body.result as Record<string, unknown>,
    warnings: warnings as string[],
    constraint_violations: violations as Array<Record<string, unknown>>,
    is_feasible: body.is_feasible === true,
    assumptions: assumptions as string[],
    limitations: limitations as string[],
    inputs_hash: body.inputs_hash as string,
    outputs_hash: body.outputs_hash as string,
  }
}
