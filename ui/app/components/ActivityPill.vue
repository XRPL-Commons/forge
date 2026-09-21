<script setup lang="ts">
import { summarizeActivity, ROLE_LABELS, type Role } from '~/utils/activity'
const api = useForgeApi()
const poll = usePoll(() => api.get<any>('/api/factory'), { interval: 15000 })
onMounted(poll.start)
const summary = computed(() => poll.data.value?.active && poll.stale.value === false ? summarizeActivity(poll.data.value, Date.now() / 1000) : null)
const ROLE_TARGET: Record<Role, string> = { build: '/#stage-build', review: '/#stage-review', audit: '/#stage-audit', merge: '/#runners', recovery: '/#runners' }
const ROLE_COLOR: Record<Role, string> = { build: '#94bfff', review: '#c9a4f4', audit: '#80d7bd', merge: '#edbd77', recovery: '#c0cbcd' }
const items = computed(() => {
  if (!summary.value || !summary.value.fresh) return []
  const out = (Object.keys(ROLE_LABELS) as Role[]).filter(r => summary.value!.working[r]).map(r => ({ role: r, count: summary.value!.working[r], label: `${summary.value!.working[r]} ${ROLE_LABELS[r][summary.value!.working[r] === 1 ? 0 : 1]} working`, to: ROLE_TARGET[r] }))
  if (summary.value.queued) out.push({ role: 'queued' as any, count: summary.value.queued, label: `${summary.value.queued} queued`, to: '/#columns' })
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
