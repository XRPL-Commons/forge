<script setup lang="ts">
// GET /_ui/help resolves and renders docs/onboarding.md (or the installation
// copy at $FORGE_ROOT/docs/onboarding.md) on the server; this page only lays
// the result out. It is the target of the "Waiting for the Forge service"
// and "GitHub repository protection" deep links other pages send operators
// to (#host-installation, #github-repository-protection), so the headings'
// ids come straight from the server response, never recomputed here. The
// rendered document's own typography lives in HelpBody.vue (a scoped
// :deep() stylesheet for its v-html content, mirroring MarkdownBody.vue) so
// this page stays Tailwind-utility-only.
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
      <HelpBody v-else :html="html" />
    </div>
  </div>
</template>
