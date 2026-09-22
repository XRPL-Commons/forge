<script setup lang="ts">
// Presentational only: the HTML is rendered and sanitized on the server by
// GET /_ui/help, so this component never fetches and never sanitizes. It
// mirrors MarkdownBody.vue's scoping pattern (a scoped :deep() stylesheet for
// server-rendered v-html content) so the setup-help page itself stays
// Tailwind-utility-only. Headings get `scroll-margin-top` so a deep link
// (#host-installation, #github-repository-protection, and every id in the
// page's own "On this page" sidebar) lands clear of the sticky header instead
// of underneath it.
defineProps<{ html: string }>()
</script>

<template>
  <!-- eslint-disable-next-line vue/no-v-html -- sanitized server-side by server/utils/help.ts -->
  <div class="forge-help text-sm text-white/90" v-html="html" />
</template>

<style scoped>
.forge-help :deep(h1), .forge-help :deep(h2), .forge-help :deep(h3) {
  scroll-margin-top: 6.5rem; font-weight: 650; color: #fff;
}
.forge-help :deep(h1) { font-size: 1.75rem; margin: 0 0 0.6em; letter-spacing: -0.01em; }
.forge-help :deep(h2) { font-size: 1.25rem; margin: 1.7em 0 0.5em; }
.forge-help :deep(h3) { font-size: 1.05rem; margin: 1.3em 0 0.4em; }
.forge-help :deep(p) { margin: 0.7em 0; }
.forge-help :deep(ul), .forge-help :deep(ol) { margin: 0.7em 0; padding-left: 1.25em; }
.forge-help :deep(ul) { list-style: disc; }
.forge-help :deep(ol) { list-style: decimal; }
.forge-help :deep(li) { margin: 0.3em 0; }
.forge-help :deep(a) { color: var(--color-sky-200); text-decoration: underline; }
.forge-help :deep(code) { background: var(--color-panel); border-radius: 4px; padding: 0.1em 0.3em; font-size: 0.9em; }
.forge-help :deep(pre) { background: var(--color-panel); border: 1px solid var(--color-line); border-radius: 8px; padding: 0.9em; overflow-x: auto; }
.forge-help :deep(pre code) { background: none; padding: 0; }
</style>
