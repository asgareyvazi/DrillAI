/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // An oilfield palette: steel/graphite neutrals with a single signal accent, so the UI reads
        // as an engineering instrument rather than a consumer dashboard.
        graphite: {
          50: '#f6f7f9', 100: '#eceef2', 200: '#d5dae3', 300: '#b0b9c8',
          400: '#8492a8', 500: '#64748b', 600: '#4e5a70', 700: '#3f4859',
          800: '#2b3140', 900: '#1c212b', 950: '#12151c',
        },
        signal: {
          DEFAULT: '#0f7b8f', deep: '#0a5c6b', light: '#3aa8bd',
        },
        warning: '#b45309',
        danger: '#b91c1c',
        ok: '#15803d',
      },
      fontFamily: {
        sans: ['Inter', 'Segoe UI', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
    },
  },
  plugins: [],
}
