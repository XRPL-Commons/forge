<script setup lang="ts">
import { helpDiagramRenderer } from '../utils/help-diagrams'
const props = defineProps<{ source: string; title: string }>()
const id = `help-diagram-${useId().replace(/[^a-zA-Z0-9-]/g, '-')}`
const svg = ref('')
const width = ref(300)
const fit = ref(false)
const failed = ref(false)
let generation = 0
async function render() {
  const current = ++generation
  svg.value = ''
  failed.value = false
  try {
    const mermaid = await helpDiagramRenderer()
    if (current !== generation) return
    const result = await mermaid.render(id, props.source)
    if (current === generation) {
      const viewBox = /viewBox="([^"]+)"/.exec(result.svg)?.[1]?.trim().split(/\s+/).map(Number)
      width.value = Math.max(300, Math.min(4000, viewBox?.[2] || 540))
      svg.value = result.svg
    }
  } catch { if (current === generation) failed.value = true }
}
onMounted(render)
watch(() => props.source, render)
onBeforeUnmount(() => { generation++ })
</script>

<template>
  <figure class="my-5 min-w-0 rounded-xl border border-line bg-panel p-3 sm:p-4">
    <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
      <figcaption class="font-semibold">{{ title }}</figcaption>
      <button v-if="svg" type="button" class="min-h-11 rounded-full border border-line px-3 text-sm text-muted hover:text-white" :aria-pressed="fit" @click="fit = !fit">{{ fit ? 'Actual size' : 'Fit diagram' }}</button>
    </div>
    <p v-if="svg && !fit" class="mb-3 text-xs text-muted">Scroll sideways to explore the diagram.</p>
    <!-- Mermaid strict mode sanitizes its SVG and disables diagram links. -->
    <div v-if="svg" class="help-diagram overflow-x-auto" tabindex="0" role="region" :aria-label="`${title} diagram`">
      <div class="mx-auto" :style="{ width: fit ? '100%' : `${width}px` }" v-html="svg" />
    </div>
    <p v-else role="status" class="text-muted">{{ failed ? 'Diagram unavailable. Its source and the explanation below are still available.' : 'Loading diagram…' }}</p>
    <details :open="failed" class="mt-3">
      <summary class="min-h-11 cursor-pointer py-3 text-sm text-muted">Diagram source</summary>
      <pre class="overflow-x-auto text-xs"><code>{{ source }}</code></pre>
    </details>
  </figure>
</template>

<style scoped>
.help-diagram :deep(svg) { display: block; margin: auto; width: 100%; max-width: none !important; }
</style>
