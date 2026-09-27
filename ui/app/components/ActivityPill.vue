<script setup lang="ts">
import { summarizeActivity, ROLE_LABELS, ROLE_TARGET, type Role } from '~/utils/activity'
const api = useForgeApi()
const pollInterval = useRuntimeConfig().public.pollInterval * 3
const poll = useFactory()
onMounted(poll.start)
const summary = computed(() => poll.data.value?.ok !== false && poll.data.value?.active && poll.stale.value === false ? summarizeActivity(poll.data.value, Date.now() / 1000) : null)
const ROLE_COLOR: Record<Role, string> = { build: '#94bfff', review: '#c9a4f4', audit: '#80d7bd', merge: '#edbd77', recovery: '#c0cbcd' }
// The queue is the one entry that is not a worker role, so the item type
// says so rather than smuggling it past the role type.
type PillRole = Role | 'queued'
interface Pill { role: PillRole; count: number; label: string; to: string }
const items = computed<Pill[]>(() => {
  if (!summary.value || !summary.value.fresh) return []
  const out: Pill[] = (Object.keys(ROLE_LABELS) as Role[]).filter(r => summary.value!.working[r]).map(r => ({ role: r, count: summary.value!.working[r], label: `${summary.value!.working[r]} ${ROLE_LABELS[r][summary.value!.working[r] === 1 ? 0 : 1]} working`, to: ROLE_TARGET[r] }))
  if (summary.value.queued) out.push({ role: 'queued', count: summary.value.queued, label: `${summary.value.queued} queued`, to: '/#columns' })
  return out
})
</script>
<template>
  <span v-if="items.length" class="inline-flex items-center gap-3 text-xs" role="status" aria-live="polite">
    <NuxtLink
      v-for="item in items"
      :key="item.role"
      :to="item.to"
      :title="item.label"
      :aria-label="item.label"
      class="inline-flex items-center gap-1"
      :class="item.role === 'queued' ? 'text-muted hover:text-white' : ''"
      :style="item.role !== 'queued' ? { color: ROLE_COLOR[item.role as Role] } : undefined"
      :data-role="item.role"
    >
      <span v-if="item.role !== 'queued'" class="size-3 rounded-full border border-current border-r-transparent animate-spin motion-reduce:animate-none" aria-hidden="true" />{{ item.count }}<span v-if="item.role === 'queued'">&nbsp;queued</span>
    </NuxtLink>
  </span>
</template>
