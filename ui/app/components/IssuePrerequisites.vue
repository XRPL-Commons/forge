<script setup lang="ts">
import type { IssuePrerequisite } from '~/utils/issues'
defineProps<{ prerequisites: IssuePrerequisite[] }>()
const reasons: Record<string, string> = {
  prerequisite_open: 'Waiting for completion',
  prerequisite_output_pending: 'Waiting for this delivery’s child output',
  prerequisite_closed_unmerged: 'Closed without merging',
  prerequisite_duplicate: 'Closed as a duplicate; update the prerequisite',
  prerequisite_not_planned: 'Closed as not planned; update the prerequisite',
  prerequisite_completion_unknown: 'Completion is not verified',
  prerequisite_inaccessible: 'Forge cannot read this prerequisite',
  prerequisite_identity_changed: 'The referenced repository or issue changed identity',
  prerequisite_cycle: 'This dependency creates a cycle',
  prerequisite_unobserved: 'Waiting for Forge to check',
}
</script>
<template>
  <section id="issue-dependencies" aria-label="Prerequisites" class="space-y-1.5 text-xs">
    <p class="text-muted">Prerequisites</p>
    <ul class="space-y-1.5">
      <li v-for="item in prerequisites" :key="item.reference" class="flex flex-wrap items-baseline gap-x-2">
        <a :href="item.url" target="_blank" rel="noopener noreferrer" class="text-sky-200 underline-offset-4 hover:underline">{{ item.reference }} ↗</a>
        <span :class="item.complete ? 'text-mint-200' : 'text-muted'">{{ item.complete ? 'Complete' : reasons[item.reason!] }}</span>
      </li>
    </ul>
  </section>
</template>
