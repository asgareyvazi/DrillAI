/**
 * Catalogue coverage, checked against the code that uses it.
 *
 * `t()` falls back to `⟦key⟧` when a key is missing, which is visible at runtime but easy to miss in
 * review — a screen full of brackets in Persian only shows up if someone switches language and walks
 * into that screen. This test reads every `t('...')` call in the source tree and fails if either
 * catalogue cannot resolve it, so a key that is written but not translated fails here instead of in
 * front of a user.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import en from './en'
import fa from './fa'

// Vitest runs from the package root; the jsdom URL is not a filesystem path, so resolve from cwd.
const SOURCE_ROOT = join(process.cwd(), 'src')

function sourceFiles(dir: string, found: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      if (entry === '__screenshots__' || entry === 'node_modules') continue
      sourceFiles(full, found)
      continue
    }
    if (/\.(ts|tsx)$/.test(entry) && !entry.endsWith('.test.ts') && !entry.endsWith('.test.tsx')) {
      found.push(full)
    }
  }
  return found
}

function usedKeys(): Map<string, string[]> {
  const keys = new Map<string, string[]>()
  for (const file of sourceFiles(SOURCE_ROOT)) {
    if (file.includes('/i18n/')) continue
    const text = readFileSync(file, 'utf8')
    for (const match of text.matchAll(/\bt\(\s*'([^'\n]+)'/g)) {
      const key = match[1]
      if (key === undefined) continue
      const holders = keys.get(key) ?? []
      holders.push(file.replace(SOURCE_ROOT, ''))
      keys.set(key, holders)
    }
  }
  return keys
}

function lookup(catalogue: Record<string, unknown>, key: string): string | undefined {
  let current: unknown = catalogue
  for (const part of key.split('.')) {
    if (typeof current !== 'object' || current === null) return undefined
    current = (current as Record<string, unknown>)[part]
  }
  return typeof current === 'string' ? current : undefined
}

describe('message catalogue coverage', () => {
  const keys = usedKeys()

  it('finds the translation calls in the application source', () => {
    // A guard on the guard: if the scan silently found nothing, the other assertions would pass
    // vacuously and missing keys would go unnoticed.
    expect(keys.size).toBeGreaterThan(100)
  })

  it.each(['en', 'fa'] as const)('resolves every used key in %s', (locale) => {
    const catalogue = (locale === 'en' ? en : fa) as Record<string, unknown>
    const unresolved = [...keys.entries()]
      .filter(([key]) => lookup(catalogue, key) === undefined)
      .map(([key, holders]) => `${key} (used in ${holders.join(', ')})`)
    expect(unresolved).toEqual([])
  })
})
