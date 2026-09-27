export default defineNuxtConfig({
  compatibilityDate: '2026-09-01',
  modules: ['@nuxt/ui'],
  css: ['~/assets/css/main.css'],
  ssr: true,
  devtools: { enabled: false },
  runtimeConfig: {
    forgeApi: 'http://127.0.0.1:8788',   // NITRO_FORGE_API overrides
    forgeUpstreamSecretFile: '',         // NITRO_FORGE_UPSTREAM_SECRET_FILE: proves requests come through this UI
    helpDoc: '../docs/onboarding.md',    // NITRO_HELP_DOC overrides
    public: { pollInterval: 5000 },      // NUXT_PUBLIC_POLL_INTERVAL overrides
  },
  app: {
    head: {
      titleTemplate: '%s · Forge',
      htmlAttrs: { lang: 'en', class: 'dark' },
      link: [{ rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg' }],
    },
  },
  colorMode: { preference: 'dark', fallback: 'dark', classSuffix: '' },
  // Nuxt UI draws its own `i-lucide-*` icons (loading spinners, the
  // USelectMenu chevron and check). @nuxt/icon serves those from
  // /api/_nuxt_icon/**, and under the default `serverBundle: 'auto'` it
  // fetches them from the Iconify API at runtime — which a Forge server has
  // no route to. `local` compiles @iconify-json/lucide into the Nitro
  // bundle, so the icons come off disk and nothing leaves the box.
  icon: { serverBundle: 'local' },
  nitro: { compressPublicAssets: false },
})
