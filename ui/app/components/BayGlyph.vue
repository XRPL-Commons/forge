<script setup lang="ts">
import { BAY_GLYPHS, GLYPH_INK } from '~/utils/bayGlyphs'
import type { AttentionReason } from '~/utils/factoryLine'

// The crate bay's character (a stamp, a speech bubble…) as an inline SVG,
// for the modal's filter chips and the hover cards. The canvas draws the
// same paths.
const props = withDefaults(defineProps<{ reason: AttentionReason; size?: number }>(), { size: 16 })
const parts = computed(() => BAY_GLYPHS[props.reason])
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    class="bay-glyph inline-block shrink-0"
    aria-hidden="true"
    :data-glyph="reason"
  >
    <path
      v-for="(part, index) in parts"
      :key="index"
      :d="part.d"
      :fill="part.fill ?? 'none'"
      :stroke="part.stroke === 'none' ? undefined : part.stroke ?? GLYPH_INK"
      :stroke-width="part.stroke === 'none' ? undefined : part.width ?? 1.4"
      stroke-linecap="round"
      stroke-linejoin="round"
    />
  </svg>
</template>
