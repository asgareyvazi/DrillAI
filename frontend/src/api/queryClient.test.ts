/**
 * The query client's policy, asserted rather than assumed.
 *
 * Two of its settings decide how failures behave everywhere in the product, and both were wrong by
 * default: React Query retries everything twice unless told otherwise, and — much less obviously — it
 * *pauses* a request instead of failing it when the browser reports itself offline. A paused read
 * leaves the screen loading for the whole outage, with no error to show and nothing for the operator
 * to retry, which is the opposite of what the error model promises.
 */

import { describe, expect, it } from 'vitest'
import { shouldRetryRequest } from './client'
import { createQueryClient } from './queryClient'

describe('the query client', () => {
  it('retries by the classification the API client produced, not by a count of its own', () => {
    const queries = createQueryClient().getDefaultOptions().queries
    expect(queries?.retry).toBe(shouldRetryRequest)
  })

  it('attempts a read during an outage instead of pausing it until the network returns', () => {
    const queries = createQueryClient().getDefaultOptions().queries
    expect(queries?.networkMode).toBe('always')
  })

  it('does not refetch every screen when a window regains focus, and holds data for a minute', () => {
    const queries = createQueryClient().getDefaultOptions().queries
    expect(queries?.refetchOnWindowFocus).toBe(false)
    expect(queries?.staleTime).toBe(30_000)
  })
})
