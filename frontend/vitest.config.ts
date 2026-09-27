import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config'

/**
 * The test configuration extends the application configuration rather than repeating it.
 *
 * A separate file is used because `vite.config.ts` must stay the *application* config: adding a
 * Vitest-only `test` block to it forces a type cast, and a cast there would hide real
 * configuration mistakes as well as this one. `mergeConfig` keeps the plugin, alias and proxy
 * settings in one place, so a test that imports `@/…` resolves exactly as the application does.
 */
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: ['./src/test/setup.ts'],
      include: ['src/**/*.test.{ts,tsx}'],
      css: false,
      restoreMocks: true,
      clearMocks: true,
    },
  }),
)
