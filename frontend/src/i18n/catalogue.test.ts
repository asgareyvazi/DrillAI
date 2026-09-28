/**
 * The two catalogues have to describe the same interface.
 *
 * A missing Persian key does not throw: `t()` falls back to English and the screen shows a
 * half-translated page, which is exactly the kind of defect nobody notices until a user does. This
 * test walks both catalogues and fails on any key that exists in one and not the other — the same
 * reason a missing translation should be a build failure rather than a runtime surprise.
 */

import { describe, expect, it } from 'vitest'
import en from './en'
import fa from './fa'

function flatten(value: unknown, prefix = ''): string[] {
  if (typeof value !== 'object' || value === null) return [prefix]
  return Object.entries(value as Record<string, unknown>).flatMap(([key, child]) =>
    flatten(child, prefix ? `${prefix}.${key}` : key),
  )
}

describe('message catalogues', () => {
  it('define the same keys', () => {
    const english = flatten(en).sort()
    const persian = flatten(fa).sort()
    const missingInPersian = english.filter((key) => !persian.includes(key))
    const missingInEnglish = persian.filter((key) => !english.includes(key))
    expect(missingInPersian, 'keys present in en.ts but not fa.ts').toEqual([])
    expect(missingInEnglish, 'keys present in fa.ts but not en.ts').toEqual([])
  })

  it('translate every leaf with a non-empty string', () => {
    for (const [locale, catalogue] of [
      ['en', en],
      ['fa', fa],
    ] as const) {
      for (const key of flatten(catalogue)) {
        const value = key
          .split('.')
          .reduce<unknown>((node, part) => (node as Record<string, unknown>)[part], catalogue)
        expect(typeof value, `${locale}:${key} must be a string`).toBe('string')
        expect((value as string).trim(), `${locale}:${key} must not be empty`).not.toBe('')
      }
    }
  })

  it('translates every Persian value, rather than copying the English one', () => {
    // A key that exists and is still English is the failure this catches: the page renders, nothing
    // throws, and the Persian interface silently shows another language.
    const persian = /[\u0600-\u06FF]/
    for (const key of flatten(fa)) {
      const value = key
        .split('.')
        .reduce<unknown>((node, part) => (node as Record<string, unknown>)[part], fa) as string
      expect(persian.test(value), `fa:${key} = ${JSON.stringify(value)} is not translated`).toBe(true)
    }
  })
})
