/**
 * Switching identity, and what it must move.
 *
 * The three facts below are the reason this bridge exists at all. Each of them was, at some point,
 * only *believed* to hold:
 *
 * 1. the identity the API client sends changes,
 * 2. the answers fetched for the previous identity are gone rather than merely being refetched, and
 * 3. a display preference does not touch either of them.
 *
 * These run against a real query client and the real session store, because the property being
 * tested is precisely *what the wiring does*, and a mocked store would test the mock.
 */

import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { getIdentity, setIdentity, subscribeIdentity } from '../api/client'
import { DEFAULT_DEV_ROLES, useSession } from './session'
import { installIdentityBridge } from './identityBridge'

let client: QueryClient
let dispose: (() => void) | null = null
let stopListening: (() => void) | null = null
let identityEvents: string[] = []

beforeEach(() => {
  localStorage.clear()
  useSession.setState({ devRoles: DEFAULT_DEV_ROLES, unitSystem: 'si' })
  // The API client's identity is global mutable state; leaving one test's principal in place would
  // let the next test's first request be attributed to it.
  setIdentity({ devRoles: null })
  client = new QueryClient()
  identityEvents = []
  // Records every identity change the client announces, so a switch can be observed from outside.
  // The subscription is owned and released by the test: the listener set is global mutable state, and
  // leaking listeners between tests would make each one see the previous ones' events.
  stopListening = subscribeIdentity(() => identityEvents.push(`${getIdentity().devRoles ?? ''}`))
})

afterEach(() => {
  dispose?.()
  dispose = null
  stopListening?.()
  stopListening = null
})

function seedCache() {
  client.setQueryData(['wells'], [{ id: 'wel_1', name: 'SYNTH-DEMO-01' }])
  client.setQueryData(['approvals'], [{ id: 'apr_1', state: 'pending' }])
  client.setQueryData(['platform-capabilities'], { items: [] })
}

describe('the identity bridge', () => {
  it('makes the session identity the one the API client sends, before any query runs', () => {
    expect(getIdentity().devRoles).toBeNull()
    dispose = installIdentityBridge(client).dispose

    expect(getIdentity().devRoles).toBe(DEFAULT_DEV_ROLES)
  })

  it('replaces the cache when the identity changes, so nothing read as the old principal stays visible', async () => {
    dispose = installIdentityBridge(client).dispose
    seedCache()
    expect(client.getQueryData(['wells'])).toBeDefined()

    useSession.getState().setDevRoles('auditor')

    expect(getIdentity().devRoles).toBe('auditor')
    // Dropped, not merely stale: an invalidated entry would still answer `getQueryData` and any
    // mounted screen would keep rendering another principal's rows until the refetch returned.
    expect(client.getQueryData(['wells'])).toBeUndefined()
    expect(client.getQueryData(['approvals'])).toBeUndefined()
    expect(client.getQueryData(['platform-capabilities'])).toBeUndefined()
    await vi.waitFor(() => {
      expect(client.getQueryCache().getAll().every((query) => query.state.data === undefined)).toBe(true)
    })
  })

  it('asks the screens that are on display to read again, under the new identity', async () => {
    // The half a `clear()` cannot do. A mounted screen is an active query: after the switch it must be
    // fetched again, and the fetch must carry the new identity — a screen left empty, or refetched as
    // the previous principal, are both failures this pins.
    const identities: Array<string | null | undefined> = []
    const queryFn = async () => {
      identities.push(getIdentity().devRoles)
      return [{ id: 'wel_1' }]
    }

    // The application's order: the bridge is in place before anything mounts.
    dispose = installIdentityBridge(client).dispose
    const observer = new QueryObserver(client, { queryKey: ['wells'], queryFn })
    const unsubscribe = observer.subscribe(() => {})
    await vi.waitFor(() => expect(observer.getCurrentResult().data).toBeDefined())

    useSession.getState().setDevRoles('auditor')

    await vi.waitFor(() => expect(observer.getCurrentResult().data).toBeDefined())
    expect(identities).toEqual([DEFAULT_DEV_ROLES, 'auditor'])
    unsubscribe()
  })

  it('leaves a screen on its loading state rather than on the previous identity’s rows', async () => {
    // Between the switch and the new answer there is no data to render, which is the truth: the old
    // answer belonged to somebody else.
    dispose = installIdentityBridge(client).dispose
    const observer = new QueryObserver(client, { queryKey: ['approvals'], queryFn: async () => ['apr_1'] })
    const unsubscribe = observer.subscribe(() => {})
    await vi.waitFor(() => expect(observer.getCurrentResult().data).toEqual(['apr_1']))

    useSession.getState().setDevRoles('data_manager')

    expect(observer.getCurrentResult().data).toBeUndefined()
    expect(observer.getCurrentResult().isPending).toBe(true)
    unsubscribe()
  })

  it('announces the change once, through the channel the event stream listens on', () => {
    // The socket cannot be closed by the cache: the stream layer learns about the switch the same way
    // this test does, and rebuilding the URL with the new identity is what stops the old principal's
    // events from arriving on the new screen.
    dispose = installIdentityBridge(client).dispose
    identityEvents = []

    useSession.getState().setDevRoles('data_manager')

    expect(identityEvents).toEqual(['data_manager'])
  })

  it('does not clear the cache when only the display units change', () => {
    dispose = installIdentityBridge(client).dispose
    // Installing the bridge is itself a change — the client had no identity before and has one now —
    // and the stream layer is told, so it opens its socket for the identity that is actually in use.
    expect(identityEvents).toEqual([DEFAULT_DEV_ROLES])
    identityEvents = []
    seedCache()

    useSession.getState().setUnitSystem('oilfield')

    // A unit preference is not an identity: dropping every read for it would lose a screen's worth of
    // engineering data and re-fetch it for a change that only affects formatting.
    expect(client.getQueryData(['wells'])).toBeDefined()
    expect(identityEvents).toEqual([])
  })

  it('ignores a write that leaves the identity unchanged', () => {
    dispose = installIdentityBridge(client).dispose
    // Installing is itself a change (there was no identity before), and the event stream is told so it
    // opens its socket for the identity actually in use. What this test is about starts after that.
    identityEvents = []
    seedCache()

    useSession.getState().setDevRoles(DEFAULT_DEV_ROLES)

    expect(client.getQueryData(['wells'])).toBeDefined()
    expect(identityEvents).toEqual([])
  })

  it('stops reacting once it is disposed', () => {
    const bridge = installIdentityBridge(client)
    seedCache()
    bridge.dispose()

    useSession.getState().setDevRoles('auditor')

    expect(client.getQueryData(['wells'])).toBeDefined()
  })
})
