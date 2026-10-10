/**
 * Permission matching, pinned against the platform's role catalogue.
 *
 * The patterns below are copied from `drillai.security.rbac.ROLE_CATALOGUE`; if the catalogue or the
 * matching rules change on the server, these tests fail and the interface stops disagreeing with the
 * authority. The end-to-end suite additionally switches identity and asserts the buttons the UI
 * offers are the ones the server accepts.
 */

import { describe, expect, it } from 'vitest'
import { holdsAll, holdsPermission, patternMatches } from './permissions'

const VIEWER = ['well.read', 'project.read', 'workflow.read', 'registry.read']
const ENGINEER = [...VIEWER, 'workflow.draft', 'workflow.run', 'engineer.engine.run']
const SUPERVISOR = [...ENGINEER, 'workflow.publish', 'approval.decide']
const WELL_MANAGER = ['**', 'registry.read']

describe('patternMatches', () => {
  it('matches a permission exactly', () => {
    expect(patternMatches('workflow.read', 'workflow.read')).toBe(true)
    expect(patternMatches('workflow.read', 'workflow.publish')).toBe(false)
  })

  it('treats `.*` as exactly one more segment', () => {
    expect(patternMatches('workflow.*', 'workflow.publish')).toBe(true)
    expect(patternMatches('workflow.*', 'workflow.read')).toBe(true)
    expect(patternMatches('workflow.*', 'workflow.publish.scheduled')).toBe(false)
    expect(patternMatches('well.*', 'workflow.read')).toBe(false)
  })

  it('treats `.**` as any depth below the prefix', () => {
    expect(patternMatches('workflow.**', 'workflow.publish')).toBe(true)
    expect(patternMatches('workflow.**', 'workflow.publish.scheduled')).toBe(true)
    expect(patternMatches('workflow.**', 'well.read')).toBe(false)
  })

  it('treats a bare star as everything, as the server does', () => {
    expect(patternMatches('**', 'workflow.publish')).toBe(true)
    expect(patternMatches('*', 'approval.decide')).toBe(true)
  })

  it('does not invent a match for an unrelated literal pattern', () => {
    expect(patternMatches('well.read', 'workflow.read')).toBe(false)
    expect(patternMatches('', 'workflow.read')).toBe(false)
  })
})

describe('holdsPermission', () => {
  it('is false for an empty or missing permission set', () => {
    expect(holdsPermission([], 'workflow.read')).toBe(false)
    expect(holdsPermission(null, 'workflow.read')).toBe(false)
    expect(holdsPermission(undefined, 'workflow.read')).toBe(false)
  })

  it('covers the seeded roles as the server evaluates them', () => {
    expect(holdsPermission(VIEWER, 'workflow.read')).toBe(true)
    expect(holdsPermission(VIEWER, 'workflow.draft')).toBe(false)
    expect(holdsPermission(VIEWER, 'workflow.publish')).toBe(false)

    expect(holdsPermission(ENGINEER, 'workflow.draft')).toBe(true)
    expect(holdsPermission(ENGINEER, 'workflow.publish')).toBe(false)

    expect(holdsPermission(SUPERVISOR, 'workflow.publish')).toBe(true)
    expect(holdsPermission(SUPERVISOR, 'approval.decide')).toBe(true)
    expect(holdsPermission(SUPERVISOR, 'org.manage')).toBe(false)

    expect(holdsPermission(WELL_MANAGER, 'workflow.publish')).toBe(true)
    expect(holdsPermission(WELL_MANAGER, 'anything.at.all')).toBe(true)
  })

  it('ignores an empty permission string rather than matching everything', () => {
    expect(holdsPermission(['**'], '')).toBe(false)
  })
})

describe('holdsAll', () => {
  it('requires every permission an action needs', () => {
    // Saving with publish: true needs both draft and publish rights.
    expect(holdsAll(SUPERVISOR, ['workflow.draft', 'workflow.publish'])).toBe(true)
    expect(holdsAll(ENGINEER, ['workflow.draft', 'workflow.publish'])).toBe(false)
  })

  it('is true for an empty requirement list', () => {
    expect(holdsAll([], [])).toBe(true)
  })
})
