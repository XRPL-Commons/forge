<script setup lang="ts">
import { drawPortrait, portraitKey, type PortraitLook } from '~/utils/cube'

// An issue card's cube: one still frame, drawn when the card first comes into
// view and again only when its look changes. Decorative — the card's text
// carries the meaning — so the canvas is hidden from assistive technology.
const props = withDefaults(defineProps<{ look: PortraitLook; size?: number }>(), { size: 56 })

const canvas = ref<HTMLCanvasElement | null>(null)
const key = computed(() => portraitKey(props.look))
let drawn = ''

function paint(blink = false) {
  const el = canvas.value
  const ctx = el?.getContext('2d')
  if (!el || !ctx) return
  const dpr = Math.min(2, window.devicePixelRatio || 1)
  const side = Math.round(props.size * dpr)
  if (el.width !== side) { el.width = side; el.height = side }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  drawPortrait(ctx, props.size, props.look, blink)
  drawn = blink ? '' : key.value
}

const { visible } = useCubePortrait(canvas, {
  show: () => { if (drawn !== key.value) paint() },
  // Only an open-eyed cube blinks; a sleeping or happy one has nothing to close.
  blink: (closed) => { if (props.look.mood !== 'sleep' && props.look.mood !== 'happy') paint(closed) },
})

watch(key, () => { if (visible()) paint(); else drawn = '' })
</script>

<template>
  <canvas
    ref="canvas"
    class="cube-portrait block shrink-0"
    aria-hidden="true"
    :data-look="key"
    :width="size"
    :height="size"
    :style="{ width: `${size}px`, height: `${size}px` }"
  />
</template>
