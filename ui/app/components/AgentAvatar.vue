<script setup lang="ts">
import { drawAgent, type AgentMood } from '~/utils/cube'
import { persona } from '~/utils/personas'

// The discussion agent: its persona's worker (a body on two feet, unlike the
// issues' cube heads), looking around while Forge
// finds someone, busy while it works, happy with an answer, eyes shut once it
// left. Only looking and busy move, and not at all with reduced motion or a
// hidden tab. Decorative: the name beside it carries the meaning.
const props = withDefaults(defineProps<{ provider?: string | null; mood?: AgentMood; size?: number }>(), { provider: null, mood: 'happy', size: 28 })

const canvas = ref<HTMLCanvasElement | null>(null)
const who = computed(() => persona(props.provider))
let frame = 0
let motion: MediaQueryList | null = null

function still(): boolean {
  return !!motion?.matches || document.visibilityState !== 'visible'
}

function paint(t = 0) {
  const el = canvas.value
  const ctx = el?.getContext('2d')
  if (!el || !ctx) return
  const dpr = Math.min(2, window.devicePixelRatio || 1)
  const side = Math.round(props.size * dpr)
  if (el.width !== side) { el.width = side; el.height = side }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  drawAgent(ctx, props.size, who.value.fill, who.value.mark, props.mood, t, still())
}

function loop(t: number) {
  paint(t)
  frame = requestAnimationFrame(loop)
}

function run() {
  cancelAnimationFrame(frame)
  frame = 0
  if ((props.mood === 'looking' || props.mood === 'busy') && !still()) frame = requestAnimationFrame(loop)
  else paint()
}

onMounted(() => {
  motion = window.matchMedia('(prefers-reduced-motion: reduce)')
  motion.addEventListener?.('change', run)
  document.addEventListener('visibilitychange', run)
  run()
})
onBeforeUnmount(() => {
  cancelAnimationFrame(frame)
  motion?.removeEventListener?.('change', run)
  document.removeEventListener('visibilitychange', run)
})
watch(() => [props.mood, props.provider, props.size], run)
</script>

<template>
  <canvas
    ref="canvas"
    class="agent-avatar block shrink-0"
    aria-hidden="true"
    data-shape="worker"
    :data-persona="who.name"
    :data-mood="mood"
    :width="size"
    :height="size"
    :style="{ width: `${size}px`, height: `${size}px` }"
  />
</template>
