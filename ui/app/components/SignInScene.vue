<script setup lang="ts">
import { drawSignInScene, SCENE_STILL_T, type SignInMood } from '~/utils/signIn'

// The little factory above the sign-in card. Purely decorative: a clock
// drives it, nothing is fetched, and the card's text carries every message,
// so the canvas is hidden from assistive technology. One still frame with
// reduced motion; paused while the tab is hidden.
const props = withDefaults(defineProps<{ mood?: SignInMood }>(), { mood: 'waving' })

const canvas = ref<HTMLCanvasElement | null>(null)
const moving = ref(false)
let frame = 0
let size = { w: 0, h: 0 }
let motion: MediaQueryList | null = null
let observer: ResizeObserver | null = null

function still(): boolean {
  return !!motion?.matches
}

function paint(t: number) {
  const el = canvas.value
  const ctx = el?.getContext('2d')
  if (!el || !ctx || !size.w || !size.h) return
  const dpr = Math.min(2, window.devicePixelRatio || 1)
  const w = Math.round(size.w * dpr), h = Math.round(size.h * dpr)
  if (el.width !== w || el.height !== h) { el.width = w; el.height = h }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  drawSignInScene(ctx, size.w, size.h, t, still(), props.mood)
}

function loop(t: number) {
  paint(t)
  frame = requestAnimationFrame(loop)
}

function run() {
  cancelAnimationFrame(frame)
  frame = 0
  moving.value = !still() && document.visibilityState === 'visible'
  if (moving.value) frame = requestAnimationFrame(loop)
  else paint(SCENE_STILL_T)
}

function measure() {
  const el = canvas.value
  if (!el) return
  size = { w: el.clientWidth, h: el.clientHeight }
  if (!frame) paint(SCENE_STILL_T)
}

onMounted(() => {
  motion = window.matchMedia('(prefers-reduced-motion: reduce)')
  motion.addEventListener?.('change', run)
  document.addEventListener('visibilitychange', run)
  observer = new ResizeObserver(measure)
  if (canvas.value) observer.observe(canvas.value)
  measure()
  run()
})
onBeforeUnmount(() => {
  cancelAnimationFrame(frame)
  motion?.removeEventListener?.('change', run)
  document.removeEventListener('visibilitychange', run)
  observer?.disconnect()
})
watch(() => props.mood, () => { if (!frame) paint(SCENE_STILL_T) })
</script>

<template>
  <canvas
    ref="canvas"
    class="sign-in-scene block h-[170px] w-full sm:h-[230px]"
    aria-hidden="true"
    :data-mood="mood"
    :data-motion="moving ? 'moving' : 'still'"
  />
</template>
