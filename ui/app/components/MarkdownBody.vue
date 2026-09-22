<script setup lang="ts">
// Presentational only: the HTML is rendered and sanitized on the server by
// POST /_ui/markdown, so this component never fetches and never sanitizes.
// It renders the description and nothing else — the `body_truncated` note is
// the issue's, not the description's, so the detail route renders it.
const props = defineProps<{ html: string }>()
const empty = computed(() => !(props.html ?? '').trim())
</script>

<template>
  <div>
    <p v-if="empty" class="text-sm text-muted">No description has been added yet.</p>
    <!-- eslint-disable-next-line vue/no-v-html -- sanitized server-side by server/utils/markdown.ts -->
    <div v-else class="forge-markdown text-sm text-white/90" v-html="html" />
  </div>
</template>

<style scoped>
.forge-markdown :deep(h1), .forge-markdown :deep(h2), .forge-markdown :deep(h3),
.forge-markdown :deep(h4), .forge-markdown :deep(h5), .forge-markdown :deep(h6) {
  margin: 1.1em 0 0.4em; font-weight: 600; color: #fff; font-size: 1rem;
}
.forge-markdown :deep(h1) { font-size: 1.15rem; }
.forge-markdown :deep(p), .forge-markdown :deep(blockquote), .forge-markdown :deep(table) { margin: 0.6em 0; }
.forge-markdown :deep(ul), .forge-markdown :deep(ol) { margin: 0.6em 0; padding-left: 1.25em; }
.forge-markdown :deep(ul) { list-style: disc; }
.forge-markdown :deep(ol) { list-style: decimal; }
.forge-markdown :deep(li) { margin: 0.2em 0; }
.forge-markdown :deep(a) { color: var(--color-sky-200); text-decoration: underline; }
.forge-markdown :deep(code) { background: var(--color-panel); border-radius: 4px; padding: 0.1em 0.3em; font-size: 0.9em; }
.forge-markdown :deep(pre) { background: var(--color-panel); border: 1px solid var(--color-line); border-radius: 8px; padding: 0.7em; overflow-x: auto; }
.forge-markdown :deep(pre code) { background: none; padding: 0; }
.forge-markdown :deep(blockquote) { border-left: 3px solid var(--color-line); padding-left: 0.8em; color: var(--color-muted); }
.forge-markdown :deep(table) { border-collapse: collapse; display: block; overflow-x: auto; }
.forge-markdown :deep(th), .forge-markdown :deep(td) { border: 1px solid var(--color-line); padding: 0.3em 0.6em; text-align: left; }
.forge-markdown :deep(hr) { border: 0; border-top: 1px solid var(--color-line); margin: 1em 0; }
.forge-markdown :deep(input[type="checkbox"]) { margin-right: 0.4em; }
</style>
