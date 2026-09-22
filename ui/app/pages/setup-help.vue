<script setup lang="ts">
// GET /_ui/help resolves and renders docs/onboarding.md (or the installation
// copy at $FORGE_ROOT/docs/onboarding.md) on the server; this page only lays
// the result out. It is the target of the "Waiting for the Forge service"
// and "GitHub repository protection" deep links other pages send operators
// to (#host-installation, #github-repository-protection), so the headings'
// ids come straight from the server response, never recomputed here.
type TocEntry = { id: string; text: string; level: 1 | 2 }
type HelpDoc = { html: string; toc: TocEntry[] }

useHead({ title: 'Setup help' })

const { data, error } = await useAsyncData('setup-help', () => $fetch<HelpDoc>('/_ui/help'))
const toc = computed(() => data.value?.toc ?? [])
const html = computed(() => data.value?.html ?? '')
</script>

<template>
  <div class="grid gap-8 lg:grid-cols-[220px_minmax(0,760px)] lg:gap-12">
    <aside>
      <nav aria-label="Help topics" class="flex flex-wrap gap-x-4 gap-y-1 text-sm lg:sticky lg:top-24 lg:flex-col lg:gap-1">
        <p class="mb-1 w-full text-xs font-semibold uppercase tracking-wide text-muted lg:mb-2">On this page</p>
        <a
          v-for="entry in toc"
          :key="entry.id"
          :href="`#${entry.id}`"
          class="rounded-md px-2 py-1.5 text-muted hover:bg-panel hover:text-white lg:px-2.5"
        >{{ entry.text }}</a>
      </nav>
    </aside>
    <!-- A `div`, not `main`: the layout already provides the page's one
         `<main>` landmark, and nesting a second one is invalid HTML. -->
    <div class="min-w-0">
      <p id="help-eyebrow" class="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Help</p>
      <UAlert
        v-if="error"
        role="status"
        color="warning"
        variant="subtle"
        description="Setup help is unavailable. Check the server configuration."
      />
      <!-- eslint-disable-next-line vue/no-v-html -- sanitized server-side by server/utils/help.ts -->
      <div v-else class="forge-help text-sm text-white/90" v-html="html" />
    </div>
  </div>
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
