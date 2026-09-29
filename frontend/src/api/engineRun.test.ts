/**
 * The engine-run body, and the shapes it comes in.
 *
 * This is a contract test for a defect rather than a nicety: the workspace reads a run result the way
 * the *recorded* run names it (`outputs`, `constraint_violations`), while the live run answers with
 * `result` and `violations`. Reading one as the other produced `undefined.length` on a successful run
 * and the page replaced a real result with a crash. The mapper is asserted against the server's own
 * body — taken from `EngineExecution.to_payload()` — so the two cannot drift again silently.
 */

import { describe, expect, it } from 'vitest'
import { ApiError } from './client'
import { engineRunEnvelope } from './engineRun'

/** The body `POST /registry/engines/{key}/run` produces, key for key. */
const serverBody = {
  engine_key: 'readiness.assessment',
  engine_version: '1.1.0',
  result: { overall_state: 'ready', ready_count: 1, as_of: '2026-01-01' },
  is_feasible: true,
  warnings: ['lead time assumed from the supplied date'],
  violations: [{ code: 'shortfall', item_id: 'RIG-01' }],
  assumptions: ['quantity available reflects the inventory at the as-of date'],
  limitations: ['no probabilistic lead-time modelling'],
  engine_run_id: 'ege_1',
  inputs_hash: 'abc123',
  outputs_hash: 'def456',
  engine: { key: 'readiness.assessment', version: '1.1.0', validation_status: 'validated' },
}

describe('the engine run response', () => {
  it('names the result and the violations the way the workspace reads them', () => {
    const envelope = engineRunEnvelope(serverBody)

    expect(envelope.outputs).toEqual(serverBody.result)
    expect(envelope.constraint_violations).toEqual(serverBody.violations)
    // The rendering path that used to throw: `constraint_violations.length` on an undefined field.
    expect(envelope.constraint_violations).toHaveLength(1)
    expect(envelope.engine).toEqual({
      key: 'readiness.assessment',
      version: '1.1.0',
      validation_status: 'validated',
    })
    expect(envelope.is_feasible).toBe(true)
    expect(envelope.inputs_hash).toBe('abc123')
  })

  it('reports a body it cannot read instead of rendering undefined as a result', () => {
    // A rename on the server: this is exactly the class of change that produced the crash.
    const { result, ...renamed } = serverBody
    void result

    expect(() => engineRunEnvelope(renamed)).toThrowError(ApiError)
    try {
      engineRunEnvelope(renamed)
    } catch (error) {
      const apiError = error as ApiError
      expect(apiError.code).toBe('protocol.malformed_response')
      expect(apiError.status).toBe(200)
      expect(apiError.details?.issue).toContain('result')
    }
  })

  it('refuses a violations field that is not a list of objects', () => {
    expect(() => engineRunEnvelope({ ...serverBody, violations: 'none' })).toThrowError(ApiError)
    expect(() => engineRunEnvelope({ ...serverBody, warnings: [1, 2] })).toThrowError(ApiError)
    expect(() => engineRunEnvelope({ ...serverBody, engine: undefined })).toThrowError(ApiError)
  })
})
