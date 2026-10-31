import { describe, expect, it } from 'vitest'
import {
  classifyNetworkFailure,
  evaluateFailureOracle as evaluateAtApplicationOrigin,
  safeFailureText,
  safeFailureUrl,
  safeNetworkFailureEvidence,
  validateFailureAllowances,
  type FailureAllowance,
  type FailedResponse,
  type NetworkFailure,
  type NetworkFailureAllowance,
} from '../../e2e/failure-oracle'

const APPLICATION_ORIGIN = 'http://127.0.0.1:5199'

const evaluateFailureOracle = (
  responses: readonly FailedResponse[],
  networkFailures: readonly NetworkFailure[],
  allowances: readonly FailureAllowance[],
) => evaluateAtApplicationOrigin(responses, networkFailures, allowances, APPLICATION_ORIGIN)

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

  it('redacts URL userinfo and sensitive query values from oracle messages and attachments', () => {
    const url = 'http://e2e-user:credential-secret@127.0.0.1:5199/api/v1/workflows/wfl_42/publish?version=1&password=query-secret&api_key=key-secret&session=session-secret&sig=signed-secret#hash-secret'
    const safeUrl = safeFailureUrl(url)
    expect(safeUrl).toBe(
      'http://127.0.0.1:5199/api/v1/workflows/wfl_42/publish?version=1&password=%5Bredacted%5D&api_key=%5Bredacted%5D&session=%5Bredacted%5D&sig=%5Bredacted%5D',
    )

    const result = evaluateFailureOracle(
      [invalidPublishResponse({ url, status: 500 })],
      [],
      [validHttpAllowance()],
    )
    expect(result.findings).toHaveLength(1)
    expect(result.findings[0]).toContain(safeUrl)
    expect(result.findings.join(' ')).not.toContain('e2e-user')
    expect(result.findings.join(' ')).not.toContain('credential-secret')
    expect(result.findings.join(' ')).not.toContain('query-secret')
    expect(result.findings.join(' ')).not.toContain('key-secret')
    expect(result.findings.join(' ')).not.toContain('session-secret')
    expect(result.findings.join(' ')).not.toContain('signed-secret')
    expect(result.findings.join(' ')).not.toContain('hash-secret')
  })

  it('redacts credential-like path values as well as URL userinfo and query values', () => {
    const raw = [
      'http://127.0.0.1:5199/api/v1/tokens/one-time-token-secret',
      'http://127.0.0.1:5199/api/v1/password-reset/reset-code-secret',
      'http://127.0.0.1:5199/api/v1/callback/token=inline-path-secret',
      'http://127.0.0.1:5199/api/v1/session/eyJhbGciOiJub25lIn0.eyJzdWIiOiJ1c2VyIn0.signature-value',
      'http://127.0.0.1:5199/api/v1/password/reset/nested-reset-secret',
      'http://127.0.0.1:5199/api/v1/tokens%252Fdouble-encoded-secret',
      'http://127.0.0.1:5199/api/v1/tokens%252525252Ftriple-encoded-secret',
      'http://127.0.0.1:5199/api/v1/api_key_x',
    ]
    const safe = raw.map(safeFailureUrl)

    expect(safe[0]).toContain('/api/v1/tokens/%5Bredacted%5D')
    expect(safe[1]).toContain('/api/v1/password-reset/%5Bredacted%5D')
    expect(safe[2]).toContain('/api/v1/callback/%5Bredacted%5D')
    expect(safe[3]).toContain('/api/v1/session/%5Bredacted%5D')
    expect(safe[4]).toContain('/api/v1/password/%5Bredacted%5D/%5Bredacted%5D')
    expect(safe[5]).not.toContain('double-encoded-secret')
    expect(safe[6]).not.toContain('triple-encoded-secret')
    expect(safe[7]).toContain('/api/v1/%5Bredacted%5D')
    for (const secret of [
      'one-time-token-secret',
      'reset-code-secret',
      'inline-path-secret',
      'signature-value',
      'nested-reset-secret',
      'double-encoded-secret',
      'triple-encoded-secret',
    ]) {
      expect(safe.join(' ')).not.toContain(secret)
    }

    const failure = invalidPublishResponse({
      url: 'http://127.0.0.1:5199/api/v1/tokens/path-secret-value',
      status: 500,
    })
    const findings = evaluateFailureOracle([failure], [], [validHttpAllowance()]).findings.join(' ')
    expect(findings).not.toContain('path-secret-value')
    expect(findings).toContain('/api/v1/tokens/%5Bredacted%5D')
  })

  it('redacts credentials and sensitive values from free-form diagnostic text', () => {
    const raw = [
      'Bearer header-secret',
      'password="body-secret with spaces"',
      "{\"passphrase\": \"json secret with spaces\"}",
      'authorization: unquoted-secret with spaces; status=502',
      'https://user:url-secret@127.0.0.1:5199/api/v1/items?api_key=query-secret',
    ].join(' ')
    const safe = safeFailureText(raw)
    expect(safe).not.toContain('header-secret')
    expect(safe).not.toContain('body-secret')
    expect(safe).not.toContain('json secret')
    expect(safe).not.toContain('unquoted-secret')
    expect(safe).not.toContain('url-secret')
    expect(safe).not.toContain('query-secret')
    expect(safe).toContain('Bearer [redacted]')
    expect(safe).toContain('status=502')
  })

  it('redacts network errorText in findings and the JSON attachment evidence', () => {
    const failure = disconnectedRequest({
      url: 'http://e2e-user:url-secret@127.0.0.1:5199/api/v1/runs?password=query-secret',
      errorText: 'net::ERR_FAILED password="network secret with spaces" api_key=inline-key-secret',
      resourceType: 'fetch token=resource-secret',
    })

    const result = evaluateFailureOracle([], [failure], [])
    const evidence = safeNetworkFailureEvidence(failure)
    const serialized = JSON.stringify(evidence)

    for (const secret of [
      'url-secret',
      'query-secret',
      'network secret',
      'inline-key-secret',
      'resource-secret',
    ]) {
      expect(result.findings.join(' ')).not.toContain(secret)
      expect(serialized).not.toContain(secret)
    }
    expect(result.findings.join(' ')).toContain('password=[redacted]')
    expect(evidence.errorText).toContain('api_key=[redacted]')
    expect(evidence.url).not.toContain('e2e-user')
  })

  it('fails closed for an invalid application origin and refuses cross-origin lookalike failures', () => {
    expect(evaluateAtApplicationOrigin([], [], [], 'http://127.0.0.1:5199/api/v1')).toEqual({
      findings: ['expected application origin must be a canonical absolute HTTP(S) origin'],
      matchedCounts: [],
    })

    const foreignResponse = invalidPublishResponse({
      url: 'https://attacker.example/api/v1/workflows/wfl_42/publish?version=1',
    })
    const httpResult = evaluateFailureOracle([foreignResponse], [], [validHttpAllowance()])
    expect(httpResult.matchedCounts).toEqual([0])
    expect(httpResult.findings).toHaveLength(1)
    expect(httpResult.findings[0]).toContain('https://attacker.example/')

    const foreignNetworkFailure = disconnectedRequest({
      url: 'https://attacker.example/api/v1/runs?limit=100',
    })
    const networkResult = evaluateFailureOracle([], [foreignNetworkFailure], [{
      kind: 'network',
      method: 'GET',
      pathname: '/api/v1/runs',
      query: '?limit=100',
      errorText: 'net::ERR_INTERNET_DISCONNECTED',
      maxMatches: 1,
      reason: 'the in-app runs request is deliberately taken offline and recovery is asserted',
    }])
    expect(networkResult.matchedCounts).toEqual([0])
    expect(networkResult.findings).toHaveLength(1)
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
      reasonName: 'UnknownAbortReason',
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
