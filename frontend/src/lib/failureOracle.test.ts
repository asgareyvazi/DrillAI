import { describe, expect, it } from 'vitest'
import {
  classifyNetworkFailure,
  evaluateFailureOracle,
  validateFailureAllowances,
  type FailureAllowance,
  type FailedResponse,
  type NetworkFailure,
  type NetworkFailureAllowance,
} from '../../e2e/failure-oracle'

const validHttpAllowance = (): FailureAllowance => ({
  kind: 'http',
  method: 'POST',
  pathname: '/api/v1/workflows/:workflowId/publish',
  query: '?version=1',
  status: 400,
  maxMatches: 1,
  reason: 'the workflow journey publishes its invalid first draft and asserts this server refusal',
})

const invalidPublishResponse = (overrides: Partial<FailedResponse> = {}): FailedResponse => ({
  kind: 'http',
  method: 'POST',
  url: 'http://127.0.0.1:5199/api/v1/workflows/wfl_42/publish?version=1',
  status: 400,
  ...overrides,
})

const disconnectedRequest = (overrides: Partial<NetworkFailure> = {}): NetworkFailure => ({
  kind: 'network',
  method: 'GET',
  url: 'http://127.0.0.1:5199/api/v1/runs?limit=100',
  errorText: 'net::ERR_INTERNET_DISCONNECTED',
  resourceType: 'fetch',
  isNavigationRequest: false,
  disposition: 'unexpected',
  ...overrides,
})

const expectedTimeout = (): NetworkFailureAllowance => ({
  kind: 'network',
  method: 'GET',
  pathname: '/api/v1/wells',
  query: '?limit=200',
  errorText: 'net::ERR_ABORTED',
  signalReason: 'TimeoutError',
  maxMatches: 4,
  reason: 'the client deadline aborts the deliberately delayed wells read and the UI asserts timeout',
})

