export default defineNuxtConfig({
  compatibilityDate: '2026-09-01',
  modules: ['@nuxt/ui'],
  css: ['~/assets/css/main.css'],
  ssr: true,
  devtools: { enabled: false },
  runtimeConfig: {
    forgeApi: 'http://127.0.0.1:8788',   // NITRO_FORGE_API overrides
    helpDoc: '../docs/onboarding.md',    // NITRO_HELP_DOC overrides
  },
  app: {
    head: {
      titleTemplate: '%s · Forge',
      htmlAttrs: { lang: 'en', class: 'dark' },
      link: [{ rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg' }],
    },
  },
  colorMode: { preference: 'dark', fallback: 'dark', classSuffix: '' },
  nitro: { compressPublicAssets: false },
})
