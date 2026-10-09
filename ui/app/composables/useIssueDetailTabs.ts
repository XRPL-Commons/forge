import { issueTab, tabQuery, type IssueTab } from '~/utils/discussion'
import type { IssueDetail as IssueDetailView } from '~/utils/issues'
import type { Ref, ComputedRef } from 'vue'

export function useIssueDetailTabs(props: { fill?: 'container' | 'viewport' }, detail: ComputedRef<IssueDetailView | null>, conversation: Ref<{ focus: () => boolean } | null>) {
  const route = useRoute()
  const router = useRouter()
  const TABS = [
    { label: 'Details', value: 'details', slot: 'details' as const, class: 'pt-4' },
    { label: 'Discussion', value: 'discussion', slot: 'discussion' as const, class: 'flex min-h-0 flex-1 flex-col pt-3' },
  ]
  const tab = ref<IssueTab>(issueTab(route.query))
  // The URL follows the tab, and the tab follows the URL (back and forward),
  // except while the panel's own writes are still landing: a quick switch
  // back and forth must not be undone by the earlier navigation.
  let writing = 0
  watch(() => route.query.tab, () => { if (!writing) tab.value = issueTab(route.query) })
  // What the chat reports for the tab's dot: a reply in progress, or one that
  // arrived while the operator was on Details.
  const chat = ref({ replying: false, unread: false })
  const chatAttention = computed(() => tab.value !== 'discussion' && (chat.value.replying || chat.value.unread))
  const root = ref<HTMLElement | null>(null)
  let detailsScroll = 0
  function scroller(): HTMLElement | null {
    let node = root.value?.parentElement ?? null
    while (node && node !== document.body) {
      if (/auto|scroll/.test(getComputedStyle(node).overflowY)) return node
      node = node.parentElement
    }
    return document.scrollingElement as HTMLElement | null
  }
  // The page's chat fills the window below everything above it, so the prompt
  // sits at the bottom of the screen and the window itself does not scroll.
  const fillHeight = ref<string | undefined>(undefined)
  function measure() {
    if (props.fill !== 'viewport' || !root.value) return
    const top = root.value.getBoundingClientRect().top + window.scrollY
    fillHeight.value = `calc(100dvh - ${Math.round(top)}px - 1.5rem)`
  }
  // The panel is revealed a frame later (the tabs' presence transition), and a
  // tab clicked with the mouse takes focus on mousedown: focus the box after.
  async function openChat() {
    await nextTick()
    measure()
    for (let frame = 0; frame < 10; frame++) {
      await new Promise(resolve => requestAnimationFrame(resolve))
      if (tab.value !== 'discussion') return
      if (conversation.value?.focus()) return
    }
  }
  watch(tab, async (next, previous) => {
    if (previous === 'details') detailsScroll = scroller()?.scrollTop ?? 0
    if (writing || issueTab(route.query) !== next) {
      writing++
      void router.replace({ query: tabQuery(route.query, next) }).finally(() => { writing-- })
    }
    if (next === 'discussion') return openChat()
    await nextTick()
    const box = scroller()
    if (box) box.scrollTop = detailsScroll
  })
  // A link straight to the discussion: focus waits in the message box once the
  // issue (and with it the chat) has rendered.
  const stopInitialFocus = watch(conversation, (value) => {
    if (!value) return
    if (tab.value === 'discussion') void openChat()
    stopInitialFocus()
  }, { flush: 'post' })
  onMounted(() => window.addEventListener('resize', measure))
  onBeforeUnmount(() => window.removeEventListener('resize', measure))
  const filling = computed(() => !!detail.value && tab.value === 'discussion')
  const rootClass = computed(() => {
    if (!filling.value) return ''
    return props.fill === 'container' ? 'flex h-full min-h-0 flex-col' : 'flex min-h-96 flex-col'
  })

  return { root, rootClass, filling, fillHeight, tab, TABS, chat, chatAttention }
}
