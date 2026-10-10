/**
 * Internationalisation and text direction.
 *
 * Built in from the start rather than retrofitted: every user-visible string goes through `t()`,
 * the catalogue is typed against the English one so a missing Persian key is a compile error, and
 * direction (`ltr`/`rtl`) plus `lang` are applied to the document element so layout mirrors for
 * Persian. Engineering values are formatted by `lib/format.ts`, which is locale-aware too.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import en from './en'
import fa from './fa'

export const LOCALES = ['en', 'fa'] as const
export type Locale = (typeof LOCALES)[number]

const CATALOGUES: Record<Locale, Record<string, unknown>> = { en, fa }

export const LOCALE_LABELS: Record<Locale, string> = { en: 'English', fa: 'فارسی' }

/** Directions are a property of the language, not a layout preference. */
export const LOCALE_DIRECTION: Record<Locale, 'ltr' | 'rtl'> = { en: 'ltr', fa: 'rtl' }

const STORAGE_KEY = 'drillai.locale'

function lookup(catalogue: Record<string, unknown>, key: string): string | undefined {
  const parts = key.split('.')
  let current: unknown = catalogue
  for (const part of parts) {
    if (typeof current !== 'object' || current === null) return undefined
    current = (current as Record<string, unknown>)[part]
  }
  return typeof current === 'string' ? current : undefined
}

interface I18nValue {
  locale: Locale
  direction: 'ltr' | 'rtl'
  setLocale: (locale: Locale) => void
  /** Translate a dotted key. Returns the key itself plus a marker when a translation is missing. */
  t: (key: string, vars?: Record<string, string | number>) => string
}

const I18nContext = createContext<I18nValue | null>(null)

function initialLocale(): Locale {
  if (typeof window === 'undefined') return 'en'
  const stored = window.localStorage.getItem(STORAGE_KEY)
  if (stored && (LOCALES as readonly string[]).includes(stored)) return stored as Locale
  const preferred = window.navigator.language?.slice(0, 2)
  return preferred === 'fa' ? 'fa' : 'en'
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(initialLocale)

  useEffect(() => {
    const direction = LOCALE_DIRECTION[locale]
    document.documentElement.lang = locale
    document.documentElement.dir = direction
  }, [locale])

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next)
    window.localStorage.setItem(STORAGE_KEY, next)
  }, [])

  const value = useMemo<I18nValue>(() => {
    const catalogue = CATALOGUES[locale]
    const fallback = CATALOGUES.en
    return {
      locale,
      direction: LOCALE_DIRECTION[locale],
      setLocale,
      t: (key, vars) => {
        const template = lookup(catalogue, key) ?? lookup(fallback, key) ?? `⟦${key}⟧`
        if (!vars) return template
        return Object.entries(vars).reduce(
          (text, [name, replacement]) => text.replaceAll(`{${name}}`, String(replacement)),
          template,
        )
      },
    }
  }, [locale, setLocale])

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18nValue {
  const value = useContext(I18nContext)
  if (!value) throw new Error('useI18n must be used inside <I18nProvider>')
  return value
}
