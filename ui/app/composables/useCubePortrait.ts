// Shared plumbing for the cube portraits on issue cards, so a page with many
// cards costs one IntersectionObserver and one throttled timer, never one
// animation loop per card. A portrait draws a still frame when it first comes
// into view and when its look changes; the only motion is an occasional blink
// of one visible cube, and none at all with reduced motion or a hidden tab.
import type { Ref } from 'vue'

interface Portrait { el: HTMLElement; visible: boolean; show: () => void; blink: (closed: boolean) => void }

const BLINK_EVERY = 2600
const BLINK_FOR = 140

const portraits = new Map<HTMLElement, Portrait>()
let observer: IntersectionObserver | null = null
let timer: ReturnType<typeof setInterval> | null = null
let motion: MediaQueryList | null = null

function still(): boolean {
  return !!motion?.matches || document.visibilityState !== 'visible'
}

function tick() {
  if (still()) return
  const visible = [...portraits.values()].filter(portrait => portrait.visible)
  const portrait = visible[Math.floor(Math.random() * visible.length)]
  if (!portrait) return
  portrait.blink(true)
  setTimeout(() => { if (portraits.get(portrait.el) === portrait) portrait.blink(false) }, BLINK_FOR)
}

function setup() {
  if (!observer) {
    observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const portrait = portraits.get(entry.target as HTMLElement)
        if (!portrait) continue
        portrait.visible = entry.isIntersecting
        if (entry.isIntersecting) portrait.show()
      }
    }, { rootMargin: '120px 0px' })
  }
  motion ??= window.matchMedia('(prefers-reduced-motion: reduce)')
  timer ??= setInterval(tick, BLINK_EVERY)
}

function teardown() {
  if (portraits.size) return
  observer?.disconnect(); observer = null
  if (timer) clearInterval(timer)
  timer = null
}

// `show` draws the current look (it is called again whenever the canvas comes
// back into view, and is expected to skip a frame it already drew); `blink`
// redraws with the eyes closed or open again.
export function useCubePortrait(el: Ref<HTMLElement | null>, handlers: { show: () => void; blink: (closed: boolean) => void }) {
  const visible = () => !!el.value && !!portraits.get(el.value)?.visible
  onMounted(() => {
    const node = el.value
    if (!node || typeof IntersectionObserver === 'undefined') { handlers.show(); return }
    setup()
    portraits.set(node, { el: node, visible: false, ...handlers })
    observer!.observe(node)
  })
  onBeforeUnmount(() => {
    const node = el.value
    if (!node || !portraits.has(node)) return
    observer?.unobserve(node)
    portraits.delete(node)
    teardown()
  })
  return { visible }
}