describe('E2E request-failure oracle', () => {
  it('matches one exact HTTP method, canonical route, query shape, and status', () => {
    const result = evaluateFailureOracle([invalidPublishResponse()], [], [validHttpAllowance()])
    expect(result).toEqual({ findings: [], matchedCounts: [1] })
  })

  it('rejects mutations to method, route, query, or status instead of matching a substring', () => {
    const mutants: FailedResponse[] = [
      invalidPublishResponse({ method: 'GET' }),
      invalidPublishResponse({ url: 'http://127.0.0.1:5199/api/v1/workflows/wfl_42/publish/other?version=1' }),
      invalidPublishResponse({ url: 'http://127.0.0.1:5199/api/v1/workflows/wfl_42/publish?version=1&tenant=other' }),
      invalidPublishResponse({ url: 'http://127.0.0.1:5199/api/v1/workflows/wfl_42/publish' }),
      invalidPublishResponse({ status: 500 }),
    ]

    for (const mutant of mutants) {
      const result = evaluateFailureOracle([mutant], [], [validHttpAllowance()])
      expect(result.findings, `mutated failure must not match: ${mutant.method} ${mutant.url} ${mutant.status}`)
        .toHaveLength(1)
      expect(result.matchedCounts).toEqual([0])
    }
  })

  it('rejects broad, wildcard, blank, malformed, and unexplained HTTP tolerances at runtime', () => {
    const invalidAllowances = [
      { ...validHttpAllowance(), pathname: '/api/v1/*' },
      { ...validHttpAllowance(), pathname: '/api/v1' },
      { ...validHttpAllowance(), query: '*' },
      { ...validHttpAllowance(), query: '?' },
      { ...validHttpAllowance(), query: '?version=*' },
      { ...validHttpAllowance(), query: '?well_id=:wellId' },
      { ...validHttpAllowance(), method: '*' },
      { ...validHttpAllowance(), reason: 'expected' },
      { ...validHttpAllowance(), maxMatches: undefined },
      { ...validHttpAllowance(), maxMatches: 0 },
    ] as unknown as FailureAllowance[]

    expect(validateFailureAllowances(invalidAllowances)).toHaveLength(invalidAllowances.length)
    expect(evaluateFailureOracle([], [], invalidAllowances).findings).not.toEqual([])
  })

  it('fails closed when the runtime allowance container is not an array', () => {
    const result = evaluateFailureOracle([], [], null as unknown as FailureAllowance[])
    expect(result).toEqual({ findings: ['allowances must be an array'], matchedCounts: [] })
  })

  it('rejects ambiguous allowances instead of using declaration order as an escape hatch', () => {
    const allowance = validHttpAllowance()
    const result = evaluateFailureOracle([invalidPublishResponse()], [], [allowance, { ...allowance }])
    expect(result.findings[0]).toContain('ambiguous tolerances (0, 1)')
    expect(result.matchedCounts).toEqual([0, 0])
  })

  it('enforces maxMatches one-for-one and rejects the first excess occurrence', () => {
    const allowance = { ...validHttpAllowance(), maxMatches: 2 }
    const result = evaluateFailureOracle(
      [invalidPublishResponse(), invalidPublishResponse(), invalidPublishResponse()],
      [],
      [allowance],
    )
    expect(result.matchedCounts).toEqual([2])
    expect(result.findings).toHaveLength(1)
    expect(result.findings[0]).toContain('unprovoked')
  })

  it('matches deliberate offline failures only on their exact endpoint, query, and Chromium code', () => {
    const allowance: FailureAllowance = {
      kind: 'network',
      method: 'GET',
      pathname: '/api/v1/runs',
      query: '?limit=100',
      errorText: 'net::ERR_INTERNET_DISCONNECTED',
      maxMatches: 4,
      reason: 'the test turns the browser offline while loading runs and asserts network recovery',
    }
    const result = evaluateFailureOracle([], [disconnectedRequest()], [allowance])
    expect(result).toEqual({ findings: [], matchedCounts: [1] })

    const mutants = [
      disconnectedRequest({ method: 'POST' }),
      disconnectedRequest({ url: 'http://127.0.0.1:5199/api/v1/runs?limit=100&scope=all' }),
      disconnectedRequest({ errorText: 'net::ERR_CONNECTION_RESET' }),
    ]
    for (const mutant of mutants) {
      expect(evaluateFailureOracle([], [mutant], [allowance]).findings).toHaveLength(1)
    }
  })

  it('requires timeout evidence tied to the exact failed fetch before tolerating ERR_ABORTED', () => {
    const timeoutWithoutReason: NetworkFailureAllowance = { ...expectedTimeout(), signalReason: undefined }
    expect(validateFailureAllowances([timeoutWithoutReason])).not.toEqual([])

    const timeoutFailure = disconnectedRequest({
      url: 'http://127.0.0.1:5199/api/v1/wells?limit=200',
      errorText: 'net::ERR_ABORTED',
      signalReason: 'TimeoutError',
    })
    expect(evaluateFailureOracle([], [timeoutFailure], [expectedTimeout()]).findings).toEqual([])

    const missingSignal = { ...timeoutFailure, signalReason: undefined }
    expect(evaluateFailureOracle([], [missingSignal], [expectedTimeout()]).findings).toHaveLength(1)
    const callerAbort = { ...timeoutFailure, signalReason: 'AbortError' }
    expect(evaluateFailureOracle([], [callerAbort], [expectedTimeout()]).findings).toHaveLength(1)
  })

  it('does not classify ERR_ABORTED as benign without the same request ID and AbortError signal', () => {
    const failure = {
      method: 'GET',
      url: 'http://127.0.0.1:5199/api/v1/runs/run_7',
      errorText: 'net::ERR_ABORTED',
      isNavigationRequest: false,
      requestId: 'e2e-doc-7',
    }
    expect(classifyNetworkFailure(failure)).toBe('unexpected')
    expect(classifyNetworkFailure(failure, {
      requestId: 'another-request',
      url: failure.url,
      method: 'GET',
      reasonName: 'AbortError',
    })).toBe('unexpected')
    expect(classifyNetworkFailure(failure, {
      requestId: 'e2e-doc-7',
      url: failure.url,
      method: 'GET',
      reasonName: 'TimeoutError',
    })).toBe('unexpected')
    expect(classifyNetworkFailure(failure, {
      requestId: 'e2e-doc-7',
      url: failure.url,
      method: 'GET',
      reasonName: 'AbortError',
    })).toBe('caller_abort')
  })

  it('classifies a request as navigation-superseded only after a successor commits in the same frame', () => {
    const failed = {
      method: 'GET',
      url: 'http://127.0.0.1:5199/api/v1/runs/run_7',
      errorText: 'net::ERR_ABORTED',
      isNavigationRequest: false,
    }
    expect(classifyNetworkFailure(failed, undefined, {
      startedAfterOriginalRequest: true,
      startedBeforeFailure: true,
      committedInSameFrame: false,
    })).toBe('unexpected')
    expect(classifyNetworkFailure(failed, undefined, {
      startedAfterOriginalRequest: true,
      startedBeforeFailure: false,
      committedInSameFrame: true,
    })).toBe('unexpected')
    expect(classifyNetworkFailure(failed, undefined, {
      startedAfterOriginalRequest: false,
      startedBeforeFailure: true,
      committedInSameFrame: true,
    })).toBe('unexpected')
    // A fetch/subresource can be canceled only by a same-frame navigation that began first and committed.
    expect(classifyNetworkFailure(failed, undefined, {
      startedAfterOriginalRequest: true,
      startedBeforeFailure: true,
      committedInSameFrame: true,
    })).toBe('superseded_navigation')
  })

  it('never lets an unknown non-abort network failure disappear', () => {
    const unexpected: NetworkFailure = disconnectedRequest({ errorText: 'net::ERR_CONNECTION_RESET' })
    expect(evaluateFailureOracle([], [unexpected], []).findings).toHaveLength(1)
  })
})
