<script setup lang="ts">
import { drawBench } from '~/utils/cube'
import type { WorkbenchLook } from '~/utils/workbench'

// The bench row at the top of the issue panel: the issue's cube on its
// workbench (a canvas), and a speech bubble with Forge's summary (the slot).
// The cube idles — a blink, a small bob, a hop when the stage changes between
// polls — only while the tab is visible, the canvas is on screen and motion is
// allowed; otherwise one still frame is drawn on demand.
const props = defineProps<{ look: WorkbenchLook }>()

const HEIGHT = 176
const canvas = ref<HTMLCanvasElement | null>(null)
let ctx: CanvasRenderingContext2D | null = null
let width = 0
let raf = 0, pending = false, onScreen = true, reduced = false, hop = -1e9
const seed = Math.random() * 10
let resizeObserver: ResizeObserver | null = null
let intersectionObserver: IntersectionObserver | null = null
let motionQuery: MediaQueryList | null = null

function paint(t: number) {
  if (!ctx || !width) return
  const dpr = Math.min(2, window.devicePixelRatio || 1)
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, width, HEIGHT)
  drawBench(ctx, width, HEIGHT, props.look.scene, t, hop, seed, reduced)
}
const animating = () => !reduced && onScreen && document.visibilityState === 'visible'
function tick(t: number) {
  raf = 0
  paint(t)
  if (animating()) raf = requestAnimationFrame(tick)
}
function start() { if (!raf && animating()) raf = requestAnimationFrame(tick) }
function stop() { if (raf) cancelAnimationFrame(raf); raf = 0 }
function requestDraw() {
  if (!import.meta.client || raf || pending) return
  pending = true
  requestAnimationFrame((t) => { pending = false; if (!raf) paint(t) })
}
function resize() {
  const el = canvas.value
  if (!el) return
  const dpr = Math.min(2, window.devicePixelRatio || 1)
  width = el.clientWidth
  el.width = Math.round(width * dpr)
  el.height = Math.round(HEIGHT * dpr)
  requestDraw()
}
function onVisibility() { if (animating()) start(); else stop() }
function onMotion() { reduced = !!motionQuery?.matches; onVisibility(); requestDraw() }

// A new stage between two polls: the cube hops into it.
watch(() => props.look.stage, () => { if (!reduced) hop = performance.now() })
watch(() => props.look.scene, requestDraw, { deep: true })

onMounted(() => {
  const el = canvas.value!
  ctx = el.getContext('2d')
  motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
  reduced = motionQuery.matches
  motionQuery.addEventListener('change', onMotion)
  resizeObserver = new ResizeObserver(resize)
  resizeObserver.observe(el)
  intersectionObserver = new IntersectionObserver(([entry]) => { onScreen = !!entry?.isIntersecting; onVisibility() })
  intersectionObserver.observe(el)
  document.addEventListener('visibilitychange', onVisibility)
  resize()
  start()
})
onBeforeUnmount(() => {
  stop()
  resizeObserver?.disconnect()
  intersectionObserver?.disconnect()
  document.removeEventListener('visibilitychange', onVisibility)
  motionQuery?.removeEventListener('change', onMotion)
  ctx = null
})
</script>

<template>
  <div class="workbench">
    <div class="workbench-row">
      <div class="workbench-canvas">
        <canvas
          id="issue-workbench"
          ref="canvas"
          role="img"
          :aria-label="look.label"
          :data-stage="look.stage"
          :data-mood="look.mood"
          :height="HEIGHT"
          class="block w-full"
          :style="{ height: HEIGHT + 'px' }"
        />
      </div>
      <div id="issue-bubble" class="workbench-bubble" :class="{ attention: look.needsYou }">
        <slot />
      </div>
    </div>
  </div>
</template>

<style scoped>
/* The bubble points at the cube; in a narrow panel the two stack and the
   bubble points up. */
.workbench { container-type: inline-size; }
.workbench-row { display: grid; grid-template-columns: 196px minmax(0, 1fr); gap: 14px; align-items: center; }
.workbench-canvas { overflow: hidden; border: 1px solid var(--color-line); border-radius: 10px; background: #16191f; }
/* An opaque surface, tail included: the page never shows through it, and
   only the border carries the state colour (mixed into the bubble, not into
   transparency, so it stays solid too). */
.workbench-bubble {
  --bubble-line: var(--color-bubble-line);
  position: relative; min-width: 0; padding: 12px 14px; border-radius: 14px;
  background-color: var(--color-bubble); border: 1.5px solid var(--bubble-line);
}
.workbench-bubble::before {
  content: ""; position: absolute; left: -9px; top: 50%; width: 14px; height: 14px;
  background-color: var(--color-bubble); border-left: 1.5px solid var(--bubble-line); border-bottom: 1.5px solid var(--bubble-line);
  transform: translateY(-50%) rotate(45deg);
}
.workbench-bubble.attention { --bubble-line: color-mix(in srgb, var(--color-amber-200) 65%, var(--color-bubble)); }
@container (max-width: 440px) {
  .workbench-row { grid-template-columns: minmax(0, 1fr); }
  .workbench-bubble::before { left: 50%; top: -9px; transform: translateX(-50%) rotate(135deg); }
}
</style>
