<script setup lang="ts">
import { summarizeActivity, ROLE_LABELS, ROLE_TARGET, type Role } from '~/utils/activity'
const now = useClock()
const poll = useFactory()
onMounted(poll.start)
const summary = computed(() => poll.data.value?.ok !== false && poll.data.value?.active && poll.stale.value === false ? summarizeActivity(poll.data.value, now.value) : null)
// The queue is the one entry that is not a worker role, so the item type
// says so rather than smuggling it past the role type.
type PillRole = Role | 'queued'
// Each count carries a visible word, so roles never rely on colour alone.
const WORD: Record<PillRole, string> = { build: 'building', review: 'reviewing', audit: 'auditing', merge: 'merging', recovery: 'investigating', queued: 'queued' }
interface Pill { role: PillRole; count: number; label: string; to: string }
const items = computed<Pill[]>(() => {
  if (!summary.value || !summary.value.fresh) return []
  const out: Pill[] = (Object.keys(ROLE_LABELS) as Role[]).filter(r => summary.value!.working[r]).map(r => ({ role: r, count: summary.value!.working[r], label: `${summary.value!.working[r]} ${ROLE_LABELS[r][summary.value!.working[r] === 1 ? 0 : 1]} working`, to: ROLE_TARGET[r] }))
  if (summary.value.queued) out.push({ role: 'queued', count: summary.value.queued, label: `${summary.value.queued} queued`, to: '/#factory-line' })
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
      :style="item.role !== 'queued' ? { color: `var(--color-role-${item.role})` } : undefined"
      :data-role="item.role"
    >
      <span v-if="item.role !== 'queued'" class="size-3 rounded-full border border-current border-r-transparent animate-spin motion-reduce:animate-none" aria-hidden="true" />{{ item.count }}&nbsp;{{ WORD[item.role] }}
    </NuxtLink>
  </span>
</template>
